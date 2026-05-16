"""Tests for joryu.virtual_schema (§12)."""
from __future__ import annotations

import joryu
from joryu import op, types as t
from joryu.registry import MIGRATIONS, register_operations
from joryu.virtual_schema import (
    VirtualSchema,
    historical_table,
    replay_migrations,
)


def test_create_and_drop_table_replays():
    @joryu.migration(id="001_users")
    def up():
        op.create_table(
            "users",
            op.column("id", t.BigInt, primary_key=True),
            op.column("email", t.Text, nullable=False),
        )

    @joryu.migration(id="002_drop")
    def up2():
        op.drop_table("users")

    schema = replay_migrations(MIGRATIONS.values())
    assert "users" not in schema.tables


def test_add_and_drop_column():
    @joryu.migration(id="001")
    def up():
        op.create_table("posts", op.column("id", t.BigInt, primary_key=True))
        op.add_column("posts", "title", t.Text)
        op.add_column("posts", "draft", t.Bool)
        op.drop_column("posts", "draft")

    schema = replay_migrations(MIGRATIONS.values())
    posts = schema.tables["posts"]
    assert "id" in posts.columns
    assert "title" in posts.columns
    assert "draft" not in posts.columns


def test_rename_table_and_column():
    @joryu.migration(id="001")
    def up():
        op.create_table("a", op.column("x", t.Int, primary_key=True))
        op.rename_column("a", "x", "y")
        op.rename_table("a", "b")

    schema = replay_migrations(MIGRATIONS.values())
    assert "a" not in schema.tables
    assert "y" in schema.tables["b"].columns


def test_indexes_and_constraints():
    @joryu.migration(id="001")
    def up():
        op.create_table("u", op.column("id", t.Int, primary_key=True),
                        op.column("email", t.Text))
        op.create_index("idx_email", "u", ["email"], unique=True)
        op.create_check_constraint("ck_nonempty", "u", "email <> ''")
        op.create_foreign_key("fk_self", "u", "u", ["id"], ["id"])

    schema = replay_migrations(MIGRATIONS.values())
    u = schema.tables["u"]
    assert "idx_email" in u.indexes and u.indexes["idx_email"].unique
    assert "ck_nonempty" in u.constraints
    assert u.constraints["fk_self"].kind == "fk"


def test_drop_index_and_constraint():
    @joryu.migration(id="001")
    def up():
        op.create_table("u", op.column("id", t.Int, primary_key=True),
                        op.column("name", t.Text))
        op.create_index("idx_name", "u", ["name"])
        op.create_unique_constraint("uq_name", "u", ["name"])
        op.drop_index("idx_name", "u")
        op.drop_constraint("uq_name", "u")

    schema = replay_migrations(MIGRATIONS.values())
    u = schema.tables["u"]
    assert "idx_name" not in u.indexes
    assert "uq_name" not in u.constraints


def test_declare_schema_change_column_added():
    @joryu.migration(id="001")
    def up():
        op.create_table("users", op.column("id", t.Int, primary_key=True))
        op.execute("ALTER TABLE users ADD COLUMN api_key TEXT")
        op.declare_schema_change(
            column_added=("users", "api_key", t.Text, {"nullable": False})
        )

    schema = replay_migrations(MIGRATIONS.values())
    api = schema.tables["users"].columns["api_key"]
    assert api.nullable is False


def test_declare_schema_change_extensions_and_enums():
    @joryu.migration(id="001")
    def up():
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        op.declare_schema_change(extension_added=("pgcrypto",))
        op.declare_schema_change(
            enum_added=("color", ["red", "green", "blue"]),
            enum_value_added=("color", "yellow", {"after": "green"}),
        )

    schema = replay_migrations(MIGRATIONS.values())
    assert "pgcrypto" in schema.extensions
    assert schema.enums["color"] == ["red", "green", "yellow", "blue"]


def test_replay_stops_at_up_to():
    @joryu.migration(id="001")
    def up():
        op.create_table("a", op.column("id", t.Int, primary_key=True))

    @joryu.migration(id="002")
    def up2():
        op.create_table("b", op.column("id", t.Int, primary_key=True))

    schema = replay_migrations(MIGRATIONS.values(), up_to="002")
    assert "a" in schema.tables
    assert "b" not in schema.tables


def test_historical_table_returns_vtable():
    @joryu.migration(id="001")
    def up():
        op.create_table("users", op.column("id", t.Int, primary_key=True))

    register_operations(MIGRATIONS["001"])
    vt = historical_table("users")
    assert vt is not None
    assert "id" in vt.columns


def test_views_and_triggers_via_declare():
    @joryu.migration(id="001")
    def up():
        op.execute("CREATE VIEW active_users AS SELECT * FROM users")
        op.declare_schema_change(
            view_added=("active_users", "SELECT * FROM users"),
            trigger_added=("trg_audit", "users", "CREATE TRIGGER ..."),
        )

    schema = replay_migrations(MIGRATIONS.values())
    assert "active_users" in schema.views
    assert schema.triggers["trg_audit"][0] == "users"


def test_apply_unknown_op_is_noop():
    schema = VirtualSchema()
    # Use a raw RunPythonOp via op.run_python; that's an opaque op.
    @joryu.migration(id="001")
    def up():
        op.run_python(lambda c, d, ck: None)

    out = replay_migrations(MIGRATIONS.values())
    assert out.tables == {}
