"""op.alter_column on SQLite auto-routes through a table-rebuild (§A.5)."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from joryu.runner import apply


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_alter_column_nullable_on_sqlite(
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
                    op.column("email", t.Text, nullable=True))
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000001_notnull",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_notnull",
                 depends_on=["20260101T000000_init"])
def upgrade():
    op.alter_column("users", "email", nullable=False)
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    insp = inspect(engine)
    email = next(c for c in insp.get_columns("users") if c["name"] == "email")
    assert email["nullable"] is False
