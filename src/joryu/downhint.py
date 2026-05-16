"""Parser + emitter for the ``JORYU-DOWN-HINT:`` structured comments (§11.2).

These hints sit inside a migration's ``downgrade()`` body as a block of
comments. They are the contract between the generator, AI tools, and human
reviewers — so the field vocabulary is frozen in v1.

Round-trip guarantee: ``emit_hints(parse_hints(s)) == s`` up to whitespace
inside the recognised block.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HINT_PREFIX = "# JORYU-DOWN-HINT:"
_CONT_PREFIX = "#   - "

# Fields that always carry a list value (may appear inline as "[]" or as a
# multi-line continuation block).
_LIST_FIELDS = {
    "schema-impact",
    "cross-references",
    "order-constraint",
    "app-knowledge-needed",
    "manual-steps",
}

_BOOL_FIELDS = {"requires-app-knowledge"}

_VALID_RISKS = {"none", "low", "medium", "high", "irreversible"}
_VALID_STATUSES = {"stub", "partial", "complete", "manual-review-required"}


@dataclass
class DownHints:
    schema_impact: list[str] = field(default_factory=list)
    cross_references: list[str] = field(default_factory=list)
    data_loss_risk: str = "none"
    data_loss_reason: str | None = None
    order_constraint: list[str] = field(default_factory=list)
    requires_app_knowledge: bool = False
    app_knowledge_needed: list[str] = field(default_factory=list)
    completion_status: str = "stub"
    manual_steps: list[str] = field(default_factory=list)


# ---- parsing --------------------------------------------------------------


def parse_hints(source: str) -> DownHints:
    """Parse a ``downgrade()`` body (or any source) for JORYU-DOWN-HINT lines.

    Recognised forms:

    Inline scalar:
        # JORYU-DOWN-HINT: data-loss-risk: high

    Inline list (empty or single-line ``[]`` is supported):
        # JORYU-DOWN-HINT: cross-references: []

    Multi-line list (continuation bullets follow the header line):
        # JORYU-DOWN-HINT: schema-impact:
        #   - drop_table: users
        #   - drop_index: idx_users_email
    """
    hints = DownHints()
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped.startswith(_HINT_PREFIX):
            i += 1
            continue
        body = stripped[len(_HINT_PREFIX):].strip()
        # body is "<key>: <value>" or "<key>:" (multiline)
        m = re.match(r"^([a-z][a-z0-9-]*)\s*:\s*(.*)$", body)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2)
        if key in _LIST_FIELDS:
            collected: list[str]
            if value and value != "[]":
                # Treat inline value as a single-element list (rare but legal).
                collected = [value]
            elif value == "[]":
                collected = []
            else:
                collected = []
            # Consume continuation bullets.
            j = i + 1
            while j < len(lines):
                cont = lines[j].strip()
                if cont.startswith(_CONT_PREFIX):
                    collected.append(cont[len(_CONT_PREFIX):].strip())
                    j += 1
                    continue
                # A blank "# " line breaks the block.
                break
            _store_list(hints, key, collected)
            i = j
            continue
        # Scalar field.
        _store_scalar(hints, key, value)
        i += 1
    return hints


def _store_list(hints: DownHints, key: str, values: list[str]) -> None:
    attr = key.replace("-", "_")
    if hasattr(hints, attr):
        setattr(hints, attr, list(values))


def _store_scalar(hints: DownHints, key: str, value: str) -> None:
    attr = key.replace("-", "_")
    if key in _BOOL_FIELDS:
        setattr(hints, attr, value.strip().lower() in ("true", "yes", "1"))
        return
    if key == "data-loss-risk":
        v = value.strip().lower()
        if v in _VALID_RISKS:
            hints.data_loss_risk = v
        return
    if key == "completion-status":
        v = value.strip().lower()
        if v in _VALID_STATUSES:
            hints.completion_status = v
        return
    if key == "data-loss-reason":
        hints.data_loss_reason = value.strip() or None
        return
    # Unknown scalar fields are ignored (forward-compat).


# ---- emitting -------------------------------------------------------------


def emit_hints(hints: DownHints) -> str:
    """Render a DownHints record as a block of ``# JORYU-DOWN-HINT:`` lines.

    Field order matches §11.2 to keep diffs stable. Optional fields are
    omitted when empty / unset.
    """
    lines: list[str] = []
    _emit_list(lines, "schema-impact", hints.schema_impact)
    _emit_list(lines, "cross-references", hints.cross_references)
    lines.append(f"{_HINT_PREFIX} data-loss-risk: {hints.data_loss_risk}")
    if hints.data_loss_reason:
        lines.append(f"{_HINT_PREFIX} data-loss-reason: {hints.data_loss_reason}")
    if hints.order_constraint:
        _emit_list(lines, "order-constraint", hints.order_constraint)
    lines.append(
        f"{_HINT_PREFIX} requires-app-knowledge: "
        f"{'true' if hints.requires_app_knowledge else 'false'}"
    )
    if hints.app_knowledge_needed:
        _emit_list(lines, "app-knowledge-needed", hints.app_knowledge_needed)
    lines.append(f"{_HINT_PREFIX} completion-status: {hints.completion_status}")
    if hints.manual_steps:
        _emit_list(lines, "manual-steps", hints.manual_steps)
    return "\n".join(lines)


def _emit_list(out: list[str], key: str, values: list[str]) -> None:
    if not values:
        out.append(f"{_HINT_PREFIX} {key}: []")
        return
    out.append(f"{_HINT_PREFIX} {key}:")
    for v in values:
        out.append(f"{_CONT_PREFIX}{v}")


__all__ = ["DownHints", "emit_hints", "parse_hints"]
