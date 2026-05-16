"""apply_async dispatches async step bodies on the caller's event loop (§13.2.2 / §16.1)."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from joryu.runner import apply_async


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_apply_async_runs_sync_and_async_steps(
    tmp_migrations_dir: Path, sqlite_url: str
):
    _write(
        tmp_migrations_dir,
        "20260101T000000_steps",
        '''
import joryu
from joryu import op, types as t

ORDER = []

@joryu.migration(id="20260101T000000_steps")
def upgrade():
    op.create_table("users", op.column("id", t.BigInt, primary_key=True))

    @op.step(name="sync_step")
    def s1(conn, dialect, checkpoint):
        ORDER.append("sync")
        return True

    @op.step(name="async_step")
    async def s2(conn, dialect, checkpoint):
        import anyio as _a
        await _a.sleep(0)
        ORDER.append("async")
        return True
''',
    )

    async def driver():
        await apply_async(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    anyio.run(driver)

    # Both steps executed; the decorator-built ORDER list lives on the
    # loaded migration module (the conftest's _purge_migration_modules
    # fixture clears these in teardown, so we check before then by
    # reading from sys.modules at test-time).
    import sys
    mod = sys.modules.get("joryu_migrations.20260101T000000_steps")
    assert mod is not None, "migration module should still be loaded"
    assert mod.ORDER == ["sync", "async"], (
        f"expected sync then async, got {mod.ORDER!r}"
    )


def test_apply_async_completes_without_thread_loop_errors(
    tmp_migrations_dir: Path, sqlite_url: str
):
    """Regression: calling apply_async from an existing event loop must not
    raise ``RuntimeError: nested event loops`` when an async step body runs
    via the dispatch helper."""

    _write(
        tmp_migrations_dir,
        "20260101T000000_async_only",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_async_only")
def upgrade():
    op.create_table("things", op.column("id", t.BigInt, primary_key=True))

    @op.step
    async def s(conn, dialect, checkpoint):
        return True
''',
    )

    async def driver():
        await apply_async(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    # Must complete cleanly.
    anyio.run(driver)
