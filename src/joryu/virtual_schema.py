"""In-memory SQL schema model + Operations replayer (§12).

A :class:`VirtualSchema` is a pure-Python representation of "what the schema
would look like if every Operation up to point X had been applied." It models
tables, columns, indexes, constraints, and the PG-specific objects covered by
the §12.2 ``op.declare_schema_change`` vocabulary.

The replayer is intentionally permissive: unknown shapes leave the virtual
schema untouched rather than raising. The runner-level checks (and
``joryu verify``) are responsible for hard validation; replay is best-effort
reconstruction used by ``op.historical_model`` and ``--against=replay``
autogeneration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from .op_core import Operation
    from .registry import Migration
    from ._types_impl import TypeSpec


# ---- Data classes ---------------------------------------------------------


@dataclass
class VColumn:
    name: str
    type_spec: Any           # joryu.types TypeSpec (kept as Any to avoid hard import)
    nullable: bool = True
    primary_key: bool = False
    server_default: Any | None = None
    unique: bool = False
    comment: str | None = None
    generated: str | None = None


@dataclass
class VIndex:
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    where: str | None = None


@dataclass
class VConstraint:
    name: str
    table: str
    kind: str                       # "fk" | "unique" | "check"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VTable:
    name: str
    columns: dict[str, VColumn] = field(default_factory=dict)
    indexes: dict[str, VIndex] = field(default_factory=dict)
    constraints: dict[str, VConstraint] = field(default_factory=dict)


@dataclass
class VirtualSchema:
    tables: dict[str, VTable] = field(default_factory=dict)
    extensions: set[str] = field(default_factory=set)
    enums: dict[str, list[str]] = field(default_factory=dict)
    views: dict[str, str] = field(default_factory=dict)
    materialized_views: dict[str, str] = field(default_factory=dict)
    # name -> (table, sql)
    triggers: dict[str, tuple[str, str]] = field(default_factory=dict)
    # name -> (table, sql)
    policies: dict[str, tuple[str, str]] = field(default_factory=dict)
    sequences: set[str] = field(default_factory=set)
    schemas: set[str] = field(default_factory=set)

    # --- top-level dispatch -------------------------------------------------
    def apply(self, op: "Operation") -> None:
        """Mutate this schema in place to reflect ``op``."""
        # Late imports keep this module free of cycles.
        from ._op_impl import (
            AddColumnOp,
            AlterColumnOp,
            BatchTableRebuildOp,
            CreateCheckConstraintOp,
            CreateForeignKeyOp,
            CreateIndexOp,
            CreateTableFromModelOp,
            CreateTableOp,
            CreateUniqueConstraintOp,
            DeclareSchemaChangeOp,
            DropColumnOp,
            DropConstraintOp,
            DropIndexOp,
            DropTableOp,
            RenameColumnOp,
            RenameTableOp,
        )

        kind = getattr(op, "kind", "")
        if isinstance(op, CreateTableOp):
            self._create_table(op)
        elif isinstance(op, DropTableOp):
            self.tables.pop(op.name, None)
        elif isinstance(op, RenameTableOp):
            self._rename_table(op.old, op.new)
        elif isinstance(op, AddColumnOp):
            self._add_column(op.table, _vcolumn_from_op_column(op.column))
        elif isinstance(op, DropColumnOp):
            t = self.tables.get(op.table)
            if t is not None:
                t.columns.pop(op.name, None)
        elif isinstance(op, AlterColumnOp):
            self._alter_column(op)
        elif isinstance(op, RenameColumnOp):
            self._rename_column(op.table, op.old, op.new)
        elif isinstance(op, CreateIndexOp):
            self._add_index(VIndex(
                name=op.name,
                table=op.table,
                columns=list(op.columns),
                unique=op.unique,
                where=op.where,
            ))
        elif isinstance(op, DropIndexOp):
            self._drop_index(op.name, op.table)
        elif isinstance(op, CreateUniqueConstraintOp):
            self._add_constraint(VConstraint(
                name=op.name,
                table=op.table,
                kind="unique",
                payload={"columns": list(op.columns)},
            ))
        elif isinstance(op, CreateCheckConstraintOp):
            self._add_constraint(VConstraint(
                name=op.name,
                table=op.table,
                kind="check",
                payload={"expression": op.condition},
            ))
        elif isinstance(op, CreateForeignKeyOp):
            self._add_constraint(VConstraint(
                name=op.name,
                table=op.source_table,
                kind="fk",
                payload={
                    "referred_table": op.ref_table,
                    "referred_columns": list(op.ref_cols),
                    "local_columns": list(op.source_cols),
                    "on_delete": op.on_delete,
                    "on_update": op.on_update,
                },
            ))
        elif isinstance(op, DropConstraintOp):
            self._drop_constraint(op.name, op.table)
        elif isinstance(op, CreateTableFromModelOp):
            self._create_table_from_model(op)
        elif isinstance(op, BatchTableRebuildOp):
            self._apply_batch_rebuild(op)
        elif isinstance(op, DeclareSchemaChangeOp):
            self._apply_declare(op.payload)
        else:
            # Opaque ops (ExecuteOp / RunPythonOp / StepOp) leave the schema
            # untouched. A sibling DeclareSchemaChangeOp in the same migration
            # carries the hint.
            return
        # Suppress unused-variable warnings in older Pythons.
        del kind

    # --- helpers ------------------------------------------------------------
    def _create_table(self, op: Any) -> None:
        if op.name in self.tables:
            # Idempotent — leave the existing virtual table alone.
            return
        cols: dict[str, VColumn] = {}
        for c in op.columns:
            cols[c.name] = _vcolumn_from_op_column(c)
        self.tables[op.name] = VTable(name=op.name, columns=cols)

    def _create_table_from_model(self, op: Any) -> None:
        table = getattr(op, "_table", None)
        if table is None:
            return
        if table.name in self.tables:
            return
        cols: dict[str, VColumn] = {}
        for c in table.columns:
            cols[c.name] = VColumn(
                name=c.name,
                type_spec=str(c.type),
                nullable=bool(c.nullable),
                primary_key=bool(c.primary_key),
                server_default=str(c.server_default.arg) if c.server_default is not None else None,
                unique=bool(c.unique) if c.unique is not None else False,
            )
        self.tables[table.name] = VTable(name=table.name, columns=cols)

    def _rename_table(self, old: str, new: str) -> None:
        t = self.tables.pop(old, None)
        if t is None:
            return
        t.name = new
        # Re-home indexes / constraints that pointed at the old name.
        for ix in t.indexes.values():
            if ix.table == old:
                ix.table = new
        for c in t.constraints.values():
            if c.table == old:
                c.table = new
        self.tables[new] = t

    def _add_column(self, table: str, col: VColumn) -> None:
        t = self.tables.setdefault(table, VTable(name=table))
        if col.name in t.columns:
            return  # ensure semantics
        t.columns[col.name] = col

    def _alter_column(self, op: Any) -> None:
        t = self.tables.get(op.table)
        if t is None:
            return
        col = t.columns.get(op.name)
        if col is None:
            return
        if getattr(op, "new_type", None) is not None:
            col.type_spec = op.new_type
        if getattr(op, "new_nullable", None) is not None:
            col.nullable = bool(op.new_nullable)
        sd = getattr(op, "new_server_default", None)
        # _Unset is a private sentinel; check class name to avoid importing it.
        if sd is not None and type(sd).__name__ != "_Unset":
            col.server_default = sd

    def _rename_column(self, table: str, old: str, new: str) -> None:
        t = self.tables.get(table)
        if t is None:
            return
        col = t.columns.pop(old, None)
        if col is None:
            return
        col.name = new
        t.columns[new] = col

    def _add_index(self, ix: VIndex) -> None:
        t = self.tables.setdefault(ix.table, VTable(name=ix.table))
        t.indexes[ix.name] = ix

    def _drop_index(self, name: str, table: str | None) -> None:
        if table is not None:
            t = self.tables.get(table)
            if t is not None:
                t.indexes.pop(name, None)
            return
        for t in self.tables.values():
            t.indexes.pop(name, None)

    def _add_constraint(self, c: VConstraint) -> None:
        t = self.tables.setdefault(c.table, VTable(name=c.table))
        t.constraints[c.name] = c

    def _drop_constraint(self, name: str, table: str) -> None:
        t = self.tables.get(table)
        if t is None:
            # Could be in any table; sweep.
            for tt in self.tables.values():
                tt.constraints.pop(name, None)
            return
        t.constraints.pop(name, None)

    def _apply_batch_rebuild(self, op: Any) -> None:
        """Approximate the SQLite table-rebuild against the virtual schema."""
        from ._types_impl import _normalize_type
        t = self.tables.get(op.table)
        if t is None:
            return
        for child_kind, args, kwargs in op.children:
            if child_kind == "add_column":
                name, type_arg = args
                col = VColumn(
                    name=name,
                    type_spec=_safe_normalize_type(type_arg),
                    nullable=bool(kwargs.get("nullable", True)),
                    primary_key=bool(kwargs.get("primary_key", False)),
                    server_default=kwargs.get("server_default"),
                    unique=bool(kwargs.get("unique", False)),
                    comment=kwargs.get("comment"),
                    generated=kwargs.get("generated"),
                )
                t.columns[name] = col
            elif child_kind == "drop_column":
                (name,) = args
                t.columns.pop(name, None)
            elif child_kind == "rename_column":
                old, new = args
                col = t.columns.pop(old, None)
                if col is not None:
                    col.name = new
                    t.columns[new] = col
            elif child_kind == "alter_column":
                (name,) = args
                col = t.columns.get(name)
                if col is None:
                    continue
                if "type" in kwargs and kwargs["type"] is not None:
                    col.type_spec = _safe_normalize_type(kwargs["type"])
                if kwargs.get("nullable") is not None:
                    col.nullable = bool(kwargs["nullable"])
                if "server_default" in kwargs:
                    col.server_default = kwargs["server_default"]
            elif child_kind == "create_check_constraint":
                cname, cond = args
                t.constraints[cname] = VConstraint(
                    name=cname, table=op.table, kind="check",
                    payload={"expression": cond},
                )
            elif child_kind == "create_unique_constraint":
                cname, cols = args
                t.constraints[cname] = VConstraint(
                    name=cname, table=op.table, kind="unique",
                    payload={"columns": list(cols)},
                )

    # --- §12.2 declare_schema_change handling -------------------------------
    def _apply_declare(self, payload: dict[str, Any]) -> None:
        for key, value in payload.items():
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                self._apply_declare_entry(key, entry)

    def _apply_declare_entry(self, key: str, entry: Any) -> None:
        # All tuple shapes are listed in §12.2. Be defensive: skip malformed
        # entries rather than raising — replay is best-effort.
        try:
            if key == "column_added":
                table, name, type_, opts = _unpack(entry, 4, default_last={})
                col = VColumn(
                    name=name,
                    type_spec=_safe_normalize_type(type_),
                    nullable=bool(opts.get("nullable", True)),
                    primary_key=bool(opts.get("primary_key", False)),
                    server_default=opts.get("server_default") or opts.get("default"),
                    unique=bool(opts.get("unique", False)),
                    comment=opts.get("comment"),
                    generated=opts.get("generated"),
                )
                self._add_column(table, col)
            elif key == "column_dropped":
                table, name = _unpack(entry, 2)
                t = self.tables.get(table)
                if t is not None:
                    t.columns.pop(name, None)
            elif key == "column_altered":
                table, name, diff = _unpack(entry, 3)
                t = self.tables.get(table)
                if t is None:
                    return
                col = t.columns.get(name)
                if col is None:
                    return
                new = diff.get("new") or {}
                if "type" in new:
                    col.type_spec = _safe_normalize_type(new["type"])
                if "nullable" in new:
                    col.nullable = bool(new["nullable"])
                if "default" in new:
                    col.server_default = new["default"]
            elif key == "column_renamed":
                table, old, new = _unpack(entry, 3)
                self._rename_column(table, old, new)
            elif key == "table_added":
                table, spec = _unpack(entry, 2, default_last={})
                if table in self.tables:
                    return
                vt = VTable(name=table)
                for c in spec.get("columns") or []:
                    if isinstance(c, dict):
                        vt.columns[c["name"]] = VColumn(
                            name=c["name"],
                            type_spec=_safe_normalize_type(c.get("type")),
                            nullable=bool(c.get("nullable", True)),
                            primary_key=bool(c.get("primary_key", False)),
                            server_default=c.get("server_default"),
                            unique=bool(c.get("unique", False)),
                            comment=c.get("comment"),
                            generated=c.get("generated"),
                        )
                self.tables[table] = vt
            elif key == "table_dropped":
                (table,) = _unpack(entry, 1)
                self.tables.pop(table, None)
            elif key == "table_renamed":
                old, new = _unpack(entry, 2)
                self._rename_table(old, new)
            elif key == "index_added":
                name, table, cols, opts = _unpack(entry, 4, default_last={})
                self._add_index(VIndex(
                    name=name, table=table, columns=list(cols),
                    unique=bool(opts.get("unique", False)),
                    where=opts.get("where"),
                ))
            elif key == "index_dropped":
                name, table = _unpack(entry, 2)
                self._drop_index(name, table)
            elif key == "constraint_added":
                name, table, spec = _unpack(entry, 3)
                kind = spec.get("kind", "check")
                payload = {k: v for k, v in spec.items() if k != "kind"}
                self._add_constraint(VConstraint(
                    name=name, table=table, kind=kind, payload=payload,
                ))
            elif key == "constraint_dropped":
                name, table, _spec = _unpack(entry, 3, default_last={})
                self._drop_constraint(name, table)
            elif key == "extension_added":
                (name,) = _unpack(entry, 1)
                self.extensions.add(name)
            elif key == "extension_dropped":
                (name,) = _unpack(entry, 1)
                self.extensions.discard(name)
            elif key == "enum_added":
                name, labels = _unpack(entry, 2)
                self.enums[name] = list(labels)
            elif key == "enum_dropped":
                (name,) = _unpack(entry, 1)
                self.enums.pop(name, None)
            elif key == "enum_value_added":
                name, label, pos = _unpack(entry, 3, default_last={})
                labels = self.enums.setdefault(name, [])
                if label in labels:
                    return
                before = pos.get("before")
                after = pos.get("after")
                if before and before in labels:
                    labels.insert(labels.index(before), label)
                elif after and after in labels:
                    labels.insert(labels.index(after) + 1, label)
                else:
                    labels.append(label)
            elif key == "view_added":
                name, sql = _unpack(entry, 2)
                self.views[name] = sql
            elif key == "view_dropped":
                (name,) = _unpack(entry, 1)
                self.views.pop(name, None)
            elif key == "materialized_view_added":
                name, sql = _unpack(entry, 2)
                self.materialized_views[name] = sql
            elif key == "materialized_view_dropped":
                (name,) = _unpack(entry, 1)
                self.materialized_views.pop(name, None)
            elif key == "trigger_added":
                name, table, defn = _unpack(entry, 3)
                self.triggers[name] = (table, defn)
            elif key == "trigger_dropped":
                name, table = _unpack(entry, 2)
                existing = self.triggers.get(name)
                if existing is None or existing[0] == table:
                    self.triggers.pop(name, None)
            elif key == "policy_added":
                name, table, defn = _unpack(entry, 3)
                self.policies[name] = (table, defn)
            elif key == "policy_dropped":
                name, table = _unpack(entry, 2)
                existing = self.policies.get(name)
                if existing is None or existing[0] == table:
                    self.policies.pop(name, None)
            elif key == "sequence_added":
                (name,) = _unpack(entry, 1)
                self.sequences.add(name)
            elif key == "sequence_dropped":
                (name,) = _unpack(entry, 1)
                self.sequences.discard(name)
            elif key == "schema_added":
                (name,) = _unpack(entry, 1)
                self.schemas.add(name)
            elif key == "schema_dropped":
                (name,) = _unpack(entry, 1)
                self.schemas.discard(name)
            # Unknown keys are silently ignored — additive vocabulary changes
            # in future versions must not break old replayers.
        except Exception:
            # Defensive: malformed declare entries should not abort replay.
            return


# ---- helpers --------------------------------------------------------------


def _unpack(entry: Any, n: int, *, default_last: Any = None) -> tuple[Any, ...]:
    """Tolerate tuples/lists of length n or n-1 (last field optional)."""
    if not isinstance(entry, (tuple, list)):
        raise TypeError("expected tuple/list payload")
    if len(entry) == n:
        return tuple(entry)
    if len(entry) == n - 1 and default_last is not None:
        return tuple(entry) + (default_last,)
    raise ValueError(f"expected {n}-tuple, got {len(entry)}: {entry!r}")


def _safe_normalize_type(value: Any) -> Any:
    """Normalize a type spec. Strings pass through (engine-side rendering)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        from ._types_impl import _normalize_type
        return _normalize_type(value)
    except Exception:
        return value


