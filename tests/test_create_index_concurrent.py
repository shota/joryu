"""create_index(..., concurrent=True) handling across dialects (§A.3 / §6.2).

On Postgres, ``concurrent=True`` must:
- render ``CREATE INDEX CONCURRENTLY``
- require the surrounding migration's ``transaction_mode == "none"``
On other dialects (sqlite, mysql) the flag is silently ignored — neither
engine honours a CONCURRENTLY token but both can usually create indexes
without taking a long lock through other means.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from joryu._op_impl import CreateIndexOp
from joryu.exceptions import JoryuError
from joryu.op_core import ExecutionContext
from joryu.runner import apply


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_concurrent_ignored_on_sqlite(tmp_migrations_dir: Path, sqlite_url: str):
    """On SQLite ``concurrent=True`` is a silent no-op."""
    _write(
        tmp_migrations_dir,
        "20260101T000000_init",
        '''
import joryu
from joryu import op, types as t

@joryu.migration(id="20260101T000000_init")
def upgrade():
    op.create_table("users",
                    op.column("id", t.BigInt, primary_key=True),
                    op.column("email", t.Text))
    op.create_index("idx_users_email", "users", ["email"], concurrent=True)
''',
    )
    apply(url=sqlite_url, migrations_dir=tmp_migrations_dir)

    engine = create_engine(sqlite_url, future=True)
    ix_names = {ix["name"] for ix in inspect(engine).get_indexes("users")}
    assert "idx_users_email" in ix_names


def test_concurrent_postgres_requires_transaction_none():
    """Static unit check: the op raises JoryuError when concurrent=True but
    the transaction_mode is not ``"none"`` on a Postgres dialect.

    Driving a real Postgres engine in CI is heavy; we exercise the
    dispatch path directly with a minimal ExecutionContext stand-in. The
    op only consults ``ctx.dialect_name`` and ``ctx.transaction_mode``
    before the guard fires, so no SQLAlchemy connection is needed.
    """

    class _FakeConn:
        # The op never reaches inspect()/execute() because the guard
        # triggers earlier. Provide attribute stubs just in case.
        def execute(self, *args, **kwargs):  # pragma: no cover - guard fires first
            raise AssertionError("execute() should not be called")

    # We *do* need inspect(conn).get_indexes(table) to return [] so the
    # guard path is reached. Patch via a dummy: use a real SQLite engine
    # (it just needs to exist) but lie about dialect_name in the ctx.
    from sqlalchemy import create_engine, text
    eng = create_engine("sqlite://", future=True)
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        ))
        ctx = ExecutionContext(
            conn=conn,
            dialect_name="postgresql",  # fake
            transaction_mode="per_step",
            on_mismatch="error",
        )
        op_ = CreateIndexOp(
            "idx_users_email", "users", ["email"], concurrent=True
        )
        with pytest.raises(JoryuError) as exc:
            op_.apply(ctx)
        msg = str(exc.value).lower()
        assert "concurrent" in msg
        assert "transaction" in msg or "transaction_mode" in msg


def test_concurrent_postgres_renders_concurrently_token(monkeypatch):
    """Verify the SQL emitted carries the ``CONCURRENTLY`` token when the
    transaction_mode is correctly ``"none"``.

    Real Postgres is not available in unit CI and SQLite rejects the
    syntax. We intercept the connection's ``execute`` to capture the SQL.
    """
    from sqlalchemy import create_engine, text

    captured: list[str] = []

    eng = create_engine("sqlite://", future=True)
    with eng.connect() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)"
        ))

        original_execute = conn.execute

        def fake_execute(clause, *args, **kwargs):
            sql = str(clause)
            if sql.lstrip().upper().startswith("CREATE") and "INDEX" in sql.upper():
                captured.append(sql)
                return None
            return original_execute(clause, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", fake_execute)

        ctx = ExecutionContext(
            conn=conn,
            dialect_name="postgresql",  # pretend we're on PG for the op logic
            transaction_mode="none",
            on_mismatch="error",
        )
        op_ = CreateIndexOp(
            "idx_users_email", "users", ["email"], concurrent=True
        )
        op_.apply(ctx)
    assert captured, "CreateIndexOp.apply did not execute any CREATE INDEX SQL"
    sql = captured[-1].upper()
    assert "CONCURRENTLY" in sql
    assert "CREATE INDEX" in sql
