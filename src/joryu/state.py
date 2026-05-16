"""State tables and CRUD helpers (SPEC §9.2).

Two tables track every runtime fact joryu needs:

  * ``joryu_migrations``       — one row per migration that has been attempted
                                 (running, applied, failed, paused, or explicitly
                                 marked pending).
  * ``joryu_migration_steps``  — one row per (migration_id, step_index) — what
                                 the runner uses to know whether an individual
                                 op has already executed and, for resumable
                                 data migrations, the JSON checkpoint.

The enum columns are stored as ``VARCHAR`` rather than SQL ``ENUM`` to keep the
DDL portable across PostgreSQL / MySQL / SQLite. Values are validated in Python
at write time (see ``_MIGRATION_STATUSES`` / ``_STEP_STATUSES``).

The runner imports this module to:

* lazily provision the two tables (``ensure_state_tables``),
* insert / update the migration row on every transition,
* upsert step rows (``upsert_step``) and persist checkpoint progress
  (``update_step_progress``).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TIMESTAMP,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.sql import func

log = logging.getLogger("joryu")


# ---- Schema ----------------------------------------------------------------

_metadata = MetaData()

migrations_table = Table(
    "joryu_migrations",
    _metadata,
    Column("id", String(120), primary_key=True),
    Column("checksum", String(80), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", TIMESTAMP, nullable=False, server_default=func.current_timestamp()),
    Column("finished_at", TIMESTAMP, nullable=True),
    Column("joryu_version", String(20), nullable=False),
    Column("dialect", String(20), nullable=False),
    Column("last_error", Text, nullable=True),
    Column("pause_reason", Text, nullable=True),
)

Index("idx_joryu_migrations_status", migrations_table.c.status)

steps_table = Table(
    "joryu_migration_steps",
    _metadata,
    Column("migration_id", String(120), primary_key=True),
    Column("step_index", Integer, primary_key=True),
    Column("op_kind", String(40), nullable=False),
    Column("op_fingerprint", String(80), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", TIMESTAMP, nullable=False, server_default=func.current_timestamp()),
    Column("finished_at", TIMESTAMP, nullable=True),
    Column("progress", Text, nullable=True),
)


_MIGRATION_STATUSES = {"pending", "running", "applied", "failed", "paused"}
_STEP_STATUSES = {"pending", "running", "done", "failed", "skipped"}


# ---- Provisioning ----------------------------------------------------------

def ensure_state_tables(conn: Connection) -> None:
    """Create the state tables if they do not yet exist (idempotent)."""
    _metadata.create_all(conn, checkfirst=True)


# ---- Migration row CRUD ----------------------------------------------------

def _row_to_dict(row: Any) -> dict[str, Any]:
    # SQLAlchemy Row supports ._mapping; coerce to plain dict for callers.
    return dict(row._mapping)


def get_migration_row(conn: Connection, mig_id: str) -> dict[str, Any] | None:
    stmt = select(migrations_table).where(migrations_table.c.id == mig_id)
    row = conn.execute(stmt).first()
    return _row_to_dict(row) if row is not None else None


def insert_migration(
    conn: Connection,
    mig_id: str,
    checksum: str,
    status: str,
    dialect: str,
    joryu_version: str,
) -> None:
    if status not in _MIGRATION_STATUSES:
        raise ValueError(f"invalid migration status {status!r}")
    stmt = insert(migrations_table).values(
        id=mig_id,
        checksum=checksum,
        status=status,
        joryu_version=joryu_version,
        dialect=dialect,
        started_at=func.current_timestamp(),
    )
    conn.execute(stmt)


def update_migration_status(
    conn: Connection,
    mig_id: str,
    *,
    status: str,
    checksum: str | None = None,
    finished_at: Any = None,
    last_error: str | None = None,
    pause_reason: str | None = None,
) -> None:
    """Transition a migration row.

    Per §9.2: when transitioning *out of* ``failed`` / ``paused``, both
    ``last_error`` and ``pause_reason`` are cleared. Callers can leave
    ``last_error`` / ``pause_reason`` as ``None`` to keep existing values
    unchanged — the runner explicitly passes empty strings or new values when
    it wants to overwrite.
    """
    if status not in _MIGRATION_STATUSES:
        raise ValueError(f"invalid migration status {status!r}")
    values: dict[str, Any] = {"status": status}
    if checksum is not None:
        values["checksum"] = checksum
    if finished_at is not None:
        # Pass through caller-provided value; otherwise use server clock when
        # the target status is a terminal one.
        values["finished_at"] = finished_at
    elif status in ("applied",):
        values["finished_at"] = func.current_timestamp()
    if status in ("running", "applied"):
        # Always clear error metadata on transitions away from failed/paused.
        values["last_error"] = None
        values["pause_reason"] = None
    if last_error is not None:
        values["last_error"] = last_error
    if pause_reason is not None:
        values["pause_reason"] = pause_reason
    stmt = (
        update(migrations_table)
        .where(migrations_table.c.id == mig_id)
        .values(**values)
    )
    conn.execute(stmt)


def list_migration_rows(conn: Connection) -> list[dict[str, Any]]:
    stmt = select(migrations_table).order_by(migrations_table.c.started_at, migrations_table.c.id)
    return [_row_to_dict(r) for r in conn.execute(stmt).all()]


def delete_migration_row(conn: Connection, mig_id: str) -> None:
    stmt = delete(migrations_table).where(migrations_table.c.id == mig_id)
    conn.execute(stmt)


# ---- Step row CRUD ---------------------------------------------------------

def get_step_rows(conn: Connection, mig_id: str) -> list[dict[str, Any]]:
    stmt = (
        select(steps_table)
        .where(steps_table.c.migration_id == mig_id)
        .order_by(steps_table.c.step_index)
    )
    return [_row_to_dict(r) for r in conn.execute(stmt).all()]


def upsert_step(
    conn: Connection,
    mig_id: str,
    step_index: int,
    *,
    op_kind: str,
    op_fingerprint: str,
    status: str,
    progress: dict[str, Any] | None = None,
    finished_at: Any = None,
) -> None:
    """Insert or update a row in ``joryu_migration_steps``.

    SQLAlchemy doesn't offer a single portable upsert across dialects, so we do
    a SELECT + INSERT/UPDATE. The advisory lock (§9.7) serialises apply runs,
    so there is no concurrent writer.
    """
    if status not in _STEP_STATUSES:
        raise ValueError(f"invalid step status {status!r}")

    progress_json: str | None
    if progress is None:
        progress_json = None
    else:
        progress_json = json.dumps(progress)

    existing = conn.execute(
        select(steps_table.c.migration_id).where(
            (steps_table.c.migration_id == mig_id)
            & (steps_table.c.step_index == step_index)
        )
    ).first()

    if existing is None:
        values: dict[str, Any] = {
            "migration_id": mig_id,
            "step_index": step_index,
            "op_kind": op_kind,
            "op_fingerprint": op_fingerprint,
            "status": status,
            "started_at": func.current_timestamp(),
        }
        if progress_json is not None:
            values["progress"] = progress_json
        if finished_at is not None:
            values["finished_at"] = finished_at
        elif status in ("done", "skipped"):
            values["finished_at"] = func.current_timestamp()
        conn.execute(insert(steps_table).values(**values))
        return

    values = {
        "op_kind": op_kind,
        "op_fingerprint": op_fingerprint,
        "status": status,
    }
    if progress_json is not None:
        values["progress"] = progress_json
    if finished_at is not None:
        values["finished_at"] = finished_at
    elif status in ("done", "skipped"):
        values["finished_at"] = func.current_timestamp()
    elif status == "running":
        # Reset finished_at when re-running a step.
        values["finished_at"] = None
        values["started_at"] = func.current_timestamp()
    conn.execute(
        update(steps_table)
        .where(
            (steps_table.c.migration_id == mig_id)
            & (steps_table.c.step_index == step_index)
        )
        .values(**values)
    )


def update_step_progress(
    conn: Connection,
    mig_id: str,
    step_index: int,
    progress: dict[str, Any],
) -> None:
    """Persist a Checkpoint snapshot to ``joryu_migration_steps.progress``.

    Called by the ``Checkpoint`` instance the runner constructs; the connection
    used here must be the same as the step's working connection so the write
    commits in the same transaction as the user's preceding DML (§13.1.3).
    """
    encoded = json.dumps(progress)
    conn.execute(
        update(steps_table)
        .where(
            (steps_table.c.migration_id == mig_id)
            & (steps_table.c.step_index == step_index)
        )
        .values(progress=encoded)
    )


def delete_step_rows(conn: Connection, mig_id: str) -> None:
    conn.execute(delete(steps_table).where(steps_table.c.migration_id == mig_id))


def mark_migration(
    mig_id: str,
    as_state: str,
    *,
    reason: str | None = None,
    url: str | None = None,
) -> None:
    """CLI helper: ``joryu mark <id> --as=<state>`` (§16, §9.2).

    Implements the lifecycle transitions allowed by §9.2:

    * ``--as=applied`` / ``--as=pending`` / ``--as=failed`` / ``--as=paused``
      update the migration row in place.
    * ``--as=paused`` requires a ``reason`` (the CLI validates this earlier);
      we propagate it into ``pause_reason``.
    * ``--as=failed`` / ``--as=paused`` clear ``finished_at`` so subsequent
      retries see a fresh attempt window.

    Raises ``ValueError`` if ``as_state`` is unknown or if the migration row
    does not exist (cannot mark a row that was never recorded — apply has to
    insert it first, or the operator should run ``joryu apply`` once).
    """
    if as_state not in _MIGRATION_STATUSES:
        raise ValueError(
            f"invalid migration status {as_state!r}; "
            f"expected one of {sorted(_MIGRATION_STATUSES)}"
        )
    if as_state == "paused" and not reason:
        raise ValueError("--as=paused requires a reason")

    from sqlalchemy import create_engine

    from .config import load_config, resolve_database_url

    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    engine = create_engine(resolved_url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin():
                ensure_state_tables(conn)
            row = get_migration_row(conn, mig_id)
            if conn.in_transaction():
                conn.commit()
            if row is None:
                raise ValueError(
                    f"migration {mig_id!r} has no row in joryu_migrations; "
                    "run `joryu apply` once before marking"
                )
            values: dict[str, Any] = {"status": as_state}
            if as_state == "applied":
                values["finished_at"] = func.current_timestamp()
                values["last_error"] = None
                values["pause_reason"] = None
            elif as_state == "pending":
                values["finished_at"] = None
                values["last_error"] = None
                values["pause_reason"] = None
            elif as_state == "failed":
                values["finished_at"] = None
                values["last_error"] = (
                    f"set to failed by `joryu mark` (was {row['status']!r})"
                )
                values["pause_reason"] = None
            elif as_state == "paused":
                values["finished_at"] = None
                values["pause_reason"] = reason
                values["last_error"] = None
            with conn.begin():
                conn.execute(
                    update(migrations_table)
                    .where(migrations_table.c.id == mig_id)
                    .values(**values)
                )
    finally:
        engine.dispose()


def mark_step(
    mig_id: str,
    step_index: int,
    as_state: str,
    *,
    url: str | None = None,
) -> None:
    """CLI helper: ``joryu mark <id>.<step> --as=<state>`` (§16, §9.2).

    Per §9.2, marking a step inside an ``applied`` migration downgrades the
    parent migration row to ``failed`` (with a synthetic ``last_error``) so
    the inconsistent "applied parent / pending child" state never appears.
    """
    if as_state not in _STEP_STATUSES:
        raise ValueError(
            f"invalid step status {as_state!r}; "
            f"expected one of {sorted(_STEP_STATUSES)}"
        )

    from sqlalchemy import create_engine

    from .config import load_config, resolve_database_url

    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    engine = create_engine(resolved_url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin():
                ensure_state_tables(conn)
            mig_row = get_migration_row(conn, mig_id)
            if mig_row is None:
                raise ValueError(
                    f"migration {mig_id!r} has no row in joryu_migrations; "
                    "cannot mark a step without a parent row"
                )
            step_rows = {
                r["step_index"]: r for r in get_step_rows(conn, mig_id)
            }
            if conn.in_transaction():
                conn.commit()
            existing = step_rows.get(step_index)
            if existing is None:
                raise ValueError(
                    f"step {mig_id}.{step_index} has no row in joryu_migration_steps"
                )
            with conn.begin():
                values: dict[str, Any] = {"status": as_state}
                if as_state in ("done", "skipped"):
                    values["finished_at"] = func.current_timestamp()
                elif as_state == "pending":
                    values["finished_at"] = None
                conn.execute(
                    update(steps_table)
                    .where(
                        (steps_table.c.migration_id == mig_id)
                        & (steps_table.c.step_index == step_index)
                    )
                    .values(**values)
                )
                # §9.2: marking a step pending inside an applied migration
                # downgrades the parent to failed.
                if as_state == "pending" and mig_row["status"] == "applied":
                    conn.execute(
                        update(migrations_table)
                        .where(migrations_table.c.id == mig_id)
                        .values(
                            status="failed",
                            finished_at=None,
                            last_error=(
                                f"downgraded from applied because step "
                                f"{step_index} was reset to pending via "
                                "`joryu mark`"
                            ),
                            pause_reason=None,
                        )
                    )
    finally:
        engine.dispose()


def repair_checksum(
    mig_id: str,
    *,
    url: str | None = None,
    migrations_dir: Any = None,
) -> str:
    """CLI helper: ``joryu repair <id>`` (§7.1).

    Recomputes the file checksum for ``mig_id`` and writes it into the
    ``joryu_migrations.checksum`` column. Returns the new checksum so callers
    (the CLI) can echo it.

    Raises ``ValueError`` if the migration is not present on disk or in the
    state table.
    """
    from pathlib import Path

    from sqlalchemy import create_engine

    from .config import load_config, resolve_database_url, resolve_migrations_dir
    from .loader import load_migrations
    from .registry import MIGRATIONS, register_operations

    cfg = load_config()
    resolved_url = resolve_database_url(url, cfg)
    resolved_dir = resolve_migrations_dir(
        Path(migrations_dir) if migrations_dir is not None else None, cfg
    )

    load_migrations(resolved_dir)
    m = MIGRATIONS.get(mig_id)
    if m is None:
        raise ValueError(
            f"migration {mig_id!r} not found under {resolved_dir!s}"
        )
    if not m.registered:
        register_operations(m)
    new_checksum = m.checksum()

    engine = create_engine(resolved_url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin():
                ensure_state_tables(conn)
            row = get_migration_row(conn, mig_id)
            if conn.in_transaction():
                conn.commit()
            if row is None:
                raise ValueError(
                    f"migration {mig_id!r} has no row in joryu_migrations; "
                    "nothing to repair"
                )
            with conn.begin():
                conn.execute(
                    update(migrations_table)
                    .where(migrations_table.c.id == mig_id)
                    .values(checksum=new_checksum)
                )
    finally:
        engine.dispose()
    return new_checksum


def load_step_progress(row: dict[str, Any]) -> dict[str, Any]:
    """Decode the ``progress`` column (JSON text) back into a dict."""
    raw = row.get("progress")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return dict(raw) if isinstance(raw, dict) else {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("could not decode step progress for %s.%s", row.get("migration_id"), row.get("step_index"))
        return {}
    return decoded if isinstance(decoded, dict) else {}


__all__ = [
    "ensure_state_tables",
    "migrations_table",
    "steps_table",
    "get_migration_row",
    "insert_migration",
    "update_migration_status",
    "list_migration_rows",
    "delete_migration_row",
    "get_step_rows",
    "upsert_step",
    "update_step_progress",
    "delete_step_rows",
    "load_step_progress",
    "mark_migration",
    "mark_step",
    "repair_checksum",
]
