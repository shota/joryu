"""End-to-end apply lifecycle on in-memory SQLite (§9)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

import joryu
from joryu import op, types as t
from joryu.runner import apply, status


def _write(dir_: Path, name: str, body: str) -> Path:
    path = dir_ / f"{name}.py"
    path.write_text(body)
    return path


def test_apply_creates_table_and_records_state(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_add_users",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_add_users")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True),
                              op.column("email", t.Text, nullable=False))
''',
    )

    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    assert insp.has_table("users")
    assert insp.has_table("joryu_migrations")
    assert insp.has_table("joryu_migration_steps")

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"


def test_apply_is_idempotent(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_add_users",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_add_users")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True))
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    # Second apply: must not raise even though table exists.
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "applied"


def test_apply_ensure_semantics_add_column(tmp_migrations_dir: Path, sqlite_url: str):
    # Migration A creates users with email.
    _write(
        tmp_migrations_dir,
        "20260101T000000_users",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_users")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True),
                              op.column("email", t.Text))
''',
    )
    # Migration B adds another column AND re-asserts email.
    _write(
        tmp_migrations_dir,
        "20260101T000001_phone",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_phone", depends_on=["20260101T000000_users"])
def upgrade():
    op.add_column("users", "phone", t.Text)
    op.add_column("users", "email", t.Text)  # already exists, must skip
''',
    )

    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert {"id", "email", "phone"} <= cols
