"""Runtime contextvars set by the runner during the execution phase.

This module is intentionally tiny: it owns the "which migration is currently
executing?" handle so other layers (most notably :mod:`joryu.virtual_schema`)
can answer questions like ``op.historical_model(table)`` without circular
imports.

The runner sub-agent is expected to call :func:`set_current_migration` before
invoking each migration's ops, and :func:`reset_current_migration` afterwards.
Until that wiring lands, the contextvar simply stays ``None`` and
:func:`get_current_migration` returns ``None`` — callers fall back to "replay
everything declared so far" semantics.
"""
from __future__ import annotations

import contextvars

_current_executing_migration_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_joryu_current_executing_migration_id", default=None
)


def set_current_migration(mig_id: str | None) -> contextvars.Token:
    """Bind the id of the migration whose ops are currently being executed."""
    return _current_executing_migration_id.set(mig_id)


def reset_current_migration(token: contextvars.Token) -> None:
    _current_executing_migration_id.reset(token)


def get_current_migration() -> str | None:
    """Return the migration id currently being executed, or ``None``."""
    return _current_executing_migration_id.get()


__all__ = [
    "get_current_migration",
    "reset_current_migration",
    "set_current_migration",
]
