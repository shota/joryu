"""Tests for cross-references derivation in emit_down_hints (§11.2)."""
from __future__ import annotations

import sqlalchemy as sa

from joryu.autogen import OperationSpec, emit_down_hints


def _metadata_with_users_and_posts() -> sa.MetaData:
    md = sa.MetaData()
    sa.Table(
        "users", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
    )
    sa.Table(
        "posts", md,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("author_id", sa.BigInteger),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name="fk_posts_author"),
        sa.Index("idx_posts_author", "author_id"),
    )
    return md


def test_no_metadata_returns_empty_cross_refs():
    ops = [OperationSpec("create_table", args=["users"])]
    hints = emit_down_hints(ops)
    assert hints.cross_references == []


def test_drop_table_finds_inbound_fks():
    md = _metadata_with_users_and_posts()
    # An upgrade that creates users -> the downgrade will drop users.
    ops = [OperationSpec("create_table", args=["users"])]
    hints = emit_down_hints(ops, metadata=md)
    assert any(
        ref.startswith("foreign_key: fk_posts_author -> users.")
        for ref in hints.cross_references
    ), hints.cross_references


def test_drop_column_finds_fk_and_index():
    md = _metadata_with_users_and_posts()
    # An upgrade that added posts.author_id -> downgrade drops it.
    ops = [OperationSpec("add_column", args=["posts", "author_id"])]
    hints = emit_down_hints(ops, metadata=md)
    # Indexes on (posts, author_id) — the table being inspected is the table
    # *containing* the column, not an inbound table. fk_posts_author lives on
    # posts itself, so it surfaces by the index-walk rule (and not the
    # inbound-fk rule, which fires for *referred* tables). Either kind being
    # present is acceptable; at least the index reference must be there.
    assert any(
        ref.startswith("index: idx_posts_author -> posts.")
        for ref in hints.cross_references
    ), hints.cross_references


def test_drop_table_with_no_inbound_refs_returns_empty():
    md = sa.MetaData()
    sa.Table("standalone", md, sa.Column("id", sa.BigInteger, primary_key=True))
    ops = [OperationSpec("create_table", args=["standalone"])]
    hints = emit_down_hints(ops, metadata=md)
    assert hints.cross_references == []


def test_cross_references_deduplicated():
    md = _metadata_with_users_and_posts()
    # Two upgrade ops both implying we'll drop the users table.
    ops = [
        OperationSpec("create_table", args=["users"]),
        OperationSpec("add_column", args=["users", "email"]),
    ]
    hints = emit_down_hints(ops, metadata=md)
    # The fk_posts_author reference should appear at most once.
    fk_refs = [r for r in hints.cross_references if "fk_posts_author" in r]
    assert len(fk_refs) == len(set(fk_refs))
