"""Progress emitter behaviour (§14.2)."""
from __future__ import annotations

import io
import time

from joryu.progress import (
    InteractiveEmitter,
    JsonEmitter,
    PlainEmitter,
    make_emitter,
)


class _FakeTTY(io.StringIO):
    """StringIO that claims to be a TTY."""

    def isatty(self) -> bool:  # type: ignore[override]
        return True


def test_interactive_emitter_falls_back_to_plain_on_non_tty():
    sink = io.StringIO()
    em = InteractiveEmitter(stream=sink)
    em.migration_start("mig", 2, "per_step")
    em.step_start(1, 2, "create_table", "users", None)
    em.step_done(1, 42)
    out = sink.getvalue()
    # PlainEmitter fallback uses "[joryu]" prefix.
    assert "[joryu]" in out
    assert "create_table" in out


def test_interactive_emitter_renders_ansi_on_tty():
    sink = _FakeTTY()
    em = InteractiveEmitter(stream=sink)
    em.migration_start("mig", 2, "per_step")
    em.step_start(1, 2, "create_table", "users", None)
    em.step_done(1, 42)
    em.step_start(2, 2, "alter_column", "users.email", None)
    em.step_failed(2, RuntimeError("boom"))
    out = sink.getvalue()
    # Header line is present.
    assert "mig" in out
    # In-progress marker used during step_start, OK marker used after done.
    assert "OK " in out
    assert "  FAIL " in out
    # Red ANSI sequence around the failure line.
    assert "\x1b[31m" in out
    # Reset escape closes the colored region.
    assert "\x1b[0m" in out
    # Clear-EOL escape used for in-place updates.
    assert "\x1b[K" in out


def test_interactive_emitter_skipped_is_dim():
    sink = _FakeTTY()
    em = InteractiveEmitter(stream=sink)
    em.step_start(1, 1, "noop", "skip-me", None)
    em.step_skipped(1)
    out = sink.getvalue()
    assert "SKIP " in out
    assert "\x1b[2m" in out  # dim


def test_interactive_emitter_progress_rate_limited():
    sink = _FakeTTY()
    em = InteractiveEmitter(stream=sink)
    em.step_start(1, 1, "run_python", "backfill", None)
    em.step_progress(1, {"percent": 10})
    # Immediately again — must be rate-limited (no extra output).
    snap = sink.getvalue()
    em.step_progress(1, {"percent": 11})
    assert sink.getvalue() == snap
    # After 100 ms a new progress line gets through.
    time.sleep(0.11)
    em.step_progress(1, {"percent": 20})
    assert sink.getvalue() != snap


def test_make_emitter_auto_picks_interactive_on_tty():
    sink = _FakeTTY()
    em = make_emitter("auto", stream=sink)
    assert isinstance(em, InteractiveEmitter)


def test_make_emitter_auto_picks_plain_on_non_tty():
    sink = io.StringIO()
    em = make_emitter("auto", stream=sink)
    assert isinstance(em, PlainEmitter)


def test_make_emitter_json():
    sink = io.StringIO()
    em = make_emitter("json", stream=sink)
    assert isinstance(em, JsonEmitter)
