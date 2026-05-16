"""run_python checkpoint + op.step (§13)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from joryu.runner import apply, status


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.py").write_text(body)


def test_run_python_checkpoint_persists(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t
from sqlalchemy import text

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("k", op.column("id", t.BigInt, primary_key=True),
                          op.column("v", t.Int, nullable=False))

    def seed(conn, dialect, checkpoint):
        already = checkpoint.get("seeded", False)
        if already:
            return
        conn.execute(text("INSERT INTO k (id, v) VALUES (1, 100)"))
        checkpoint.set("seeded", True)

    op.run_python(seed)
''',
    )

    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT v FROM k WHERE id = 1")).scalar()
        assert row == 100
        # checkpoint persisted
        prog = conn.execute(
            text("SELECT progress FROM joryu_migration_steps WHERE step_index = 1")
        ).scalar()
        assert "seeded" in prog


def test_op_step_pause_marks_paused(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_pause",
        '''
import joryu
from joryu import op

@joryu.migration(id="20260101T000000_pause")
def upgrade():
    @op.step
    def wait(conn, dialect, checkpoint):
        if checkpoint.get("ready"):
            return True
        raise op.PauseStep("not ready")
''',
    )

    import pytest

    from joryu.exceptions import MigrationPaused

    with pytest.raises(MigrationPaused):
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "paused"
    assert rows[0]["pause_reason"]


def test_op_step_skip_marks_skipped(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_skip",
        '''
import joryu
from joryu import op

@joryu.migration(id="20260101T000000_skip")
def upgrade():
    @op.step
    def maybe(conn, dialect, checkpoint):
        raise op.SkipStep("nothing to do")
''',
    )

    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "applied"
    assert rows[0]["steps"][0]["status"] == "skipped"
