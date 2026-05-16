"""CLI smoke (§16)."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from joryu.cli import cli


def test_init_creates_files(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    res = runner.invoke(cli, ["init"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "joryu.toml").exists()
    assert (tmp_path / "migrations").is_dir()


def test_generate_empty_creates_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    res = runner.invoke(cli, ["generate", "add_users", "--empty"])
    assert res.exit_code == 0, res.output
    files = list((tmp_path / "migrations").glob("*.py"))
    assert files, "generate did not produce a file"


def test_verify_no_migrations_returns_zero(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    res = runner.invoke(cli, ["verify"])
    assert res.exit_code == 0, res.output


def test_apply_progress_flags_mutually_exclusive(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    res = runner.invoke(cli, ["apply", "--plain", "--json"])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output.lower() or "mutually exclusive" in (res.stderr_bytes or b"").decode("utf-8", "replace").lower() or "mutually" in (res.output or "").lower()
