"""SQLAlchemy-diff autogenerator for ``joryu generate`` (§8).

Pipeline:

1. Build a "current" :class:`VirtualSchema` from one of two sources
   (``--against=db`` reflects the live database; ``--against=replay`` runs
   every migration in :mod:`joryu.loader` through
   :func:`joryu.virtual_schema.replay_migrations`).
2. Diff it against a user-provided :class:`sqlalchemy.MetaData` (the target).
3. Render a fresh migration file under ``migrations/``, including a
   downgrade skeleton stamped with §11.2 JORYU-DOWN-HINT structured comments.

Diffing produces :class:`OperationSpec` records — *not* live Operation
objects — because the output is source code, not an in-process execution.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal

import sqlalchemy as sa

from .downhint import DownHints, emit_hints
from .virtual_schema import (
    VColumn,
    VConstraint,
    VIndex,
    VTable,
    VirtualSchema,
    replay_migrations,
)

if TYPE_CHECKING:
    from sqlalchemy import MetaData


# ---- OperationSpec --------------------------------------------------------


@dataclass
class OperationSpec:
    """Source-code-bound description of one ``op.*`` call.

    ``kind`` matches the leading function name (``"create_table"`` etc.).
    ``args`` / ``kwargs`` are rendered verbatim into the migration body.
    """

    kind: str
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    warning: str | None = None


# ---- Diff -----------------------------------------------------------------


def diff_schemas(
    current: VirtualSchema,
    target_metadata: "MetaData",
    *,
    dialect: str = "sqlite",
) -> list[OperationSpec]:
    """Diff a virtual schema against a SQLAlchemy ``MetaData``.

    Detects (per §8): new/dropped tables, added/dropped columns, added/dropped
    indexes (by name), added/dropped foreign keys, and altered columns
    (nullability or rendered type). Dangerous ops carry a ``warning=`` that
    :func:`render_migration` surfaces as ``# WARNING:`` comments per §8.3.
    """
    ops: list[OperationSpec] = []

    target_tables = {t.name: t for t in target_metadata.sorted_tables}
    current_tables = current.tables

    # --- new tables ---
    for name, tbl in target_tables.items():
        if name in current_tables:
            continue
        ops.append(_create_table_op(tbl, dialect=dialect))

    # --- dropped tables ---
    for name in current_tables:
        if name in target_tables:
            continue
        spec = OperationSpec(
            kind="drop_table",
            args=[name],
            warning=f"dropping table {name!r} loses its data — review carefully",
        )
        ops.append(spec)

    # --- columns / indexes / FKs on shared tables ---
    for name, tbl in target_tables.items():
        if name not in current_tables:
            continue
        vt = current_tables[name]
        target_cols = {c.name: c for c in tbl.columns}

        # Added columns
        for col_name, col in target_cols.items():
            if col_name in vt.columns:
                continue
            ops.append(_add_column_op(name, col, dialect=dialect))
        # Dropped columns
        for col_name in vt.columns:
            if col_name in target_cols:
                continue
            ops.append(OperationSpec(
                kind="drop_column",
                args=[name, col_name],
                warning=f"dropping column {name}.{col_name} loses its data",
            ))
        # Altered columns (shared name) — compare nullability and rendered type.
        for col_name, target_col in target_cols.items():
            if col_name not in vt.columns:
                continue
            spec = _alter_column_op_if_changed(name, target_col, vt.columns[col_name])
            if spec is not None:
                ops.append(spec)

        # Added indexes (by name)
        target_indexes = {ix.name: ix for ix in tbl.indexes if ix.name}
        for ix_name, ix in target_indexes.items():
            if ix_name in vt.indexes:
                continue
            ops.append(OperationSpec(
                kind="create_index",
                args=[ix_name, name, [c.name for c in ix.columns]],
                kwargs={"unique": bool(ix.unique)} if ix.unique else {},
            ))
        # Dropped indexes
        for ix_name in vt.indexes:
            if ix_name in target_indexes:
                continue
            ops.append(OperationSpec(
                kind="drop_index",
                args=[ix_name, name],
            ))

        # Added / dropped foreign keys
        target_fks = {
            fk.name: fk
            for fk in tbl.foreign_key_constraints
            if getattr(fk, "name", None)
        }
        current_fks = {
            cn: c for cn, c in vt.constraints.items() if c.kind == "fk"
        }
        for fk_name, fk in target_fks.items():
            if fk_name in current_fks:
                continue
            local = [c.name for c in fk.columns]
            referred_table = (
                fk.referred_table.name if fk.referred_table is not None else ""
            )
            referred_cols = [el.column.name for el in fk.elements]
            kw: dict[str, Any] = {}
            if fk.ondelete:
                kw["on_delete"] = fk.ondelete
            if fk.onupdate:
                kw["on_update"] = fk.onupdate
            ops.append(OperationSpec(
                kind="create_foreign_key",
                args=[fk_name, name, referred_table, local, referred_cols],
                kwargs=kw,
            ))
        for fk_name in current_fks:
            if fk_name in target_fks:
                continue
            ops.append(OperationSpec(
                kind="drop_constraint",
                args=[fk_name, name],
            ))

    return ops


def _alter_column_op_if_changed(
    table: str, target_col: sa.Column, current_col: VColumn
) -> OperationSpec | None:
    """Return an alter_column spec if nullability or type rendering differs."""
    new_type = _render_sa_type(target_col.type)
    cur_type = _stringify_type(current_col.type_spec)
    nullable_diff = bool(target_col.nullable) != bool(current_col.nullable)
    type_diff = new_type != cur_type
    if not nullable_diff and not type_diff:
        return None
    kwargs: dict[str, Any] = {}
    warning: str | None = None
    if type_diff:
        kwargs["type"] = new_type
    if nullable_diff:
        kwargs["nullable"] = bool(target_col.nullable)
        if not target_col.nullable and (
            target_col.server_default is None and target_col.default is None
        ):
            warning = (
                f"tightening {table}.{target_col.name} to NOT NULL without a default "
                "may fail on populated tables"
            )
    return OperationSpec(
        kind="alter_column",
        args=[table, target_col.name],
        kwargs=kwargs,
        warning=warning,
    )


def _stringify_type(spec: Any) -> str:
    """Coerce a VColumn.type_spec to the same ``t.*`` literal as targets render."""
    if spec is None:
        return ""
    if isinstance(spec, str):
        return spec
    kind = getattr(spec, "kind", None)
    if kind:
        # Best-effort: map the joryu TypeSpec kind back into the t.X literal we
        # emit for targets. Lower-case BigInt etc. are intentional.
        return f"t.{kind}"
    return str(spec)


def _create_table_op(tbl: sa.Table, *, dialect: str) -> OperationSpec:
    cols: list[Any] = []
    for c in tbl.columns:
        col_kwargs: dict[str, Any] = {}
        if c.primary_key:
            col_kwargs["primary_key"] = True
        if not c.nullable:
            col_kwargs["nullable"] = False
        if c.unique:
            col_kwargs["unique"] = True
        cols.append(_ColumnSpec(
            name=c.name,
            type=_render_sa_type(c.type),
            kwargs=col_kwargs,
        ))
    return OperationSpec(kind="create_table", args=[tbl.name, *cols])


def _add_column_op(table: str, col: sa.Column, *, dialect: str) -> OperationSpec:
    kw: dict[str, Any] = {}
    if not col.nullable:
        kw["nullable"] = False
    if col.unique:
        kw["unique"] = True
    return OperationSpec(
        kind="add_column",
        args=[table, col.name, _render_sa_type(col.type)],
        kwargs=kw,
        warning=("adding NOT NULL without a default may fail on populated tables"
                 if not col.nullable and col.server_default is None and col.default is None
                 else None),
    )


def _render_sa_type(sa_type: Any) -> str:
    """Map a SQLAlchemy column type to the ``t.*`` literal we want to emit."""
    name = sa_type.__class__.__name__.lower()
    mapping = {
        "biginteger": "t.BigInt",
        "smallinteger": "t.SmallInt",
        "integer": "t.Int",
        "boolean": "t.Bool",
        "float": "t.Float",
        "double": "t.Double",
        "numeric": "t.Decimal",
        "text": "t.Text",
        "date": "t.Date",
        "time": "t.Time",
        "datetime": "t.Timestamp",
        "timestamp": "t.Timestamp",
        "uuid": "t.Uuid",
        "json": "t.Json",
        "jsonb": "t.Json",
        "largebinary": "t.Binary",
    }
    if name in mapping:
        return mapping[name]
    if name in ("string", "varchar"):
        length = getattr(sa_type, "length", None)
        return f"t.String({length})" if length else "t.String"
    if name == "enum":
        labels = getattr(sa_type, "enums", None) or ()
        enum_name = getattr(sa_type, "name", None)
        body = ", ".join(repr(l) for l in labels)
        if enum_name:
            return f"t.Enum({body}, name={enum_name!r})"
        return f"t.Enum({body})"
    # Fallback — let the user fix it up.
    return f"t.dialect({sa_type.__class__.__module__.split('.')[-1] + '.' + name!r})"


# ---- Rendering -----------------------------------------------------------


@dataclass
class _ColumnSpec:
    name: str
    type: str
    kwargs: dict[str, Any]

    def render(self) -> str:
        kw = "".join(f", {k}={v!r}" for k, v in self.kwargs.items())
        return f"op.column({self.name!r}, {self.type}{kw})"


def render_migration(
    slug: str,
    ops: list[OperationSpec],
    *,
    dialect: str = "sqlite",
    migration_id: str | None = None,
    metadata: "MetaData | None" = None,
) -> str:
    """Render the source code of a generated migration file.

    ``metadata`` (the SQLAlchemy MetaData passed to :func:`diff_schemas`) is
    forwarded into :func:`emit_down_hints` so the generated
    ``cross-references`` hint reflects the live FK graph (§11.2).
    """
    mig_id = migration_id or _slugify(slug)
    upgrade_body = _render_upgrade(ops)
    hints = emit_down_hints(ops, metadata=metadata)
    down_hint_block = _indent(emit_hints(hints), "    ")
    downgrade_body = _render_downgrade(ops)

    body = (
        f'"""Auto-generated migration for {slug}."""\n'
        "import joryu\n"
        "from joryu import op, types as t\n"
        "\n"
        "\n"
        f"@joryu.migration(id={mig_id!r})\n"
        "def upgrade():\n"
        f"{upgrade_body}\n"
        "\n"
        "\n"
        "@joryu.downgrade\n"
        "def downgrade():\n"
        f"{down_hint_block}\n"
        f"{downgrade_body}\n"
    )
    return body


def _render_upgrade(ops: list[OperationSpec]) -> str:
    if not ops:
        return "    pass"
    lines: list[str] = []
    for op in ops:
        if op.warning:
            lines.append(f"    # WARNING: {op.warning}")
        lines.append(_render_op_call(op))
        # Per §8.3, an ``add_column`` tightening to NOT NULL without a default
        # is the canonical "needs a backfill" case: emit a placeholder
        # ``op.run_python`` followed by ``op.declare_schema_change`` (§12.2)
        # so the migration both flags the gap and stays replay-friendly.
        if _needs_backfill(op):
            lines.extend(_render_backfill_block(op))
    return "\n".join(lines)


def _needs_backfill(op: OperationSpec) -> bool:
    if op.kind != "add_column":
        return False
    if op.kwargs.get("nullable", True):
        return False
    if op.kwargs.get("default") is not None or op.kwargs.get("server_default") is not None:
        return False
    return True


def _render_backfill_block(op: OperationSpec) -> list[str]:
    table = op.args[0] if op.args else "?"
    col_name = op.args[1] if len(op.args) > 1 else "?"
    fn_name = f"backfill_{table}_{col_name}".replace("-", "_")
    return [
        "    # TODO: implement the backfill below before tightening to NOT NULL.",
        f"    def {fn_name}(conn, dialect, checkpoint):",
        f"        # Populate {table}.{col_name} so the NOT NULL tightening succeeds.",
        "        raise NotImplementedError(",
        f"            \"backfill for {table}.{col_name} is not implemented yet\"",
        "        )",
        f"    op.run_python({fn_name})",
        f"    op.declare_schema_change(column_altered=("
        f"{table!r}, {col_name!r}, {{'old': {{'nullable': True}}, 'new': {{'nullable': False}}}}))",
    ]


def _render_downgrade(ops: list[OperationSpec]) -> str:
    inverted = _invert_ops(ops)
    if not inverted:
        return "    pass"
    return "\n".join(_render_op_call(op) for op in inverted)


def _render_op_call(op: OperationSpec) -> str:
    arg_strs: list[str] = []
    for a in op.args:
        if isinstance(a, _ColumnSpec):
            arg_strs.append(a.render())
        else:
            arg_strs.append(repr(a))
    for k, v in op.kwargs.items():
        arg_strs.append(f"{k}={v!r}")
    return f"    op.{op.kind}({', '.join(arg_strs)})"


def _invert_ops(ops: list[OperationSpec]) -> list[OperationSpec]:
    """Naive inversion (§11.1 warns: not always safe — generator emits a stub)."""
    out: list[OperationSpec] = []
    for op in ops:
        if op.kind == "create_table":
            out.append(OperationSpec("drop_table", args=[op.args[0]]))
        elif op.kind == "drop_table":
            out.append(OperationSpec(
                "execute",
                args=[f"-- TODO: recreate table {op.args[0]} (original definition unknown)"],
            ))
        elif op.kind == "add_column":
            table, col_name = op.args[0], op.args[1]
            out.append(OperationSpec("drop_column", args=[table, col_name]))
        elif op.kind == "drop_column":
            out.append(OperationSpec(
                "execute",
                args=[f"-- TODO: re-add dropped column {op.args[0]}.{op.args[1]}"],
            ))
        elif op.kind == "create_index":
            ix_name, table = op.args[0], op.args[1]
            out.append(OperationSpec("drop_index", args=[ix_name, table]))
        elif op.kind == "drop_index":
            ix_name = op.args[0]
            out.append(OperationSpec(
                "execute",
                args=[f"-- TODO: recreate index {ix_name}"],
            ))
        elif op.kind == "create_foreign_key":
            fk_name, src_tbl = op.args[0], op.args[1]
            out.append(OperationSpec("drop_constraint", args=[fk_name, src_tbl]))
        elif op.kind == "drop_constraint":
            out.append(OperationSpec(
                "execute",
                args=[f"-- TODO: recreate constraint {op.args[0]} on {op.args[1]}"],
            ))
        elif op.kind == "alter_column":
            out.append(OperationSpec(
                "execute",
                args=[
                    f"-- TODO: restore prior shape of {op.args[0]}.{op.args[1]}"
                ],
            ))
        # Other ops fall through silently; they'd require domain knowledge.
    return out


def _indent(block: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in block.splitlines())


# ---- Down-hint derivation -------------------------------------------------


def emit_down_hints(
    ops_for_upgrade: list[OperationSpec],
    *,
    metadata: "MetaData | None" = None,
) -> DownHints:
    """Build a :class:`DownHints` reflecting an upgrade op list (§11.2).

    - ``schema-impact``: mechanically inverted (drop_* / restore_*).
    - ``cross-references``: derived from ``metadata`` (the target SQLAlchemy
      MetaData) when provided — for each ``drop_table: T`` we list every FK
      pointing at ``T``; for each ``drop_column: T.C`` we list every FK and
      every multi-column index that touches ``(T, C)``. Without ``metadata``
      the list is empty (the v0.2 default; AI completers fill the gap from
      ``models/`` themselves).
    - ``data-loss-risk``: heuristic per §11.2.
    - ``requires-app-knowledge``: true iff any op is ``run_python`` or
      ``execute``.
    """
    schema_impact: list[str] = []
    risk = "none"
    requires_app = False
    for op in ops_for_upgrade:
        if op.kind == "create_table":
            schema_impact.append(f"drop_table: {op.args[0]}")
            risk = _max_risk(risk, "high")
        elif op.kind == "drop_table":
            schema_impact.append(f"restore_table: {op.args[0]}")
            risk = "irreversible"
        elif op.kind == "add_column":
            schema_impact.append(f"drop_column: {op.args[0]}.{op.args[1]}")
            risk = _max_risk(risk, "high")
        elif op.kind == "drop_column":
            schema_impact.append(f"restore_column: {op.args[0]}.{op.args[1]}")
            risk = "irreversible"
        elif op.kind == "alter_column":
            schema_impact.append(f"restore_column_type: {op.args[0]}.{op.args[1]}")
            risk = _max_risk(risk, "medium")
        elif op.kind == "create_index":
            schema_impact.append(f"drop_index: {op.args[0]}")
            risk = _max_risk(risk, "low")
        elif op.kind == "drop_index":
            schema_impact.append(f"restore_index: {op.args[0]}")
            risk = _max_risk(risk, "low")
        elif op.kind in ("create_unique_constraint", "create_check_constraint",
                          "create_foreign_key"):
            schema_impact.append(f"drop_constraint: {op.args[0]}")
            risk = _max_risk(risk, "low")
        elif op.kind == "drop_constraint":
            schema_impact.append(f"restore_constraint: {op.args[0]}")
            risk = _max_risk(risk, "low")
        elif op.kind in ("execute", "run_python"):
            requires_app = True
            risk = _max_risk(risk, "high")
    reason = None
    if risk in ("medium", "high", "irreversible"):
        reason = _risk_reason(ops_for_upgrade)
    cross_refs = _derive_cross_references(schema_impact, metadata) if metadata is not None else []
    return DownHints(
        schema_impact=schema_impact,
        cross_references=cross_refs,
        data_loss_risk=risk,
        data_loss_reason=reason,
        requires_app_knowledge=requires_app,
        completion_status="stub",
    )


def _derive_cross_references(
    schema_impact: list[str], metadata: "MetaData"
) -> list[str]:
    """Walk the FK / index graph for cross-references touching ``schema_impact``.

    Output strings follow §11.2 verbiage:

    - ``foreign_key: <fk_name> -> <table>.<col>``
    - ``index: <ix_name> -> <table>.<col,col>``
    """
    drops_table: list[str] = []
    drops_column: list[tuple[str, str]] = []
    for entry in schema_impact:
        if entry.startswith("drop_table: "):
            drops_table.append(entry[len("drop_table: "):])
        elif entry.startswith("drop_column: "):
            tail = entry[len("drop_column: "):]
            if "." in tail:
                t, c = tail.split(".", 1)
                drops_column.append((t, c))

    if not drops_table and not drops_column:
        return []

    refs: list[str] = []
    for tbl in metadata.sorted_tables:
        for fk in tbl.foreign_key_constraints:
            referred = (
                fk.referred_table.name if fk.referred_table is not None else None
            )
            if referred is None:
                continue
            referred_cols = [el.column.name for el in fk.elements]
            fk_name = fk.name or f"<unnamed fk on {tbl.name}>"
            if referred in drops_table:
                for col_name in referred_cols:
                    refs.append(f"foreign_key: {fk_name} -> {referred}.{col_name}")
                continue
            for dt, dc in drops_column:
                if referred == dt and dc in referred_cols:
                    refs.append(f"foreign_key: {fk_name} -> {referred}.{dc}")
        # Indexes that include any dropped column on a non-dropped table.
        if tbl.name in drops_table:
            continue
        for ix in tbl.indexes:
            if ix.name is None:
                continue
            ix_cols = [c.name for c in ix.columns]
            for dt, dc in drops_column:
                if tbl.name == dt and dc in ix_cols:
                    refs.append(
                        f"index: {ix.name} -> {tbl.name}.{','.join(ix_cols)}"
                    )
    # De-dupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for r in refs:
        if r in seen:
            continue
        seen.add(r)
        deduped.append(r)
    return deduped


_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "irreversible": 4}


def _max_risk(a: str, b: str) -> str:
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b


def _risk_reason(ops: list[OperationSpec]) -> str:
    parts: list[str] = []
    for op in ops:
        if op.kind == "drop_table":
            parts.append(f"dropping table {op.args[0]} loses its data")
        elif op.kind == "drop_column":
            parts.append(f"dropping column {op.args[0]}.{op.args[1]} loses its data")
        elif op.kind == "run_python":
            parts.append("run_python is not mechanically reversible")
        elif op.kind == "execute":
            parts.append("raw SQL is opaque to autogen")
    return "; ".join(parts) if parts else "schema change may not be reversible without data backups"


# ---- generate_diff (entry point) ------------------------------------------


def generate_diff(
    slug: str,
    *,
    target: "MetaData",
    against: Literal["db", "replay"] = "db",
    url: str | None = None,
    migrations_dir: str | Path = "migrations",
    dialect: str = "sqlite",
) -> Path:
    """Generate a migration file by diffing ``target`` against ``against``."""
    migrations_dir = Path(migrations_dir)
    if against == "db":
        if url is None:
            raise ValueError("generate_diff(against='db', ...) requires url=")
        engine = sa.create_engine(url)
        try:
            current = _reflect_to_virtual_schema(engine)
            dialect = engine.dialect.name
        finally:
            engine.dispose()
    elif against == "replay":
        from .loader import load_migrations
        loaded = load_migrations(migrations_dir)
        current = replay_migrations(loaded.values(), dialect=dialect)
    else:
        raise ValueError(f"invalid against={against!r}")

    ops = diff_schemas(current, target, dialect=dialect)
    file_path = _allocate_file(slug, migrations_dir)
    body = render_migration(
        slug, ops, dialect=dialect, migration_id=file_path.stem, metadata=target,
    )
    file_path.write_text(body)
    return file_path


def _reflect_to_virtual_schema(engine: sa.Engine) -> VirtualSchema:
    md = sa.MetaData()
    md.reflect(bind=engine)
    return metadata_to_virtual_schema(md)


def metadata_to_virtual_schema(md: "MetaData") -> VirtualSchema:
    """Convert a SQLAlchemy :class:`MetaData` into a :class:`VirtualSchema`.

    Used both by ``--against=db`` (after reflection) and by tests that want a
    quick fixture.
    """
    schema = VirtualSchema()
    for tbl in md.sorted_tables:
        cols: dict[str, VColumn] = {}
        for c in tbl.columns:
            cols[c.name] = VColumn(
                name=c.name,
                type_spec=str(c.type),
                nullable=bool(c.nullable),
                primary_key=bool(c.primary_key),
                server_default=str(c.server_default.arg) if c.server_default is not None else None,
                unique=bool(c.unique) if c.unique is not None else False,
            )
        vt = VTable(name=tbl.name, columns=cols)
        for ix in tbl.indexes:
            if ix.name is None:
                continue
            vt.indexes[ix.name] = VIndex(
                name=ix.name,
                table=tbl.name,
                columns=[c.name for c in ix.columns],
                unique=bool(ix.unique),
            )
        for c in tbl.constraints:
            cname = getattr(c, "name", None)
            if not cname:
                continue
            kind = "check"
            payload: dict[str, Any] = {}
            if c.__class__.__name__ == "UniqueConstraint":
                kind = "unique"
                payload = {"columns": [col.name for col in c.columns]}
            elif c.__class__.__name__ == "CheckConstraint":
                kind = "check"
                payload = {"expression": str(c.sqltext)}
            elif c.__class__.__name__ == "ForeignKeyConstraint":
                kind = "fk"
                payload = {
                    "local_columns": [col.name for col in c.columns],
                    "referred_table": c.referred_table.name if c.referred_table is not None else None,
                    "referred_columns": [el.column.name for el in c.elements],
                }
            else:
                continue
            vt.constraints[cname] = VConstraint(
                name=cname, table=tbl.name, kind=kind, payload=payload,
            )
        schema.tables[tbl.name] = vt
    return schema


def _allocate_file(slug: str, migrations_dir: Path) -> Path:
    migrations_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = _slugify(slug)
    path = migrations_dir / f"{ts}_{safe}.py"
    suffix = 2
    while path.exists():
        path = migrations_dir / f"{ts}_{safe}_{suffix}.py"
        suffix += 1
    return path


def _slugify(slug: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_")[:60] or "migration"


__all__ = [
    "OperationSpec",
    "diff_schemas",
    "emit_down_hints",
    "generate_diff",
    "metadata_to_virtual_schema",
    "render_migration",
]
