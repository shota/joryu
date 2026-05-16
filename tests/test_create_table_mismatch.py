"""create_table deep ensure-semantics mismatch detection (§9.4).

Per the §9.4 table: ``create_table`` skips if the table is already in the
desired state, but ERRORs if it exists with a *different* column set (type or
nullability mismatch, or a desired column missing). Extra columns added by
later migrations are tolerated — the ensure-style check is asymmetric: every
desired column must be present and correct; the DB may carry more.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from joryu.exceptions import MigrationFailed
from joryu.runner import apply


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_matching_schema_skips(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text, nullable=False))
''',
    )
    # Second migration re-asserts an identical create_table — must skip.
    _write(
        tmp_migrations_dir,
        "20260101T000001_reassert",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_reassert",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text, nullable=False))
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert cols == {"id", "email"}


def test_missing_column_errors(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True))
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    # Now re-declare create_table with a column the DB does not have.
    _write(
        tmp_migrations_dir,
        "20260101T000001_extracol",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_extracol",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text))
''',
    )
    with pytest.raises(MigrationFailed) as exc:
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    msg = str(exc.value.cause).lower()
    assert "missing" in msg
    assert "email" in msg


def test_type_mismatch_errors(tmp_migrations_dir: Path, sqlite_url: str):
    # Create users with payload BLOB via raw SQL so the reflection
    # diverges from a Text-declared create_table replay.
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("widgets", op.column("id", t.BigInt, primary_key=True))
    op.execute("ALTER TABLE widgets ADD COLUMN payload BLOB")
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    _write(
        tmp_migrations_dir,
        "20260101T000001_mismatch",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_mismatch",
                 depends_on=["20260101T000000_init"])
def upgrade():
    # Desired payload as Text, existing as BLOB -> type mismatch.
    op.create_table("widgets",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("payload", t.Text))
''',
    )
    with pytest.raises(MigrationFailed) as exc:
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    msg = str(exc.value.cause).lower()
    assert "match" in msg or "mismatch" in msg or "type" in msg


def test_extra_db_columns_tolerated(tmp_migrations_dir: Path, sqlite_url: str):
    # First migration creates users(id, email). Second migration drifts the DB
    # by adding an extra column via raw SQL. Third migration re-asserts
    # create_table users(id, email) — must skip silently, the extra column
    # is not the create_table op's concern.
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text))
    op.execute("ALTER TABLE users ADD COLUMN bonus TEXT")
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    _write(
        tmp_migrations_dir,
        "20260101T000001_reassert",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_reassert",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text))
''',
    )
    # Must not raise: bonus is extra on DB, every desired column is correct.
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert {"id", "email", "bonus"} <= cols
