"""Concrete ``op.*`` function implementations (§4, §6, §9, §13, §14).

Every ``op.*`` call:

1. Looks up the active migration via :func:`joryu.registry.current_migration`,
   so calling an op outside a registration scope raises ``RuntimeError``.
2. Constructs a concrete :class:`joryu.op_core.Operation` subclass instance.
3. Appends it to ``current_migration().operations``.

Ensure-style semantics (§9.4) are implemented inside each ``apply`` method by
inspecting the live database via SQLAlchemy. DDL is rendered through tiny
helpers in :mod:`joryu.ddl`; the few cases that do not fit (ALTER COLUMN per
dialect) emit hand-rolled SQL.
"""
from __future__ import annotations

import contextlib
import contextvars
import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

import anyio
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import TypeEngine

from .exceptions import JoryuError, UnsupportedTypeUsage

_log = logging.getLogger("joryu")
from .op_core import ExecutionContext, OpaqueOperation, Operation, PauseStep, SkipStep
from .registry import current_migration
from ._types_impl import ServerDefault, TypeSpec, _norm_dialect, _normalize_type
from . import _types_impl as _t

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# ---- Runner-set context vars (§14.1) --------------------------------------

_current_dialect_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_joryu_current_dialect", default=None
)
_current_engine_var: contextvars.ContextVar["Engine | None"] = contextvars.ContextVar(
    "_joryu_current_engine", default=None
)

# When set, the runner was entered via ``apply_async`` from inside an event
# loop; async step bodies should dispatch back to that loop using
# ``anyio.from_thread.run`` rather than spawning a fresh loop via
# ``anyio.run`` (§13.2.2 / §16.1). The contextvar carries no value beyond
# True/False; the dispatch helper consults the live anyio thread-blocking
# portal availability to decide which path to take.
_in_async_caller_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_joryu_in_async_caller", default=False
)


def set_async_caller(flag: bool) -> contextvars.Token:
    return _in_async_caller_var.set(flag)


def reset_async_caller(token: contextvars.Token) -> None:
    _in_async_caller_var.reset(token)


def _dispatch_async(fn: Callable[..., Any], *args: Any) -> Any:
    """Run an async callable from a synchronous Operation.apply().

    If the runner was entered via ``apply_async`` from inside a live event
    loop (``set_async_caller(True)``), dispatch the coroutine back to the
    caller's loop via ``anyio.from_thread.run`` — this is the §13.2.2
    requirement that async steps run on the caller's loop. Otherwise fall
    back to ``anyio.run`` which spins up a fresh loop just for this step.
    """
    if _in_async_caller_var.get():
        try:
            return anyio.from_thread.run(fn, *args)
        except RuntimeError:
            # No portal available (we are not in a worker thread, or the
            # portal has been torn down). Fall through to anyio.run.
            pass
    return anyio.run(fn, *args)


def set_current_dialect(name: str | None) -> contextvars.Token:
    """Runner-side helper: bind the dialect name visible to ``op.dialect.name``.

    Returns a Token the runner can pass back to ``reset_current_dialect``.
    """
    return _current_dialect_var.set(name)


def reset_current_dialect(token: contextvars.Token) -> None:
    _current_dialect_var.reset(token)


def set_current_engine(engine: "Engine | None") -> contextvars.Token:
    return _current_engine_var.set(engine)


def reset_current_engine(token: contextvars.Token) -> None:
    _current_engine_var.reset(token)


class _DialectProxy:
    """Read-only proxy exposing the currently-bound dialect name.

    During registration the runner sets ``_current_dialect_var`` to whichever
    target dialect was selected (§14.1). When unset (e.g. tests calling
    ``register_operations`` directly without a runner) the default is
    ``"sqlite"`` so simple code still runs.
    """

    @property
    def name(self) -> str:
        return _current_dialect_var.get() or "sqlite"

    def __repr__(self) -> str:
        return f"<op.dialect name={self.name!r}>"


dialect = _DialectProxy()


# ---- func namespace -------------------------------------------------------


class _FuncNS:
    """Tiny namespace exposing server-side default helpers (``op.func.now()``)."""

    def now(self) -> ServerDefault:
        return _t.now()


func = _FuncNS()


# ---- Column helper (used by op.create_table) ------------------------------


@dataclass
class Column:
    """Lightweight column descriptor — not an Operation.

    Used by :func:`create_table` (and replay tooling) to describe each column
    before SQL is rendered. ``type`` is always a TypeSpec instance after
    normalisation.
    """

    name: str
    type: TypeSpec
    nullable: bool = True
    default: Any = None
    server_default: "str | ServerDefault | None" = None
    primary_key: bool = False
    unique: bool = False
    comment: str | None = None
    generated: str | None = None
    autoincrement: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.kind,
            "type_opts": dict(self.type.opts),
            "nullable": self.nullable,
            "default": self.default,
            "server_default": _serialize_server_default(self.server_default),
            "primary_key": self.primary_key,
            "unique": self.unique,
            "comment": self.comment,
            "generated": self.generated,
            "autoincrement": self.autoincrement,
        }


def column(
    name: str,
    type: Any,
    *,
    nullable: bool = True,
    default: Any = None,
    server_default: "str | ServerDefault | None" = None,
    primary_key: bool = False,
    unique: bool = False,
    comment: str | None = None,
    generated: str | None = None,
    autoincrement: bool | None = None,
) -> Column:
    """Build a Column descriptor (used by ``op.create_table``)."""
    return Column(
        name=name,
        type=_normalize_type(type),
        nullable=nullable,
        default=default,
        server_default=server_default,
        primary_key=primary_key,
        unique=unique,
        comment=comment,
        generated=generated,
        autoincrement=autoincrement,
    )


# ---- Internal helpers -----------------------------------------------------


def _serialize_server_default(sd: "str | ServerDefault | None") -> Any:
    if sd is None:
        return None
    if isinstance(sd, str):
        return {"kind": "raw", "expr": sd}
    return {
        "kind": "server_default",
        "expr": sd.expr,
        "per_dialect": dict(sd.per_dialect) if sd.per_dialect else None,
    }


def _render_server_default(sd: "str | ServerDefault | None", dialect_name: str) -> str | None:
    if sd is None:
        return None
    if isinstance(sd, str):
        return sd
    return sd.render(dialect_name)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _quote(name: str) -> str:
    """Lightweight identifier quoting. SQLAlchemy preparers handle the
    dialect-specific edge cases at execute time; this is for hand-rolled
    fragments (ALTER COLUMN syntax) where we want a stable rendering."""
    return '"' + name.replace('"', '""') + '"'


def _table_exists(conn: Connection, name: str) -> bool:
    return sa.inspect(conn).has_table(name)


def _column_info(conn: Connection, table: str, column_name: str) -> dict[str, Any] | None:
    insp = sa.inspect(conn)
    try:
        cols = insp.get_columns(table)
    except SQLAlchemyError:
        return None
    for c in cols:
        if c["name"] == column_name:
            return c
    return None


# ---- Deep type-mismatch detection (§9.4 / §9.5) ---------------------------

