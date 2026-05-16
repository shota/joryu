"""Migration decorator + per-process registry.

The registration model is two-phase (§14.1):
  1. Importing the migration file evaluates @joryu.migration(...) which builds a
     Migration record and stores it in MIGRATIONS without invoking upgrade().
  2. A separate call to ``register_operations(migration)`` enters a registration
     context (so op.* calls attach to this migration) and invokes upgrade().
"""
from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .op_core import Operation

# Module-level: every Migration ever registered, keyed by id.
MIGRATIONS: dict[str, "Migration"] = {}

# Context var pointing to the migration currently being built (registration phase).
_current_migration: contextvars.ContextVar["Migration | None"] = contextvars.ContextVar(
    "_current_migration", default=None
)


@dataclass
class Migration:
    id: str
    upgrade_fn: Callable[[], None]
    depends_on: list[str] = field(default_factory=list)
    transaction_mode: str = "per_step"
    dialects: list[str] | None = None
    tags: list[str] = field(default_factory=list)
    group: str | None = None
    on_mismatch: str = "error"
    downgrade_fn: Callable[[], None] | None = None
    file_path: Path | None = None
    operations: list["Operation"] = field(default_factory=list)
    registered: bool = False

    def fingerprint_source(self) -> str:
        """Stable representation of the migration source for checksum (§7.1)."""
        try:
            src = inspect.getsource(self.upgrade_fn)
        except (OSError, TypeError):
            src = self.upgrade_fn.__qualname__
        return src

    def checksum(self) -> str:
        h = hashlib.sha256()
        h.update(self.id.encode())
        h.update(b"\0")
        # Use the canonical op sequence once registered; fall back to source.
        if self.registered:
            payload = json.dumps(
                [(op.kind, op.fingerprint()) for op in self.operations],
                sort_keys=True,
            )
            h.update(payload.encode())
        else:
            h.update(self.fingerprint_source().encode())
        return h.hexdigest()[:64]


def migration(
    *,
    id: str,
    depends_on: list[str] | None = None,
    transaction_mode: str = "per_step",
    dialects: list[str] | None = None,
    tags: list[str] | None = None,
    group: str | None = None,
    on_mismatch: str = "error",
) -> Callable[[Callable[[], None]], Callable[[], None]]:
    """Decorator: declare a migration (§3.2)."""

    if transaction_mode not in ("per_migration", "per_step", "none"):
        raise ValueError(f"invalid transaction_mode={transaction_mode!r}")
    if on_mismatch not in ("error", "alter", "skip"):
        raise ValueError(f"invalid on_mismatch={on_mismatch!r}")

    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        if id in MIGRATIONS:
            raise ValueError(f"duplicate migration id {id!r}")
        try:
            file_path = Path(inspect.getsourcefile(fn) or "")
        except TypeError:
            file_path = None
        m = Migration(
            id=id,
            upgrade_fn=fn,
            depends_on=list(depends_on or []),
            transaction_mode=transaction_mode,
            dialects=list(dialects) if dialects else None,
            tags=list(tags or []),
            group=group,
            on_mismatch=on_mismatch,
            file_path=file_path,
        )
        MIGRATIONS[id] = m
        # Attach the migration object to the function so the user can introspect.
        fn.__joryu_migration__ = m  # type: ignore[attr-defined]
        return fn

    return decorator


def downgrade(fn: Callable[[], None]) -> Callable[[], None]:
    """Decorator: attach a downgrade body to the most-recently-declared migration
    in the same source file (§3.2)."""

    src_file = inspect.getsourcefile(fn)
    candidates = [m for m in MIGRATIONS.values() if m.file_path and str(m.file_path) == src_file]
    if not candidates:
        raise RuntimeError("@joryu.downgrade must follow @joryu.migration in the same file")
    candidates[-1].downgrade_fn = fn
    return fn


# ---- Registration context (used by op.* during the registration phase) ----

def current_migration() -> "Migration":
    m = _current_migration.get()
    if m is None:
        raise RuntimeError(
            "op.* called outside of a registration phase; "
            "this usually means an op.* call ran at import time or in production code"
        )
    return m


class _RegistrationScope:
    def __init__(self, m: "Migration") -> None:
        self.m = m
        self.token: Any = None

    def __enter__(self) -> "Migration":
        self.m.operations.clear()
        self.token = _current_migration.set(self.m)
        return self.m

    def __exit__(self, *exc: object) -> None:
        _current_migration.reset(self.token)


def register_operations(m: "Migration") -> None:
    """Phase 1: run upgrade() to populate the ops list, without touching DB."""
    with _RegistrationScope(m):
        m.upgrade_fn()
    m.registered = True


def reset_registry() -> None:
    MIGRATIONS.clear()
