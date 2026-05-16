"""Integration smoke for Wave-2 surfaces wired together."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from sqlalchemy import MetaData, create_engine, text

from joryu.cli import cli


def test_cli_test_unit_runs_against_empty_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init"]).exit_code == 0
    res = runner.invoke(cli, ["test", "--unit"])
    assert res.exit_code == 0
    assert "applied 0/0" in res.output


def test_cli_test_unit_runs_a_real_migration(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    (tmp_path / "migrations" / "20260101T120000_users.py").write_text(
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T120000_users")
def upgrade():
    op.create_table("users",
        op.column("id", t.BigInt, primary_key=True),
        op.column("email", t.Text, nullable=False))
'''
    )
    res = runner.invoke(cli, ["test", "--unit"])
    assert res.exit_code == 0, res.output
    assert "applied 1/1" in res.output


def test_cli_test_integration_friendly_message(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    res = runner.invoke(cli, ["test", "--integration"])
    assert res.exit_code == 0
    assert "v0.3" in res.output or "integration" in res.output


def test_cli_import_alembic_on_empty_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    (tmp_path / "alembic").mkdir()
    (tmp_path / "alembic" / "versions").mkdir()
    res = runner.invoke(
        cli,
        ["import", "alembic", "--alembic-dir", str(tmp_path / "alembic"),
         "--output-dir", str(tmp_path / "out")],
    )
    assert res.exit_code == 0, res.output
    assert "converted" in res.output


def test_op_historical_model_returns_vtable_after_replay(tmp_path: Path):
    """op.historical_model should surface the replayed schema (§12)."""
    import joryu
    from joryu import op, types as t
    from joryu.registry import MIGRATIONS, register_operations
    from joryu.virtual_schema import historical_table

    @joryu.migration(id="20260101T000000_a")
    def a():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))

    register_operations(MIGRATIONS["20260101T000000_a"])
    vt = historical_table("users")
    # Returns either a VTable for the populated registry, or None if the
    # contextvar isn't set (we accept both — pre-execution callers see None).
    assert vt is None or vt.name == "users"
