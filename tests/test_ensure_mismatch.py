"""Deep ensure-style mismatch detection for add_column (§9.4 / §9.5)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from joryu.exceptions import MigrationFailed
from joryu.runner import apply


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_add_column_same_type_skips(tmp_migrations_dir: Path, sqlite_url: str):
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True),
                              op.column("email", t.Text))
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000001_reassert",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_reassert",
                 depends_on=["20260101T000000_init"])
def upgrade():
    # Same type as already present -> ensure semantics: skip.
    op.add_column("users", "email", t.Text)
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    engine = create_engine(sqlite_url, future=True)
    cols = {c["name"] for c in inspect(engine).get_columns("users")}
    assert "email" in cols


def test_add_column_type_mismatch_errors_by_default(
    tmp_migrations_dir: Path, sqlite_url: str
):
    # Step 1: create the table with email as TEXT via raw SQL so SQLite's
    # type affinity gives us an unambiguous-but-different reflection.
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

    # Step 2: declare payload as TEXT — mismatch on SQLite (BLOB vs TEXT).
    _write(
        tmp_migrations_dir,
        "20260101T000001_mismatch",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_mismatch",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.add_column("widgets", "payload", t.Text)
''',
    )

    with pytest.raises(MigrationFailed) as exc:
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert "mismatch" in str(exc.value.cause).lower()


def test_add_column_type_mismatch_skip_is_silent(
    tmp_migrations_dir: Path, sqlite_url: str
):
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
        "20260101T000001_skip",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_skip",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.add_column("widgets", "payload", t.Text, on_mismatch="skip")
''',
    )
    # Should not raise.
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    # Type was not silently mutated.
    payload = next(c for c in insp.get_columns("widgets") if c["name"] == "payload")
    assert "BLOB" in str(payload["type"]).upper()


def test_add_column_nullability_mismatch_errors(
    tmp_migrations_dir: Path, sqlite_url: str
):
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
                    op.column("name", t.Text, nullable=True))
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    _write(
        tmp_migrations_dir,
        "20260101T000001_re_null",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_re_null",
                 depends_on=["20260101T000000_init"])
def upgrade():
    # Desired NOT NULL, existing nullable -> mismatch.
    op.add_column("users", "name", t.Text, nullable=False)
''',
    )
    with pytest.raises(MigrationFailed) as exc:
        apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert "nullable" in str(exc.value.cause).lower()
