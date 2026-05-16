"""Tests for the Alembic importer (§19)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from joryu.importer import ImportReport, import_alembic


def _write_alembic_file(versions_dir: Path, name: str, body: str) -> Path:
    path = versions_dir / name
    path.write_text(body)
    return path


@pytest.fixture
def alembic_layout(tmp_path: Path) -> Path:
    alembic = tmp_path / "alembic"
    versions = alembic / "versions"
    versions.mkdir(parents=True)
    return alembic


def test_phase1_minimal_conversion(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_add_users.py",
        '''"""Add users table.

Revision ID: abc123
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "abc123"
down_revision = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
    )


def downgrade():
    op.drop_table("users")
''',
    )
    out = tmp_path / "migrations"
    report = import_alembic(alembic_dir=alembic_layout, output_dir=out)
    assert isinstance(report, ImportReport)
    assert report.files_converted == 1
    assert report.files_skipped == 0

    [generated] = list(out.glob("*.py"))
    text_src = generated.read_text()
    assert "@joryu.migration(" in text_src
    assert "tags=['alembic:abc123']" in text_src or 'tags=["alembic:abc123"]' in text_src
    assert "@joryu.downgrade" in text_src
    # The id should be a 15-char ISO basic timestamp + underscore + slug.
    stem = generated.stem
    assert stem[8] == "T", stem
    assert "_" in stem[15:]
    # File must be syntactically valid Python.
    ast.parse(text_src)


def test_rewrite_add_column_sa_column(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_add_phone.py",
        '''"""Add phone column."""
from alembic import op
import sqlalchemy as sa

revision = "abc123"
down_revision = None


def upgrade():
    op.add_column("users", sa.Column("phone", sa.String(32), nullable=True))


def downgrade():
    op.drop_column("users", "phone")
''',
    )
    out = tmp_path / "migrations"
    import_alembic(alembic_dir=alembic_layout, output_dir=out)
    [generated] = list(out.glob("*.py"))
    src = generated.read_text()
    # The rewritten form should call op.add_column with a positional name.
    assert 'op.add_column("users", \'phone\', t.String(32)' in src or \
           "op.add_column('users', 'phone', t.String(32)" in src
    assert "sa.Column" not in src.split("def upgrade")[1].split("def downgrade")[0]


def test_rewrite_execute_text(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_seed.py",
        '''"""Seed data."""
from alembic import op
from sqlalchemy import text

revision = "abc123"
down_revision = None


def upgrade():
    op.execute(text("UPDATE users SET status='active' WHERE status IS NULL"))


def downgrade():
    pass
''',
    )
    out = tmp_path / "migrations"
    import_alembic(alembic_dir=alembic_layout, output_dir=out)
    [generated] = list(out.glob("*.py"))
    src = generated.read_text()
    assert "op.execute(\"UPDATE" in src
    assert "text(" not in src.split("def upgrade")[1].split("def downgrade")[0]


def test_depends_on_resolved_from_down_revision(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "aaa111_one.py",
        '''"""First migration."""
from alembic import op

revision = "aaa111"
down_revision = None

def upgrade():
    pass

def downgrade():
    pass
''',
    )
    _write_alembic_file(
        versions,
        "bbb222_two.py",
        '''"""Second migration."""
from alembic import op

revision = "bbb222"
down_revision = "aaa111"

def upgrade():
    pass

def downgrade():
    pass
''',
    )
    out = tmp_path / "migrations"
    import_alembic(alembic_dir=alembic_layout, output_dir=out)
    files = sorted(out.glob("*.py"))
    assert len(files) == 2
    # The second file should reference the first migration's joryu id.
    second = next(p for p in files if "second" in p.stem)
    first = next(p for p in files if "first" in p.stem)
    second_src = second.read_text()
    assert first.stem in second_src


def test_batch_alter_rewrite(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_batch.py",
        '''"""Batch alter."""
from alembic import op
import sqlalchemy as sa

revision = "abc123"
down_revision = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("nickname", sa.String(64)))


def downgrade():
    pass
''',
    )
    out = tmp_path / "migrations"
    import_alembic(alembic_dir=alembic_layout, output_dir=out)
    [generated] = list(out.glob("*.py"))
    src = generated.read_text()
    assert "with op.batch(" in src
    assert "batch_alter_table" not in src


def test_bulk_insert_stubbed(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_bulk.py",
        '''"""Bulk insert."""
