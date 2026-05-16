"""``joryu schema-snapshot`` implementation (§16).

Emits the current schema either as JSON (a stable, sorted document derived
from :class:`joryu.virtual_schema.VirtualSchema`) or as SQL (``CREATE`` DDL
statements). Two comparison sources mirror ``joryu generate`` (§8.2):

* ``against="db"`` — reflect the live database via
  :meth:`sqlalchemy.MetaData.reflect`.
* ``against="replay"`` — load migrations from ``migrations/`` and replay the
  Operations into a virtual schema (no DB required, CI-friendly).

The JSON shape is intentionally minimal and additive: keys absent because the
underlying schema element is empty (``"extensions": []`` etc.) are omitted at
the top level, but every present key has a stable ordering so the document
diffs cleanly across runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.schema import CreateIndex, CreateTable

from .virtual_schema import VirtualSchema, replay_migrations


__all__ = ["snapshot"]


def snapshot(
    *,
    url: str | None = None,
    migrations_dir: Path = Path("migrations"),
    against: Literal["db", "replay"] = "db",
    fmt: Literal["json", "sql"] = "json",
) -> str:
    """Return the serialized current schema.

    Contract:

    * ``against="db"`` requires ``url``; raises ``ValueError`` otherwise.
    * ``against="replay"`` reads from ``migrations_dir`` and never touches a DB.
    * ``fmt="json"`` returns a single line of compact, sorted-key JSON.
    * ``fmt="sql"`` returns one statement per line, each terminated with ``;``.
    """
    if against == "db":
        if url is None:
            raise ValueError("snapshot(against='db', ...) requires url=")
        engine = sa.create_engine(url)
        try:
            md = sa.MetaData()
            md.reflect(bind=engine)
            if fmt == "sql":
                return _render_metadata_sql(md, engine.dialect)
            from .autogen import metadata_to_virtual_schema
            schema = metadata_to_virtual_schema(md)
        finally:
            engine.dispose()
    elif against == "replay":
        from .loader import load_migrations
        loaded = load_migrations(Path(migrations_dir))
        schema = replay_migrations(loaded.values())
        if fmt == "sql":
            return _render_virtual_sql(schema)
    else:
        raise ValueError(f"invalid against={against!r}")

    if fmt == "json":
        return _render_json(schema)
    if fmt == "sql":
        return _render_virtual_sql(schema)
    raise ValueError(f"invalid fmt={fmt!r}")


# ---- JSON -----------------------------------------------------------------


def _render_json(schema: VirtualSchema) -> str:
    doc: dict[str, Any] = {}
    if schema.tables:
        doc["tables"] = {
            name: _table_to_json(t) for name, t in sorted(schema.tables.items())
        }
    if schema.extensions:
        doc["extensions"] = sorted(schema.extensions)
    if schema.enums:
        doc["enums"] = {name: list(vals) for name, vals in sorted(schema.enums.items())}
    if schema.views:
        doc["views"] = {name: sql for name, sql in sorted(schema.views.items())}
    if schema.materialized_views:
        doc["materialized_views"] = {
            name: sql for name, sql in sorted(schema.materialized_views.items())
        }
    if schema.triggers:
        doc["triggers"] = {
            name: {"table": tbl, "definition": defn}
            for name, (tbl, defn) in sorted(schema.triggers.items())
        }
    if schema.policies:
        doc["policies"] = {
            name: {"table": tbl, "definition": defn}
            for name, (tbl, defn) in sorted(schema.policies.items())
        }
    if schema.sequences:
        doc["sequences"] = sorted(schema.sequences)
    if schema.schemas:
        doc["schemas"] = sorted(schema.schemas)
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)


def _table_to_json(t: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "columns": {
            name: _column_to_json(c) for name, c in sorted(t.columns.items())
        },
    }
    if t.indexes:
        out["indexes"] = {
            name: {
                "table": ix.table,
                "columns": list(ix.columns),
                "unique": bool(ix.unique),
                **({"where": ix.where} if ix.where else {}),
            }
            for name, ix in sorted(t.indexes.items())
        }
    if t.constraints:
        out["constraints"] = {
            name: {"kind": c.kind, "payload": _coerce(c.payload)}
            for name, c in sorted(t.constraints.items())
        }
    return out


def _column_to_json(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": _type_to_str(c.type_spec),
        "nullable": bool(c.nullable),
    }
    if c.primary_key:
        out["primary_key"] = True
    if c.unique:
        out["unique"] = True
    if c.server_default is not None:
        out["server_default"] = _coerce(c.server_default)
    if c.comment:
        out["comment"] = c.comment
    if c.generated:
        out["generated"] = c.generated
    return out


def _type_to_str(spec: Any) -> str:
    if spec is None:
        return "UNKNOWN"
    if isinstance(spec, str):
        return spec
    # joryu TypeSpec carries a .kind; fall back to repr for opaque values.
    kind = getattr(spec, "kind", None)
    if kind:
        return kind
    return str(spec)


def _coerce(v: Any) -> Any:
    """Coerce arbitrary payload values into JSON-friendly primitives."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _coerce(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_coerce(x) for x in v]
    return str(v)


