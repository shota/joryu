"""Alembic -> joryu importer (§19).

Implements "Phase 1 structural conversion" plus a lightweight handful of
Phase 2 / Phase 3 rewrites. The Alembic package is **not** required at
runtime: every input file is parsed with the standard-library :mod:`ast`
module and treated as plain text for source substitution.

Public surface::

    @dataclass
    class ImportReport:
        files_converted: int
        files_skipped: int
        todos: list[tuple[Path, str]]
        state_migrated: bool

    def import_alembic(
        *,
        alembic_dir: Path,
        output_dir: Path,
        migrate_state: bool = False,
        drop_alembic_table: bool = False,
        url: str | None = None,
    ) -> ImportReport: ...

Conversion summary (Phase 1, automatic):

* ``revision`` / ``down_revision`` module-level strings -> ``depends_on=[...]``.
* ``op.add_column("t", sa.Column("c", sa.Type(), ...))``
    -> ``op.add_column("t", "c", t.<Type>, ...)``.
* ``op.execute(text("..."))`` -> ``op.execute("...")``.
* Wrap ``upgrade()`` / ``downgrade()`` in ``@joryu.migration(id=...)`` /
  ``@joryu.downgrade``.
* Filename rewritten to ``<UTC ISO basic timestamp>_<slug>.py`` (§3.1).
* Original Alembic hex preserved in ``tags=["alembic:<hex>"]``.

Lightweight Phase 2 / Phase 3 (best-effort, leaves TODO comments):

* ``op.batch_alter_table("t") as batch`` -> ``with op.batch("t") as batch``.
* ``if op.get_bind().dialect.name == ...`` blocks are left intact, but a
  ``# JORYU-IMPORT-TODO: consider rewriting as op.execute({...})`` line is
  inserted above them.
* ``op.bulk_insert(...)`` is stubbed as
  ``op.run_python(lambda conn, dialect, checkpoint: None)`` with a TODO.

State handover (``migrate_state=True``):

* Open a SQLAlchemy engine on ``url``.
* Read ``SELECT version_num FROM alembic_version``.
* Insert rows into ``joryu_migrations`` for every alembic revision that maps
  to a converted joryu id, status ``"applied"``.
* If ``drop_alembic_table=True``, drop ``alembic_version`` afterwards.

Constraints (per CLAUDE.md):

* stdlib + ``sqlalchemy>=2.0`` only (alembic itself is not imported).
* Python 3.11+, English output.
"""
from __future__ import annotations

