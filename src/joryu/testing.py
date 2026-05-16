"""``joryu test`` (§6.4) — unit and integration tiers.

This module provides :func:`run_unit_tests` and :func:`run_integration_tests`.

* **Unit** (default, fast): apply every migration against an in-memory SQLite,
  re-apply to assert ensure-semantics idempotency, then run
  :func:`joryu.verify.verify` for semantic conflict detection. Completes in
  seconds.
* **Integration** (optional, via ``testcontainers``): runs the same
  apply/re-apply/verify cycle against real PostgreSQL and MySQL engines spun
  up as throwaway Docker containers. Requires the optional
  ``joryu[test-integration]`` extra and a reachable Docker daemon; otherwise
  individual dialects are gracefully skipped.

Verification scope per the spec:

1. All migrations apply successfully in order.
2. Re-applying is a no-op (ensure semantics; no exception).
3. ``joryu verify`` reports no semantic conflicts.
"""
from __future__ import annotations

import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .conflicts import Conflict


@dataclass
class UnitTestReport:
    """Outcome of :func:`run_unit_tests` (and per-dialect entries of an
    :class:`IntegrationReport`)."""

    total: int = 0
    applied: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    conflicts: list["Conflict"] = field(default_factory=list)
    duration_ms: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.conflicts


@dataclass
class IntegrationReport:
    """Outcome of :func:`run_integration_tests`."""

    per_dialect: dict[str, UnitTestReport] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.per_dialect.values())


def run_unit_tests(
    *,
    migrations_dir: Path,
    dialect: str = "sqlite",
    verbose: bool = False,
) -> UnitTestReport:
    """Apply every migration against an in-memory SQLite, then run verify.

    Parameters
    ----------
    migrations_dir:
        Directory containing joryu migration files.
    dialect:
        Currently only ``"sqlite"`` is supported. Any other value raises
        ``NotImplementedError`` (testcontainers integration tier — v0.3).
    verbose:
        When ``True``, print progress to stderr.

    Returns
    -------
    UnitTestReport
        ``report.ok`` is ``True`` when every migration applied (twice) and
        no semantic conflicts were detected.
    """
    migrations_dir = Path(migrations_dir)
    if not migrations_dir.exists():
        raise FileNotFoundError(
            f"migrations directory does not exist: {migrations_dir}"
        )

    if dialect != "sqlite":
        # Delegate to the integration tier; if testcontainers isn't reachable
        # we re-raise as ``NotImplementedError`` to preserve the v0.2 contract
        # for callers that pass ``dialect=...`` expecting sqlite-or-bust.
        try:
            integ = run_integration_tests(
                migrations_dir=migrations_dir,
                dialects=(dialect,),
                verbose=verbose,
            )
        except _TestcontainersUnavailable as exc:
            raise NotImplementedError(
                f"unit tests for dialect={dialect!r} require the optional "
                f"`joryu[test-integration]` extra and a reachable Docker daemon "
                f"({exc})"
            )
        report = integ.per_dialect.get(dialect)
        if report is not None:
            return report
        # Dialect was skipped (e.g. Docker offline) — surface as NotImplemented
        # so the historical caller contract holds.
        reasons = [r for d, r in integ.skipped if d == dialect]
        raise NotImplementedError(
            f"unit tests for dialect={dialect!r} skipped: "
            f"{reasons[0] if reasons else 'unknown reason'}"
        )

    return _run_cycle(
        url_factory=lambda tmpdir: f"sqlite:///{Path(tmpdir) / 'joryu_unit.db'}",
        migrations_dir=migrations_dir,
        verbose=verbose,
    )


# ---------------------------------------------------------------------------
# Integration tier
# ---------------------------------------------------------------------------


class _TestcontainersUnavailable(RuntimeError):
    """Raised internally when ``testcontainers`` cannot be used."""


def run_integration_tests(
    *,
    migrations_dir: Path,
    dialects: tuple[str, ...] = ("postgresql", "mysql", "sqlite"),
    pg_image: str = "postgres:16",
    mysql_image: str = "mysql:8",
    verbose: bool = False,
) -> IntegrationReport:
    """Run the apply/re-apply/verify cycle on every requested dialect.

    For ``sqlite`` we use the same throwaway file-backed DB as
    :func:`run_unit_tests`. For ``postgresql`` and ``mysql`` we spin up a
    container via ``testcontainers``; if the package or Docker daemon is
    unavailable, that dialect is recorded under ``report.skipped`` with a
    human-readable reason.
    """
    migrations_dir = Path(migrations_dir)
    if not migrations_dir.exists():
        raise FileNotFoundError(
            f"migrations directory does not exist: {migrations_dir}"
        )

    report = IntegrationReport()
    for dialect in dialects:
        if dialect == "sqlite":
            report.per_dialect[dialect] = _run_cycle(
                url_factory=lambda tmpdir: f"sqlite:///{Path(tmpdir) / 'joryu_int.db'}",
                migrations_dir=migrations_dir,
                verbose=verbose,
            )
            continue
        try:
            url_cm = _container_url(dialect, pg_image=pg_image, mysql_image=mysql_image)
        except _TestcontainersUnavailable as exc:
            report.skipped.append((dialect, str(exc)))
            continue
        try:
            with url_cm as url:
                cycle = _run_cycle(
                    url_factory=lambda _tmpdir, _u=url: _u,
                    migrations_dir=migrations_dir,
                    verbose=verbose,
                )
                report.per_dialect[dialect] = cycle
        except _TestcontainersUnavailable as exc:
            report.skipped.append((dialect, str(exc)))

    return report