# Map of (dialect, normalised-existing-token) -> set of normalised
# desired-token strings considered equivalent. Each key is a canonical SQL
# type token *without* parenthesised parameters (those are handled
# separately by `_canon_type`).
#
# False-positive risk: aggressive normalisation may declare two genuinely
# different precisions as equal when the engine reports a stripped-down
# rendering (e.g. SQLAlchemy returns ``VARCHAR`` with no length on some
# reflectors). When in doubt we err on the side of *equal* to avoid
# spurious ERRORs in the common "ensure semantics on rerun" path; users who
# rely on width invariants can always run an explicit ``alter_column``.
_TYPE_SYNONYMS: dict[str, dict[str, set[str]]] = {
    "postgresql": {
        "INTEGER": {"INT", "INTEGER", "INT4"},
        "BIGINT": {"BIGINT", "INT8"},
        "SMALLINT": {"SMALLINT", "INT2"},
        "REAL": {"REAL", "FLOAT4"},
        "DOUBLE PRECISION": {"DOUBLE PRECISION", "FLOAT8"},
        "TEXT": {"TEXT"},
        "TIMESTAMPTZ": {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE"},
        "TIMESTAMP": {"TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE"},
        "BOOLEAN": {"BOOL", "BOOLEAN"},
        "BYTEA": {"BYTEA"},
        "NUMERIC": {"NUMERIC", "DECIMAL"},
        "UUID": {"UUID"},
        "JSONB": {"JSONB"},
        "JSON": {"JSON"},
    },
    "mysql": {
        "INT": {"INT", "INTEGER"},
        "BIGINT": {"BIGINT"},
        "SMALLINT": {"SMALLINT"},
        "TINYINT": {"TINYINT"},
        "FLOAT": {"FLOAT"},
        "DOUBLE": {"DOUBLE", "DOUBLE PRECISION", "REAL"},
        # Text family: joryu renders Text as LONGTEXT on mysql, but engines
        # often report TEXT/MEDIUMTEXT/LONGTEXT. Treat all as equivalent for
        # ensure semantics.
        "LONGTEXT": {"TEXT", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT"},
        "TEXT": {"TEXT", "MEDIUMTEXT", "LONGTEXT", "TINYTEXT"},
        "TIMESTAMP": {"TIMESTAMP", "DATETIME"},
        "DATE": {"DATE"},
        "TIME": {"TIME"},
        "JSON": {"JSON"},
        "CHAR": {"CHAR"},
        "VARCHAR": {"VARCHAR"},
        "LONGBLOB": {"BLOB", "MEDIUMBLOB", "LONGBLOB", "TINYBLOB", "BINARY", "VARBINARY"},
        "VARBINARY": {"VARBINARY", "BINARY", "BLOB"},
        "DECIMAL": {"DECIMAL", "NUMERIC"},
    },
    "mariadb": {  # alias mapping; intentionally identical to mysql
    },
    "sqlite": {
        # SQLite type affinity is famously loose; everything maps to one of
        # INTEGER/REAL/TEXT/BLOB/NUMERIC. We compare via affinity buckets.
        "INTEGER": {"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT", "INT2", "INT8"},
        "REAL": {"REAL", "FLOAT", "DOUBLE", "DOUBLE PRECISION", "NUMERIC", "DECIMAL"},
        "TEXT": {"TEXT", "VARCHAR", "CHAR", "CLOB", "STRING"},
        "BLOB": {"BLOB", "BINARY", "VARBINARY"},
        "NUMERIC": {"NUMERIC", "DECIMAL"},
    },
}
# Cross-link mariadb -> mysql.
_TYPE_SYNONYMS["mariadb"] = _TYPE_SYNONYMS["mysql"]


_PAREN_RE = re.compile(r"\s*\(([^)]*)\)\s*")


def _canon_type(sql_fragment: str) -> tuple[str, tuple[str, ...]]:
    """Split a SQL type fragment into (base, args).

    ``VARCHAR(255)`` -> (``"VARCHAR"``, (``"255"``,))
    ``NUMERIC(10, 2)`` -> (``"NUMERIC"``, (``"10"``, ``"2"``))
    ``TIMESTAMP WITH TIME ZONE`` -> (``"TIMESTAMP WITH TIME ZONE"``, ())
    """
    s = sql_fragment.strip().upper()
    m = _PAREN_RE.search(s)
    if not m:
        return (" ".join(s.split()), ())
    base = (s[: m.start()] + s[m.end():]).strip()
    base = " ".join(base.split())
    args = tuple(a.strip() for a in m.group(1).split(",") if a.strip())
    return (base, args)


def _type_matches(typespec: TypeSpec, sqla_type: TypeEngine, dialect: str) -> bool:
    """Return True if a TypeSpec is equivalent to a reflected SQLAlchemy type.

    Strategy: render both sides as SQL fragments, canonicalise (uppercase,
    strip parens, normalise whitespace), then consult a small per-dialect
    synonym table. Parenthesised arguments must match exactly when both
    sides supply them; if either side omits them we treat as equal (mainly
    because some inspectors drop the length).
    """
    d = _norm_dialect(dialect)
    try:
        desired = typespec.render(d)
    except Exception:
        return False
    # Reflected type: prefer compile(dialect=...) for accuracy.
    try:
        from sqlalchemy.dialects import postgresql, mysql, sqlite  # noqa: F401
        existing = str(sqla_type.compile(dialect=_dialect_for(d)))
    except Exception:
        existing = str(sqla_type)

    d_base, d_args = _canon_type(desired)
    e_base, e_args = _canon_type(existing)
    if d_base == e_base:
        if not d_args or not e_args or d_args == e_args:
            return True
        return False
    syns = _TYPE_SYNONYMS.get(d, {})
    # An equivalence is symmetric: existing-base maps to a set containing
    # desired-base, or vice versa.
    bucket_existing = syns.get(e_base, {e_base})
    bucket_desired = syns.get(d_base, {d_base})
    if d_base in bucket_existing or e_base in bucket_desired:
        if not d_args or not e_args or d_args == e_args:
            return True
    return False


def _dialect_for(name: str) -> Any:
    """Best-effort dialect object factory used by :func:`_type_matches`."""
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import dialect as _d
        return _d()
    if name in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import dialect as _d
        return _d()
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import dialect as _d
        return _d()
    from sqlalchemy.engine.default import DefaultDialect
    return DefaultDialect()


# ---- DDL operations -------------------------------------------------------


class CreateTableOp(Operation):
    kind = "create_table"

    def __init__(
        self,
        name: str,
        columns: list[Column],
        *,
        if_not_exists: bool = False,
        table_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.columns = columns
        self.if_not_exists = if_not_exists
        self.table_kwargs = dict(table_kwargs or {})
        _validate_columns(self.columns)

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.name,)]

    def describe(self) -> str:
        col_names = ", ".join(c.name for c in self.columns)
        return f"create_table {self.name}({col_names})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
        }

    def apply(self, ctx: ExecutionContext) -> None:
        if _table_exists(ctx.conn, self.name):
            # Ensure semantics (§9.4):
            # - every desired column present + type/nullability match -> skip
            # - any desired column missing or mismatched -> ERROR
            # - extra columns on the DB are TOLERATED (later migrations may
            #   have added them already; this is the resume-friendly path)
            existing_cols = sa.inspect(ctx.conn).get_columns(self.name)
            by_name = {c["name"]: c for c in existing_cols}
            missing: list[str] = []
            mismatches: list[str] = []
            for desired in self.columns:
                ex = by_name.get(desired.name)
                if ex is None:
                    missing.append(desired.name)
                    continue
                ex_type: TypeEngine = ex["type"]
                ex_nullable: bool = bool(ex.get("nullable", True))
                type_ok = _type_matches(desired.type, ex_type, ctx.dialect_name)
                null_ok = (ex_nullable == desired.nullable)
                if not type_ok:
                    mismatches.append(
                        f"{desired.name}: type existing {str(ex_type)!r}, "
                        f"desired {desired.type.render(ctx.dialect_name)!r}"
                    )
                if not null_ok:
                    mismatches.append(
                        f"{desired.name}: nullable existing {ex_nullable}, "
                        f"desired {desired.nullable}"
                    )
            if not missing and not mismatches:
                return  # already in desired state
            if ctx.on_mismatch == "skip":
                _log.warning(
                    "create_table(%s): mismatch ignored (missing=%r, mismatch=%r)",
                    self.name,
                    missing,
                    mismatches,
                )
                return
            details: list[str] = []
            if missing:
                details.append(f"missing desired columns {sorted(missing)!r}")
            if mismatches:
                details.append("; ".join(mismatches))
            raise JoryuError(
                f"create_table({self.name!r}): table already exists but does not "
                f"match desired schema — {'; '.join(details)}. Use op.add_column "
                "/ op.alter_column to reconcile incremental changes; create_table "
                "asserts the full initial shape."
            )

        col_defs = [_render_column_inline(c, ctx.dialect_name) for c in self.columns]
        sql = f"CREATE TABLE {_quote(self.name)} (\n  " + ",\n  ".join(col_defs) + "\n)"
        ctx.conn.execute(text(sql))