# ---- SQL ------------------------------------------------------------------


def _render_metadata_sql(md: sa.MetaData, dialect: Any) -> str:
    """Render a reflected MetaData via SQLAlchemy's DDL compilers."""
    lines: list[str] = []
    for tbl in md.sorted_tables:
        sql = str(CreateTable(tbl).compile(dialect=dialect)).strip()
        lines.append(_terminate(sql))
        for ix in sorted(tbl.indexes, key=lambda i: i.name or ""):
            if ix.name is None:
                continue
            ix_sql = str(CreateIndex(ix).compile(dialect=dialect)).strip()
            lines.append(_terminate(ix_sql))
    return "\n".join(lines)


def _render_virtual_sql(schema: VirtualSchema) -> str:
    """Hand-roll DDL from a :class:`VirtualSchema`.

    Used by ``against="replay"`` (where there is no SQLAlchemy MetaData to
    feed into ``CreateTable``) and for the non-table objects (extensions,
    enums, views) that have no first-class SQLAlchemy DDL element.
    """
    lines: list[str] = []
    for name in sorted(schema.extensions):
        lines.append(f"CREATE EXTENSION IF NOT EXISTS {_q(name)};")
    for name in sorted(schema.enums):
        labels = ", ".join("'" + lab.replace("'", "''") + "'" for lab in schema.enums[name])
        lines.append(f"CREATE TYPE {_q(name)} AS ENUM ({labels});")
    for name, table in sorted(schema.tables.items()):
        lines.append(_render_vtable_sql(table))
        for ix_name, ix in sorted(table.indexes.items()):
            unique = "UNIQUE " if ix.unique else ""
            cols = ", ".join(_q(c) for c in ix.columns)
            where = f" WHERE {ix.where}" if ix.where else ""
            lines.append(
                f"CREATE {unique}INDEX {_q(ix_name)} ON {_q(table.name)} ({cols}){where};"
            )
    for name, sql in sorted(schema.views.items()):
        lines.append(f"CREATE VIEW {_q(name)} AS {sql};")
    for name, sql in sorted(schema.materialized_views.items()):
        lines.append(f"CREATE MATERIALIZED VIEW {_q(name)} AS {sql};")
    return "\n".join(lines)


def _render_vtable_sql(table: Any) -> str:
    cols: list[str] = []
    for name, col in table.columns.items():
        parts = [_q(name), _type_to_str(col.type_spec)]
        if col.primary_key:
            parts.append("PRIMARY KEY")
        if not col.nullable:
            parts.append("NOT NULL")
        if col.unique and not col.primary_key:
            parts.append("UNIQUE")
        if col.server_default is not None:
            parts.append(f"DEFAULT {col.server_default}")
        if col.generated:
            parts.append(f"GENERATED ALWAYS AS ({col.generated}) STORED")
        cols.append(" ".join(parts))
    inner = ",\n  ".join(cols)
    return f"CREATE TABLE {_q(table.name)} (\n  {inner}\n);"


def _terminate(stmt: str) -> str:
    s = stmt.rstrip().rstrip(";")
    return s + ";"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
