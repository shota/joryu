"""Cross-dialect type abstractions (§6.3).

Every type is either a singleton (``t.Text``) or a callable producing an
instance (``t.String(255)``). Internally each carries a ``.render(dialect)``
that emits the SQL fragment for that dialect.

Filled in by the types/ops sub-agent.
"""
from __future__ import annotations

# Sub-agent fills this module in. The public names below are the contract:
#   SmallInt, Int, BigInt, Serial, BigSerial, Float, Double, Decimal,
#   Bool, String, Text, Binary, Date, Time, Timestamp, Interval, Json,
#   Uuid, Enum, Array, dialect, now
#
# Conventions:
# - "type instances" expose `.render(dialect_name: str) -> str` returning the
#   SQL type fragment.
# - Helpers like `now()` return a sentinel object used as a server_default.

# The actual implementations live in `_types_impl.py` so the sub-agent can
# rewrite that file without disturbing this module's public surface.
from ._types_impl import (  # noqa: F401
    Array,
    BigInt,
    BigSerial,
    Binary,
    Bool,
    Date,
    Decimal,
    Double,
    Enum,
    Float,
    Int,
    Interval,
    Json,
    ServerDefault,
    SmallInt,
    String,
    Serial,
    Text,
    Time,
    Timestamp,
    TypeSpec,
    Uuid,
    dialect,
    now,
)