class DropTableOp(Operation):
    kind = "drop_table"

    def __init__(self, name: str) -> None:
        self.name = name

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.name,)]

    def describe(self) -> str:
        return f"drop_table {self.name}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "name": self.name}

    def apply(self, ctx: ExecutionContext) -> None:
        if not _table_exists(ctx.conn, self.name):
            return
        ctx.conn.execute(text(f"DROP TABLE {_quote(self.name)}"))


class RenameTableOp(Operation):
    kind = "rename_table"

    def __init__(self, old: str, new: str) -> None:
        self.old = old
        self.new = new

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.old,), (self.new,)]

    def describe(self) -> str:
        return f"rename_table {self.old} -> {self.new}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "old": self.old, "new": self.new}

    def apply(self, ctx: ExecutionContext) -> None:
        old_exists = _table_exists(ctx.conn, self.old)
        new_exists = _table_exists(ctx.conn, self.new)
        if new_exists and not old_exists:
            return  # ensure: already renamed
        if new_exists and old_exists:
            raise JoryuError(
                f"rename_table({self.old!r} -> {self.new!r}): both names exist"
            )
        if not old_exists:
            raise JoryuError(
                f"rename_table({self.old!r} -> {self.new!r}): source table not found"
            )
        d = _norm_dialect(ctx.dialect_name)
        if d in ("mysql", "mariadb"):
            sql = f"RENAME TABLE {_quote(self.old)} TO {_quote(self.new)}"
        else:
            sql = f"ALTER TABLE {_quote(self.old)} RENAME TO {_quote(self.new)}"
        ctx.conn.execute(text(sql))


class AddColumnOp(Operation):
    kind = "add_column"

    def __init__(
        self,
        table: str,
        name: str,
        type: Any,
        *,
        nullable: bool = True,
        default: Any = None,
        server_default: "str | ServerDefault | None" = None,
        primary_key: bool = False,
        unique: bool = False,
        comment: str | None = None,
        generated: str | None = None,
        on_mismatch: str = "error",
    ) -> None:
        self.column = Column(
            name=name,
            type=_normalize_type(type),
            nullable=nullable,
            default=default,
            server_default=server_default,
            primary_key=primary_key,
            unique=unique,
            comment=comment,
            generated=generated,
        )
        self.table = table
        self.on_mismatch_local = on_mismatch
        _validate_columns([self.column])

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table, self.column.name)]

    def describe(self) -> str:
        nn = "NOT NULL" if not self.column.nullable else "NULL"
        return f"add_column {self.table}.{self.column.name} ({self.column.type.kind}, {nn})"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "table": self.table, "column": self.column.to_dict()}

    def _resolved_on_mismatch(self, ctx: ExecutionContext) -> str:
        """Per-op override wins over the migration-wide default (§9.5)."""
        if self.on_mismatch_local and self.on_mismatch_local != "error":
            return self.on_mismatch_local
        # If the op was constructed with the explicit "error" default, still
        # let the migration-wide setting (e.g. "alter") apply.
        if self.on_mismatch_local == "error" and ctx.on_mismatch != "error":
            return ctx.on_mismatch
        return self.on_mismatch_local

    def apply(self, ctx: ExecutionContext) -> None:
        existing = _column_info(ctx.conn, self.table, self.column.name)
        if existing is None:
            col_sql = _render_column_inline(self.column, ctx.dialect_name)
            ctx.conn.execute(
                text(f"ALTER TABLE {_quote(self.table)} ADD COLUMN {col_sql}")
            )
            return

        # Column exists: ensure-style deep comparison (§9.4 / §9.5).
        existing_type: TypeEngine = existing["type"]
        existing_nullable: bool = bool(existing.get("nullable", True))
        type_ok = _type_matches(self.column.type, existing_type, ctx.dialect_name)
        null_ok = (existing_nullable == self.column.nullable)
        if type_ok and null_ok:
            return  # already matches desired state

        mode = self._resolved_on_mismatch(ctx)
        got_type = str(existing_type)
        want_type = self.column.type.render(ctx.dialect_name)
        details: list[str] = []
        if not type_ok:
            details.append(f"type: existing {got_type!r}, desired {want_type!r}")
        if not null_ok:
            details.append(
                f"nullable: existing {existing_nullable}, desired {self.column.nullable}"
            )
        diff = "; ".join(details)

        if mode == "skip":
            _log.warning(
                "add_column(%s.%s): mismatch ignored (%s)",
                self.table,
                self.column.name,
                diff,
            )
            return

        if mode == "error":
            raise JoryuError(
                f"column {self.table}.{self.column.name}: mismatch — {diff}; "
                "pass on_mismatch='alter' to reconcile or 'skip' to ignore"
            )

        if mode == "alter":
            self._apply_alter(ctx, existing_type, type_ok, null_ok)
            return

        raise JoryuError(
            f"add_column: unknown on_mismatch={mode!r} (expected error|alter|skip)"
        )

    def _apply_alter(
        self,
        ctx: ExecutionContext,
        existing_type: TypeEngine,
        type_ok: bool,
        null_ok: bool,
    ) -> None:
        d = _norm_dialect(ctx.dialect_name)
        if d == "sqlite":
            raise JoryuError(
                f"add_column on sqlite cannot reconcile a type mismatch via ALTER; "
                f"wrap the change in `with op.batch({self.table!r})` for a "
                "table-rebuild"
            )
        if d == "postgresql":
            if not type_ok:
                rendered = self.column.type.render(ctx.dialect_name)
                ctx.conn.execute(
                    text(
                        f"ALTER TABLE {_quote(self.table)} "
                        f"ALTER COLUMN {_quote(self.column.name)} TYPE {rendered}"
                    )
                )
            if not null_ok:
                clause = "DROP NOT NULL" if self.column.nullable else "SET NOT NULL"
                ctx.conn.execute(
                    text(
                        f"ALTER TABLE {_quote(self.table)} "
                        f"ALTER COLUMN {_quote(self.column.name)} {clause}"
                    )
                )
            return
        if d in ("mysql", "mariadb"):
            type_sql = self.column.type.render(ctx.dialect_name)
            null_sql = " NOT NULL" if not self.column.nullable else " NULL"
            ctx.conn.execute(
                text(
                    f"ALTER TABLE {_quote(self.table)} "
                    f"MODIFY COLUMN {_quote(self.column.name)} {type_sql}{null_sql}"
                )
            )
            return
        raise JoryuError(
            f"add_column on_mismatch='alter': unsupported dialect {d!r}"
        )


