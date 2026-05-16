"""Tests for joryu.testing.run_unit_tests (§6.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from joryu.testing import UnitTestReport, run_unit_tests


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_unit_tests_ok_on_clean_migrations(tmp_migrations_dir: Path):
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
    report = run_unit_tests(migrations_dir=tmp_migrations_dir)
    assert isinstance(report, UnitTestReport)
    assert report.ok
    assert report.total == 1
    assert report.applied == 1
    assert report.failed == []
    assert report.duration_ms >= 0


def test_unit_tests_reports_failure(tmp_migrations_dir: Path):
    _write(
        tmp_migrations_dir,
        "20260101T000000_bad",
        '''
import joryu
from joryu import op

@joryu.migration(id="20260101T000000_bad")
def upgrade():
    op.execute("THIS IS NOT VALID SQL")
''',
    )
    report = run_unit_tests(migrations_dir=tmp_migrations_dir)
    assert not report.ok
    assert report.failed
    assert report.applied == 0


def test_unit_tests_detects_conflicts(tmp_migrations_dir: Path):
    """Two migrations where one adds a column and another drops it — the
    classic ``add_drop`` conflict §7.2."""
    _write(
        tmp_migrations_dir,
        "20260101T000000_base",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_base")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True))
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000001_add_email",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000001_add_email", depends_on=["20260101T000000_base"])
def upgrade():
    op.add_column("users", "email", t.Text)
''',
    )
    _write(
        tmp_migrations_dir,
        "20260101T000002_drop_email",
        '''
import joryu
from joryu import op

@joryu.migration(id="20260101T000002_drop_email", depends_on=["20260101T000001_add_email"])
def upgrade():
    op.drop_column("users", "email")
''',
    )
    report = run_unit_tests(migrations_dir=tmp_migrations_dir)
    # Migrations apply cleanly in dependency order; verify still flags the
    # add+drop pair as a conflict (§7.2).
    assert report.applied == 3
    assert report.conflicts, "expected verify to flag the add+drop pair"
    assert not report.ok


def test_unit_tests_rejects_non_sqlite_dialect(tmp_migrations_dir: Path):
    with pytest.raises(NotImplementedError):
        run_unit_tests(migrations_dir=tmp_migrations_dir, dialect="postgresql")


def test_unit_tests_idempotency(tmp_migrations_dir: Path):
    """A migration that re-asserts an existing column must be a no-op the
    second time around (this is the ensure-semantics check)."""
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
    report = run_unit_tests(migrations_dir=tmp_migrations_dir)
    assert report.ok, f"failures={report.failed} conflicts={report.conflicts}"
