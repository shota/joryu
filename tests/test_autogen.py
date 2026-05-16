"""Tests for joryu.autogen (§8)."""
from __future__ import annotations

import sqlalchemy as sa

import joryu
from joryu import op, types as t
from joryu.autogen import (
    OperationSpec,
    diff_schemas,
    emit_down_hints,
    generate_diff,
    metadata_to_virtual_schema,
    render_migration,
)
from joryu.registry import MIGRATIONS
from joryu.virtual_schema import VirtualSchema, replay_migrations


def _build_target_metadata():
    md = sa.MetaData()
    sa.Table(
        "users", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
    )
    return md


def test_diff_from_empty_creates_table():
    md = _build_target_metadata()
    ops = diff_schemas(VirtualSchema(), md)
    kinds = [o.kind for o in ops]
    assert "create_table" in kinds


def test_diff_added_column_on_existing_table():
    @joryu.migration(id="001")
    def up():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))

    current = replay_migrations(MIGRATIONS.values())
    md = _build_target_metadata()
    ops = diff_schemas(current, md)
    kinds = [o.kind for o in ops]
    assert "add_column" in kinds
    add = [o for o in ops if o.kind == "add_column"][0]
    assert add.args[0] == "users"
    assert add.args[1] == "email"


def test_diff_dropped_table_and_column():
    @joryu.migration(id="001")
    def up():
        op.create_table("ghost", op.column("id", t.BigInt, primary_key=True))
        op.create_table("users",
                        op.column("id", t.BigInt, primary_key=True),
                        op.column("legacy", t.Text))

    current = replay_migrations(MIGRATIONS.values())
    md = _build_target_metadata()
    ops = diff_schemas(current, md)
    kinds = {o.kind for o in ops}
    assert "drop_table" in kinds
    assert "drop_column" in kinds


def test_render_migration_emits_hint_block():
    ops = [
        OperationSpec("create_table", args=["users"]),
        OperationSpec("add_column", args=["users", "email", "t.Text"]),
    ]
    src = render_migration("add_users", ops)
    assert "@joryu.migration" in src
    assert "def upgrade" in src
    assert "def downgrade" in src
    assert "JORYU-DOWN-HINT: schema-impact" in src
    assert "JORYU-DOWN-HINT: completion-status: stub" in src


def test_emit_down_hints_data_loss_risk():
    ops = [OperationSpec("drop_column", args=["u", "x"])]
    hints = emit_down_hints(ops)
    assert hints.data_loss_risk == "irreversible"
    assert hints.requires_app_knowledge is False

    ops = [OperationSpec("run_python", args=[])]
    hints = emit_down_hints(ops)
    assert hints.requires_app_knowledge is True


def test_metadata_to_virtual_schema():
    md = _build_target_metadata()
    schema = metadata_to_virtual_schema(md)
    assert "users" in schema.tables
    assert "email" in schema.tables["users"].columns


def test_generate_diff_against_db_writes_file(tmp_path, sqlite_url):
    # Create the "current" DB state with an old version of the schema.
    eng = sa.create_engine(sqlite_url)
    md_existing = sa.MetaData()
    sa.Table("users", md_existing,
             sa.Column("id", sa.BigInteger, primary_key=True))
    md_existing.create_all(eng)
    eng.dispose()

    md_target = _build_target_metadata()
    migrations_dir = tmp_path / "migs"
    out = generate_diff(
        "add_email",
        target=md_target,
        against="db",
        url=sqlite_url,
        migrations_dir=migrations_dir,
    )
    body = out.read_text()
    assert out.exists()
    assert "add_column" in body
    assert "email" in body


def test_generate_diff_against_replay(tmp_migrations_dir):
    # Empty replay state -> diff against fresh model -> create_table.
    md = _build_target_metadata()
    out = generate_diff(
        "init",
        target=md,
        against="replay",
        migrations_dir=tmp_migrations_dir,
    )
    body = out.read_text()
    assert "create_table" in body