class DropColumnOp(Operation):
    kind = "drop_column"

    def __init__(self, table: str, name: str) -> None:
        self.table = table
        self.name = name

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table, self.name)]

    def describe(self) -> str:
        return f"drop_column {self.table}.{self.name}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "table": self.table, "name": self.name}

    def apply(self, ctx: ExecutionContext) -> None:
        if _column_info(ctx.conn, self.table, self.name) is None:
            return
        d = _norm_dialect(ctx.dialect_name)
        if d == "sqlite":
            # SQLite gained ALTER TABLE DROP COLUMN in 3.35; we attempt it but
            # bubble up a hint if the engine is older.
            try:
                ctx.conn.execute(
                    text(f"ALTER TABLE {_quote(self.table)} DROP COLUMN {_quote(self.name)}")
                )
                return
            except SQLAlchemyError as exc:
                raise JoryuError(
                    f"drop_column on sqlite failed ({exc}); wrap the change in `with op.batch({self.table!r})` for a table-rebuild"
                ) from exc
        ctx.conn.execute(
            text(f"ALTER TABLE {_quote(self.table)} DROP COLUMN {_quote(self.name)}")
        )


class AlterColumnOp(Operation):
    kind = "alter_column"

    def __init__(
        self,
        table: str,
        name: str,
        *,
        type: Any = None,
        nullable: bool | None = None,
        server_default: "str | ServerDefault | None | _Unset" = None,
    ) -> None:
        self.table = table
        self.name = name
        self.new_type: TypeSpec | None = _normalize_type(type) if type is not None else None
        self.new_nullable = nullable
        # ``server_default=None`` is ambiguous (drop default? leave alone?);
        # use a sentinel _UNSET so callers can opt in explicitly.
        self.new_server_default = server_default

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table, self.name)]

    def describe(self) -> str:
        parts: list[str] = []
        if self.new_type is not None:
            parts.append(f"type={self.new_type.kind}")
        if self.new_nullable is not None:
            parts.append("NOT NULL" if not self.new_nullable else "NULL")
        if not isinstance(self.new_server_default, _Unset):
            parts.append(f"server_default={self.new_server_default!r}")
        return f"alter_column {self.table}.{self.name} ({', '.join(parts) or 'no-op'})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "table": self.table,
            "name": self.name,
            "type": self.new_type.kind if self.new_type else None,
            "type_opts": dict(self.new_type.opts) if self.new_type else None,
            "nullable": self.new_nullable,
            "server_default": (
                None
                if isinstance(self.new_server_default, _Unset)
                else _serialize_server_default(self.new_server_default)
            ),
        }

    def apply(self, ctx: ExecutionContext) -> None:
        existing = _column_info(ctx.conn, self.table, self.name)
        if existing is None:
            raise JoryuError(
                f"alter_column({self.table}.{self.name}): column does not exist"
            )
        d = _norm_dialect(ctx.dialect_name)
        if d == "sqlite":
            # Auto-wrap into a one-op batch rebuild (§A.5 ergonomic).
            # Static analysis (joryu verify) still sees the original
            # AlterColumnOp because we do not change the registered op.
            rebuild = BatchTableRebuildOp(self.table)
            kwargs: dict[str, Any] = {}
            if self.new_type is not None:
                kwargs["type"] = self.new_type
            if self.new_nullable is not None:
                kwargs["nullable"] = self.new_nullable
            if not isinstance(self.new_server_default, _Unset):
                kwargs["server_default"] = self.new_server_default
            rebuild.children.append(("alter_column", (self.name,), kwargs))
            rebuild.apply(ctx)
            return

        statements: list[str] = []
        if self.new_type is not None:
            rendered = self.new_type.render(ctx.dialect_name)
            if d == "postgresql":
                statements.append(
                    f"ALTER TABLE {_quote(self.table)} ALTER COLUMN {_quote(self.name)} TYPE {rendered}"
                )
            elif d in ("mysql", "mariadb"):
                # MySQL MODIFY requires the full column definition including
                # nullability; combine into a single statement below.
                pass

        if self.new_nullable is not None:
            if d == "postgresql":
                clause = "DROP NOT NULL" if self.new_nullable else "SET NOT NULL"
                statements.append(
                    f"ALTER TABLE {_quote(self.table)} ALTER COLUMN {_quote(self.name)} {clause}"
                )

        if not isinstance(self.new_server_default, _Unset):
            if d == "postgresql":
                if self.new_server_default is None:
                    statements.append(
                        f"ALTER TABLE {_quote(self.table)} ALTER COLUMN {_quote(self.name)} DROP DEFAULT"
                    )
                else:
                    rendered = _render_server_default(
                        self.new_server_default, ctx.dialect_name
                    )
                    statements.append(
                        f"ALTER TABLE {_quote(self.table)} ALTER COLUMN {_quote(self.name)} SET DEFAULT {rendered}"
                    )

        if d in ("mysql", "mariadb"):
            # Build one MODIFY COLUMN statement carrying every desired piece.
            type_sql = (
                self.new_type.render(ctx.dialect_name)
                if self.new_type is not None
                else _column_type_sql(existing)
            )
            null_sql = ""
            if self.new_nullable is False:
                null_sql = " NOT NULL"
            elif self.new_nullable is True:
                null_sql = " NULL"
            default_sql = ""
            if not isinstance(self.new_server_default, _Unset):
                if self.new_server_default is None:
                    pass  # nothing -> default cleared
                else:
                    rendered = _render_server_default(
                        self.new_server_default, ctx.dialect_name
                    )
                    default_sql = f" DEFAULT {rendered}"
            statements.append(
                f"ALTER TABLE {_quote(self.table)} MODIFY COLUMN {_quote(self.name)} {type_sql}{null_sql}{default_sql}"
            )

        for sql in statements:
            ctx.conn.execute(text(sql))


class RenameColumnOp(Operation):
    kind = "rename_column"

    def __init__(self, table: str, old: str, new: str) -> None:
        self.table = table
        self.old = old
        self.new = new

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table, self.old), (self.table, self.new)]

    def describe(self) -> str:
        return f"rename_column {self.table}.{self.old} -> {self.new}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "table": self.table, "old": self.old, "new": self.new}

    def apply(self, ctx: ExecutionContext) -> None:
        new_exists = _column_info(ctx.conn, self.table, self.new) is not None
        old_exists = _column_info(ctx.conn, self.table, self.old) is not None
        if new_exists and not old_exists:
            return  # already renamed
        if new_exists and old_exists:
            raise JoryuError(
                f"rename_column({self.table}.{self.old} -> {self.new}): both names exist"
            )
        if not old_exists:
            raise JoryuError(
                f"rename_column({self.table}.{self.old} -> {self.new}): source column missing"
            )
        d = _norm_dialect(ctx.dialect_name)
        if d == "postgresql":
            sql = f"ALTER TABLE {_quote(self.table)} RENAME COLUMN {_quote(self.old)} TO {_quote(self.new)}"
        elif d in ("mysql", "mariadb"):
            sql = f"ALTER TABLE {_quote(self.table)} RENAME COLUMN {_quote(self.old)} TO {_quote(self.new)}"
        elif d == "sqlite":
            sql = f"ALTER TABLE {_quote(self.table)} RENAME COLUMN {_quote(self.old)} TO {_quote(self.new)}"
        else:
            raise JoryuError(f"rename_column: unsupported dialect {d!r}")
        ctx.conn.execute(text(sql))


