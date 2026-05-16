"""Conflict detection (§7.2)."""
from __future__ import annotations

import joryu
from joryu import op, types as t
from joryu.verify import verify


def _mig(id_, body):
    return joryu.migration(id=id_)(body)


def test_no_conflict_independent_tables():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.create_table("orders", op.column("id", t.BigInt, primary_key=True))

    assert verify() == []


def test_no_conflict_distinct_columns_same_table():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.add_column("users", "email", t.Text)

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.add_column("users", "phone", t.Text)

    assert verify() == []


def test_double_alter_emits_conflict():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.alter_column("users", "email", nullable=False)

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.alter_column("users", "email", nullable=True)

    conflicts = verify()
    assert len(conflicts) == 1
    assert conflicts[0].kind == "double_alter"
    assert conflicts[0].left.migration_id == "20260101T000000_a"
    assert conflicts[0].right.migration_id == "20260101T000001_b"


def test_add_then_drop_conflict():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.add_column("users", "phone", t.Text)

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.drop_column("users", "phone")

    conflicts = verify()
    assert len(conflicts) == 1
    assert conflicts[0].kind == "add_drop"


def test_table_drop_priority_over_add_drop():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.drop_column("users", "phone")

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.drop_table("users")

    conflicts = verify()
    assert len(conflicts) == 1
    assert conflicts[0].kind == "table_drop"


def test_opaque_ops_silent():
    @joryu.migration(id="20260101T000000_a")
    def a():
        op.execute("UPDATE users SET email = LOWER(email)")

    @joryu.migration(id="20260101T000001_b")
    def b():
        op.drop_column("users", "email")

    # Either side opaque → no Conflict.
    assert verify() == []