def _container_url(dialect: str, *, pg_image: str, mysql_image: str):
    """Return a context manager that yields a SQLAlchemy URL for ``dialect``.

    Raises :class:`_TestcontainersUnavailable` if the optional dependency or
    Docker daemon isn't reachable.
    """
    try:
        if dialect == "postgresql":
            from testcontainers.postgres import PostgresContainer  # type: ignore
            container = PostgresContainer(pg_image)
        elif dialect == "mysql":
            from testcontainers.mysql import MySqlContainer  # type: ignore
            container = MySqlContainer(mysql_image)
        else:
            raise _TestcontainersUnavailable(
                f"unsupported integration dialect: {dialect!r}"
            )
    except ImportError as exc:
        raise _TestcontainersUnavailable(
            "the `testcontainers` package is not installed; "
            "install with `pip install joryu[test-integration]`"
        ) from exc

    from contextlib import contextmanager

    @contextmanager
    def _run():
        try:
            container.start()
        except Exception as exc:
            raise _TestcontainersUnavailable(
                f"failed to start {dialect} container "
                f"(is Docker running?): {type(exc).__name__}: {exc}"
            ) from exc
        try:
            url = container.get_connection_url()
            # SQLAlchemy 2.x prefers the +psycopg / +pymysql driver tags.
            url = _normalise_driver(url, dialect)
            yield url
        finally:
            try:
                container.stop()
            except Exception:  # noqa: BLE001
                pass

    return _run()


def _normalise_driver(url: str, dialect: str) -> str:
    """Force a driver tag SQLAlchemy 2.x understands across testcontainers
    versions (which sometimes hand out ``postgresql://`` or
    ``postgresql+psycopg2://``)."""
    if dialect == "postgresql":
        if url.startswith("postgresql+psycopg2://"):
            return "postgresql+psycopg://" + url[len("postgresql+psycopg2://"):]
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://"):]
    if dialect == "mysql":
        if url.startswith("mysql://"):
            return "mysql+pymysql://" + url[len("mysql://"):]
        if url.startswith("mysql+mysqlconnector://"):
            return "mysql+pymysql://" + url[len("mysql+mysqlconnector://"):]
    return url


# ---------------------------------------------------------------------------
# Shared apply/re-apply/verify cycle
# ---------------------------------------------------------------------------


def _run_cycle(
    *,
    url_factory,
    migrations_dir: Path,
    verbose: bool,
) -> UnitTestReport:
    from .loader import load_migrations
    from .registry import MIGRATIONS, reset_registry
    from .runner import apply
    from .verify import verify

    report = UnitTestReport()
    started = time.perf_counter()

    reset_registry()
    _purge_migration_modules()

    with tempfile.TemporaryDirectory() as tmpdir:
        url = url_factory(tmpdir)

        try:
            load_migrations(migrations_dir)
        except Exception as exc:
            report.failed.append(("<load>", _format_exc(exc, verbose=verbose)))
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report

        report.total = len(MIGRATIONS)

        try:
            apply(url=url, migrations_dir=migrations_dir)
        except Exception as exc:
            mig_id = getattr(exc, "migration_id", "<apply>")
            report.failed.append((mig_id, _format_exc(exc, verbose=verbose)))
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report
        report.applied = report.total

        try:
            apply(url=url, migrations_dir=migrations_dir)
        except Exception as exc:
            mig_id = getattr(exc, "migration_id", "<reapply>")
            report.failed.append((mig_id, _format_exc(exc, verbose=verbose)))
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            return report

    try:
        report.conflicts = list(verify(migrations_dir))
    except Exception as exc:
        report.failed.append(("<verify>", _format_exc(exc, verbose=verbose)))

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    return report


def _format_exc(exc: BaseException, *, verbose: bool) -> str:
    if verbose:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__}: {exc}"


def _purge_migration_modules() -> None:
    """Mirror conftest's cleanup so consecutive invocations don't see stale
    cached modules under ``joryu_migrations.*``."""
    for name in list(sys.modules):
        if name.startswith("joryu_migrations."):
            sys.modules.pop(name, None)


__all__ = [
    "IntegrationReport",
    "UnitTestReport",
    "run_integration_tests",
    "run_unit_tests",
]