import ast
import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("joryu.importer.alembic")


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    """Summary of what :func:`import_alembic` did."""

    files_converted: int = 0
    files_skipped: int = 0
    todos: list[tuple[Path, str]] = field(default_factory=list)
    state_migrated: bool = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def import_alembic(
    *,
    alembic_dir: Path,
    output_dir: Path,
    migrate_state: bool = False,
    drop_alembic_table: bool = False,
    url: str | None = None,
) -> ImportReport:
    """Convert an Alembic migrations directory into joryu files.

    Parameters
    ----------
    alembic_dir:
        Path to the existing Alembic root (the one containing
        ``versions/``). Typically ``./alembic``.
    output_dir:
        Where to write converted joryu migration files.
    migrate_state:
        When ``True``, read ``alembic_version`` from the live DB pointed to by
        ``url`` and insert matching rows into ``joryu_migrations``.
    drop_alembic_table:
        When ``True``, drop ``alembic_version`` after a successful state
        migration. Implies ``migrate_state=True``.
    url:
        SQLAlchemy URL for state handover. Required when ``migrate_state``
        (or ``drop_alembic_table``) is set.

    Returns
    -------
    ImportReport
        Per-file conversion + TODO listing + state-migration flag.
    """
    alembic_dir = Path(alembic_dir)
    output_dir = Path(output_dir)
    versions_dir = alembic_dir / "versions"
    if not versions_dir.is_dir():
        raise FileNotFoundError(
            f"alembic versions directory not found: {versions_dir}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    report = ImportReport()

    # First pass: parse every file, capture (path, parsed_metadata, source).
    sources: list[_AlembicSource] = []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            parsed = _parse_alembic_file(path)
        except _UnconvertibleFile as exc:
            log.warning("skipping %s: %s", path, exc)
            report.files_skipped += 1
            report.todos.append((path, f"skipped: {exc}"))
            continue
        sources.append(parsed)

    if not sources:
        return report

    # Build a hex -> joryu-id map. We need this *before* writing depends_on
    # so child migrations can reference their parents by the new id.
    used_filenames: set[str] = set()
    for src in sources:
        src.joryu_id = _build_joryu_id(src, used_filenames)
        used_filenames.add(src.joryu_id)

    hex_to_id = {src.revision: src.joryu_id for src in sources if src.revision}

    # Second pass: rewrite + emit.
    for src in sources:
        try:
            output = _emit_joryu_file(src, hex_to_id, report)
        except Exception as exc:
            log.exception("conversion failed for %s", src.path)
            report.files_skipped += 1
            report.todos.append((src.path, f"conversion failed: {exc}"))
            continue
        out_path = output_dir / f"{src.joryu_id}.py"
        out_path.write_text(output)
        report.files_converted += 1

    # State handover.
    if migrate_state or drop_alembic_table:
        if not url:
            raise ValueError(
                "migrate_state / drop_alembic_table require url=<sqlalchemy url>"
            )
        report.state_migrated = _migrate_state(
            url=url,
            hex_to_id=hex_to_id,
            drop_alembic_table=drop_alembic_table,
        )

    return report


# ---------------------------------------------------------------------------
# Phase 1 — parse
# ---------------------------------------------------------------------------


class _UnconvertibleFile(Exception):
    """Raised when an Alembic file lacks the minimum we can convert."""


@dataclass
class _AlembicSource:
    path: Path
    source: str
    revision: str | None
    down_revision: list[str]
    docstring: str | None
    has_upgrade: bool
    has_downgrade: bool
    mtime: float
    joryu_id: str = ""


def _parse_alembic_file(path: Path) -> _AlembicSource:
    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise _UnconvertibleFile(f"syntax error: {exc}") from exc

    docstring = ast.get_docstring(tree)
    revision: str | None = None
    down_revision: list[str] = []
    has_upgrade = False
    has_downgrade = False

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                if target.id == "revision":
                    revision = _coerce_str(node.value)
                elif target.id == "down_revision":
                    down_revision = _coerce_str_list(node.value)
        elif isinstance(node, ast.FunctionDef):
            if node.name == "upgrade":
                has_upgrade = True
            elif node.name == "downgrade":
                has_downgrade = True

    if not has_upgrade:
        raise _UnconvertibleFile("no upgrade() function")
    if revision is None:
        raise _UnconvertibleFile("no `revision` module-level assignment")

    return _AlembicSource(
        path=path,
        source=source,
        revision=revision,
        down_revision=down_revision,
        docstring=docstring,
        has_upgrade=has_upgrade,
        has_downgrade=has_downgrade,
        mtime=path.stat().st_mtime,
    )


def _coerce_str(value: ast.AST) -> str | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.Constant) and value.value is None:
        return None
    return None


