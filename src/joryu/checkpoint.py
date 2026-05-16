"""Checkpoint API (§13.3).

Persists JSON-serializable state into joryu_migration_steps.progress.
"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import text


def _default(o: Any) -> Any:
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(
        f"checkpoint values must be JSON-compatible (got {type(o).__name__})"
    )


class Checkpoint:
    """Dict-like persistent state for a single step.

    The "commit" half of set/update is delegated to ``commit`` (a callable the
    runner passes in). For per_step mode the commit closes the step transaction.
    For ``none`` mode the user is in charge of transactions; ``commit`` is a
    no-op and the user's ``with conn.begin():`` does the commit.
    """

    SIZE_SOFT_LIMIT = 1_000_000

    def __init__(
        self,
        migration_id: str,
        step_index: int,
        initial: dict[str, Any] | None = None,
        *,
        persist: Callable[[dict[str, Any]], None] | None = None,
        on_report: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._migration_id = migration_id
        self._step_index = step_index
        self._state: dict[str, Any] = dict(initial or {})
        self._persist = persist or (lambda _state: None)
        self._on_report = on_report or (lambda _payload: None)

    # ---- read ----
    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._state)

    # ---- write ----
    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self._flush()

    def update(self, mapping: dict[str, Any]) -> None:
        self._state.update(mapping)
        self._flush()

    def clear(self) -> None:
        self._state.clear()
        self._flush()

    # ---- progress display ----
    def report(self, *, percent: int | float | None = None, message: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if percent is not None:
            payload["percent"] = percent
        if message is not None:
            payload["message"] = message
        self._on_report(payload)

    # ---- internal ----
    def _flush(self) -> None:
        encoded = json.dumps(self._state, default=_default)
        if len(encoded) > self.SIZE_SOFT_LIMIT:
            import warnings

            warnings.warn(
                f"checkpoint for {self._migration_id}.{self._step_index} exceeds "
                "1 MB soft limit",
                stacklevel=3,
            )
        self._persist(self._state)


__all__ = ["Checkpoint"]
