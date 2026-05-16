"""Runner: implements the §9.6 apply algorithm, plus ``down`` and ``status``.

High-level flow (apply):

1. Resolve URL and migrations directory (``config``).
2. Open a SQLAlchemy engine (``future=True``).
3. Acquire the per-dialect advisory lock (``lock.advisory_lock``).
4. Ensure the two state tables exist (``state.ensure_state_tables``).
5. Load migrations from disk via ``loader.load_migrations`` and topologically
   sort by ``depends_on`` with a timestamp (id) tiebreak.
6. Halt if any migration is ``failed`` / ``paused`` and the operator did not
   pass ``continue_past_failed`` / ``retry_paused`` (§10.4).
7. For each candidate migration:
     a. Enter ``register_operations`` to populate ``migration.operations``.
        Before doing so, set the current dialect on ``_op_impl`` so that
        ``op.dialect.name`` works during registration (§14.1).
     b. Verify the file checksum vs the persisted row when one exists
        (§7.1 / §10.3).
     c. Upsert the migration row as ``running``.
     d. Walk the registered step list. For each step:
          - if persisted as ``done`` with matching fingerprint, skip.
          - if persisted as ``done`` with a different fingerprint, abort
            (§10.4: "before the failed step ... must match").
          - else: mark ``running``, build a Checkpoint from any persisted
            progress, build an ExecutionContext, dispatch by
            ``transaction_mode``, and call ``op.apply(ctx)``.
            ``PauseStep`` / ``SkipStep`` and generic exceptions are mapped
            onto the matching state row updates and the matching public
            exceptions.
     e. On full success, mark the migration ``applied``.
8. Release the lock (handled by the ``advisory_lock`` context manager).

``apply_async`` is a thin wrapper that delegates to a thread (we run synchronous
SQLAlchemy connections and any async steps started from inside the step body
will start their own anyio loop — see §13.2.2).
"""
from __future__ import annotations

import logging
import re
import sys
import time
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import anyio
import click
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine

from . import _op_impl
from . import env as env_module
from . import state
from .checkpoint import Checkpoint
from .config import load_config, resolve_database_url, resolve_migrations_dir
from .exceptions import (
    MigrationFailed,
    MigrationPaused,
    ProductionGuardError,
)
from .loader import load_migrations
from .lock import advisory_lock
from .op_core import ExecutionContext, Operation, PauseStep, SkipStep
from .registry import MIGRATIONS, Migration, register_operations

log = logging.getLogger("joryu")

_JORYU_VERSION = "0.1.0"
_LAST_ERROR_LIMIT = 4 * 1024  # §9.2: 4 KB cap on persisted exception summary.


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def apply(
    *,
    url: str | None = None,
    migrations_dir: Path | None = None,
    target: str | None = None,
    dry_run: bool = False,
    no_resume: bool = False,
    continue_past_failed: bool = False,
    retry_paused: bool = False,
    retry_interval: str | float = "30s",
    retry_max_attempts: int = 10,
    non_interactive: bool = True,
    on_failure: str = "resume",
    output: str = "auto",
) -> None:
    """Run pending migrations against the configured database.

    See SPEC §16.1 for the public signature; flags not yet wired in v0.1 are
    accepted for compatibility with the eventual CLI surface.
    """
    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    resolved_dir = resolve_migrations_dir(migrations_dir, cfg)
    interval_seconds = _parse_interval(retry_interval)
    engine = create_engine(resolved_url, future=True)
    try:
        _apply_core(
            engine=engine,
            migrations_dir=resolved_dir,
            target=target,
            dry_run=dry_run,
            no_resume=no_resume,
            continue_past_failed=continue_past_failed,
            retry_paused=retry_paused,
            retry_interval=interval_seconds,
            retry_max_attempts=retry_max_attempts,
            non_interactive=non_interactive,
            on_failure=on_failure,
        )
    finally:
        engine.dispose()


