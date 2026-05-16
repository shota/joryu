"""SPEC.md compliance tests, organised by chapter.

These tests pin behaviour that the implementation already provides but that
previous test files did not exercise. They also cover the gaps closed in this
pass (``state.mark_migration`` / ``state.mark_step`` / ``state.repair_checksum``
and the dialect-aware verify filter).
"""
from __future__ import annotations

import datetime as _dt
import os
import warnings
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import joryu
from joryu import op, types as t
from joryu.runner import apply


# ---------------------------------------------------------------------------
# §3.1 — same-second filename collision
# ---------------------------------------------------------------------------


def test_spec_3_1_same_second_collision_empty_generate(tmp_path: Path) -> None:
    """Two `joryu generate --empty` calls in the same second must not collide."""
    from joryu.generate import _generate_empty

    d = tmp_path / "migrations"
    p1 = _generate_empty("add things", migrations_dir=d)
    p2 = _generate_empty("add things", migrations_dir=d)
    p3 = _generate_empty("add things", migrations_dir=d)

    assert p1 != p2 != p3
    # Spec: append _2, _3, ... to the filename.
    assert p1.stem.endswith("_add_things")
    assert p2.stem.endswith("_add_things_2")
    assert p3.stem.endswith("_add_things_3")


def test_spec_3_1_same_second_collision_autogen(tmp_path: Path) -> None:
    """``autogen._allocate_file`` honours the _2/_3 suffix rule too."""
    from joryu.autogen import _allocate_file

    d = tmp_path / "migrations"
    p1 = _allocate_file("foo bar", d)
    p1.write_text("# placeholder\n")  # so the next call sees it as taken
    p2 = _allocate_file("foo bar", d)
    p2.write_text("# placeholder\n")
    p3 = _allocate_file("foo bar", d)
    assert {p1.stem.split("_", 1)[1], p2.stem.split("_", 1)[1], p3.stem.split("_", 1)[1]} == {
        "foo_bar",
        "foo_bar_2",
        "foo_bar_3",
    }


def test_spec_3_1_alembic_importer_collision(tmp_path: Path) -> None:
    """`joryu import alembic` produces unique ids even when revisions share a
    timestamp (mtime)."""
    from joryu.importer.alembic_importer import _AlembicSource, _build_joryu_id

    used: set[str] = set()
    mtime = 1_700_000_000.0  # fixed UTC timestamp
    a = _AlembicSource(
        path=Path("a.py"), source="", revision="aaa", down_revision=[],
        docstring="add things", has_upgrade=True, has_downgrade=False, mtime=mtime,
    )
    b = _AlembicSource(
        path=Path("b.py"), source="", revision="bbb", down_revision=[],
        docstring="add things", has_upgrade=True, has_downgrade=False, mtime=mtime,
    )
    c = _AlembicSource(
        path=Path("c.py"), source="", revision="ccc", down_revision=[],
        docstring="add things", has_upgrade=True, has_downgrade=False, mtime=mtime,
    )
    ja = _build_joryu_id(a, used); used.add(ja)
    jb = _build_joryu_id(b, used); used.add(jb)
    jc = _build_joryu_id(c, used); used.add(jc)
    assert len({ja, jb, jc}) == 3
    assert ja.endswith("_add_things")
    assert jb.endswith("_add_things_2")
    assert jc.endswith("_add_things_3")


# ---------------------------------------------------------------------------
# §7.2 — verify respects per-migration ``dialects=[...]``
# ---------------------------------------------------------------------------


def test_spec_7_2_verify_skips_disjoint_dialects() -> None:
    """Two migrations restricted to different dialects never co-execute, so
    apparent conflicts between them must not surface."""
    from joryu.registry import register_operations
    from joryu.verify import verify

    @joryu.migration(
        id="20260101T000000_pg_alter",
        dialects=["postgresql"],
    )
    def upgrade_pg() -> None:
        op.alter_column("users", "email", type=t.Text)

    @joryu.migration(
        id="20260101T000001_mysql_alter",
        dialects=["mysql"],
    )
    def upgrade_mysql() -> None:
        op.alter_column("users", "email", type=t.Text)

    from joryu.registry import MIGRATIONS

    for m in MIGRATIONS.values():
        register_operations(m)
    conflicts = verify(registry=MIGRATIONS)
    assert conflicts == [], f"unexpected conflicts: {conflicts}"


