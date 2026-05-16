"""Progress display emitters (§14.2 / §14.4).

Five modes are supported via :func:`make_emitter`:

* ``auto``        — Interactive on TTY, Plain otherwise.
* ``interactive`` — Live-style line output (v0.1: line-oriented; no ``rich``).
* ``plain``       — Line-oriented log to stderr, one event per line.
* ``json``        — JSONL to stdout (external monitoring).
* ``quiet``       — Suppressed unless a step fails; failures still surface.

All emitters use the standard library only.
"""
from __future__ import annotations

import json
import sys
import time
from typing import IO, Protocol


class ProgressEmitter(Protocol):
    """Abstract progress sink used by the runner.

    The runner invokes these methods in order; emitters are responsible only
    for rendering. Missing wiring at the runner end is non-fatal — emitters
    must tolerate any subset of events being absent.
    """

    def migration_start(self, migration_id: str, steps: int, transaction_mode: str) -> None: ...

    def step_start(
        self,
        step: int,
        total: int,
        op_kind: str,
        description: str,
        next_description: str | None,
    ) -> None: ...

    def step_done(self, step: int, duration_ms: int) -> None: ...

    def step_failed(self, step: int, exc: BaseException) -> None: ...

    def step_skipped(self, step: int) -> None: ...

    def step_progress(self, step: int, progress: dict) -> None: ...

    def migration_done(self, migration_id: str, duration_ms: int) -> None: ...


# ---------------------------------------------------------------------------
# Concrete emitters
# ---------------------------------------------------------------------------