def _coerce_str_list(value: ast.AST) -> list[str]:
    """``down_revision`` may be a string, None, or a tuple/list of strings."""
    if isinstance(value, ast.Constant):
        if value.value is None:
            return []
        if isinstance(value.value, str):
            return [value.value]
        return []
    if isinstance(value, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
        return out
    return []


# ---------------------------------------------------------------------------
# Phase 1 — file naming
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _build_joryu_id(src: _AlembicSource, used: set[str]) -> str:
    timestamp = _dt.datetime.fromtimestamp(src.mtime, tz=_dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S"
    )
    slug = _derive_slug(src)
    base = f"{timestamp}_{slug}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _derive_slug(src: _AlembicSource) -> str:
    raw = src.docstring or src.path.stem
    # First sentence (up to "." or newline).
    raw = raw.strip()
    if not raw:
        raw = src.path.stem
    # Take everything up to the first newline / period.
    first = re.split(r"[\.\n]", raw, maxsplit=1)[0]
    slug = _SLUG_RE.sub("_", first.lower()).strip("_")
    if not slug:
        slug = _SLUG_RE.sub("_", src.path.stem.lower()).strip("_") or "migration"
    return slug[:60]


# ---------------------------------------------------------------------------
# Phase 1 — emit
# ---------------------------------------------------------------------------


_HEADER = '''\
"""{docstring}

Imported from Alembic revision {revision} (originally {orig_filename}).
"""
from __future__ import annotations

import joryu
from joryu import op, types as t
'''


def _emit_joryu_file(
    src: _AlembicSource,
    hex_to_id: dict[str, str],
    report: ImportReport,
) -> str:
    doc = (src.docstring or src.path.stem).splitlines()[0] if (src.docstring or src.path.stem) else "Imported migration."
    header = _HEADER.format(
        docstring=doc.replace('"""', "'''"),
        revision=src.revision,
        orig_filename=src.path.name,
    )

    depends_on = [hex_to_id[h] for h in src.down_revision if h in hex_to_id]
    unresolved = [h for h in src.down_revision if h not in hex_to_id]
    if unresolved:
        report.todos.append(
            (
                src.path,
                f"unresolved down_revision references: {unresolved!r} "
                f"(originals not found in versions/)",
            )
        )

    upgrade_body = _extract_function_body(src.source, "upgrade")
    downgrade_body = (
        _extract_function_body(src.source, "downgrade") if src.has_downgrade else None
    )

    # Apply text rewrites to the function bodies.
    upgrade_body = _rewrite_body(upgrade_body, src.path, report)
    if downgrade_body is not None:
        downgrade_body = _rewrite_body(downgrade_body, src.path, report)

    out = [header, ""]
    decorator_lines = [
        "@joryu.migration(",
        f"    id={src.joryu_id!r},",
        f"    depends_on={depends_on!r},",
        f'    tags=["alembic:{src.revision}"],',
        ")",
    ]
    out.extend(decorator_lines)
    out.append("def upgrade() -> None:")
    out.append(_indent(upgrade_body, "    "))
    out.append("")

    if downgrade_body is not None:
        out.append("@joryu.downgrade")
        out.append("def downgrade() -> None:")
        out.append("    # JORYU-DOWN-HINT: completion-status: imported-from-alembic")
        out.append(_indent(downgrade_body, "    "))
        out.append("")

    return "\n".join(out)


def _extract_function_body(source: str, name: str) -> str:
    """Return the source text of ``def name():`` body, dedented.

    Falls back to ``pass`` when the function is empty or cannot be located.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            if not node.body:
                return "pass"
            # Use ast.unparse for portability (3.9+); preserves rewriting room.
            body_segments: list[str] = []
            for stmt in node.body:
                # Try to preserve the original source range when available.
                seg = ast.get_source_segment(source, stmt)
                if seg is None:
                    seg = ast.unparse(stmt)
                body_segments.append(seg)
            body = "\n".join(body_segments)
            # Drop a sole docstring-only body to avoid double doc.
            if (
                len(node.body) == 1
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                return "pass"
            return body
    return "pass"


def _indent(text: str, prefix: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(prefix + line if line.strip() else line for line in lines)


# ---------------------------------------------------------------------------
# Phase 1 / 2 / 3 — text rewrites
# ---------------------------------------------------------------------------


def _rewrite_body(body: str, src_path: Path, report: ImportReport) -> str:
    """Apply the documented rewrites to a function body in text form."""
    body = _rewrite_add_column(body)
    body = _rewrite_execute_text(body)
    body = _rewrite_batch_alter_table(body)
    body, bulk_insert_hits = _rewrite_bulk_insert(body)
    body, dialect_branch_hits = _annotate_dialect_branches(body)
    body, type_unknown_hits = _annotate_unknown_types(body)
    if bulk_insert_hits:
        report.todos.append(
            (src_path, f"{bulk_insert_hits} op.bulk_insert(...) call(s) stubbed; rewrite as op.run_python")
        )
    if dialect_branch_hits:
        report.todos.append(
            (src_path, f"{dialect_branch_hits} dialect-branching `if op.get_bind()` block(s) left intact")
        )
    if type_unknown_hits:
        report.todos.append(
            (src_path, f"{type_unknown_hits} unrecognised SQLAlchemy type(s) preserved as-is")
        )
    return body


# Mapping of common ``sa.<Type>`` names -> ``t.<JoryuType>`` references.
# The joryu type module is filled in by another sub-agent; we map only the
# widely-used types and leave the rest as TODO.
_SA_TO_JORYU_TYPE = {
    "Integer": "t.Int",
    "SmallInteger": "t.SmallInt",
    "BigInteger": "t.BigInt",
    "Float": "t.Float",
    "Numeric": "t.Decimal",
    "Boolean": "t.Bool",
    "String": "t.String",
    "Unicode": "t.String",
    "Text": "t.Text",
    "UnicodeText": "t.Text",
    "LargeBinary": "t.Binary",
    "Binary": "t.Binary",
    "Date": "t.Date",
    "Time": "t.Time",
    "DateTime": "t.Timestamp",
    "TIMESTAMP": "t.Timestamp",
    "Interval": "t.Interval",
    "JSON": "t.Json",
    "JSONB": "t.Json",
    "UUID": "t.Uuid",
    "Enum": "t.Enum",
}


def _rewrite_add_column(body: str) -> str:
    """Rewrite ``op.add_column("t", sa.Column("c", sa.X(), ...))``.

    Walks the body as an expression list — we re-parse the body inside a
    synthetic ``def __body__()`` wrapper to use the AST.
    """
    try:
        tree = ast.parse(body)
    except SyntaxError:
        # Body is fragments; can't rewrite. Return as-is.
        return body

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_attr_call(node.func, "op", "add_column"):
            continue
        # Expect: op.add_column(<table_expr>, sa.Column(<name>, <type>, ...))
        if len(node.args) < 2:
            continue
        col_call = node.args[1]
        if not isinstance(col_call, ast.Call):
            continue
        if not _is_sa_column(col_call.func):
            continue
        # Need a name + type.
        if len(col_call.args) < 2:
            continue
        name_arg = col_call.args[0]
        type_arg = col_call.args[1]
        if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
            continue
        type_src = _render_type(type_arg)
        if type_src is None:
            continue
        table_src = ast.unparse(node.args[0])
        name_src = repr(name_arg.value)
        extra_args = [ast.unparse(a) for a in col_call.args[2:]]
        extra_kwargs = [f"{kw.arg}={ast.unparse(kw.value)}" for kw in col_call.keywords if kw.arg]
        outer_kwargs = [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
        all_extras = extra_args + extra_kwargs + outer_kwargs
        joined = ", ".join([table_src, name_src, type_src, *all_extras])
        new_text = f"op.add_column({joined})"
        # Splice via str.replace on the original source slice; the AST tree
        # itself becomes stale after the first replacement, but each iteration
        # re-checks via `original is None`.
        original = ast.get_source_segment(body, node)
        if original is None:
            continue
        body = body.replace(original, new_text, 1)
    return body


def _is_attr_call(func: ast.AST, owner: str, name: str) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == name
        and isinstance(func.value, ast.Name)
        and func.value.id == owner
    )


def _is_sa_column(func: ast.AST) -> bool:
    """Match ``sa.Column``, ``sqlalchemy.Column``, or bare ``Column``."""
    if isinstance(func, ast.Name) and func.id == "Column":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "Column":
        return True
    return False


def _render_type(node: ast.AST) -> str | None:
    """Translate a SQLAlchemy type expression into the joryu equivalent.

    Returns ``None`` if the type is unrecognised; the caller leaves the
    original code in place.
    """
    # ``sa.Integer()`` / ``sa.String(255)`` / ``Integer`` (bare).
    if isinstance(node, ast.Call):
        func = node.func
        type_name = _type_name(func)
        if type_name is None or type_name not in _SA_TO_JORYU_TYPE:
            return None
        target = _SA_TO_JORYU_TYPE[type_name]
        if not node.args and not node.keywords:
            return target
        # Pass through positional args (e.g. String(255)) and known kwargs.
        args = [ast.unparse(a) for a in node.args]
        kwargs = [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
        return f"{target}({', '.join(args + kwargs)})"
    # Bare ``sa.Integer`` (no call) is unusual but legal.
    type_name = _type_name(node)
    if type_name is None or type_name not in _SA_TO_JORYU_TYPE:
        return None
    return _SA_TO_JORYU_TYPE[type_name]


def _type_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_TEXT_CALL_RE = re.compile(r"op\.execute\(\s*text\(\s*(?P<lit>(?:r?[bu]?['\"]).*?['\"])\s*\)\s*\)", re.DOTALL)


def _rewrite_execute_text(body: str) -> str:
    """``op.execute(text("..."))`` -> ``op.execute("...")``."""
    def _sub(match: re.Match[str]) -> str:
        literal = match.group("lit")
        return f"op.execute({literal})"

    return _TEXT_CALL_RE.sub(_sub, body)


_BATCH_RE = re.compile(
    r"\bwith\s+op\.batch_alter_table\(\s*(?P<arg>[^)]+)\)\s+as\s+(?P<bind>\w+)",
)


def _rewrite_batch_alter_table(body: str) -> str:
    """``with op.batch_alter_table("t") as batch`` -> ``with op.batch("t") as batch``."""
    return _BATCH_RE.sub(r"with op.batch(\g<arg>) as \g<bind>", body)


_BULK_INSERT_RE = re.compile(r"^(?P<indent>[ \t]*)op\.bulk_insert\([^)]*\)\s*$", re.MULTILINE)
_BULK_INSERT_MULTILINE_RE = re.compile(r"op\.bulk_insert\(")


def _rewrite_bulk_insert(body: str) -> tuple[str, int]:
    """Replace ``op.bulk_insert(...)`` with a stub + TODO comment."""
    hits = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        indent = match.group("indent")
        return (
            f"{indent}# JORYU-IMPORT-TODO: rewrite as op.run_python\n"
            f"{indent}op.run_python(lambda conn, dialect, checkpoint: None)"
        )

    body = _BULK_INSERT_RE.sub(_sub, body)

    # Multi-line bulk_insert: leave intact, just count + warn via TODO comment.
    if hits == 0 and _BULK_INSERT_MULTILINE_RE.search(body):
        # Count parenthesised forms with a naive scan.
        for m in _BULK_INSERT_MULTILINE_RE.finditer(body):
            hits += 1
        body = (
            "# JORYU-IMPORT-TODO: rewrite op.bulk_insert(...) below as op.run_python\n"
            + body
        )
    return body, hits


_DIALECT_IF_RE = re.compile(
    r"^(?P<indent>[ \t]*)if\s+op\.get_bind\(\)\.dialect\.name\s*==.*:$",
    re.MULTILINE,
)


def _annotate_dialect_branches(body: str) -> tuple[str, int]:
    hits = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        indent = match.group("indent")
        return (
            f"{indent}# JORYU-IMPORT-TODO: consider rewriting as op.execute({{...}})\n"
            f"{match.group(0)}"
        )

    body = _DIALECT_IF_RE.sub(_sub, body)
    return body, hits


_SA_CALL_RE = re.compile(r"\bsa\.(\w+)\s*\(")


def _annotate_unknown_types(body: str) -> tuple[str, int]:
    """Flag ``sa.<UnknownType>`` references the importer didn't rewrite."""
    seen: set[str] = set()
    for m in _SA_CALL_RE.finditer(body):
        name = m.group(1)
        if name in _SA_TO_JORYU_TYPE:
            continue
        if name in {"Column", "ForeignKey", "ForeignKeyConstraint", "Index", "UniqueConstraint", "CheckConstraint", "PrimaryKeyConstraint", "text"}:
            # Common helpers, not types.
            continue
        seen.add(name)
    if not seen:
        return body, 0
    todo = "# JORYU-IMPORT-TODO: unrecognised types preserved as-is: " + ", ".join(sorted(seen))
    return f"{todo}\n{body}", len(seen)


# ---------------------------------------------------------------------------
# §19.2 — state handover
# ---------------------------------------------------------------------------


def _migrate_state(
    *,
    url: str,
    hex_to_id: dict[str, str],
    drop_alembic_table: bool,
) -> bool:
    """Read ``alembic_version`` and INSERT joryu_migrations rows."""
    from sqlalchemy import create_engine, text

    from .. import state as state_module

    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            with conn.begin():
                state_module.ensure_state_tables(conn)
            # Read alembic_version. Be defensive: the table may not exist.
            try:
                rows = list(conn.execute(text("SELECT version_num FROM alembic_version")))
            except Exception as exc:
                log.warning("could not read alembic_version: %s", exc)
                return False
            applied_hexes = [r[0] for r in rows]
            existing_ids = {
                r["id"] for r in state_module.list_migration_rows(conn)
            }
            # Close the autobegun read txn before opening a write txn below.
            if conn.in_transaction():
                conn.commit()
            inserted = 0
            with conn.begin():
                for hex_rev in applied_hexes:
                    j_id = hex_to_id.get(hex_rev)
                    if j_id is None:
                        log.warning(
                            "alembic_version rev %s has no matching joryu id; skipping",
                            hex_rev,
                        )
                        continue
                    if j_id in existing_ids:
                        continue
                    # Use a placeholder checksum: the user will run ``joryu
                    # repair`` (or the first re-apply will compute the real one).
                    state_module.insert_migration(
                        conn,
                        j_id,
                        checksum="imported-from-alembic",
                        status="applied",
                        dialect=conn.dialect.name,
                        joryu_version="imported",
                    )
                    inserted += 1
            if drop_alembic_table:
                with conn.begin():
                    conn.execute(text("DROP TABLE alembic_version"))
            return inserted > 0 or not applied_hexes
    finally:
        engine.dispose()


__all__ = ["ImportReport", "import_alembic"]