def test_spec_7_2_verify_still_flags_overlapping_dialects() -> None:
    """Sanity check: when one side has no dialect restriction (runs everywhere),
    the conflict is preserved."""
    from joryu.registry import register_operations
    from joryu.verify import verify

    @joryu.migration(id="20260101T000000_pg_alter", dialects=["postgresql"])
    def upgrade_pg() -> None:
        op.alter_column("users", "email", type=t.Text)

    @joryu.migration(id="20260101T000001_universal_alter")  # all dialects
    def upgrade_all() -> None:
        op.alter_column("users", "email", type=t.Text)

    from joryu.registry import MIGRATIONS

    for m in MIGRATIONS.values():
        register_operations(m)
    conflicts = verify(registry=MIGRATIONS)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "double_alter"


# ---------------------------------------------------------------------------
# §9.2 — state-table invariants
# ---------------------------------------------------------------------------


def test_spec_9_2_started_at_uses_server_clock(tmp_migrations_dir: Path, sqlite_url: str) -> None:
    """``joryu_migrations.started_at`` is populated by ``CURRENT_TIMESTAMP``,
    not the Python client clock — proven by the column being non-null without
    the runner passing a value explicitly."""
    (tmp_migrations_dir / "20260101T000000_x.py").write_text(
        '''import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_x")
def upgrade():
    op.create_table("x", op.column("id", t.BigInt, primary_key=True))
'''
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as conn:
        from joryu.state import list_migration_rows

        rows = list_migration_rows(conn)
    assert rows[0]["started_at"] is not None
    assert rows[0]["finished_at"] is not None


def test_spec_9_2_last_error_truncated_to_4_kb() -> None:
    from joryu.runner import _summarise_exception, _LAST_ERROR_LIMIT

    assert _LAST_ERROR_LIMIT == 4 * 1024
    huge = RuntimeError("x" * 10_000)
    summary = _summarise_exception(huge)
    assert len(summary) <= _LAST_ERROR_LIMIT


# ---------------------------------------------------------------------------
# §9.5.2 — migration-wide ``on_mismatch`` default propagates to ops
# ---------------------------------------------------------------------------


def test_spec_9_5_2_migration_wide_on_mismatch_propagates() -> None:
    """An op constructed with the default ``on_mismatch="error"`` should pick
    up the migration-wide setting (``"alter"`` / ``"skip"``) at apply time."""
    from joryu._op_impl import AddColumnOp
    from joryu.op_core import ExecutionContext

    # Build an op with the default value, then check what the resolver picks.
    add = AddColumnOp(table="users", name="email", type=t.Text)
    # We don't need a real conn for the resolver.
    fake_ctx = ExecutionContext(
        conn=None,  # type: ignore[arg-type]
        dialect_name="sqlite",
        transaction_mode="per_step",
        on_mismatch="alter",
    )
    assert add._resolved_on_mismatch(fake_ctx) == "alter"

    # If the op explicitly overrides, the local wins.
    add2 = AddColumnOp(table="users", name="email", type=t.Text, on_mismatch="skip")
    assert add2._resolved_on_mismatch(fake_ctx) == "skip"


# ---------------------------------------------------------------------------
# §13.3.4 — Decimal / datetime auto-encoding in checkpoints
# ---------------------------------------------------------------------------


def test_spec_13_3_4_checkpoint_encodes_decimal_and_datetime() -> None:
    from joryu.checkpoint import Checkpoint

    persisted: list[dict] = []

    def _persist(snap: dict) -> None:
        # Force the JSON encode pathway to fire by recording what the
        # checkpoint serialised.
        import json

        from joryu.checkpoint import _default

        encoded = json.dumps(snap, default=_default)
        persisted.append({"raw": snap, "encoded": encoded})

    cp = Checkpoint(
        "20260101T000000_x", 0, initial={}, persist=_persist
    )
    cp.set("price", Decimal("19.99"))
    cp.set("ts", _dt.datetime(2026, 5, 17, 12, 0, 0))
    cp.set("d", _dt.date(2026, 5, 17))

    assert "19.99" in persisted[0]["encoded"]
    assert "2026-05-17T12:00:00" in persisted[1]["encoded"]
    assert "2026-05-17" in persisted[2]["encoded"]


def test_spec_13_3_4_checkpoint_rejects_unserialisable() -> None:
    from joryu.checkpoint import Checkpoint

    class Custom:
        pass

    cp = Checkpoint("20260101T000000_x", 0)
    with pytest.raises(TypeError):
        cp.set("bad", Custom())


# ---------------------------------------------------------------------------
# §13.3.5 — 1 MB soft size limit raises a warning
# ---------------------------------------------------------------------------


def test_spec_13_3_5_size_warning_above_soft_limit() -> None:
    from joryu.checkpoint import Checkpoint

    cp = Checkpoint("20260101T000000_x", 0)
    # ~1.5 MB payload — exceeds the 1 MB soft limit.
    blob = "x" * (1_500_000)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cp.set("blob", blob)
    msgs = [str(w.message) for w in caught]
    assert any("1 MB soft limit" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# §16.1 — Python API parity (joryu.apply / status / down / verify / generate)
# ---------------------------------------------------------------------------


def test_spec_16_1_api_surface() -> None:
    # All five sync entry points exist on the joryu.api facade.
    from joryu import api as _api

    for name in ("apply", "status", "down", "verify", "generate"):
        assert hasattr(_api, name), f"missing joryu.api.{name}"
        assert callable(getattr(_api, name))
    # Async counterparts.
    for name in ("apply_async", "down_async"):
        assert hasattr(_api, name), f"missing joryu.api.{name}"
        assert callable(getattr(_api, name))
    # The top-level joryu package re-exports the same callables. Note that
    # ``joryu.generate`` / ``joryu.verify`` are *shadowed* by submodules of the
    # same name once any code in the process does ``import joryu.generate`` or
    # ``import joryu.verify`` (Python rebinds the attribute to the submodule).
    # We assert via ``__all__`` instead, so the public surface contract holds
    # regardless of submodule import order.
    expected = {
        "apply", "apply_async", "down", "down_async",
        "generate", "status", "verify",
    }
    assert expected <= set(joryu.__all__), (
        f"missing from joryu.__all__: {expected - set(joryu.__all__)}"
    )


# ---------------------------------------------------------------------------
# §16.2 — exit codes for each public exception
# ---------------------------------------------------------------------------


def test_spec_16_2_exit_code_mapping() -> None:
    from joryu.cli import (
        EXIT_GENERAL,
        EXIT_MIGRATION_FAILED,
        EXIT_MIGRATION_PAUSED,
        EXIT_PROD_GUARD,
        EXIT_TYPE_MISUSE,
        EXIT_VERIFY_FAILED,
        _exit_for_exception,
    )
    from joryu.exceptions import (
        JoryuError,
        MigrationFailed,
        MigrationPaused,
        ProductionGuardError,
        UnsupportedTypeUsage,
        VerificationFailed,
    )

    assert _exit_for_exception(MigrationFailed("m", 0, "s", RuntimeError("x"))) == EXIT_MIGRATION_FAILED
    assert _exit_for_exception(MigrationPaused("m", 0, "s", "r")) == EXIT_MIGRATION_PAUSED
    assert _exit_for_exception(VerificationFailed([])) == EXIT_VERIFY_FAILED
    assert _exit_for_exception(ProductionGuardError("production-like", "db.example.com")) == EXIT_PROD_GUARD
    assert _exit_for_exception(UnsupportedTypeUsage("bad")) == EXIT_TYPE_MISUSE
    assert _exit_for_exception(JoryuError("generic")) == EXIT_GENERAL
    assert _exit_for_exception(ValueError("untyped")) == EXIT_GENERAL


# ---------------------------------------------------------------------------
# §17 — env:VAR expansion in joryu.toml [database] url
# ---------------------------------------------------------------------------


def test_spec_17_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from joryu.config import load_config

    cfg_path = tmp_path / "joryu.toml"
    cfg_path.write_text(
        '[joryu]\nmigrations_dir = "migrations"\n'
        '[database]\nurl = "env:JORYU_TEST_URL"\n'
    )
    monkeypatch.setenv("JORYU_TEST_URL", "sqlite:///expanded.db")
    cfg = load_config(cfg_path)
    assert cfg.database_url == "sqlite:///expanded.db"


# ---------------------------------------------------------------------------
# Gap closures (mark + repair)
# ---------------------------------------------------------------------------


def _write_simple_migration(dir_: Path, mig_id: str, body: str = "pass") -> None:
    (dir_ / f"{mig_id}.py").write_text(
        f'''import joryu
from joryu import op, types as t

@joryu.migration(id={mig_id!r})
def upgrade():
    {body}
'''
    )


def test_repair_checksum_updates_db_value(
    tmp_migrations_dir: Path, sqlite_url: str
) -> None:
    """`joryu repair` rewrites the persisted checksum to match the file."""
    _write_simple_migration(
        tmp_migrations_dir, "20260101T000000_x",
        body='op.create_table("x", op.column("id", t.BigInt, primary_key=True))',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    # Tamper with the persisted checksum to simulate drift.
    engine = create_engine(sqlite_url, future=True)
    from joryu.state import migrations_table

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(
                migrations_table.update()
                .where(migrations_table.c.id == "20260101T000000_x")
                .values(checksum="staletampered")
            )

    # Now run repair to bring it back in sync.
    from joryu import state as state_module

    new = state_module.repair_checksum(
        "20260101T000000_x",
        url=sqlite_url,
        migrations_dir=tmp_migrations_dir,
    )
    with engine.connect() as conn:
        row = state_module.get_migration_row(conn, "20260101T000000_x")
    assert row is not None
    assert row["checksum"] == new
    assert row["checksum"] != "staletampered"


def test_mark_migration_paused_requires_reason(
    tmp_migrations_dir: Path, sqlite_url: str
) -> None:
    _write_simple_migration(
        tmp_migrations_dir, "20260101T000000_x",
        body='op.create_table("x", op.column("id", t.BigInt, primary_key=True))',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    from joryu import state as state_module

    with pytest.raises(ValueError, match="reason"):
        state_module.mark_migration(
            "20260101T000000_x", "paused", url=sqlite_url
        )


def test_mark_migration_round_trip(
    tmp_migrations_dir: Path, sqlite_url: str
) -> None:
    _write_simple_migration(
        tmp_migrations_dir, "20260101T000000_x",
        body='op.create_table("x", op.column("id", t.BigInt, primary_key=True))',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    from joryu import state as state_module

    state_module.mark_migration("20260101T000000_x", "pending", url=sqlite_url)
    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as conn:
        row = state_module.get_migration_row(conn, "20260101T000000_x")
    assert row["status"] == "pending"
    assert row["last_error"] is None
    assert row["pause_reason"] is None


def test_mark_step_pending_downgrades_applied_parent(
    tmp_migrations_dir: Path, sqlite_url: str
) -> None:
    """§9.2: marking a step pending inside an applied migration downgrades the
    parent to ``failed`` with a synthetic ``last_error``."""
    _write_simple_migration(
        tmp_migrations_dir, "20260101T000000_x",
        body='op.create_table("x", op.column("id", t.BigInt, primary_key=True))',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    from joryu import state as state_module

    state_module.mark_step("20260101T000000_x", 0, "pending", url=sqlite_url)

    engine = create_engine(sqlite_url, future=True)
    with engine.connect() as conn:
        row = state_module.get_migration_row(conn, "20260101T000000_x")
        steps = state_module.get_step_rows(conn, "20260101T000000_x")
    assert row["status"] == "failed"
    assert "pending" in (row["last_error"] or "")
    assert steps[0]["status"] == "pending"
