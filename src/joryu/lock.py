"""Advisory lock context managers (SPEC §9.7).

Apply runs must be serialised across processes so two concurrent ``joryu
apply`` invocations cannot interleave step writes. Each dialect ships a
different primitive:

  * PostgreSQL — ``pg_advisory_lock(key)`` / ``pg_advisory_unlock(key)``.
  * MySQL / MariaDB — ``GET_LOCK(name, timeout)`` / ``RELEASE_LOCK(name)``.
  * SQLite — there is no real advisory lock; SQLite is single-writer anyway.
    We acquire a process-local ``threading.Lock`` for the duration of the
    apply run so multiple threads inside the same process serialise correctly.
    Cross-process concurrency on SQLite still relies on SQLite's own writer
    exclusion. Document this in operator docs.

All implementations are exposed through a single
``advisory_lock(conn, dialect_name, key)`` context manager.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("joryu")


_PG_KEY_MASK = 0x7FFFFFFF  # pg_advisory_lock accepts a signed 32-bit integer.

# One lock per (dialect, key) pair; protects against concurrent threads in the
# same process attempting to apply at the same time on SQLite.
_local_locks: dict[tuple[str, str], threading.Lock] = {}
_local_locks_guard = threading.Lock()


def _local_lock(dialect_name: str, key: str) -> threading.Lock:
    with _local_locks_guard:
        lock = _local_locks.get((dialect_name, key))
        if lock is None:
            lock = threading.Lock()
            _local_locks[(dialect_name, key)] = lock
        return lock


def _hash_key(key: str) -> int:
    h = hashlib.sha256(key.encode()).digest()
    # Take the low 4 bytes and mask to a signed-positive 32-bit value.
    return int.from_bytes(h[:4], "big") & _PG_KEY_MASK


@contextmanager
def advisory_lock(
    conn: Connection,
    dialect_name: str,
    key: str = "joryu",
) -> Iterator[None]:
    """Acquire the dialect-specific advisory lock for the duration of the block."""
    if dialect_name in ("postgresql", "postgres"):
        yield from _pg_lock(conn, key)
    elif dialect_name in ("mysql", "mariadb"):
        yield from _mysql_lock(conn, key)
    elif dialect_name == "sqlite":
        yield from _sqlite_lock(conn, key)
    else:
        log.warning("advisory_lock: no native primitive for dialect=%s; using process-local lock", dialect_name)
        yield from _process_lock(dialect_name, key)


def _pg_lock(conn: Connection, key: str) -> Iterator[None]:
    lock_key = _hash_key(key)
    log.debug("acquiring pg_advisory_lock(%s) for key=%r", lock_key, key)
    conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key})
    # pg_advisory_lock is session-scoped; commit so the autobegin transaction
    # closes and surrounding code can open its own ``conn.begin()`` blocks.
    _safe_commit(conn)
    try:
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            _safe_commit(conn)
        except Exception:  # pragma: no cover - best-effort release
            log.exception("failed to release pg_advisory_lock(%s)", lock_key)


def _mysql_lock(conn: Connection, key: str) -> Iterator[None]:
    log.debug("acquiring GET_LOCK(%r)", key)
    # -1 = wait indefinitely. We don't poll; concurrent applies should be rare.
    result = conn.execute(text("SELECT GET_LOCK(:k, -1)"), {"k": key}).scalar()
    _safe_commit(conn)
    if result != 1:
        raise RuntimeError(f"could not acquire MySQL GET_LOCK({key!r}); got {result!r}")
    try:
        yield
    finally:
        try:
            conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": key})
            _safe_commit(conn)
        except Exception:  # pragma: no cover - best-effort release
            log.exception("failed to release MySQL GET_LOCK(%r)", key)


def _safe_commit(conn: Connection) -> None:
    """Commit any open auto-begun transaction. No-op if nothing is open."""
    if conn.in_transaction():
        conn.commit()


def _sqlite_lock(conn: Connection, key: str) -> Iterator[None]:
    # SQLite advisory lock is best-effort: in-process serialisation only.
    # Cross-process safety still depends on SQLite's own writer exclusion.
    lock = _local_lock("sqlite", key)
    log.debug("acquiring process-local sqlite lock for key=%r", key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _process_lock(dialect_name: str, key: str) -> Iterator[None]:
    lock = _local_lock(dialect_name, key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


__all__ = ["advisory_lock"]