def _vcolumn_from_op_column(c: Any) -> VColumn:
    """Convert an ``op.Column`` descriptor into a :class:`VColumn`."""
    return VColumn(
        name=c.name,
        type_spec=c.type,
        nullable=bool(c.nullable),
        primary_key=bool(c.primary_key),
        server_default=c.server_default if c.server_default is not None else c.default,
        unique=bool(c.unique),
        comment=c.comment,
        generated=c.generated,
    )


# ---- Replay driver --------------------------------------------------------


def replay_migrations(
    migrations: "Iterable[Migration]",
    *,
    up_to: str | None = None,
    dialect: str = "sqlite",
) -> VirtualSchema:
    """Replay a sequence of migrations into a fresh :class:`VirtualSchema`.

    - ``up_to``: stop *before* the migration with this id. ``None`` = all.
    - ``dialect``: dialect name passed to ``op.*`` rendering hooks (mostly
      informational; the virtual schema itself is dialect-agnostic).
    """
    from .registry import register_operations
    from ._op_impl import set_current_dialect, reset_current_dialect

    schema = VirtualSchema()
    tok = set_current_dialect(dialect)
    try:
        ordered = list(migrations)
        for m in ordered:
            if up_to is not None and m.id == up_to:
                return schema
            if not m.registered:
                register_operations(m)
            for op in m.operations:
                schema.apply(op)
    finally:
        reset_current_dialect(tok)
    return schema


def historical_table(name: str) -> VTable | None:
    """Return the :class:`VTable` for ``name`` as of the currently-executing
    migration (or as of "everything declared so far" when no runner context
    is active).

    Used by the public ``op.historical_model(table_name)`` entry point. The
    runner is expected to set :func:`joryu._runtime.set_current_migration`
    before each migration's execution phase; until that wiring lands we fall
    back to the "everything declared so far" schema, which is the safe
    bottom-rung behaviour (callers that need the *pre*-migration snapshot
    will see an extra step they didn't want, but never *missing* state).
    """
    from .registry import MIGRATIONS
    from ._runtime import get_current_migration

    current = get_current_migration()
    schema = replay_migrations(list(MIGRATIONS.values()), up_to=current)
    return schema.tables.get(name)


__all__ = [
    "VColumn",
    "VConstraint",
    "VIndex",
    "VTable",
    "VirtualSchema",
    "historical_table",
    "replay_migrations",
]