from alembic import op

revision = "abc123"
down_revision = None


def upgrade():
    op.bulk_insert(some_table, [{"x": 1}])


def downgrade():
    pass
''',
    )
    out = tmp_path / "migrations"
    report = import_alembic(alembic_dir=alembic_layout, output_dir=out)
    [generated] = list(out.glob("*.py"))
    src = generated.read_text()
    assert "JORYU-IMPORT-TODO" in src
    assert "op.run_python" in src
    # Report should track the TODO.
    assert any("bulk_insert" in msg for _, msg in report.todos)


def test_dialect_branch_annotated(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_dialect.py",
        '''"""Dialect branch."""
from alembic import op

revision = "abc123"
down_revision = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade():
    pass
''',
    )
    out = tmp_path / "migrations"
    report = import_alembic(alembic_dir=alembic_layout, output_dir=out)
    [generated] = list(out.glob("*.py"))
    src = generated.read_text()
    assert "JORYU-IMPORT-TODO: consider rewriting as op.execute" in src
    # Original branch is preserved.
    assert "if op.get_bind().dialect.name" in src
    assert any("dialect-branching" in msg for _, msg in report.todos)


def test_state_handover(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_add_users.py",
        '''"""Add users."""
from alembic import op

revision = "abc123"
down_revision = None

def upgrade():
    pass

def downgrade():
    pass
''',
    )
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    engine = create_engine(db_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('abc123')"))
    engine.dispose()

    out = tmp_path / "migrations"
    report = import_alembic(
        alembic_dir=alembic_layout,
        output_dir=out,
        migrate_state=True,
        url=db_url,
    )
    assert report.state_migrated is True

    engine = create_engine(db_url, future=True)
    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT id, status FROM joryu_migrations")))
    engine.dispose()
    assert len(rows) == 1
    assert rows[0][1] == "applied"


def test_state_drop_alembic_table(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "abc123_x.py",
        '''"""x."""
from alembic import op
revision = "abc123"
down_revision = None
def upgrade(): pass
def downgrade(): pass
''',
    )
    db_url = f"sqlite:///{tmp_path / 'state.db'}"
    engine = create_engine(db_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('abc123')"))
    engine.dispose()

    out = tmp_path / "migrations"
    import_alembic(
        alembic_dir=alembic_layout,
        output_dir=out,
        migrate_state=True,
        drop_alembic_table=True,
        url=db_url,
    )
    engine = create_engine(db_url, future=True)
    insp = inspect(engine)
    assert not insp.has_table("alembic_version")
    engine.dispose()


def test_unconvertible_file_is_skipped(alembic_layout: Path, tmp_path: Path):
    versions = alembic_layout / "versions"
    _write_alembic_file(
        versions,
        "broken.py",
        '''"""no revision, no upgrade — should be skipped."""\n''',
    )
    out = tmp_path / "migrations"
    report = import_alembic(alembic_dir=alembic_layout, output_dir=out)
    assert report.files_converted == 0
    assert report.files_skipped == 1


def test_filename_collision_suffix(alembic_layout: Path, tmp_path: Path, monkeypatch):
    """Two files with the same mtime + slug get _2 / _3 suffixes."""
    import os

    versions = alembic_layout / "versions"
    p1 = _write_alembic_file(
        versions,
        "aaa_first.py",
        '''"""same slug."""
revision = "aaa"
down_revision = None
def upgrade(): pass
def downgrade(): pass
''',
    )
    p2 = _write_alembic_file(
        versions,
        "bbb_other.py",
        '''"""same slug."""
revision = "bbb"
down_revision = None
def upgrade(): pass
def downgrade(): pass
''',
    )
    # Force identical mtimes.
    mtime = 1_700_000_000
    os.utime(p1, (mtime, mtime))
    os.utime(p2, (mtime, mtime))

    out = tmp_path / "migrations"
    import_alembic(alembic_dir=alembic_layout, output_dir=out)
    names = sorted(p.stem for p in out.glob("*.py"))
    assert len(names) == 2
    # Same timestamp + slug for both, but one has the _2 suffix.
    assert names[0].rsplit("_", 1)[0] == names[1].rsplit("_", 1)[0] or \
           names[1].endswith("_2")