class CreateIndexOp(Operation):
    kind = "create_index"

    def __init__(
        self,
        name: str,
        table: str,
        columns: Iterable[str],
        *,
        unique: bool = False,
        concurrent: bool = False,
        where: str | None = None,
    ) -> None:
        self.name = name
        self.table = table
        self.columns = list(columns)
        self.unique = unique
        self.concurrent = concurrent
        self.where = where

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)]

    def describe(self) -> str:
        u = "UNIQUE " if self.unique else ""
        return f"create_index {u}{self.name} ON {self.table}({', '.join(self.columns)})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "table": self.table,
            "columns": self.columns,
            "unique": self.unique,
            "concurrent": self.concurrent,
            "where": self.where,
        }

    def apply(self, ctx: ExecutionContext) -> None:
        insp = sa.inspect(ctx.conn)
        existing = {ix["name"] for ix in insp.get_indexes(self.table)}
        if self.name in existing:
            return
        d = _norm_dialect(ctx.dialect_name)
        # ``concurrent=True`` is only meaningful on PostgreSQL — both MySQL
        # online DDL and SQLite have different (and dialect-specific) ways to
        # avoid full-table locks, and neither honours a CONCURRENTLY token.
        # We silently ignore the flag on non-Postgres dialects so a single
        # migration file can express the Postgres intent and still run
        # elsewhere (§6 multi-dialect). Document any divergence in the
        # migration body if you need it.
        concurrent_token = ""
        if self.concurrent and d == "postgresql":
            # CREATE INDEX CONCURRENTLY cannot run inside a transaction
            # block (Postgres error 25001). Require the surrounding
            # migration to opt out of transactions via
            # ``transaction_mode="none"`` (§A.3 / §6.2).
            if ctx.transaction_mode != "none":
                raise JoryuError(
                    f"create_index({self.name!r}, concurrent=True): "
                    "CREATE INDEX CONCURRENTLY cannot run inside a "
                    "transaction. Set the migration's transaction_mode "
                    "to \"none\" (see §A.3)."
                )
            concurrent_token = "CONCURRENTLY "
        unique = "UNIQUE " if self.unique else ""
        cols = ", ".join(_quote(c) for c in self.columns)
        where = f" WHERE {self.where}" if self.where else ""
        sql = (
            f"CREATE {unique}INDEX {concurrent_token}{_quote(self.name)} "
            f"ON {_quote(self.table)} ({cols}){where}"
        )
        ctx.conn.execute(text(sql))


class DropIndexOp(Operation):
    kind = "drop_index"

    def __init__(self, name: str, table: str | None = None) -> None:
        self.name = name
        self.table = table

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)] if self.table else []

    def describe(self) -> str:
        if self.table:
            return f"drop_index {self.name} ON {self.table}"
        return f"drop_index {self.name}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "name": self.name, "table": self.table}

    def apply(self, ctx: ExecutionContext) -> None:
        d = _norm_dialect(ctx.dialect_name)
        if d in ("mysql", "mariadb"):
            if not self.table:
                raise JoryuError("drop_index on mysql requires table=")
            ctx.conn.execute(text(f"DROP INDEX {_quote(self.name)} ON {_quote(self.table)}"))
        else:
            ctx.conn.execute(text(f"DROP INDEX IF EXISTS {_quote(self.name)}"))


class CreateUniqueConstraintOp(Operation):
    kind = "create_unique_constraint"

    def __init__(self, name: str, table: str, columns: Iterable[str]) -> None:
        self.name = name
        self.table = table
        self.columns = list(columns)

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)]

    def describe(self) -> str:
        return f"unique_constraint {self.name} ON {self.table}({', '.join(self.columns)})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "table": self.table,
            "columns": self.columns,
        }

    def apply(self, ctx: ExecutionContext) -> None:
        cols = ", ".join(_quote(c) for c in self.columns)
        sql = (
            f"ALTER TABLE {_quote(self.table)} "
            f"ADD CONSTRAINT {_quote(self.name)} UNIQUE ({cols})"
        )
        ctx.conn.execute(text(sql))


class CreateCheckConstraintOp(Operation):
    kind = "create_check_constraint"

    def __init__(self, name: str, table: str, condition: str) -> None:
        self.name = name
        self.table = table
        self.condition = condition

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)]

    def describe(self) -> str:
        return f"check_constraint {self.name} ON {self.table} ({self.condition})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "table": self.table,
            "condition": self.condition,
        }

    def apply(self, ctx: ExecutionContext) -> None:
        sql = (
            f"ALTER TABLE {_quote(self.table)} "
            f"ADD CONSTRAINT {_quote(self.name)} CHECK ({self.condition})"
        )
        ctx.conn.execute(text(sql))


class CreateForeignKeyOp(Operation):
    kind = "create_foreign_key"

    def __init__(
        self,
        name: str,
        source_table: str,
        ref_table: str,
        source_cols: Iterable[str],
        ref_cols: Iterable[str],
        *,
        on_delete: str | None = None,
        on_update: str | None = None,
        deferrable: bool | None = None,
        initially: str | None = None,
    ) -> None:
        self.name = name
        self.source_table = source_table
        self.ref_table = ref_table
        self.source_cols = list(source_cols)
        self.ref_cols = list(ref_cols)
        self.on_delete = on_delete
        self.on_update = on_update
        self.deferrable = deferrable
        self.initially = initially

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.source_table,)]

    def describe(self) -> str:
        return (
            f"foreign_key {self.name} {self.source_table}({', '.join(self.source_cols)}) "
            f"-> {self.ref_table}({', '.join(self.ref_cols)})"
        )

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "source_table": self.source_table,
            "ref_table": self.ref_table,
            "source_cols": self.source_cols,
            "ref_cols": self.ref_cols,
            "on_delete": self.on_delete,
            "on_update": self.on_update,
        }

    def apply(self, ctx: ExecutionContext) -> None:
        src = ", ".join(_quote(c) for c in self.source_cols)
        ref = ", ".join(_quote(c) for c in self.ref_cols)
        extras: list[str] = []
        if self.on_delete:
            extras.append(f"ON DELETE {self.on_delete}")
        if self.on_update:
            extras.append(f"ON UPDATE {self.on_update}")
        if self.deferrable:
            extras.append("DEFERRABLE")
            if self.initially:
                extras.append(f"INITIALLY {self.initially}")
        tail = (" " + " ".join(extras)) if extras else ""
        sql = (
            f"ALTER TABLE {_quote(self.source_table)} "
            f"ADD CONSTRAINT {_quote(self.name)} "
            f"FOREIGN KEY ({src}) REFERENCES {_quote(self.ref_table)} ({ref}){tail}"
        )
        ctx.conn.execute(text(sql))


class DropConstraintOp(Operation):
    kind = "drop_constraint"

    def __init__(self, name: str, table: str) -> None:
        self.name = name
        self.table = table

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)]

    def describe(self) -> str:
        return f"drop_constraint {self.name} ON {self.table}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "name": self.name, "table": self.table}

    def apply(self, ctx: ExecutionContext) -> None:
        ctx.conn.execute(
            text(f"ALTER TABLE {_quote(self.table)} DROP CONSTRAINT {_quote(self.name)}")
        )


# ---- Escape-hatch operations ----------------------------------------------


class ExecuteOp(OpaqueOperation):
    kind = "execute"

    def __init__(self, sql_or_dict: "str | dict[str, str]") -> None:
        if not isinstance(sql_or_dict, (str, dict)):
            raise TypeError("op.execute expects a str or a dict[str, str]")
        self.payload = sql_or_dict

    def describe(self) -> str:
        if isinstance(self.payload, str):
            preview = self.payload.strip().splitlines()[0][:60]
            return f"execute: {preview}"
        keys = sorted(self.payload.keys())
        return f"execute (per-dialect: {', '.join(keys)})"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "sql": self.payload}

    def apply(self, ctx: ExecutionContext) -> None:
        sql = _select_dialect_sql(self.payload, ctx.dialect_name)
        ctx.conn.execute(text(sql))


def _select_dialect_sql(payload: "str | dict[str, str]", dialect_name: str) -> str:
    if isinstance(payload, str):
        return payload
    d = _norm_dialect(dialect_name)
    # Normalise input keys too so callers can use "postgres"/"pg".
    normalized: dict[str, str] = {}
    for k, v in payload.items():
        if k == "default":
            normalized["default"] = v
        else:
            normalized[_norm_dialect(k)] = v
    if d in normalized:
        return normalized[d]
    # mariadb specifically falls back to mysql before default (close cousins),
    # only if neither an explicit mariadb nor mysql key is present and there
    # is a default. The spec wording is "mariadb is its own key but falls back
    # to default if not present and mysql is also absent".
    if "default" in normalized:
        return normalized["default"]
    raise JoryuError(
        f"op.execute: no SQL for dialect {d!r} (keys: {sorted(normalized.keys())})"
    )


