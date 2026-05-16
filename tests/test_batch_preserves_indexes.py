"""Batch rebuild on SQLite preserves user-defined indexes (§4.4 / A.5)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from joryu.runner import apply


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_batch_preserves_existing_index(tmp_migrations_dir: Path, sqlite_url: str):
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
    op.create_index("idx_users_email", "users", ["email"])
''',
    )
    # Batch op adds a column; the rebuild must keep idx_users_email.
    _write(
        tmp_migrations_dir,
        "20260101T000001_add_phone",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_add_phone",
                 depends_on=["20260101T000000_init"])
def upgrade():
    with op.batch("users") as batch:
        batch.add_column("phone", t.Text)
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert {"id", "email", "phone"} <= cols
    ix_names = {ix["name"] for ix in insp.get_indexes("users")}
    assert "idx_users_email" in ix_names


def test_batch_drops_index_for_dropped_column(
    tmp_migrations_dir: Path, sqlite_url: str
):
    """If the batch drops the indexed column, the rebuild must not re-create
    an index on a now-missing column."""
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("widgets",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("legacy", t.Text))
    op.create_index("idx_widgets_legacy", "widgets", ["legacy"])
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000001_drop_legacy",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_drop_legacy",
                 depends_on=["20260101T000000_init"])
def upgrade():
    with op.batch("widgets") as batch:
        batch.drop_column("legacy")
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("widgets")}
    assert "legacy" not in cols
    ix_names = {ix["name"] for ix in insp.get_indexes("widgets")}
    assert "idx_widgets_legacy" not in ix_names


def test_batch_preserves_foreign_key(tmp_migrations_dir: Path, sqlite_url: str):
    """Foreign keys live inline in CREATE TABLE on SQLite; the rebuild must
    carry them across."""
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("authors",
                    op.column("id", t.BigInt, primary_key=True))
    # SQLite needs the FK declared inline at CREATE TABLE; emit raw SQL.
    op.execute(
        "CREATE TABLE books ("
        "  id INTEGER PRIMARY KEY,"
        "  author_id INTEGER,"
        "  CONSTRAINT fk_books_author FOREIGN KEY (author_id) REFERENCES authors(id)"
        ")"
    )
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000001_add_title",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_add_title",
                 depends_on=["20260101T000000_init"])
def upgrade():
    with op.batch("books") as batch:
        batch.add_column("title", t.Text)
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("books")}
    assert {"id", "author_id", "title"} <= cols
    fks = insp.get_foreign_keys("books")
    assert any(
        fk.get("referred_table") == "authors"
        and "author_id" in (fk.get("constrained_columns") or [])
        for fk in fks
    )
