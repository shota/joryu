"""Verify the type rendering table (§6.3)."""
from __future__ import annotations

import pytest

from joryu import types as t
from joryu.exceptions import UnsupportedTypeUsage


def _render(spec, dialect):
    # Accept either a TypeSpec instance or a class (no-arg).
    if isinstance(spec, type):
        spec = spec()
    return spec.render(dialect)


def test_int_renders_per_dialect():
    assert _render(t.BigInt, "postgresql").upper() == "BIGINT"
    assert _render(t.BigInt, "mysql").upper() == "BIGINT"
    assert _render(t.BigInt, "sqlite").upper() == "INTEGER"


def test_text_renders_per_dialect():
    assert _render(t.Text, "postgresql").upper() == "TEXT"
    assert _render(t.Text, "mysql").upper() == "LONGTEXT"
    assert _render(t.Text, "sqlite").upper() == "TEXT"


def test_string_with_length():
    spec = t.String(255)
    assert "255" in spec.render("postgresql")
    assert "255" in spec.render("mysql")
    # SQLite: spec says TEXT (length is ignored).
    assert "TEXT" in spec.render("sqlite").upper()


def test_interval_unsupported_on_mysql_sqlite():
    with pytest.raises(UnsupportedTypeUsage):
        _render(t.Interval, "mysql")
    with pytest.raises(UnsupportedTypeUsage):
        _render(t.Interval, "sqlite")
    # postgres OK
    assert "INTERVAL" in _render(t.Interval, "postgresql").upper()


def test_now_helper_returns_server_default():
    sd = t.now()
    assert "CURRENT_TIMESTAMP" in sd.render("sqlite").upper()
    assert "CURRENT_TIMESTAMP" in sd.render("mysql").upper()


def test_type_equality():
    assert t.Int == t.Int
    assert t.String(10) == t.String(10)
    assert t.String(10) != t.String(20)