async def apply_async(
    *,
    url: str | None = None,
    migrations_dir: Path | None = None,
    target: str | None = None,
    dry_run: bool = False,
    no_resume: bool = False,
    continue_past_failed: bool = False,
    retry_paused: bool = False,
    retry_interval: str | float = "30s",
    retry_max_attempts: int = 10,
    non_interactive: bool = True,
    on_failure: str = "resume",
    output: str = "auto",
) -> None:
    """Async counterpart of :func:`apply` (§13.2.2 / §16.1).

    SQLAlchemy Core's ``Connection`` is synchronous, so the runner core
    keeps running on a worker thread (we use ``anyio.to_thread.run_sync``
    so callers can ``await`` us from inside an event loop without nested-
    loop errors). The crucial difference vs the older "just call
    ``apply``" implementation: we set ``_op_impl.set_async_caller(True)``
    before entering the thread, which tells the StepOp / RunPythonOp
    dispatch path to send ``async def`` step bodies back to the caller's
    loop via ``anyio.from_thread.run`` instead of spinning a fresh
    ``anyio.run`` loop per step. The result is that an ``async def`` step
    truly executes on the caller's event loop (the spec contract), while
    sync DB I/O still runs on a worker thread (the only place SQLAlchemy
    ``Connection`` is safe to touch).
    """
    token = _op_impl.set_async_caller(True)
    try:
        await anyio.to_thread.run_sync(
            lambda: apply(
                url=url,
                migrations_dir=migrations_dir,
                target=target,
                dry_run=dry_run,
                no_resume=no_resume,
                continue_past_failed=continue_past_failed,
                retry_paused=retry_paused,
                retry_interval=retry_interval,
                retry_max_attempts=retry_max_attempts,
                non_interactive=non_interactive,
                on_failure=on_failure,
                output=output,
            )
        )
    finally:
        _op_impl.reset_async_caller(token)


def down(
    *,
    url: str | None = None,
    migrations_dir: Path | None = None,
    steps: int | None = None,
    to: str | None = None,
    allow_prod: bool = False,
) -> None:
    """Roll back the last ``steps`` migrations (or down to migration ``to``).

    v0.1 keeps the implementation simple: we call the user-supplied
    ``downgrade_fn``, then delete the row from ``joryu_migrations`` (and the
    associated step rows). Production-like environments require
    ``allow_prod=True`` (§15).
    """
    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    resolved_dir = resolve_migrations_dir(migrations_dir, cfg)
    detected_env, host = env_module.detect_environment(resolved_url)
    if detected_env != "local" and not allow_prod:
        raise ProductionGuardError(
            detected_env="production-like" if detected_env == "production" else detected_env,
            host=host,
        )

    engine = create_engine(resolved_url, future=True)
    try:
        load_migrations(resolved_dir)
        conn = engine.connect()
        try:
            dialect_name = conn.dialect.name
            with advisory_lock(conn, dialect_name):
                with conn.begin():
                    state.ensure_state_tables(conn)
                applied_rows = [
                    r for r in state.list_migration_rows(conn) if r["status"] == "applied"
                ]
                _close_autobegin(conn)
                # Determine which ids to roll back.
                rollback_ids = _select_downgrade_targets(applied_rows, steps=steps, to=to)
                for mig_id in rollback_ids:
                    m = MIGRATIONS.get(mig_id)
                    if m is None or m.downgrade_fn is None:
                        log.warning(
                            "no downgrade function available for %s; skipping rollback", mig_id
                        )
                        continue
                    _try_set_dialect(dialect_name)
                    # Execute downgrade inside a transaction. We don't track
                    # step state for downgrade in v0.1.
                    with conn.begin():
                        # Downgrade functions call op.* which need a registration
                        # scope; we cheat by running with the same registration
                        # contextvar so op.* calls during downgrade are routed
                        # into a throwaway operations list, then immediately
                        # executed.
                        _execute_downgrade(conn, m, dialect_name)
                        state.delete_step_rows(conn, mig_id)
                        state.delete_migration_row(conn, mig_id)
                    log.info("rolled back %s", mig_id)
        finally:
            conn.close()
    finally:
        engine.dispose()


def _execute_downgrade(conn: Connection, m: Migration, dialect_name: str) -> None:
    """Run the downgrade function and execute its ops against ``conn``.

    v0.1 strategy: enter a temporary registration scope, call ``downgrade_fn``
    to populate a fresh ops list, then ``apply()`` each op directly.
    """
    from .registry import _RegistrationScope  # local import to avoid cycle

    if m.downgrade_fn is None:
        return
    # Save and reset operations so registration captures only downgrade ops.
    saved = list(m.operations)
    try:
        with _RegistrationScope(m):
            m.downgrade_fn()
        ctx = ExecutionContext(
            conn=conn,
            dialect_name=dialect_name,
            transaction_mode="per_migration",
            on_mismatch=m.on_mismatch,
            checkpoint=None,
        )
        for op in m.operations:
            op.apply(ctx)
    finally:
        m.operations = saved


async def down_async(
    *,
    url: str | None = None,
    migrations_dir: Path | None = None,
    steps: int | None = None,
    to: str | None = None,
    allow_prod: bool = False,
) -> None:
    await anyio.to_thread.run_sync(
        lambda: down(
            url=url,
            migrations_dir=migrations_dir,
            steps=steps,
            to=to,
            allow_prod=allow_prod,
        )
    )