class RunPythonOp(OpaqueOperation):
    kind = "run_python"

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn

    def describe(self) -> str:
        name = getattr(self.fn, "__name__", "fn")
        return f"run_python: {name}"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": getattr(self.fn, "__name__", "fn"),
            "qualname": getattr(self.fn, "__qualname__", ""),
        }

    def apply(self, ctx: ExecutionContext) -> None:
        if inspect.iscoroutinefunction(self.fn):
            _dispatch_async(self.fn, ctx.conn, dialect, ctx.checkpoint)
        else:
            self.fn(ctx.conn, dialect, ctx.checkpoint)


class StepOp(OpaqueOperation):
    kind = "step"

    def __init__(
        self,
        fn: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "step")
        self.description = description or _first_doc_line(fn) or self.name

    def describe(self) -> str:
        return f"step: {self.name} ({self.description})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "name": self.name,
            "qualname": getattr(self.fn, "__qualname__", ""),
        }

    def apply(self, ctx: ExecutionContext) -> None:
        # PauseStep / SkipStep propagate so the runner can branch on them.
        if inspect.iscoroutinefunction(self.fn):
            result = _dispatch_async(self.fn, ctx.conn, dialect, ctx.checkpoint)
        else:
            result = self.fn(ctx.conn, dialect, ctx.checkpoint)
        # Completion semantics from §13.2.2 — runner-side logic interprets
        # this return value; we just surface it for visibility.
        ctx.extras[f"step_result:{self.name}"] = result


class DeclareSchemaChangeOp(OpaqueOperation):
    """Replay hint (§12.2); has no on-DB effect."""

    kind = "declare_schema_change"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def describe(self) -> str:
        keys = sorted(self.payload.keys())
        return f"declare_schema_change: {', '.join(keys)}"

    def _fingerprint_payload(self) -> Any:
        return {"kind": self.kind, "payload": _coerce_jsonable(self.payload)}

    def apply(self, ctx: ExecutionContext) -> None:
        return None


class CreateTableFromModelOp(Operation):
    kind = "create_table_from_model"

    def __init__(self, model: Any, only: list[str] | None = None) -> None:
        self.model = model
        self.only = list(only) if only else None
        try:
            self._table = model.__table__
        except AttributeError as exc:
            raise TypeError(
                "op.create_table_from_model requires a SQLAlchemy declarative model"
            ) from exc

    def targets(self) -> list[tuple[str, ...]]:
        return [(self._table.name,)]

    def describe(self) -> str:
        return f"create_table_from_model {self._table.name}"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "table": self._table.name,
            "columns": [c.name for c in self._table.columns],
            "only": self.only,
        }

    def apply(self, ctx: ExecutionContext) -> None:
        # checkfirst=True gives ensure-style behaviour for free.
        self._table.create(bind=ctx.conn, checkfirst=True)


# ---- Batch (SQLite table rebuild) -----------------------------------------


class _BatchProxy:
    """Proxy returned by ``with op.batch(table) as batch:``.

    On non-SQLite dialects every call forwards to the corresponding op.* fn,
    so each child op registers individually. On SQLite the proxy accumulates
    the child ops into a :class:`BatchTableRebuildOp`.
    """

    def __init__(self, table: str, rebuild_op: "BatchTableRebuildOp | None") -> None:
        self.table = table
        self._rebuild = rebuild_op

    def add_column(self, name: str, type: Any, **kwargs: Any) -> None:
        if self._rebuild is None:
            add_column(self.table, name, type, **kwargs)
        else:
            self._rebuild.children.append(("add_column", (name, type), kwargs))

    def drop_column(self, name: str) -> None:
        if self._rebuild is None:
            drop_column(self.table, name)
        else:
            self._rebuild.children.append(("drop_column", (name,), {}))

    def alter_column(self, name: str, **kwargs: Any) -> None:
        if self._rebuild is None:
            alter_column(self.table, name, **kwargs)
        else:
            self._rebuild.children.append(("alter_column", (name,), kwargs))

    def rename_column(self, old: str, new: str) -> None:
        if self._rebuild is None:
            rename_column(self.table, old, new)
        else:
            self._rebuild.children.append(("rename_column", (old, new), {}))

    def create_check_constraint(self, name: str, condition: str) -> None:
        if self._rebuild is None:
            create_check_constraint(name, self.table, condition)
        else:
            self._rebuild.children.append(
                ("create_check_constraint", (name, condition), {})
            )

    def create_unique_constraint(self, name: str, columns: Iterable[str]) -> None:
        if self._rebuild is None:
            create_unique_constraint(name, self.table, columns)
        else:
            self._rebuild.children.append(
                ("create_unique_constraint", (name, list(columns)), {})
            )


