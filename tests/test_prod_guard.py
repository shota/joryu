"""Production-guard confirmation prompts (§15.2)."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from joryu.cli import cli


def _init_with_prod_url(tmp_path: Path, monkeypatch, url: str) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    toml = tmp_path / "joryu.toml"
    text = toml.read_text().replace(
        'url = "env:DATABASE_URL"', f'url = "{url}"'
    )
    toml.write_text(text)


def test_apply_continue_past_failed_aborts_on_no(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["apply", "--continue-past-failed"], input="n\n"
    )
    # EXIT_PROD_GUARD == 5
    assert res.exit_code == 5, res.output
    assert "production-like" in res.output.lower() or "aborted" in res.output.lower()


def test_apply_continue_past_failed_proceeds_on_yes(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["apply", "--continue-past-failed"], input="y\n"
    )
    # Will fail to connect to the bogus URL — we only care that the prompt was
    # answered yes and execution proceeded past the guard. Any error from the
    # runner ends up as exit code 1 (general) or another non-5 code.
    assert res.exit_code != 5, res.output


def test_apply_continue_past_failed_skipped_with_non_interactive(
    tmp_path: Path, monkeypatch
):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["apply", "--continue-past-failed", "--non-interactive"]
    )
    # No prompt fired; exit code is whatever the runner produces but not the
    # guard code.
    assert res.exit_code != 5, res.output


def test_apply_continue_past_failed_local_skips_prompt(tmp_path: Path, monkeypatch):
    """A local-by-heuristic URL must NOT trigger the prompt."""
    _init_with_prod_url(
        tmp_path, monkeypatch, f"sqlite:///{tmp_path / 'app.db'}"
    )
    runner = CliRunner()
    res = runner.invoke(cli, ["apply", "--continue-past-failed"])
    # No prompt, sqlite URL works; whatever exit code the runner returns is
    # fine — guard must not trigger.
    assert "production-like" not in res.output.lower()


def test_mark_aborts_on_n(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["mark", "20260101T000000_foo", "--as=applied"], input="n\n"
    )
    assert res.exit_code == 5, res.output


def test_mark_proceeds_on_y(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["mark", "20260101T000000_foo", "--as=applied"], input="y\n"
    )
    # The state module does not have mark_migration wired, so the CLI returns
    # gracefully (exit code 0). Either way, the guard must not have aborted.
    assert res.exit_code != 5, res.output


def test_mark_non_interactive_skips_prompt(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["mark", "20260101T000000_foo", "--as=applied", "--non-interactive"],
    )
    assert res.exit_code != 5, res.output


def test_down_with_allow_prod_aborts_on_n(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["down", "--steps", "1", "--allow-prod"], input="n\n"
    )
    assert res.exit_code == 5, res.output


def test_down_with_allow_prod_and_yes_skips_prompt(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, "postgresql://db.example.com/app")
    runner = CliRunner()
    res = runner.invoke(
        cli, ["down", "--steps", "1", "--allow-prod", "--yes"]
    )
    # Guard skipped; the actual down call will likely fail against the bogus
    # URL, which is fine — we only check the guard did not abort with code 5.
    assert res.exit_code != 5, res.output


def test_down_local_does_not_prompt(tmp_path: Path, monkeypatch):
    _init_with_prod_url(tmp_path, monkeypatch, f"sqlite:///{tmp_path / 'app.db'}")
    runner = CliRunner()
    res = runner.invoke(cli, ["down", "--steps", "1"])
    assert "production-like" not in res.output.lower()
