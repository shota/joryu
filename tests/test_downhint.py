"""Tests for joryu.downhint (§11.2 parser + emitter)."""
from __future__ import annotations

from joryu.downhint import DownHints, emit_hints, parse_hints


SAMPLE = """\
def downgrade():
    # JORYU-DOWN-HINT: schema-impact:
    #   - drop_column: users.email_normalized
    #   - drop_index: idx_users_email_normalized
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: high
    # JORYU-DOWN-HINT: data-loss-reason: column data cannot be reconstructed
    # JORYU-DOWN-HINT: order-constraint:
    #   - drop_index before drop_column
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: stub
    op.drop_index("idx_users_email_normalized", "users")
"""


def test_parse_sample():
    h = parse_hints(SAMPLE)
    assert h.schema_impact == [
        "drop_column: users.email_normalized",
        "drop_index: idx_users_email_normalized",
    ]
    assert h.cross_references == []
    assert h.data_loss_risk == "high"
    assert h.data_loss_reason == "column data cannot be reconstructed"
    assert h.order_constraint == ["drop_index before drop_column"]
    assert h.requires_app_knowledge is False
    assert h.completion_status == "stub"


def test_emit_and_roundtrip():
    h = DownHints(
        schema_impact=["drop_table: foo"],
        cross_references=[],
        data_loss_risk="high",
        data_loss_reason="dropping table loses data",
        requires_app_knowledge=False,
        completion_status="stub",
    )
    text = emit_hints(h)
    again = parse_hints(text)
    assert again.schema_impact == h.schema_impact
    assert again.data_loss_risk == "high"
    assert again.completion_status == "stub"


def test_parse_requires_app_knowledge_true():
    h = parse_hints("# JORYU-DOWN-HINT: requires-app-knowledge: true\n")
    assert h.requires_app_knowledge is True


def test_emit_empty_lists():
    h = DownHints()
    out = emit_hints(h)
    assert "schema-impact: []" in out
    assert "cross-references: []" in out


def test_unknown_keys_ignored():
    h = parse_hints("# JORYU-DOWN-HINT: unknown-key: foo\n")
    assert h.data_loss_risk == "none"