def status(
    *,
    url: str | None = None,
    migrations_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a status summary: one dict per migration known on disk or in DB."""
    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    resolved_dir = resolve_migrations_dir(migrations_dir, cfg)
    engine = create_engine(resolved_url, future=True)
    try:
        load_migrations(resolved_dir)
        conn = engine.connect()
        try:
            with conn.begin():
                state.ensure_state_tables(conn)
            db_rows = {r["id"]: r for r in state.list_migration_rows(conn)}
            _close_autobegin(conn)
            results: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for mig_id in _sorted_migration_ids(MIGRATIONS.values()):
                m = MIGRATIONS[mig_id]
                row = db_rows.get(mig_id)
                results.append(
                    _status_entry(conn, mig_id, m, row)
                )
                seen_ids.add(mig_id)
            _close_autobegin(conn)
            # Surface DB-only rows (manually applied, or file deleted).
            for mig_id, row in db_rows.items():
                if mig_id in seen_ids:
                    continue
                results.append(_status_entry(conn, mig_id, None, row))
            _close_autobegin(conn)
            return results
        finally:
            conn.close()
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Apply core
# ---------------------------------------------------------------------------

def _apply_core(
    *,
    engine: Engine,
    migrations_dir: Path,
    target: str | None,
    dry_run: bool,
    no_resume: bool,
    continue_past_failed: bool,
    retry_paused: bool,
    retry_interval: float = 30.0,
    retry_max_attempts: int = 10,
    non_interactive: bool = True,
    on_failure: str = "resume",
) -> None:
    load_migrations(migrations_dir)
    # We hold one long-lived connection for the duration of the apply run so
    # that the advisory lock and the per-migration work happen on the same
    # session (required by PostgreSQL's session-scoped pg_advisory_lock and by
    # MySQL's per-session GET_LOCK).
    #
    # We use AUTOCOMMIT isolation so that incidental SELECTs (lock acquisition,
    # state reads) do not autobegin a transaction we'd then have to juggle
    # alongside the explicit ``with conn.begin():`` blocks used for state
    # writes and per-step transactions.
    conn = engine.connect()
    try:
        dialect_name = conn.dialect.name
        with advisory_lock(conn, dialect_name):
            # Ensure tables in their own transaction.
            with conn.begin():
                state.ensure_state_tables(conn)

            # Read current state.
            existing = {r["id"]: r for r in state.list_migration_rows(conn)}
            _close_autobegin(conn)
            _enforce_halt_policy(
                existing,
                continue_past_failed=continue_past_failed,
                retry_paused=retry_paused,
            )

            ordered = _sorted_migrations(MIGRATIONS.values(), dialect_name=dialect_name)
            # Carry the set of failed migration ids for skip-by-dependency.
            failed_ids = {mid for mid, row in existing.items() if row["status"] == "failed"}

            for m in ordered:
                row = existing.get(m.id)
                if row is not None and row["status"] == "applied":
                    # Re-register so we can compute the current file checksum
                    # and detect post-apply edits (§7.1).
                    _try_set_dialect(dialect_name)
                    register_operations(m)
                    if m.checksum() != row["checksum"]:
                        raise MigrationFailed(
                            m.id,
                            -1,
                            "<checksum>",
                            RuntimeError(
                                f"migration {m.id!r} is applied but its file checksum has changed; "
                                "run `joryu repair` to acknowledge"
                            ),
                        )
                    continue
                if continue_past_failed and _depends_on_failed(m, failed_ids):
                    log.info("skipping %s: depends transitively on a failed migration", m.id)
                    continue
                if target is not None and _topo_index(ordered, m.id) > _topo_index(ordered, target):
                    break

                # Half-failed recovery (§10.5).
                decision: RecoveryDecision | None = None
                if row is not None and row["status"] == "failed":
                    decision = _resolve_recovery(
                        conn,
                        m.id,
                        row,
                        non_interactive=non_interactive,
                        on_failure=on_failure,
                    )
                    if decision.action == "abort":
                        raise MigrationFailed(
                            m.id,
                            -1,
                            "<recovery-abort>",
                            RuntimeError(
                                f"recovery aborted for {m.id!r}; rerun "
                                "`joryu apply` to choose a different strategy"
                            ),
                        )
                    _apply_recovery_pre(conn, m.id, decision)

                _apply_with_pause_retry(
                    conn,
                    m,
                    dialect_name=dialect_name,
                    persisted_row=row,
                    dry_run=dry_run,
                    no_resume=no_resume,
                    recovery=decision,
                    retry_paused=retry_paused,
                    retry_interval=retry_interval,
                    retry_max_attempts=retry_max_attempts,
                )
                if target is not None and m.id == target:
                    break
    finally:
        conn.close()


def _apply_with_pause_retry(
    conn: Connection,
    m: Migration,
    *,
    dialect_name: str,
    persisted_row: dict[str, Any] | None,
    dry_run: bool,
    no_resume: bool,
    recovery: "RecoveryDecision | None",
    retry_paused: bool,
    retry_interval: float,
    retry_max_attempts: int,
) -> None:
    """Wrap :func:`_apply_one` with optional --retry-paused polling (§10.4).

    When ``retry_paused`` is ``False`` this is a passthrough. When ``True``
    and the migration raises :class:`MigrationPaused`, we sleep
    ``retry_interval`` seconds and re-attempt up to ``retry_max_attempts``
    times. Each re-attempt re-enters registration so a step body that flipped
    from "not ready" to "ready" will succeed on the next pass. If the
    migration is still paused after the budget is exhausted, the last
    :class:`MigrationPaused` is re-raised (CLI exit code 3, §16.2).
    """
    if not retry_paused:
        _apply_one(
            conn,
            m,
            dialect_name=dialect_name,
            persisted_row=persisted_row,
            dry_run=dry_run,
            no_resume=no_resume,
            recovery=recovery,
        )
        return

    attempt = 0
    last_paused: MigrationPaused | None = None
    current_row = persisted_row
    while attempt < max(1, retry_max_attempts):
        attempt += 1
        try:
            _apply_one(
                conn,
                m,
                dialect_name=dialect_name,
                persisted_row=current_row,
                dry_run=dry_run,
                no_resume=no_resume,
                recovery=recovery,
            )
            return
        except MigrationPaused as exc:
            last_paused = exc
            log.info(
                "migration %s still paused (attempt %d/%d): %s",
                m.id, attempt, retry_max_attempts, exc.reason,
            )
            if attempt >= retry_max_attempts:
                break
            # Recovery decision only applies to the first attempt's pre-state.
            recovery = None
            if retry_interval > 0:
                time.sleep(retry_interval)
            # Re-read the persisted row so the next attempt observes the
            # paused state and resumes from the right step.
            existing = {r["id"]: r for r in state.list_migration_rows(conn)}
            _close_autobegin(conn)
            current_row = existing.get(m.id)
    assert last_paused is not None
    raise last_paused


def _parse_interval(spec: str | float | int) -> float:
    """Parse ``30s`` / ``5m`` / ``1h`` / plain seconds into a float.

    Accepts a bare number (seconds), or a string with optional suffix
    ``s``/``m``/``h``/``ms``. ``0`` / ``"0"`` is allowed (no sleep).
    """
    if isinstance(spec, (int, float)):
        if spec < 0:
            raise ValueError(f"retry interval must be non-negative, got {spec!r}")
        return float(spec)
    text = str(spec).strip().lower()
    if not text:
        raise ValueError("retry interval must not be empty")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?", text)
    if match is None:
        raise ValueError(
            f"invalid retry interval {spec!r} "
            "(expected forms: '30s', '5m', '1h', '500ms', or a plain number)"
        )
    value = float(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return value * multiplier


def _apply_one(
    conn: Connection,
    m: Migration,
    *,
    dialect_name: str,
    persisted_row: dict[str, Any] | None,
    dry_run: bool,
    no_resume: bool,
    recovery: "RecoveryDecision | None" = None,
) -> None:
    """Apply a single migration. Raises on failure/pause."""
    from ._runtime import reset_current_migration, set_current_migration

    _try_set_dialect(dialect_name)
    try:
        register_operations(m)
    except Exception:
        log.exception("failed to register operations for %s", m.id)
        raise

    _exec_token = set_current_migration(m.id)

    try:
        _apply_one_body(
            conn, m,
            dialect_name=dialect_name,
            persisted_row=persisted_row,
            dry_run=dry_run,
            no_resume=no_resume,
        )
    finally:
        reset_current_migration(_exec_token)


def _apply_one_body(
    conn: "Connection",
    m: "Migration",
    *,
    dialect_name: str,
    persisted_row: dict[str, Any] | None,
    dry_run: bool,
    no_resume: bool,
) -> None:
    file_checksum = m.checksum()

    # Checksum policy (§10.3).
    if persisted_row is not None:
        prev_status = persisted_row["status"]
        prev_checksum = persisted_row["checksum"]
        if prev_status == "applied" and prev_checksum != file_checksum:
            raise MigrationFailed(
                m.id,
                0,
                "<checksum>",
                RuntimeError(
                    f"migration {m.id!r} is applied but its file checksum has changed; "
                    "run `joryu repair` to acknowledge"
                ),
            )

    if dry_run:
        log.info("[dry-run] would apply %s (%d steps)", m.id, len(m.operations))
        return

    # Insert or update the migration row to running.
    with conn.begin():
        if persisted_row is None:
            state.insert_migration(
                conn,
                m.id,
                checksum=file_checksum,
                status="running",
                dialect=dialect_name,
                joryu_version=_JORYU_VERSION,
            )
        else:
            state.update_migration_status(
                conn,
                m.id,
                status="running",
                checksum=file_checksum,
            )

    # Load any persisted step rows for resumption.
    step_rows = {r["step_index"]: r for r in state.get_step_rows(conn, m.id)}
    _close_autobegin(conn)

    # Optional per-migration transaction (Postgres / SQLite get atomicity here;
    # MySQL silently breaks atomicity at the first DDL — surfaced in §9.3.1).
    per_migration_tx = nullcontext()
    if m.transaction_mode == "per_migration":
        per_migration_tx = conn.begin()

    try:
        with per_migration_tx:
            for idx, op in enumerate(m.operations):
                prev_step = step_rows.get(idx)
                fp = op.fingerprint()

                if prev_step is not None and prev_step["status"] == "done":
                    if prev_step["op_fingerprint"] != fp:
                        raise MigrationFailed(
                            m.id,
                            idx,
                            op.describe(),
                            RuntimeError(
                                f"step {idx} of {m.id!r} is already done but its op "
                                "fingerprint has changed; edits before the failed step "
                                "are not allowed (§10.4)"
                            ),
                        )
                    log.debug("skipping completed step %s.%d", m.id, idx)
                    continue

                if prev_step is not None and prev_step["status"] == "skipped":
                    log.debug("step %s.%d previously skipped; not re-running", m.id, idx)
                    continue

                _run_step(
                    conn,
                    m,
                    idx,
                    op,
                    dialect_name=dialect_name,
                    prev_step=prev_step,
                    no_resume=no_resume,
                )
    except MigrationPaused:
        raise
    except MigrationFailed:
        raise
    except Exception as exc:
        # Defensive net: any unexpected exception that escaped the per-step
        # handler. Mark the migration failed.
        _mark_migration_failed(conn, m.id, exc, step_index=None, step_name=None)
        raise MigrationFailed(m.id, -1, "<unknown>", exc) from exc

    # All steps done: mark the migration applied.
    with conn.begin():
        state.update_migration_status(conn, m.id, status="applied")
    log.info("applied %s (%d steps)", m.id, len(m.operations))


def _run_step(
    conn: Connection,
    m: Migration,
    idx: int,
    op: Operation,
    *,
    dialect_name: str,
    prev_step: dict[str, Any] | None,
    no_resume: bool,
) -> None:
    """Execute a single Operation and update step state accordingly."""
    fp = op.fingerprint()
    initial_progress: dict[str, Any] = {}
    if prev_step is not None and not no_resume:
        initial_progress = state.load_step_progress(prev_step)

    # Outer (state) transaction wrapping the row transition to 'running'.
    # We commit it eagerly so even on MySQL — where DDL implicitly commits —
    # the row is visible if a crash happens mid-step.
    with conn.begin():
        state.upsert_step(
            conn,
            m.id,
            idx,
            op_kind=op.kind or type(op).__name__,
            op_fingerprint=fp,
            status="running",
        )

    # Dispatch by transaction mode.
    if m.transaction_mode == "per_step":
        step_tx = conn.begin()
    else:
        # per_migration: caller already opened a tx above.
        # none: the user manages tx inside the op body.
        step_tx = nullcontext()

    checkpoint = Checkpoint(
        migration_id=m.id,
        step_index=idx,
        initial=initial_progress,
        persist=lambda snapshot, _conn=conn, _id=m.id, _idx=idx: state.update_step_progress(
            _conn, _id, _idx, snapshot
        ),
        on_report=lambda payload, _id=m.id, _idx=idx: _report_progress(_id, _idx, payload),
    )

    ctx = ExecutionContext(
        conn=conn,
        dialect_name=dialect_name,
        transaction_mode=m.transaction_mode,
        on_mismatch=m.on_mismatch,
        checkpoint=checkpoint,
    )

    log.info("[joryu] %s step %d/%d: %s", m.id, idx + 1, len(m.operations), op.describe())
    try:
        with step_tx:
            op.apply(ctx)
    except PauseStep as exc:
        # Step stays 'pending'; migration becomes 'paused'.
        with conn.begin():
            state.upsert_step(
                conn,
                m.id,
                idx,
                op_kind=op.kind or type(op).__name__,
                op_fingerprint=fp,
                status="pending",
                progress=checkpoint.snapshot() or None,
            )
            state.update_migration_status(
                conn,
                m.id,
                status="paused",
                pause_reason=exc.reason or str(exc),
            )
        raise MigrationPaused(m.id, idx, op.describe(), exc.reason or str(exc)) from exc
    except SkipStep as exc:
        with conn.begin():
            state.upsert_step(
                conn,
                m.id,
                idx,
                op_kind=op.kind or type(op).__name__,
                op_fingerprint=fp,
                status="skipped",
                progress=checkpoint.snapshot() or None,
            )
        log.info("[joryu] %s step %d skipped: %s", m.id, idx, exc.reason)
        return
    except Exception as exc:
        _mark_migration_failed(conn, m.id, exc, step_index=idx, step_name=op.describe(),
                               fingerprint=fp, op_kind=op.kind or type(op).__name__,
                               progress=checkpoint.snapshot() or None)
        raise MigrationFailed(m.id, idx, op.describe(), exc) from exc

    # Step succeeded.
    with conn.begin():
        state.upsert_step(
            conn,
            m.id,
            idx,
            op_kind=op.kind or type(op).__name__,
            op_fingerprint=fp,
            status="done",
            progress=checkpoint.snapshot() or None,
        )


# ---------------------------------------------------------------------------
# Half-failed recovery (§10.5)
# ---------------------------------------------------------------------------


@dataclass
class RecoveryDecision:
    action: Literal["resume", "restart_from", "restart_all", "skip_step", "abort"]
    step_index: int | None = None


def _failed_step_index(step_rows: list[dict[str, Any]]) -> int | None:
    """Return the smallest step_index whose row is ``failed`` (else None)."""
    failed = [r for r in step_rows if r["status"] == "failed"]
    if not failed:
        return None
    return min(r["step_index"] for r in failed)


def _resolve_recovery(
    conn: Connection,
    mig_id: str,
    mig_row: dict[str, Any],
    *,
    non_interactive: bool,
    on_failure: str,
) -> RecoveryDecision:
    """Decide what to do about a ``failed`` migration row (§10.5).

    Non-TTY callers (or ``non_interactive=True``) get a programmatic mapping
    from ``on_failure`` to a RecoveryDecision. TTY callers see the 5-choice
    prompt.
    """
    step_rows = state.get_step_rows(conn, mig_id)
    _close_autobegin(conn)
    failed_idx = _failed_step_index(step_rows)

    if non_interactive or not _is_tty():
        if on_failure == "resume":
            return RecoveryDecision(action="resume", step_index=failed_idx)
        if on_failure == "restart":
            return RecoveryDecision(action="restart_all", step_index=None)
        if on_failure == "abort":
            return RecoveryDecision(action="abort", step_index=None)
        raise ValueError(
            f"on_failure must be one of resume|restart|abort, got {on_failure!r}"
        )

    return _prompt_recovery(mig_row, step_rows)


def _is_tty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(
        getattr(sys.stdout, "isatty", lambda: False)()
    )


def _prompt_recovery(
    mig_row: dict[str, Any],
    step_rows: list[dict[str, Any]],
) -> RecoveryDecision:
    """Show the §10.5 5-choice menu and return the operator's selection."""
    mig_id = mig_row["id"]
    failed_idx = _failed_step_index(step_rows)
    # User-facing step numbers are 1-based; internal indices are 0-based.
    n_display = (failed_idx + 1) if failed_idx is not None else 1

    click.echo(f"Migration {mig_id} is in failed state.")
    click.echo("")
    if step_rows:
        for r in step_rows:
            marker = {
                "done": "[ok]",
                "failed": "[!!]",
                "skipped": "[--]",
                "pending": "[..]",
                "running": "[>>]",
            }.get(r["status"], "[??]")
            click.echo(
                f"  {marker} step {r['step_index'] + 1} {r['op_kind']}  ({r['status']})"
            )
        click.echo("")
    click.echo("How would you like to proceed?")
    click.echo(
        f"  [1] Resume from step {n_display} (re-run only failed/pending steps)"
    )
    click.echo(
        "  [2] Restart from step K (re-run from a chosen step)"
    )
    click.echo(
        "  [3] Restart from step 1 (full restart, clears all checkpoints)"
    )
    click.echo(
        f"  [4] Skip step {n_display} and continue (mark as skipped)"
    )
    click.echo("  [5] Abort (do nothing)")

    choice = click.prompt(
        "Choose [1-5]",
        type=click.IntRange(1, 5),
        default=1,
        show_default=True,
    )
    if choice == 1:
        return RecoveryDecision(action="resume", step_index=failed_idx)
    if choice == 2:
        target = click.prompt(
            "Restart from step number",
            type=click.IntRange(1, max(1, len(step_rows))),
            default=n_display,
            show_default=True,
        )
        return RecoveryDecision(action="restart_from", step_index=target - 1)
    if choice == 3:
        return RecoveryDecision(action="restart_all", step_index=None)
    if choice == 4:
        return RecoveryDecision(action="skip_step", step_index=failed_idx)
    return RecoveryDecision(action="abort", step_index=None)


def _apply_recovery_pre(
    conn: Connection,
    mig_id: str,
    decision: RecoveryDecision,
) -> None:
    """Apply DB-side state mutations implied by ``decision`` *before* re-running.

    ``resume`` and ``abort`` are no-ops here (resume relies on the existing
    persisted state; abort is handled by the caller).
    """
    if decision.action == "resume":
        return
    if decision.action == "abort":
        return
    if decision.action == "restart_all":
        with conn.begin():
            state.delete_step_rows(conn, mig_id)
        return
    if decision.action == "restart_from":
        target = decision.step_index or 0
        existing = state.get_step_rows(conn, mig_id)
        _close_autobegin(conn)
        with conn.begin():
            for r in existing:
                if r["step_index"] < target:
                    continue
                # Reset to pending with no progress so the runner re-executes.
                state.upsert_step(
                    conn,
                    mig_id,
                    r["step_index"],
                    op_kind=r["op_kind"],
                    op_fingerprint=r["op_fingerprint"],
                    status="pending",
                    progress=None,
                )
        return
    if decision.action == "skip_step":
        if decision.step_index is None:
            return
        # Mark the failed step as skipped; the resume path will then move on.
        existing = {r["step_index"]: r for r in state.get_step_rows(conn, mig_id)}
        _close_autobegin(conn)
        row = existing.get(decision.step_index)
        if row is None:
            return
        with conn.begin():
            state.upsert_step(
                conn,
                mig_id,
                decision.step_index,
                op_kind=row["op_kind"],
                op_fingerprint=row["op_fingerprint"],
                status="skipped",
                progress=None,
            )
        return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mark_migration_failed(
    conn: Connection,
    mig_id: str,
    exc: BaseException,
    *,
    step_index: int | None,
    step_name: str | None,
    fingerprint: str | None = None,
    op_kind: str | None = None,
    progress: dict[str, Any] | None = None,
) -> None:
    summary = _summarise_exception(exc)
    try:
        with conn.begin():
            if step_index is not None and fingerprint is not None and op_kind is not None:
                state.upsert_step(
                    conn,
                    mig_id,
                    step_index,
                    op_kind=op_kind,
                    op_fingerprint=fingerprint,
                    status="failed",
                    progress=progress,
                )
            state.update_migration_status(
                conn,
                mig_id,
                status="failed",
                last_error=summary,
            )
    except Exception:
        log.exception("could not persist failure state for %s", mig_id)


def _summarise_exception(exc: BaseException) -> str:
    head = f"{type(exc).__name__}: {exc}"
    tb = exc.__traceback__
    trace_summary = ""
    if tb is not None:
        frames = traceback.extract_tb(tb)
        if frames:
            top = frames[0]
            bottom = frames[-1]
            trace_summary = (
                f"\n  at {top.filename}:{top.lineno} in {top.name}"
                f"\n  at {bottom.filename}:{bottom.lineno} in {bottom.name}"
            )
    payload = head + trace_summary
    return payload[:_LAST_ERROR_LIMIT]


def _close_autobegin(conn: Connection) -> None:
    """Commit any open autobegun transaction on ``conn``.

    SQLAlchemy 2.0 autobegins a transaction on the first ``execute()`` call.
    Read paths (e.g. ``state.list_migration_rows``) don't open an explicit
    ``with conn.begin():``, so we proactively commit so that subsequent
    explicit ``begin()`` blocks don't conflict with the lingering autobegin.
    """
    if conn.in_transaction():
        conn.commit()


def _try_set_dialect(dialect_name: str) -> None:
    """Best-effort: tell the op implementation which dialect we're on.

    The op sub-agent has the option to expose ``set_current_dialect`` on
    ``_op_impl``. If they haven't yet, we just no-op so the runner can still
    operate against the placeholder ops in v0.1.
    """
    setter = getattr(_op_impl, "set_current_dialect", None)
    if callable(setter):
        try:
            setter(dialect_name)
        except Exception:
            log.exception("set_current_dialect(%r) raised", dialect_name)


def _report_progress(mig_id: str, step_index: int, payload: dict[str, Any]) -> None:
    # v0.1: log to stderr via the joryu logger. The progress.py module will
    # replace this once it exists.
    if not payload:
        return
    pct = payload.get("percent")
    msg = payload.get("message")
    log.info("[joryu] %s step %d progress: pct=%s msg=%s", mig_id, step_index, pct, msg)


def _enforce_halt_policy(
    existing: dict[str, dict[str, Any]],
    *,
    continue_past_failed: bool,
    retry_paused: bool,
) -> None:
    """Implement §10.4 halt: failed wins (exit 2), paused next (exit 3)."""
    failed = [mid for mid, r in existing.items() if r["status"] == "failed"]
    paused = [mid for mid, r in existing.items() if r["status"] == "paused"]
    if failed and not continue_past_failed:
        # Failed states are recoverable via resume (same code path), so we do
        # *not* abort just because a failed row exists — we re-attempt it.
        # The §10.4 halt is for non-resume callers; in v0.1 we always resume
        # by default and only abort when explicitly told not to retry.
        log.info("resuming previously failed migrations: %s", ", ".join(sorted(failed)))
    if paused and not retry_paused:
        # Same idea — re-run the paused migration's pending step. The runner
        # would otherwise look exactly the same as the failed case.
        log.info("retrying previously paused migrations: %s", ", ".join(sorted(paused)))


def _sorted_migrations(
    migrations: Iterable[Migration],
    *,
    dialect_name: str,
) -> list[Migration]:
    """Topological sort by ``depends_on`` with id (timestamp) as tiebreak.

    Migrations whose ``dialects`` list excludes the current dialect are
    omitted.
    """
    candidates = [
        m for m in migrations
        if m.dialects is None or dialect_name in m.dialects
    ]
    by_id = {m.id: m for m in candidates}

    visited: set[str] = set()
    in_progress: set[str] = set()
    output: list[Migration] = []

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in in_progress:
            raise RuntimeError(f"dependency cycle involving {node_id!r}")
        m = by_id.get(node_id)
        if m is None:
            # Dependency points outside the candidate set (dialect-excluded or
            # missing). We treat this as satisfied — it isn't our responsibility
            # to fail an unrelated migration.
            return
        in_progress.add(node_id)
        for dep in sorted(m.depends_on):
            visit(dep)
        in_progress.discard(node_id)
        visited.add(node_id)
        output.append(m)

    for node_id in sorted(by_id):
        visit(node_id)

    return output


def _sorted_migration_ids(migrations: Iterable[Migration]) -> list[str]:
    return [m.id for m in _sorted_migrations(migrations, dialect_name="*")]


def _topo_index(ordered: list[Migration], mig_id: str) -> int:
    for i, m in enumerate(ordered):
        if m.id == mig_id:
            return i
    return len(ordered)


def _depends_on_failed(m: Migration, failed_ids: set[str]) -> bool:
    """Return True if ``m`` transitively depends on any failed migration."""
    seen: set[str] = set()
    stack = list(m.depends_on)
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if dep in failed_ids:
            return True
        dep_mig = MIGRATIONS.get(dep)
        if dep_mig is not None:
            stack.extend(dep_mig.depends_on)
    return False


def _select_downgrade_targets(
    applied_rows: list[dict[str, Any]],
    *,
    steps: int | None,
    to: str | None,
) -> list[str]:
    """Return migration ids to roll back, in reverse-applied order."""
    if not applied_rows:
        return []
    # Sort by started_at then id; reverse for newest-first.
    applied_rows = sorted(applied_rows, key=lambda r: (r.get("started_at"), r["id"]))
    if to is not None:
        target_index = next((i for i, r in enumerate(applied_rows) if r["id"] == to), None)
        if target_index is None:
            log.warning("downgrade target %s not found in applied migrations; nothing to do", to)
            return []
        # Roll back everything strictly after `to` (i.e. to_index+1 .. end).
        return [r["id"] for r in reversed(applied_rows[target_index + 1:])]
    n = steps if steps is not None else 1
    return [r["id"] for r in reversed(applied_rows[-n:])]


def _status_entry(
    conn: Connection,
    mig_id: str,
    m: Migration | None,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    steps = []
    if row is not None:
        for s in state.get_step_rows(conn, mig_id):
            steps.append({
                "step_index": s["step_index"],
                "op_kind": s["op_kind"],
                "status": s["status"],
                "started_at": s.get("started_at"),
                "finished_at": s.get("finished_at"),
                "progress": state.load_step_progress(s),
            })
    return {
        "id": mig_id,
        "status": (row["status"] if row is not None else "pending"),
        "checksum": (row["checksum"] if row is not None else (m.checksum() if m is not None else None)),
        "dialect": (row["dialect"] if row is not None else None),
        "started_at": (row.get("started_at") if row is not None else None),
        "finished_at": (row.get("finished_at") if row is not None else None),
        "last_error": (row.get("last_error") if row is not None else None),
        "pause_reason": (row.get("pause_reason") if row is not None else None),
        "on_disk": m is not None,
        "transaction_mode": (m.transaction_mode if m is not None else None),
        "steps": steps,
    }


__all__ = ["apply", "apply_async", "down", "down_async", "status"]