class BatchTableRebuildOp(Operation):
    """SQLite-only: rebuild a table to apply schema changes the engine cannot
    do via ALTER. Strategy:

    1. Reflect the current ``Table``.
    2. Apply the accumulated child mutations to a fresh ``Table`` description.
    3. ``CREATE`` the new table under a temp name.
    4. ``INSERT INTO new SELECT ... FROM old`` (column-name intersection).
    5. ``DROP`` the old table and ``RENAME`` new -> old.

    v0.1 supports drop_column, add_column (NULLable / with default),
    alter_column (nullable / type / server_default), rename_column,
    create_check_constraint, create_unique_constraint. FKs / triggers /
    indexes that referenced the old table are NOT re-created automatically;
    declare them explicitly inside the batch or after.
    """

    kind = "batch_rebuild"

    def __init__(self, table: str) -> None:
        self.table = table
        self.children: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def targets(self) -> list[tuple[str, ...]]:
        return [(self.table,)]

    def describe(self) -> str:
        ops = ", ".join(c[0] for c in self.children)
        return f"batch_rebuild {self.table} ({ops or 'no-op'})"

    def _fingerprint_payload(self) -> Any:
        return {
            "kind": self.kind,
            "table": self.table,
            "children": [(k, _coerce_jsonable(args), _coerce_jsonable(kwargs))
                         for k, args, kwargs in self.children],
        }

    def apply(self, ctx: ExecutionContext) -> None:
        d = _norm_dialect(ctx.dialect_name)
        if d != "sqlite":  # pragma: no cover - guarded at registration
            return
        # Reflect existing columns and re-render them as Column descriptors.
        insp = sa.inspect(ctx.conn)
        existing_cols = insp.get_columns(self.table)
        # Capture the pre-rebuild indexes and FKs so we can replay them on
        # the new table (§4.4 / A.5). Triggers are SQLite-rare; deferred to
        # a future release. If the batch later drops the columns the index
        # references, we silently drop the index too.
        try:
            existing_indexes = insp.get_indexes(self.table)
        except SQLAlchemyError:
            existing_indexes = []
        try:
            existing_fks = insp.get_foreign_keys(self.table)
        except SQLAlchemyError:
            existing_fks = []
        # Use the engine-reported type string as the rendered SQL fragment.
        # We wrap it in an inline TypeSpec stand-in via a tiny descriptor.
        current: list[_RebuildCol] = []
        for c in existing_cols:
            current.append(_RebuildCol(
                name=c["name"],
                type_sql=str(c["type"]),
                nullable=c["nullable"],
                primary_key=c.get("primary_key", False) or False,
                default=c.get("default"),
            ))

        rename_map: dict[str, str] = {}
        added_constraints: list[str] = []
        # Track explicit removals: a child op may drop a column, in which
        # case any index/FK referencing that column must also be discarded.
        dropped_columns: set[str] = set()
        # If the batch explicitly recreates an index/FK with the same name
        # via a child op, we let that win over the pre-existing definition.
        explicit_index_names: set[str] = set()
        for kind, args, kwargs in self.children:
            if kind == "add_column":
                name, t = args
                col_desc = column(name, t, **kwargs)
                current.append(_RebuildCol(
                    name=col_desc.name,
                    type_sql=col_desc.type.render(ctx.dialect_name),
                    nullable=col_desc.nullable,
                    primary_key=col_desc.primary_key,
                    default=None,
                    server_default=_render_server_default(col_desc.server_default, ctx.dialect_name),
                ))
            elif kind == "drop_column":
                (name,) = args
                current = [c for c in current if c.name != name]
                dropped_columns.add(name)
            elif kind == "rename_column":
                old, new = args
                rename_map[new] = old
                for c in current:
                    if c.name == old:
                        c.name = new
            elif kind == "alter_column":
                (name,) = args
                for c in current:
                    if c.name == name:
                        if kwargs.get("nullable") is not None:
                            c.nullable = kwargs["nullable"]
                        if kwargs.get("type") is not None:
                            spec = _normalize_type(kwargs["type"])
                            c.type_sql = spec.render(ctx.dialect_name)
            elif kind == "create_check_constraint":
                cname, cond = args
                added_constraints.append(
                    f"CONSTRAINT {_quote(cname)} CHECK ({cond})"
                )
            elif kind == "create_unique_constraint":
                cname, cols = args
                joined = ", ".join(_quote(x) for x in cols)
                added_constraints.append(
                    f"CONSTRAINT {_quote(cname)} UNIQUE ({joined})"
                )

        # Build inline FK clauses for the rebuilt table. SQLite requires FKs
        # to live in the CREATE TABLE body (there is no ALTER TABLE ADD
        # CONSTRAINT FOREIGN KEY), so we include them here. Discard FKs
        # whose source column was dropped or no longer exists.
        live_column_names = {c.name for c in current}
        old_to_new_cols = {old: new for new, old in rename_map.items()}
        fk_clauses: list[str] = []
        for fk in existing_fks:
            src_cols = fk.get("constrained_columns") or []
            ref_cols = fk.get("referred_columns") or []
            ref_table = fk.get("referred_table")
            if not src_cols or not ref_cols or not ref_table:
                continue
            if any(c in dropped_columns for c in src_cols):
                continue
            mapped_src = [old_to_new_cols.get(c, c) for c in src_cols]
            if any(c not in live_column_names for c in mapped_src):
                continue
            src_sql = ", ".join(_quote(c) for c in mapped_src)
            ref_sql = ", ".join(_quote(c) for c in ref_cols)
            extras: list[str] = []
            opts = fk.get("options") or {}
            if opts.get("ondelete"):
                extras.append(f"ON DELETE {opts['ondelete']}")
            if opts.get("onupdate"):
                extras.append(f"ON UPDATE {opts['onupdate']}")
            tail = (" " + " ".join(extras)) if extras else ""
            name = fk.get("name")
            name_sql = f"CONSTRAINT {_quote(name)} " if name else ""
            fk_clauses.append(
                f"{name_sql}FOREIGN KEY ({src_sql}) REFERENCES "
                f"{_quote(ref_table)} ({ref_sql}){tail}"
            )

        temp_name = f"_joryu_rebuild_{self.table}"
        col_defs = [c.render() for c in current]
        body = ",\n  ".join(col_defs + added_constraints + fk_clauses)
        ctx.conn.execute(text(f"CREATE TABLE {_quote(temp_name)} (\n  {body}\n)"))

        old_names = {c["name"] for c in existing_cols}
        shared = [
            (c.name, rename_map.get(c.name, c.name))
            for c in current
            if rename_map.get(c.name, c.name) in old_names
        ]
        if shared:
            select_cols = ", ".join(_quote(o) for _, o in shared)
            insert_cols = ", ".join(_quote(n) for n, _ in shared)
            ctx.conn.execute(text(
                f"INSERT INTO {_quote(temp_name)} ({insert_cols}) "
                f"SELECT {select_cols} FROM {_quote(self.table)}"
            ))
        ctx.conn.execute(text(f"DROP TABLE {_quote(self.table)}"))
        ctx.conn.execute(
            text(f"ALTER TABLE {_quote(temp_name)} RENAME TO {_quote(self.table)}")
        )

        # Re-create the indexes the old table carried (§4.4). We skip:
        # - indexes referencing a column the batch dropped
        # - indexes whose name was reused by a child op (the child op
        #   would have registered itself separately on non-SQLite, but
        #   on SQLite within a batch we accumulate as added_constraints
        #   instead — track via explicit_index_names if you wire that up)
        # - auto-created indexes for PRIMARY KEY / UNIQUE constraints
        #   (SQLite generates a sqlite_autoindex_<table>_N which is not
        #   user-defined; we filter by the `dialect_options` /
        #   ``name.startswith("sqlite_autoindex_")`` heuristic)
        live_columns = {c.name for c in current}
        for ix in existing_indexes:
            ix_name = ix.get("name")
            if not ix_name:
                continue
            if ix_name.startswith("sqlite_autoindex_"):
                continue
            if ix_name in explicit_index_names:
                continue
            ix_cols = ix.get("column_names") or []
            # Translate via rename_map (rename_map is new->old; we want old->new).
            old_to_new = {old: new for new, old in rename_map.items()}
            mapped_cols = [old_to_new.get(c, c) for c in ix_cols if c is not None]
            if not mapped_cols:
                continue
            if any(c in dropped_columns for c in ix_cols):
                continue
            if any(c not in live_columns for c in mapped_cols):
                continue
            unique = "UNIQUE " if ix.get("unique") else ""
            joined = ", ".join(_quote(c) for c in mapped_cols)
            ctx.conn.execute(text(
                f"CREATE {unique}INDEX {_quote(ix_name)} ON {_quote(self.table)} ({joined})"
            ))

        # Foreign keys were preserved inline at CREATE TABLE time above —
        # SQLite cannot ALTER TABLE ADD CONSTRAINT FOREIGN KEY, so there
        # is nothing more to do here.


@dataclass
class _RebuildCol:
    name: str
    type_sql: str
    nullable: bool = True
    primary_key: bool = False
    default: Any = None
    server_default: str | None = None

    def render(self) -> str:
        parts = [_quote(self.name), self.type_sql]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if not self.nullable:
            parts.append("NOT NULL")
        if self.server_default is not None:
            parts.append(f"DEFAULT {self.server_default}")
        elif self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        return " ".join(parts)


# ---- Public op.* functions ------------------------------------------------


def create_table(name: str, *columns: Column, **kwargs: Any) -> None:
    for c in columns:
        if not isinstance(c, Column):
            raise TypeError("op.create_table accepts op.column(...) entries only")
    current_migration().operations.append(CreateTableOp(name, list(columns), **kwargs))


def drop_table(name: str) -> None:
    current_migration().operations.append(DropTableOp(name))


def rename_table(old: str, new: str) -> None:
    current_migration().operations.append(RenameTableOp(old, new))


def add_column(
    table: str,
    name: str,
    type: Any,
    *,
    nullable: bool = True,
    default: Any = None,
    server_default: "str | ServerDefault | None" = None,
    primary_key: bool = False,
    unique: bool = False,
    comment: str | None = None,
    generated: str | None = None,
    on_mismatch: str = "error",
) -> None:
    current_migration().operations.append(
        AddColumnOp(
            table,
            name,
            type,
            nullable=nullable,
            default=default,
            server_default=server_default,
            primary_key=primary_key,
            unique=unique,
            comment=comment,
            generated=generated,
            on_mismatch=on_mismatch,
        )
    )


