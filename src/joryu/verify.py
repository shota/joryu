"""verify: static conflict detection across pending migrations (§7.2).

This module implements `joryu verify` semantics. Given a set of registered
migrations, it returns a list of :class:`Conflict` objects describing every
non-commutative ordered op pair across distinct migrations.

The rules (mirror of §7.2):

* Commutative (silent, no Conflict):
    - add_column(t, A) + add_column(t, B)            where A != B
    - add_column(t, A) + alter_column(t, B)          where A != B
    - any change to t1 + any change to t2            where t1 != t2
    - either side is opaque (op.execute(raw), op.run_python, op.step)

* Non-commutative (emit a single Conflict, priority order):
    table_drop > table_rename > add_drop > column_rename > double_alter

Only one Conflict per ordered pair is emitted (highest-priority kind wins).
``verify`` never raises ``VerificationFailed`` — that is the caller's job
(typically the runner or CLI).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from .conflicts import Conflict, ConflictKind, OpRef

if TYPE_CHECKING:
    from .op_core import Operation
    from .registry import Migration


# Priority order, highest first. The first kind that matches an ordered pair
# is the one we emit; later kinds are discarded for that same pair.
_KIND_PRIORITY: tuple[ConflictKind, ...] = (
    "table_drop",
    "table_rename",
    "add_drop",
    "column_rename",
    "double_alter",
)


def format_target(target: tuple[str, ...]) -> str:
    """Render a target tuple for human messages.

    ``("users", "email")`` -> ``"users.email"``
    ``("users",)``         -> ``"users"``
    ``()``                 -> ``"?"``
    """
    if not target:
        return "?"
    return ".".join(target)


def verify(
    migrations_dir: Path | str | None = None,
    *,
    registry: dict[str, "Migration"] | None = None,
) -> list[Conflict]:
    """Run static conflict detection over the supplied migrations.

    Parameters
    ----------
    migrations_dir:
        Optional path to a migrations directory. If given, migrations are
        loaded via :func:`joryu.loader.load_migrations`.
    registry:
        Optional explicit registry. If neither ``migrations_dir`` nor
        ``registry`` is given, the global ``MIGRATIONS`` dict is used.

    Returns
    -------
    list[Conflict]
        Empty when no conflicts are detected.
    """
    from .registry import MIGRATIONS, register_operations

    if migrations_dir is not None:
        try:
            from .loader import load_migrations  # type: ignore
        except ImportError:
            # Loader sub-agent hasn't shipped yet; fall back to the global
            # registry so callers can still verify what's been imported.
            migrations = dict(MIGRATIONS)
        else:
            loaded = load_migrations(Path(migrations_dir))
            # Accept either dict[str, Migration] or list[Migration].
            if isinstance(loaded, dict):
                migrations = dict(loaded)
            else:
                migrations = {m.id: m for m in loaded}
    elif registry is not None:
        migrations = dict(registry)
    else:
        migrations = dict(MIGRATIONS)

    # Ensure each migration is registered (idempotent — registering twice
    # repopulates the ops list because _RegistrationScope clears it).
    for m in migrations.values():
        if not m.registered:
            register_operations(m)

    # Flatten to (migration_id, step_index, op) triples and sort by the
    # canonical ordering: lexicographic on migration_id, numeric on step_index.
    flat: list[tuple[str, int, "Operation"]] = []
    for mid, m in migrations.items():
        for idx, op in enumerate(m.operations):
            flat.append((mid, idx, op))
    flat.sort(key=lambda t: (t[0], t[1]))

    seen: set[tuple[tuple[str, int], tuple[str, int]]] = set()
    conflicts: list[Conflict] = []

    for i in range(len(flat)):
        mid_a, idx_a, op_a = flat[i]
        targets_a = op_a.targets()
        a_opaque = not targets_a
        for j in range(i + 1, len(flat)):
            mid_b, idx_b, op_b = flat[j]
            if mid_a == mid_b:
                # Conflicts only apply across distinct migrations.
                continue
            if _dialects_disjoint(migrations[mid_a], migrations[mid_b]):
                # §7.2 / §6.1 Layer 3: two migrations that target disjoint
                # dialect sets never co-execute on the same DB, so any
                # apparent conflict between them is a false positive.
                continue
            targets_b = op_b.targets()
            if a_opaque or not targets_b:
                # Opaque pairs are silent (human-review responsibility).
                continue

            kind = _classify_pair(op_a, targets_a, op_b, targets_b)
            if kind is None:
                continue

            left_ref_key = (mid_a, idx_a)
            right_ref_key = (mid_b, idx_b)
            pair_key = (left_ref_key, right_ref_key)
            if pair_key in seen:
                continue
            seen.add(pair_key)

            # Find the specific targets that triggered the conflict so the
            # OpRef carries something meaningful (the spec says ("t",) for a
            # table op, ("t","c") for a column op — we want the actual target
            # the rule matched on, not a wildcard).
            target_a, target_b = _select_targets_for_kind(
                kind, op_a, targets_a, op_b, targets_b
            )

            left = OpRef(
                migration_id=mid_a,
                step_index=idx_a,
                op_kind=op_a.kind,
                target=target_a,
                source_line=op_a.source_line,
            )
            right = OpRef(
                migration_id=mid_b,
                step_index=idx_b,
                op_kind=op_b.kind,
                target=target_b,
                source_line=op_b.source_line,
            )
            message = (
                f"{op_a.kind}({format_target(target_a)}) in {mid_a} "
                f"conflicts with "
                f"{op_b.kind}({format_target(target_b)}) in {mid_b}"
            )
            conflicts.append(Conflict(kind=kind, left=left, right=right, message=message))

    return conflicts


# ---------------------------------------------------------------------------
# Pair classification
# ---------------------------------------------------------------------------


def _classify_pair(
    op_a: "Operation",
    targets_a: list[tuple[str, ...]],
    op_b: "Operation",
    targets_b: list[tuple[str, ...]],
) -> ConflictKind | None:
    """Return the highest-priority ConflictKind for the ordered pair, or None."""

    tables_a = {t[0] for t in targets_a if t}
    tables_b = {t[0] for t in targets_b if t}
    shared_tables = tables_a & tables_b
    if not shared_tables:
        # Different tables — fully commutative.
        return None

    kind_a = op_a.kind
    kind_b = op_b.kind

    # Walk the priority list and emit the first kind that matches.
    for kind in _KIND_PRIORITY:
        if _matches_kind(kind, kind_a, targets_a, kind_b, targets_b, shared_tables):
            return kind
        if _matches_kind(kind, kind_b, targets_b, kind_a, targets_a, shared_tables):
            # The rule may be expressed in only one direction; check the
            # reverse too so e.g. drop_table on the right is still caught.
            return kind

    # All other same-table combinations are commutative (add+add, add+alter
    # on different columns, etc.).
    if {kind_a, kind_b} <= {"add_column", "alter_column"}:
        # Need to verify they don't touch the same column.
        cols_a = {t for t in targets_a if len(t) >= 2}
        cols_b = {t for t in targets_b if len(t) >= 2}
        if cols_a & cols_b:
            # add_column + add_column on the same column would be a logical
            # error, but ensure-style ops make this safe at runtime. The spec
            # only lists add+drop, double_alter, rename, etc. as conflicts,
            # so leave it as silent (commutative).
            return None
        return None

    return None


def _matches_kind(
    kind: ConflictKind,
    k_left: str,
    targets_left: list[tuple[str, ...]],
    k_right: str,
    targets_right: list[tuple[str, ...]],
    shared_tables: set[str],
) -> bool:
    """Does the ordered pair (left, right) match this conflict kind?"""

    if kind == "table_drop":
        # right side drops a table that left touches
        if k_right != "drop_table":
            return False
        right_tables = {t[0] for t in targets_right if t}
        return bool(right_tables & shared_tables)

    if kind == "table_rename":
        if k_right != "rename_table":
            return False
        right_tables = {t[0] for t in targets_right if t}
        return bool(right_tables & shared_tables)

    if kind == "add_drop":
        if k_left != "add_column" or k_right != "drop_column":
            return False
        cols_left = {t for t in targets_left if len(t) >= 2}
        cols_right = {t for t in targets_right if len(t) >= 2}
        return bool(cols_left & cols_right)

    if kind == "column_rename":
        if k_right != "rename_column":
            return False
        # Left must touch a column that right is renaming.
        cols_left = {t for t in targets_left if len(t) >= 2}
        cols_right = {t for t in targets_right if len(t) >= 2}
        return bool(cols_left & cols_right)

    if kind == "double_alter":
        if k_left != "alter_column" or k_right != "alter_column":
            return False
        cols_left = {t for t in targets_left if len(t) >= 2}
        cols_right = {t for t in targets_right if len(t) >= 2}
        return bool(cols_left & cols_right)

    return False


def _select_targets_for_kind(
    kind: ConflictKind,
    op_a: "Operation",
    targets_a: list[tuple[str, ...]],
    op_b: "Operation",
    targets_b: list[tuple[str, ...]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pick the most informative target tuple for each side of the OpRef."""

    # Default: first declared target on each side.
    fallback_a = targets_a[0] if targets_a else ()
    fallback_b = targets_b[0] if targets_b else ()

    if kind in ("table_drop", "table_rename"):
        # Prefer a table-level tuple for both sides.
        ta = _first(targets_a, lambda t: len(t) == 1) or fallback_a
        tb = _first(targets_b, lambda t: len(t) == 1) or fallback_b
        # Surface the matching shared table when possible.
        tables_a = {t[0] for t in targets_a if t}
        tables_b = {t[0] for t in targets_b if t}
        shared = tables_a & tables_b
        if shared:
            picked = sorted(shared)[0]
            return (picked,), (picked,)
        return ta, tb

    # Column-scoped kinds: prefer the shared (table, column) tuple.
    cols_a = {t for t in targets_a if len(t) >= 2}
    cols_b = {t for t in targets_b if len(t) >= 2}
    shared_cols = cols_a & cols_b
    if shared_cols:
        picked = sorted(shared_cols)[0]
        return picked, picked
    return fallback_a, fallback_b


def _first(items: Iterable, predicate):
    for it in items:
        if predicate(it):
            return it
    return None


def _dialects_disjoint(a: "Migration", b: "Migration") -> bool:
    """Return True iff ``a.dialects`` and ``b.dialects`` cannot ever co-execute.

    A migration with ``dialects=None`` runs on every dialect, so it overlaps
    with anything. Only when *both* migrations restrict to explicit, disjoint
    dialect sets can we safely say they will never collide on a live DB.
    """
    da = a.dialects
    db = b.dialects
    if not da or not db:
        return False
    return not (set(da) & set(db))
