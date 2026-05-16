"""Tests for the richer autogen diff (§8) — alter_column, FKs, indexes,
warnings, and backfill placeholders."""
from __future__ import annotations

import sqlalchemy as sa

import joryu
from joryu import op, types as t
from joryu.autogen import (
    OperationSpec,
    diff_schemas,
    render_migration,
)
from joryu.registry import MIGRATIONS
from joryu.virtual_schema import replay_migrations


def test_diff_emits_create_foreign_key():
    @joryu.migration(id="001")
    def up():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))
        op.create_table(
            "posts",
            op.column("id", t.BigInt, primary_key=True),
            op.column("author_id", t.BigInt),
        )

    md = sa.MetaData()
    sa.Table(
        "users", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
    )
    sa.Table(
        "posts", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("author_id", sa.BigInteger),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name="fk_posts_author"),
    )
    current = replay_migrations(MIGRATIONS.values())
    ops = diff_schemas(current, md)
    fk_ops = [o for o in ops if o.kind == "create_foreign_key"]
    assert fk_ops, f"expected a create_foreign_key op, got {[o.kind for o in ops]}"
    spec = fk_ops[0]
    assert spec.args[0] == "fk_posts_author"
    assert spec.args[1] == "posts"
    assert spec.args[2] == "users"


def test_diff_emits_alter_column_on_nullability_change():
    @joryu.migration(id="001")
    def up():
        op.create_table(
            "users",
            op.column("id", t.BigInt, primary_key=True),
            op.column("name", t.Text, nullable=True),
        )

    md = sa.MetaData()
    sa.Table(
        "users", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
    )
    current = replay_migrations(MIGRATIONS.values())
    ops = diff_schemas(current, md)
    name_alters = [
        o for o in ops if o.kind == "alter_column" and o.args[1] == "name"
    ]
    assert name_alters, f"expected alter_column for name, got {[(o.kind, o.args) for o in ops]}"
    spec = name_alters[0]
    assert spec.args == ["users", "name"]
    assert spec.kwargs.get("nullable") is False
    # Warning should fire for NOT NULL tightening without default.
    assert spec.warning is not None and "NOT NULL" in spec.warning


def test_render_migration_warns_on_dangerous_drops():
    ops = [
        OperationSpec(
            "drop_table", args=["users"],
            warning="dropping table 'users' loses its data — review carefully",
        ),
        OperationSpec(
            "drop_column", args=["posts", "body"],
            warning="dropping column posts.body loses its data",
        ),
    ]
    body = render_migration("rm", ops)
    assert "# WARNING: dropping table" in body
    assert "# WARNING: dropping column" in body


def test_render_migration_emits_backfill_placeholder_for_notnull():
    ops = [
        OperationSpec(
            "add_column",
            args=["users", "name", "t.Text"],
            kwargs={"nullable": False},
            warning="adding NOT NULL without a default may fail on populated tables",
        ),
    ]
    body = render_migration("add_notnull", ops)
    assert "def backfill_users_name(" in body
    assert "op.run_python(backfill_users_name)" in body
    assert "op.declare_schema_change(column_altered=" in body
    assert "# WARNING:" in body


def test_render_migration_no_backfill_when_default_present():
    ops = [
        OperationSpec(
            "add_column",
            args=["users", "name", "t.Text"],
            kwargs={"nullable": False, "server_default": "''"},
        ),
    ]
    body = render_migration("with_default", ops)
    assert "def backfill_" not in body


def test_diff_emits_dropped_fk_when_target_drops_it():
    @joryu.migration(id="001")
    def up():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))
        op.create_table(
            "posts",
            op.column("id", t.BigInt, primary_key=True),
            op.column("author_id", t.BigInt),
        )
        op.create_foreign_key("fk_p_u", "posts", "users", ["author_id"], ["id"])

    md = sa.MetaData()
    sa.Table("users", md, sa.Column("id", sa.BigInteger, primary_key=True))
    sa.Table(
        "posts", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("author_id", sa.BigInteger),
    )
    current = replay_migrations(MIGRATIONS.values())
    ops = diff_schemas(current, md)
    drops = [o for o in ops if o.kind == "drop_constraint" and o.args[0] == "fk_p_u"]
    assert drops, f"expected drop_constraint for fk_p_u, got {[(o.kind, o.args) for o in ops]}"