class PlainEmitter:
    """Line-oriented log; one event per line, written to stderr."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def _write(self, line: str) -> None:
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            # ValueError: I/O on closed file. Swallow — progress is best-effort.
            pass

    def migration_start(self, migration_id: str, steps: int, transaction_mode: str) -> None:
        self._write(
            f"[joryu] applying {migration_id} ({steps} steps, transaction_mode={transaction_mode})"
        )

    def step_start(
        self,
        step: int,
        total: int,
        op_kind: str,
        description: str,
        next_description: str | None,
    ) -> None:
        self._write(f"[joryu]   step {step}/{total}: {op_kind} {description}")

    def step_done(self, step: int, duration_ms: int) -> None:
        self._write(f"[joryu]   step {step}: done ({_format_duration(duration_ms)})")

    def step_failed(self, step: int, exc: BaseException) -> None:
        self._write(f"[joryu]   step {step}: failed: {type(exc).__name__}: {exc}")

    def step_skipped(self, step: int) -> None:
        self._write(f"[joryu]   step {step}: skipped")

    def step_progress(self, step: int, progress: dict) -> None:
        payload = ", ".join(f"{k}={v}" for k, v in progress.items())
        self._write(f"[joryu]   step {step}: progress {payload}")

    def migration_done(self, migration_id: str, duration_ms: int) -> None:
        self._write(f"[joryu] done {migration_id} ({_format_duration(duration_ms)})")


class JsonEmitter:
    """JSONL emitter for structured external monitoring (writes to stdout)."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def _emit(self, payload: dict) -> None:
        try:
            self._stream.write(json.dumps(payload, default=str) + "\n")
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass

    def migration_start(self, migration_id: str, steps: int, transaction_mode: str) -> None:
        self._emit(
            {
                "event": "migration_start",
                "id": migration_id,
                "steps": steps,
                "transaction_mode": transaction_mode,
            }
        )

    def step_start(
        self,
        step: int,
        total: int,
        op_kind: str,
        description: str,
        next_description: str | None,
    ) -> None:
        self._emit(
            {
                "event": "step_start",
                "step": step,
                "total": total,
                "op": op_kind,
                "description": description,
                "next": next_description,
            }
        )

    def step_done(self, step: int, duration_ms: int) -> None:
        self._emit({"event": "step_done", "step": step, "duration_ms": duration_ms})

    def step_failed(self, step: int, exc: BaseException) -> None:
        self._emit(
            {
                "event": "step_failed",
                "step": step,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    def step_skipped(self, step: int) -> None:
        self._emit({"event": "step_skipped", "step": step})

    def step_progress(self, step: int, progress: dict) -> None:
        self._emit({"event": "step_progress", "step": step, "progress": progress})

    def migration_done(self, migration_id: str, duration_ms: int) -> None:
        self._emit(
            {
                "event": "migration_done",
                "id": migration_id,
                "duration_ms": duration_ms,
            }
        )


class QuietEmitter:
    """Emit nothing on success; surface only step failures (to stderr)."""

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def migration_start(self, migration_id: str, steps: int, transaction_mode: str) -> None:
        pass

    def step_start(
        self,
        step: int,
        total: int,
        op_kind: str,
        description: str,
        next_description: str | None,
    ) -> None:
        pass

    def step_done(self, step: int, duration_ms: int) -> None:
        pass

    def step_failed(self, step: int, exc: BaseException) -> None:
        try:
            self._stream.write(
                f"[joryu] step {step} failed: {type(exc).__name__}: {exc}\n"
            )
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass

    def step_skipped(self, step: int) -> None:
        pass

    def step_progress(self, step: int, progress: dict) -> None:
        pass

    def migration_done(self, migration_id: str, duration_ms: int) -> None:
        pass


class InteractiveEmitter:
    """TTY-aware emitter using raw ANSI control codes (no ``rich`` dep).

    Layout (per §14.3):

    * ``migration_start`` prints a header on its own line.
    * ``step_start`` prints an in-progress line prefixed with ``>``.
    * ``step_progress`` re-renders the same line in place (``\\r`` + ANSI
      clear-to-end-of-line), rate-limited to once per 100 ms per step.
    * ``step_done`` finalises the line as ``OK ...`` and moves to a new line.
    * ``step_failed`` finalises in red as ``FAIL ...``.
    * ``step_skipped`` finalises in dim as ``SKIP ...``.

    Falls back to :class:`PlainEmitter` behaviour when the stream is not a
    TTY. Markers are ASCII-only per CLAUDE.md ("no emojis").
    """

    _ESC = "\x1b"
    _CLEAR_EOL = f"{_ESC}[K"
    _RED = f"{_ESC}[31m"
    _DIM = f"{_ESC}[2m"
    _RESET = f"{_ESC}[0m"

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._last_progress: dict[int, float] = {}
        # Current in-progress step description, for redraws.
        self._active: dict[int, str] = {}
        self._tty = self._is_tty()
        self._fallback = PlainEmitter(stream=self._stream) if not self._tty else None

    def _is_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _write(self, text: str, *, newline: bool = True) -> None:
        try:
            self._stream.write(text + ("\n" if newline else ""))
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass

    def migration_start(self, migration_id: str, steps: int, transaction_mode: str) -> None:
        if self._fallback is not None:
            self._fallback.migration_start(migration_id, steps, transaction_mode)
            return
        self._write(
            f"> {migration_id}  ({steps} steps, transaction_mode={transaction_mode})"
        )

    def step_start(
        self,
        step: int,
        total: int,
        op_kind: str,
        description: str,
        next_description: str | None,
    ) -> None:
        if self._fallback is not None:
            self._fallback.step_start(step, total, op_kind, description, next_description)
            return
        line = f"  >  {step}/{total} {op_kind} {description}"
        self._active[step] = line
        # No trailing newline — the line stays "live" until step_done /
        # step_failed / step_skipped replaces it.
        self._write(f"\r{self._CLEAR_EOL}{line}", newline=False)

    def step_progress(self, step: int, progress: dict) -> None:
        if self._fallback is not None:
            self._fallback.step_progress(step, progress)
            return
        now = time.monotonic()
        last = self._last_progress.get(step, 0.0)
        if now - last < 0.1:
            return
        self._last_progress[step] = now
        base = self._active.get(step, f"  >  {step} (running)")
        payload = ", ".join(f"{k}={v}" for k, v in progress.items())
        self._write(f"\r{self._CLEAR_EOL}{base}  ({payload})", newline=False)

    def step_done(self, step: int, duration_ms: int) -> None:
        if self._fallback is not None:
            self._fallback.step_done(step, duration_ms)
            return
        base = self._active.pop(step, f"step {step}")
        # Swap the in-progress marker for the success marker.
        finished = base.replace("  >  ", "  OK ", 1)
        self._write(
            f"\r{self._CLEAR_EOL}{finished}  ({_format_duration(duration_ms)})"
        )

    def step_failed(self, step: int, exc: BaseException) -> None:
        if self._fallback is not None:
            self._fallback.step_failed(step, exc)
            return
        base = self._active.pop(step, f"step {step}")
        finished = base.replace("  >  ", "  FAIL ", 1)
        self._write(
            f"\r{self._CLEAR_EOL}{self._RED}{finished}  "
            f"{type(exc).__name__}: {exc}{self._RESET}"
        )

    def step_skipped(self, step: int) -> None:
        if self._fallback is not None:
            self._fallback.step_skipped(step)
            return
        base = self._active.pop(step, f"step {step}")
        finished = base.replace("  >  ", "  SKIP ", 1)
        self._write(f"\r{self._CLEAR_EOL}{self._DIM}{finished}{self._RESET}")

    def migration_done(self, migration_id: str, duration_ms: int) -> None:
        if self._fallback is not None:
            self._fallback.migration_done(migration_id, duration_ms)
            return
        self._write(f"  OK {migration_id} ({_format_duration(duration_ms)})")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_emitter(mode: str, *, stream: IO[str] | None = None) -> ProgressEmitter:
    """Build an emitter from a mode string.

    ``mode`` is one of ``"auto"``, ``"interactive"``, ``"plain"``, ``"json"``,
    ``"quiet"``. ``auto`` resolves to interactive on a TTY else plain (§14.2).
    """
    if mode == "auto":
        target = stream if stream is not None else sys.stderr
        is_tty = False
        try:
            is_tty = bool(target.isatty())
        except (AttributeError, ValueError):
            is_tty = False
        if is_tty:
            return InteractiveEmitter(stream=target)
        return PlainEmitter(stream=target)

    if mode == "interactive":
        return InteractiveEmitter(stream=stream)
    if mode == "plain":
        return PlainEmitter(stream=stream)
    if mode == "json":
        return JsonEmitter(stream=stream)
    if mode == "quiet":
        return QuietEmitter(stream=stream)

    raise ValueError(
        f"unknown progress mode {mode!r} "
        "(expected one of: auto, interactive, plain, json, quiet)"
    )


def _format_duration(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes}m{remainder:.1f}s"
