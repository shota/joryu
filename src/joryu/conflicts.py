"""Conflict shapes for verify (§7.2.1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ConflictKind = Literal[
    "double_alter",
    "add_drop",
    "table_drop",
    "column_rename",
    "table_rename",
]


@dataclass(frozen=True)
class OpRef:
    migration_id: str
    step_index: int
    op_kind: str
    target: tuple[str, ...]
    source_line: int | None = None


@dataclass(frozen=True)
class Conflict:
    kind: ConflictKind
    left: OpRef
    right: OpRef
    message: str

    def __str__(self) -> str:
        return self.message
