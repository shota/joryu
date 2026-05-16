"""``joryu explain <id>`` implementation (§16, §5).

Renders a migration's Operations as natural-language prose so an AI tool
(or a human reviewer in a hurry) can understand what the migration will do
without reading the Python source. One sentence per op, plus a header line
that captures the docstring and any non-default migration metadata
(``depends_on`` / ``transaction_mode`` / ``dialects``).

The output is intentionally rule-based — no LLM is invoked at runtime.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["explain"]


def explain(migration_id: str, *, migrations_dir: Path = Path("migrations")) -> str:
    """Render ``migration_id`` as a multi-line English description."""
    from .loader import load_migrations
    from .registry import MIGRATIONS, register_operations

    mdir = Path(migrations_dir)
    if mdir.exists() and mdir.is_dir():
        load_migrations(mdir)
    m = MIGRATIONS.get(migration_id)
    if m is None:
        raise KeyError(f"unknown migration id: {migration_id!r}")
    if not m.registered:
        register_operations(m)

    lines: list[str] = [_header(m)]
    for op in m.operations:
        lines.append(_sentence_for(op))
    if len(lines) == 1:
        lines.append("This migration declares no operations.")
    return "\n".join(lines)


# ---- Header ---------------------------------------------------------------


def _header(m: Any) -> str:
    doc = _doc(m.upgrade_fn)
    base = f"Migration {m.id}: {doc}"
    extras: list[str] = []
    if m.depends_on:
        extras.append(f"depends on {', '.join(m.depends_on)}")
    if m.transaction_mode != "per_step":
        extras.append(f"transaction_mode={m.transaction_mode}")
    if m.dialects:
        extras.append(f"dialects={list(m.dialects)}")
    if extras:
        return base + " [" + "; ".join(extras) + "]"
    return base


def _doc(fn: Any) -> str:
    doc = (getattr(fn, "__doc__", None) or "").strip()
    if not doc:
        return "(no description)"
    return doc.splitlines()[0].strip()


# ---- Per-op sentences -----------------------------------------------------


def _sentence_for(op: Any) -> str:
    kind = getattr(op, "kind", "")
    # Dispatch on kind to keep the function free of hard imports.
    handler = _HANDLERS.get(kind)
    if handler is not None:
        try:
            return handler(op)
        except Exception:
            # Fall through to the generic describer.
            pass
    desc = getattr(op, "describe", None)
    if callable(desc):
        return f"Performs {kind or 'an operation'}: {desc()}."
    return f"Performs {kind or 'an operation'}."


def _create_table(op: Any) -> str:
    parts: list[str] = []
    for c in op.columns:
        bits = [_type_kind(c.type)]
        if c.primary_key:
            bits.append("PK")
        if not c.nullable:
            bits.append("NOT NULL")
        if c.unique and not c.primary_key:
            bits.append("UNIQUE")
        default = c.server_default if c.server_default is not None else c.default
        if default is not None:
            bits.append(f"default {default}")
        parts.append(f"{c.name} ({', '.join(bits)})")
    cols = ", ".join(parts)
    return f"Creates table `{op.name}` with columns {cols}."


def _drop_table(op: Any) -> str:
    return f"Drops table `{op.name}` (loses its data)."


def _rename_table(op: Any) -> str:
    return f"Renames table `{op.old}` to `{op.new}`."


def _add_column(op: Any) -> str:
    c = op.column
    null = "nullable" if c.nullable else "non-nullable"
    extra = ""
    if c.server_default is not None or c.default is not None:
        d = c.server_default if c.server_default is not None else c.default
        extra = f" with default {d}"
    return f"Adds a {null} column `{op.table}.{c.name}` of type {_type_kind(c.type)}{extra}."


def _drop_column(op: Any) -> str:
    return f"Drops column `{op.table}.{op.name}` (loses its data)."


def _alter_column(op: Any) -> str:
    parts: list[str] = []
    if op.new_type is not None:
        parts.append(f"type {_type_kind(op.new_type)}")
    if op.new_nullable is not None:
        parts.append("NOT NULL" if not op.new_nullable else "NULL")
    sd = op.new_server_default
    if sd is not None and type(sd).__name__ != "_Unset":
        parts.append(f"server_default={sd!r}")
    detail = ", ".join(parts) or "no-op"
    return f"Alters column `{op.table}.{op.name}` ({detail})."


def _rename_column(op: Any) -> str:
    return f"Renames column `{op.table}.{op.old}` to `{op.new}`."


def _create_index(op: Any) -> str:
    unique = "unique " if op.unique else ""
    cols = ", ".join(op.columns)
    where = f" WHERE {op.where}" if op.where else ""
    return f"Creates a {unique}index `{op.name}` on `{op.table}({cols})`{where}."


def _drop_index(op: Any) -> str:
    if op.table:
        return f"Drops index `{op.name}` on `{op.table}`."
    return f"Drops index `{op.name}`."


def _create_unique(op: Any) -> str:
    cols = ", ".join(op.columns)
    return f"Adds a unique constraint `{op.name}` on `{op.table}({cols})`."


def _create_check(op: Any) -> str:
    return f"Adds a check constraint `{op.name}` on `{op.table}` ({op.condition})."


def _create_fk(op: Any) -> str:
    src = ", ".join(op.source_cols)
    ref = ", ".join(op.ref_cols)
    extras: list[str] = []
    if op.on_delete:
        extras.append(f"ON DELETE {op.on_delete}")
    if op.on_update:
        extras.append(f"ON UPDATE {op.on_update}")
    tail = (" " + " ".join(extras)) if extras else ""
    return (
        f"Adds a foreign key `{op.name}` from `{op.source_table}({src})` "
        f"to `{op.ref_table}({ref})`{tail}."
    )


def _drop_constraint(op: Any) -> str:
    return f"Drops constraint `{op.name}` on `{op.table}`."


def _execute(op: Any) -> str:
    payload = op.payload
    if isinstance(payload, dict):
        return (
            "Executes raw SQL (opaque to static analysis; per-dialect: "
            + ", ".join(sorted(payload.keys()))
            + ")."
        )
    return "Executes raw SQL (opaque to static analysis)."


def _run_python(op: Any) -> str:
    name = getattr(op.fn, "__name__", "fn")
    return f"Runs a Python data-migration callable `{name}` (resumable via checkpoint)."


def _step(op: Any) -> str:
    return f"Runs custom step `{op.name}` (resumable; may raise PauseStep/SkipStep)."


def _declare(op: Any) -> str:
    keys = sorted(op.payload.keys())
    return f"Declares a schema change for replay ({', '.join(keys)}); has no on-DB effect."


def _create_from_model(op: Any) -> str:
    return f"Creates table `{op._table.name}` from SQLAlchemy model (checkfirst)."


def _batch(op: Any) -> str:
    ops = ", ".join(c[0] for c in op.children) or "no children"
    return f"Rebuilds table `{op.table}` to apply batched changes ({ops}) [SQLite-only]."


def _type_kind(spec: Any) -> str:
    if spec is None:
        return "UNKNOWN"
    if isinstance(spec, str):
        return spec
    return getattr(spec, "kind", None) or repr(spec)


_HANDLERS = {
    "create_table": _create_table,
    "drop_table": _drop_table,
    "rename_table": _rename_table,
    "add_column": _add_column,
    "drop_column": _drop_column,
    "alter_column": _alter_column,
    "rename_column": _rename_column,
    "create_index": _create_index,
    "drop_index": _drop_index,
    "create_unique_constraint": _create_unique,
    "create_check_constraint": _create_check,
    "create_foreign_key": _create_fk,
    "drop_constraint": _drop_constraint,
    "execute": _execute,
    "run_python": _run_python,
    "step": _step,
    "declare_schema_change": _declare,
    "create_table_from_model": _create_from_model,
    "batch_rebuild": _batch,
}