def drop_column(table: str, name: str) -> None:
    current_migration().operations.append(DropColumnOp(table, name))


class _Unset:
    """Sentinel marker: ``server_default`` was not passed."""

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _Unset()


def alter_column(
    table: str,
    name: str,
    *,
    type: Any = None,
    nullable: bool | None = None,
    server_default: "str | ServerDefault | None | _Unset" = _UNSET,
) -> None:
    current_migration().operations.append(
        AlterColumnOp(table, name, type=type, nullable=nullable, server_default=server_default)
    )


def rename_column(table: str, old: str, new: str) -> None:
    current_migration().operations.append(RenameColumnOp(table, old, new))


def create_index(
    name: str,
    table: str,
    columns: Iterable[str],
    *,
    unique: bool = False,
    concurrent: bool = False,
    where: str | None = None,
) -> None:
    current_migration().operations.append(
        CreateIndexOp(name, table, columns, unique=unique, concurrent=concurrent, where=where)
    )


def drop_index(name: str, table: str | None = None) -> None:
    current_migration().operations.append(DropIndexOp(name, table))


def create_unique_constraint(name: str, table: str, columns: Iterable[str]) -> None:
    current_migration().operations.append(CreateUniqueConstraintOp(name, table, columns))


def create_check_constraint(name: str, table: str, condition: str) -> None:
    current_migration().operations.append(CreateCheckConstraintOp(name, table, condition))


def create_foreign_key(
    name: str,
    source_table: str,
    ref_table: str,
    source_cols: Iterable[str],
    ref_cols: Iterable[str],
    **fk_kwargs: Any,
) -> None:
    current_migration().operations.append(
        CreateForeignKeyOp(name, source_table, ref_table, source_cols, ref_cols, **fk_kwargs)
    )


def drop_constraint(name: str, table: str) -> None:
    current_migration().operations.append(DropConstraintOp(name, table))


def execute(sql_or_dict: "str | dict[str, str]") -> None:
    current_migration().operations.append(ExecuteOp(sql_or_dict))


def run_python(fn: Callable[..., Any]) -> None:
    current_migration().operations.append(RunPythonOp(fn))


@contextlib.contextmanager
def batch(table_name: str):
    """Context manager (§4.4). Non-SQLite: passes through to plain ops.
    SQLite: accumulates child mutations and emits one BatchTableRebuildOp."""
    d = _norm_dialect(dialect.name)
    if d == "sqlite":
        rebuild = BatchTableRebuildOp(table_name)
        proxy = _BatchProxy(table_name, rebuild)
        yield proxy
        current_migration().operations.append(rebuild)
    else:
        proxy = _BatchProxy(table_name, None)
        yield proxy


def step(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Any:
    """Three-form decorator (§13.2.1).

    - ``@op.step`` (bare): ``fn`` is the function; register and return it.
    - ``op.step(fn, name=..., description=...)`` (direct): register and return fn.
    - ``@op.step(name=..., description=...)`` (factory): return a decorator.
    """
    if fn is not None:
        if not callable(fn):
            raise TypeError("op.step: first positional argument must be callable")
        _register_step(fn, name, description)
        return fn

    def decorator(real_fn: Callable[..., Any]) -> Callable[..., Any]:
        if not callable(real_fn):
            raise TypeError("op.step decorator factory received a non-callable")
        _register_step(real_fn, name, description)
        return real_fn

    return decorator


def _register_step(
    fn: Callable[..., Any],
    name: str | None,
    description: str | None,
) -> None:
    current_migration().operations.append(StepOp(fn, name=name, description=description))


def declare_schema_change(**kwargs: Any) -> None:
    current_migration().operations.append(DeclareSchemaChangeOp(dict(kwargs)))


def create_table_from_model(model: Any, *, only: list[str] | None = None) -> None:
    current_migration().operations.append(CreateTableFromModelOp(model, only))


# ---- Read-only helpers ----------------------------------------------------


def historical_model(table_name: str):
    from .virtual_schema import historical_table

    return historical_table(table_name)


def get_engine() -> "Engine":
    eng = _current_engine_var.get()
    if eng is None:
        raise NotImplementedError(
            "op.get_engine(): no engine bound; the runner must call "
            "set_current_engine() before entering the execution phase"
        )
    return eng


# ---- Small helpers --------------------------------------------------------


def _validate_columns(columns: list[Column]) -> None:
    for c in columns:
        if c.type.kind in ("Serial", "BigSerial") and not c.primary_key:
            raise UnsupportedTypeUsage(
                f"column {c.name!r}: {c.type.kind} requires primary_key=True on every dialect"
            )
        if c.default is not None and c.server_default is not None:
            raise UnsupportedTypeUsage(
                f"column {c.name!r}: default= and server_default= are mutually exclusive"
            )


def _render_column_inline(col: Column, dialect_name: str) -> str:
    """Render a single column definition for ADD COLUMN / CREATE TABLE.

    Kept hand-rolled (rather than reusing SQLAlchemy's compiler) so that the
    output is stable across SQLAlchemy versions and easy to inspect in tests.
    """
    parts = [_quote(col.name), col.type.render(dialect_name)]
    if col.primary_key:
        parts.append("PRIMARY KEY")
    if not col.nullable:
        parts.append("NOT NULL")
    if col.unique and not col.primary_key:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {_sql_literal(col.default)}")
    elif col.server_default is not None:
        rendered = _render_server_default(col.server_default, dialect_name)
        parts.append(f"DEFAULT {rendered}")
    if col.generated:
        parts.append(f"GENERATED ALWAYS AS ({col.generated}) STORED")
    if col.comment and _norm_dialect(dialect_name) in ("mysql", "mariadb"):
        parts.append(f"COMMENT '{col.comment.replace(chr(39), chr(39) * 2)}'")
    return " ".join(parts)


def _column_type_sql(info: dict[str, Any]) -> str:
    t = info.get("type")
    if t is None:
        return "TEXT"
    try:
        return str(t.compile())
    except Exception:
        return str(t)


def _first_doc_line(fn: Callable[..., Any]) -> str | None:
    doc = (fn.__doc__ or "").strip()
    if not doc:
        return None
    return doc.splitlines()[0].strip()


def _coerce_jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _coerce_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_jsonable(v) for v in value]
    if isinstance(value, TypeSpec):
        return {"_typespec": value.kind, "opts": _coerce_jsonable(value.opts)}
    return repr(value)


__all__ = [
    "AddColumnOp",
    "AlterColumnOp",
    "BatchTableRebuildOp",
    "Column",
    "CreateCheckConstraintOp",
    "CreateForeignKeyOp",
    "CreateIndexOp",
    "CreateTableFromModelOp",
    "CreateTableOp",
    "CreateUniqueConstraintOp",
    "DeclareSchemaChangeOp",
    "DropColumnOp",
    "DropConstraintOp",
    "DropIndexOp",
    "DropTableOp",
    "ExecuteOp",
    "PauseStep",
    "RenameColumnOp",
    "RenameTableOp",
    "RunPythonOp",
    "SkipStep",
    "StepOp",
    "add_column",
    "alter_column",
    "batch",
    "column",
    "create_check_constraint",
    "create_foreign_key",
    "create_index",
    "create_table",
    "create_table_from_model",
    "create_unique_constraint",
    "declare_schema_change",
    "dialect",
    "drop_column",
    "drop_constraint",
    "drop_index",
    "drop_table",
    "execute",
    "func",
    "get_engine",
    "historical_model",
    "rename_column",
    "rename_table",
    "reset_current_dialect",
    "reset_current_engine",
    "run_python",
    "set_current_dialect",
    "set_current_engine",
    "step",
]
