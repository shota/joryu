"""Tests for joryu.snapshot (§16)."""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa

import joryu
from joryu import op, types as t
from joryu.snapshot import snapshot


def _seed_sqlite(url: str) -> None:
    eng = sa.create_engine(url)
    md = sa.MetaData()
    sa.Table(
        "users",
        md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
    )
    md.create_all(eng)
    eng.dispose()


def test_snapshot_against_db_json(sqlite_url):
    _seed_sqlite(sqlite_url)
    out = snapshot(url=sqlite_url, against="db", fmt="json")
    doc = json.loads(out)
    assert "tables" in doc
    assert "users" in doc["tables"]
    cols = doc["tables"]["users"]["columns"]
    assert "id" in cols and "email" in cols
    # JSON document is a single line (compact).
    assert "\n" not in out


def test_snapshot_against_db_sql_emits_create_table(sqlite_url):
    _seed_sqlite(sqlite_url)
    out = snapshot(url=sqlite_url, against="db", fmt="sql")
    upper = out.upper()
    assert "CREATE TABLE" in upper
    # Each ``CREATE`` statement ends with ``;`` (multi-line statements are
    # produced as single semicolon-terminated chunks).
    statements = [s.strip() for s in out.split(";") if s.strip()]
    assert statements, "expected at least one statement"
    for stmt in statements:
        assert stmt.upper().startswith(("CREATE", "ALTER"))


def test_snapshot_db_requires_url():
    import pytest
    with pytest.raises(ValueError):
        snapshot(url=None, against="db", fmt="json")


def test_snapshot_against_replay_json(tmp_migrations_dir: Path):
    # Create a migration file on disk; snapshot --against=replay must not need a DB.
    mig = tmp_migrations_dir / "20260101T000000_init.py"
    mig.write_text(
        "import joryu\n"
        "from joryu import op, types as t\n"
        "\n"
        "@joryu.migration(id='20260101T000000_init')\n"
        "def upgrade():\n"
        "    op.create_table('books', op.column('id', t.BigInt, primary_key=True),\n"
        "                              op.column('title', t.Text, nullable=False))\n"
    )
    out = snapshot(migrations_dir=tmp_migrations_dir, against="replay", fmt="json")
    doc = json.loads(out)
    assert "books" in doc["tables"]
    assert "title" in doc["tables"]["books"]["columns"]


def test_snapshot_replay_sql_includes_create_table(tmp_migrations_dir: Path):
    mig = tmp_migrations_dir / "20260101T000000_init.py"
    mig.write_text(
        "import joryu\n"
        "from joryu import op, types as t\n"
        "\n"
        "@joryu.migration(id='20260101T000000_init')\n"
        "def upgrade():\n"
        "    op.create_table('books', op.column('id', t.BigInt, primary_key=True))\n"
    )
    out = snapshot(migrations_dir=tmp_migrations_dir, against="replay", fmt="sql")
    assert "CREATE TABLE" in out
    assert "books" in out
    assert out.rstrip().endswith(";")
