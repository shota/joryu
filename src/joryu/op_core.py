"""Core abstractions for Operations and step control flow.

Every op.* call produces an Operation subclass that is appended to the current
migration's operations list during the registration phase (§14.1). During the
execution phase, the runner invokes ``op.apply(ctx)`` on each in order.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from .checkpoint import Checkpoint


# ---- Control-flow exceptions for op.step / op.run_python (§13.2.2) ----

class PauseStep(Exception):
    """Raise from inside an op.step / op.run_python body to pause the migration."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


class SkipStep(Exception):
    """Raise from inside an op.step / op.run_python body to skip the step."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


# ---- Execution context (passed into Operation.apply) ----

@dataclass
class ExecutionContext:
    conn: "Connection"
    dialect_name: str          # 'postgresql' | 'mysql' | 'mariadb' | 'sqlite'
    transaction_mode: str
    on_mismatch: str
    checkpoint: "Checkpoint | None" = None
    extras: dict[str, Any] = field(default_factory=dict)


# ---- Operation ABC ----

class Operation(ABC):
    #: Stable kind identifier used in joryu_migration_steps.op_kind.
    kind: str = ""
    #: Best-effort source-line for diagnostics.
    source_line: int | None = None

    @abstractmethod
    def targets(self) -> list[tuple[str, ...]]:
        """Targets touched by this op, for conflict detection (§7.2).

        Return ``[("users",)]`` for table-level ops and ``[("users", "email")]``
        for column-level ops. Return ``[]`` for ops that cannot be statically
        analyzed (raw SQL, run_python, op.step)."""

    @abstractmethod
    def describe(self) -> str:
        """One-line human-readable description (§14.4)."""

    def fingerprint(self) -> str:
        """SHA-256 hex of the canonical op payload (§9.2 op_fingerprint)."""
        payload = json.dumps(self._fingerprint_payload(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:64]

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "description": self.describe()}

    @abstractmethod
    def apply(self, ctx: ExecutionContext) -> None:
        """Execute the op against ``ctx.conn``. May raise to fail the step."""


class OpaqueOperation(Operation):
    """Mixin marker for ops that are not statically analyzable (op.execute(raw),
    op.run_python, op.step). These never produce Conflict objects."""

    def targets(self) -> list[tuple[str, ...]]:
        return []
