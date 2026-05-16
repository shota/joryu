"""Public exception hierarchy (§16.3)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .conflicts import Conflict


class JoryuError(Exception):
    """Base class for every joryu exception."""


class MigrationFailed(JoryuError):
    def __init__(self, migration_id: str, step_index: int, step_name: str, cause: BaseException) -> None:
        self.migration_id = migration_id
        self.step_index = step_index
        self.step_name = step_name
        self.cause = cause
        super().__init__(
            f"migration {migration_id!r} failed at step {step_index} ({step_name}): {cause}"
        )


class MigrationPaused(JoryuError):
    def __init__(self, migration_id: str, step_index: int, step_name: str, reason: str) -> None:
        self.migration_id = migration_id
        self.step_index = step_index
        self.step_name = step_name
        self.reason = reason
        super().__init__(
            f"migration {migration_id!r} paused at step {step_index} ({step_name}): {reason}"
        )


class VerificationFailed(JoryuError):
    def __init__(self, conflicts: list["Conflict"]) -> None:
        self.conflicts = conflicts
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.conflicts:
            return "verification failed"
        return "\n".join(c.message for c in self.conflicts)

    def __str__(self) -> str:
        return self._render()


class ProductionGuardError(JoryuError):
    def __init__(
        self,
        detected_env: Literal["staging", "production", "production-like"],
        host: str | None,
    ) -> None:
        self.detected_env = detected_env
        self.host = host
        super().__init__(
            f"refusing to run on detected environment {detected_env!r} (host={host}); "
            "pass --allow-prod to override"
        )


class UnsupportedTypeUsage(JoryuError):
    """Raised at registration when a type is used incorrectly for a dialect."""
