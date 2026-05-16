"""Registration + op registration smoke tests."""
from __future__ import annotations

import pytest

import joryu
from joryu import op, types as t
from joryu.registry import MIGRATIONS, register_operations


def test_migration_decorator_registers_id():
    @joryu.migration(id="20260101T000000_x")
    def upgrade():
        pass

    assert "20260101T000000_x" in MIGRATIONS
    m = MIGRATIONS["20260101T000000_x"]
    assert m.id == "20260101T000000_x"
    assert m.transaction_mode == "per_step"
    assert m.depends_on == []


def test_duplicate_id_raises():
    @joryu.migration(id="20260101T000000_dup")
    def a():
        pass

    with pytest.raises(ValueError, match="duplicate"):

        @joryu.migration(id="20260101T000000_dup")
        def b():
            pass


def test_op_calls_outside_registration_raise():
    with pytest.raises(RuntimeError, match="registration"):
        op.add_column("users", "x", t.Text)


def test_register_operations_populates_ops_list():
    @joryu.migration(id="20260102T000000_r")
    def upgrade():
        op.create_table("users", op.column("id", t.BigInt, primary_key=True))
        op.add_column("users", "email", t.Text)

    m = MIGRATIONS["20260102T000000_r"]
    register_operations(m)
    assert m.registered is True
    assert len(m.operations) == 2
    assert m.operations[0].kind == "create_table"
    assert m.operations[1].kind == "add_column"


def test_targets_for_static_analysis():
    @joryu.migration(id="20260103T000000_t")
    def upgrade():
        op.add_column("users", "email", t.Text)
        op.drop_column("users", "old")
        op.execute("SELECT 1")  # opaque

    m = MIGRATIONS["20260103T000000_t"]
    register_operations(m)
    assert m.operations[0].targets() == [("users", "email")]
    assert m.operations[1].targets() == [("users", "old")]
    assert m.operations[2].targets() == []  # opaque
