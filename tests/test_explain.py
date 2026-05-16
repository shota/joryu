"""Tests for joryu.explain (§16)."""
from __future__ import annotations

from pathlib import Path

import joryu
from joryu import op, types as t
from joryu.explain import explain
from joryu.registry import register_operations


def test_explain_create_table_and_add_column():
    @joryu.migration(id="m1")
    def upgrade():
        """Adds users table."""
        op.create_table(
            "users",
            op.column("id", t.BigInt, primary_key=True),
            op.column("email", t.Text, nullable=False),
        )
        op.add_column("users", "phone", t.Text)
        op.create_index("idx_email", "users", ["email"], unique=True)

    out = explain("m1", migrations_dir=Path("/nonexistent"))
    assert out.startswith("Migration m1: Adds users table.")
    assert "Creates table `users`" in out
    assert "Adds a nullable column `users.phone`" in out
    assert "Creates a unique index `idx_email`" in out


def test_explain_unknown_id_raises():
    import pytest
    with pytest.raises(KeyError):
        explain("does-not-exist", migrations_dir=Path("/nonexistent"))


def test_explain_includes_depends_on_and_dialects():
    @joryu.migration(id="dep1")
    def upgrade1():
        op.create_table("a", op.column("id", t.BigInt, primary_key=True))

    @joryu.migration(
        id="m2",
        depends_on=["dep1"],
        transaction_mode="per_migration",
        dialects=["postgresql"],
    )
    def upgrade2():
        """Adds posts."""
        op.create_table("posts", op.column("id", t.BigInt, primary_key=True))

    out = explain("m2", migrations_dir=Path("/nonexistent"))
    assert "depends on dep1" in out
    assert "transaction_mode=per_migration" in out
    assert "dialects=['postgresql']" in out


def test_explain_handles_execute_run_python_and_step():
    def backfill(conn, dialect, checkpoint):
        pass

    def cache_warm(conn, dialect, checkpoint):
        """Warm the cache."""
        pass

    @joryu.migration(id="m3")
    def upgrade():
        op.execute("SELECT 1")
        op.run_python(backfill)
        op.step(cache_warm, name="cache_warm")

    out = explain("m3", migrations_dir=Path("/nonexistent"))
    assert "Executes raw SQL" in out
    assert "Runs a Python data-migration callable `backfill`" in out
    assert "Runs custom step `cache_warm`" in out


def test_explain_alter_and_fk_and_drops():
    @joryu.migration(id="m4")
    def upgrade():
        op.alter_column("users", "name", nullable=False)
        op.create_foreign_key("fk_p_u", "posts", "users", ["author_id"], ["id"])
        op.drop_index("idx_x", "users")
        op.drop_constraint("c_x", "users")

    out = explain("m4", migrations_dir=Path("/nonexistent"))
    assert "Alters column `users.name`" in out
    assert "Adds a foreign key `fk_p_u`" in out
    assert "Drops index `idx_x`" in out
    assert "Drops constraint `c_x`" in out
