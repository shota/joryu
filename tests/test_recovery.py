"""Half-failed recovery prompt and non-interactive on_failure (§10.5)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from joryu.exceptions import MigrationFailed
from joryu.runner import apply, status


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.py").write_text(body)


# A migration that fails on step 2 the first time, then succeeds (the
# failure is gated by a sentinel file the test toggles).
def _write_flaky_migration(dir_: Path, sentinel: Path) -> None:
    _write(
        dir_,
        "20260101T000000_flaky",
        f'''
import os
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_flaky")
def upgrade():
    op.create_table("items",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("name", t.Text))

    @op.step
    def boom(conn, dialect, checkpoint):
        if not os.path.exists({str(sentinel)!r}):
            raise RuntimeError("simulated transient failure")
        return True
''',
    )


def test_non_interactive_resume_picks_up_failed_step(
    tmp_migrations_dir: Path, sqlite_url: str, tmp_path: Path
):
    sentinel = tmp_path / "ok.flag"
    _write_flaky_migration(tmp_migrations_dir, sentinel)

    # First apply: should fail at step 2.
    with pytest.raises(MigrationFailed):
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "failed"

    # Flip the sentinel so the step now succeeds.
    sentinel.write_text("go")

    # Default non_interactive=True, on_failure="resume" should re-run.
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "applied"


def test_non_interactive_abort_raises(
    tmp_migrations_dir: Path, sqlite_url: str, tmp_path: Path
):
    sentinel = tmp_path / "never"
    _write_flaky_migration(tmp_migrations_dir, sentinel)

    with pytest.raises(MigrationFailed):
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    with pytest.raises(MigrationFailed) as exc:
        apply(
            url=sqlite_url,
            migrations_dir=tmp_migrations_dir,
            non_interactive=True,
            on_failure="abort",
        )
    assert "recovery aborted" in str(exc.value.cause).lower()


def test_prompt_recovery_renders_menu(monkeypatch, tmp_path: Path):
    """Cover the TTY prompt path with a mocked click.prompt."""
    from joryu import runner

    captured: dict = {}

    def fake_prompt(*args, **kwargs):
        captured["called"] = True
        return 1  # choose [1] resume

    monkeypatch.setattr(runner.click, "prompt", fake_prompt)
    mig_row = {"id": "demo"}
    step_rows = [
        {"step_index": 0, "op_kind": "create_table", "status": "done",
         "op_fingerprint": "x"},
        {"step_index": 1, "op_kind": "step", "status": "failed",
         "op_fingerprint": "y"},
    ]
    decision = runner._prompt_recovery(mig_row, step_rows)
    assert captured.get("called")
    assert decision.action == "resume"
    assert decision.step_index == 1


def test_prompt_recovery_restart_from(monkeypatch):
    from joryu import runner

    answers = iter([2, 1])  # choose [2], then "restart from step 1"

    def fake_prompt(*args, **kwargs):
        return next(answers)

    monkeypatch.setattr(runner.click, "prompt", fake_prompt)
    decision = runner._prompt_recovery(
        {"id": "demo"},
        [
            {"step_index": 0, "op_kind": "k", "status": "done", "op_fingerprint": "f"},
            {"step_index": 1, "op_kind": "k", "status": "failed", "op_fingerprint": "f"},
        ],
    )
    assert decision.action == "restart_from"
    assert decision.step_index == 0
