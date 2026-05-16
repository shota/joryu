"""Concrete TypeSpec implementations (§6.3).

Each public name in ``joryu.types`` is either a singleton ``TypeSpec`` instance
(no-arg types like ``t.Int``) or a callable that returns a ``TypeSpec``
(parametrised types like ``t.String(255)``). The ops API normalises both shapes
via :func:`_normalize_type`.

Renderers map each type to the SQL fragment used by the targeted dialect; types
that have no representation on a dialect raise
:class:`joryu.exceptions.UnsupportedTypeUsage` at render time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .exceptions import UnsupportedTypeUsage

# ---- Dialect normalisation -------------------------------------------------

_POSTGRES_ALIASES = {"postgresql", "postgres", "pg"}


def _norm_dialect(name: str) -> str:
    n = name.lower()
    if n in _POSTGRES_ALIASES:
        return "postgresql"
    return n


# ---- Base ------------------------------------------------------------------


class TypeSpec:
    """Base for all joryu type specs.

    Subclasses set ``kind`` and override :meth:`render`. Equality is structural
    so ensure-semantics can compare a desired type to one discovered in the
    target database.
    """

    kind: str = "unknown"

    def __init__(self, **opts: Any) -> None:
        self.opts: dict[str, Any] = dict(opts)

    # Subclasses override.
    def render(self, dialect: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    # Equality / hashing -----------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TypeSpec):
            return NotImplemented
        return self.kind == other.kind and self.opts == other.opts

    def __hash__(self) -> int:
        return hash((self.kind, tuple(sorted(self.opts.items(), key=lambda kv: kv[0]))))

    def __repr__(self) -> str:
        if self.opts:
            opts = ", ".join(f"{k}={v!r}" for k, v in self.opts.items())
            return f"t.{self.kind}({opts})"
        return f"t.{self.kind}"


# ---- ServerDefault ---------------------------------------------------------


@dataclass(frozen=True)
class ServerDefault:
    """A SQL expression used as a column's server-side default.

    A bare string is rendered verbatim; cross-dialect helpers (e.g.
    :func:`now`) use the ``per_dialect`` mapping.
    """

    expr: str = ""
    per_dialect: dict[str, str] | None = None

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if self.per_dialect is not None:
            if d in self.per_dialect:
                return self.per_dialect[d]
            if "default" in self.per_dialect:
                return self.per_dialect["default"]
            raise UnsupportedTypeUsage(
                f"server_default has no rendering for dialect {d!r}"
            )
        return self.expr


# ---- Concrete type subclasses ---------------------------------------------


class _NoArgType(TypeSpec):
    """Helper: a TypeSpec subclass that may be invoked with no args.

    This lets users write either ``t.Int`` or ``t.Int()``. Calling the
    instance returns a fresh equivalent instance.
    """

    def __call__(self) -> "_NoArgType":
        return self.__class__()


class SmallIntType(_NoArgType):
    kind = "SmallInt"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "SMALLINT"
        if d in ("mysql", "mariadb"):
            return "SMALLINT"
        if d == "sqlite":
            return "INTEGER"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class IntType(_NoArgType):
    kind = "Int"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "INTEGER"
        if d in ("mysql", "mariadb"):
            return "INT"
        if d == "sqlite":
            return "INTEGER"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class BigIntType(_NoArgType):
    kind = "BigInt"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "BIGINT"
        if d in ("mysql", "mariadb"):
            return "BIGINT"
        if d == "sqlite":
            return "INTEGER"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class SerialType(_NoArgType):
    kind = "Serial"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "SERIAL"
        if d in ("mysql", "mariadb"):
            return "INT AUTO_INCREMENT"
        if d == "sqlite":
            return "INTEGER"  # PK marker added by column rendering
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class BigSerialType(_NoArgType):
    kind = "BigSerial"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "BIGSERIAL"
        if d in ("mysql", "mariadb"):
            return "BIGINT AUTO_INCREMENT"
        if d == "sqlite":
            return "INTEGER"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class FloatType(_NoArgType):
    kind = "Float"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "REAL"
        if d in ("mysql", "mariadb"):
            return "FLOAT"
        if d == "sqlite":
            return "REAL"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class DoubleType(_NoArgType):
    kind = "Double"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "DOUBLE PRECISION"
        if d in ("mysql", "mariadb"):
            return "DOUBLE"
        if d == "sqlite":
            return "REAL"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class DecimalType(TypeSpec):
    kind = "Decimal"

    def __init__(self, precision: int | None = None, scale: int | None = None) -> None:
        super().__init__(precision=precision, scale=scale)

    def __call__(self, precision: int | None = None, scale: int | None = None) -> "DecimalType":
        return DecimalType(precision, scale)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        p = self.opts.get("precision")
        s = self.opts.get("scale")
        if d == "sqlite":
            return "NUMERIC"
        name = "NUMERIC" if d == "postgresql" else "DECIMAL"
        if p is None and s is None:
            return name
        if s is None:
            return f"{name}({p})"
        return f"{name}({p}, {s})"


class BoolType(_NoArgType):
    kind = "Bool"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "BOOLEAN"
        if d in ("mysql", "mariadb"):
            return "TINYINT(1)"
        if d == "sqlite":
            return "INTEGER"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class StringType(TypeSpec):
    kind = "String"

    def __init__(self, length: int | None = None) -> None:
        super().__init__(length=length)

    def __call__(self, length: int | None = None) -> "StringType":
        return StringType(length)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        n = self.opts.get("length")
        if d == "sqlite":
            return "TEXT"
        if n is None:
            # VARCHAR without a length is invalid on MySQL; fall through to TEXT
            if d in ("mysql", "mariadb"):
                return "TEXT"
            return "VARCHAR"
        return f"VARCHAR({n})"


class TextType(_NoArgType):
    kind = "Text"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "TEXT"
        if d in ("mysql", "mariadb"):
            return "LONGTEXT"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class BinaryType(TypeSpec):
    kind = "Binary"

    def __init__(self, length: int | None = None) -> None:
        super().__init__(length=length)

    def __call__(self, length: int | None = None) -> "BinaryType":
        return BinaryType(length)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        n = self.opts.get("length")
        if d == "postgresql":
            return "BYTEA"
        if d in ("mysql", "mariadb"):
            return f"VARBINARY({n})" if n is not None else "LONGBLOB"
        if d == "sqlite":
            return "BLOB"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class DateType(_NoArgType):
    kind = "Date"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d in ("postgresql", "mysql", "mariadb"):
            return "DATE"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class TimeType(_NoArgType):
    kind = "Time"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d in ("postgresql", "mysql", "mariadb"):
            return "TIME"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class TimestampType(_NoArgType):
    kind = "Timestamp"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "TIMESTAMPTZ"
        if d in ("mysql", "mariadb"):
            return "TIMESTAMP"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class IntervalType(_NoArgType):
    kind = "Interval"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "INTERVAL"
        raise UnsupportedTypeUsage(
            f"t.Interval is not supported on {d!r}; wrap the column in a "
            "dialect branch (op.execute({...})) or restrict the migration to postgresql"
        )


class JsonType(_NoArgType):
    kind = "Json"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "JSONB"
        if d in ("mysql", "mariadb"):
            return "JSON"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class UuidType(_NoArgType):
    kind = "Uuid"

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            return "UUID"
        if d in ("mysql", "mariadb"):
            return "CHAR(36)"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class EnumType(TypeSpec):
    kind = "Enum"

    def __init__(self, *labels: str, name: str | None = None) -> None:
        super().__init__(labels=tuple(labels), name=name)

    def __call__(self, *labels: str, name: str | None = None) -> "EnumType":
        return EnumType(*labels, name=name)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        labels = self.opts.get("labels") or ()
        name = self.opts.get("name")
        if d == "postgresql":
            if not name:
                raise UnsupportedTypeUsage(
                    "Enum on postgresql requires name= to back the CREATE TYPE"
                )
            return name
        if d in ("mysql", "mariadb"):
            rendered = ", ".join(_sql_string_literal(label) for label in labels)
            return f"ENUM({rendered})"
        if d == "sqlite":
            return "TEXT"
        raise UnsupportedTypeUsage(f"unknown dialect {dialect!r}")


class ArrayType(TypeSpec):
    kind = "Array"

    def __init__(self, inner: "TypeSpec | type[TypeSpec] | None" = None) -> None:
        if inner is None:
            super().__init__(inner=None)
        else:
            super().__init__(inner=_normalize_type(inner))

    def __call__(self, inner: "TypeSpec | type[TypeSpec]") -> "ArrayType":
        return ArrayType(inner)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == "postgresql":
            inner = self.opts.get("inner")
            if inner is None:
                raise UnsupportedTypeUsage("t.Array requires an inner type")
            return f"{inner.render(dialect)}[]"
        raise UnsupportedTypeUsage(
            f"t.Array is not supported on {d!r}; wrap in a dialect branch"
        )


class DialectSpecificType(TypeSpec):
    """Holds an opaque dialect-qualified type name (e.g. ``postgresql.tsvector``).

    Renders fine on the named dialect; raises on any other.
    """

    kind = "Dialect"

    def __init__(self, spec: str) -> None:
        try:
            target_dialect, type_name = spec.split(".", 1)
        except ValueError as exc:
            raise UnsupportedTypeUsage(
                "t.dialect(spec) requires a 'dialect.type_name' form, e.g. 'postgresql.tsvector'"
            ) from exc
        super().__init__(target=_norm_dialect(target_dialect), type_name=type_name)

    def render(self, dialect: str) -> str:
        d = _norm_dialect(dialect)
        if d == self.opts["target"]:
            return self.opts["type_name"].upper()
        raise UnsupportedTypeUsage(
            f"type {self.opts['target']}.{self.opts['type_name']!r} not available on {d!r}"
        )


# ---- Public singletons / aliases ------------------------------------------

# No-arg types are exposed as singleton instances. Users may write either
# ``t.Int`` (instance) or ``t.Int()`` (also instance — _NoArgType.__call__).
SmallInt = SmallIntType()
Int = IntType()
BigInt = BigIntType()
Serial = SerialType()
BigSerial = BigSerialType()
Float = FloatType()
Double = DoubleType()
Bool = BoolType()
Text = TextType()
Date = DateType()
Time = TimeType()
Timestamp = TimestampType()
Interval = IntervalType()
Json = JsonType()
Uuid = UuidType()

# Parametrised types are exposed as the classes themselves so users can write
# ``t.String(255)`` / ``t.Decimal(10, 2)`` / ``t.Enum("a", "b", name="x")`` /
# ``t.Array(t.Int)`` / ``t.Binary(16)``. The ops API normalises ``t.String``
# (the class) to ``StringType()`` if needed.
String = StringType
Decimal = DecimalType
Binary = BinaryType
Enum = EnumType
Array = ArrayType


# ---- Helpers ---------------------------------------------------------------


def now() -> ServerDefault:
    """Return a ServerDefault that resolves to the dialect-appropriate
    "current UTC timestamp" expression (§6.3 notes)."""
    return ServerDefault(
        expr="CURRENT_TIMESTAMP",
        per_dialect={
            "postgresql": "now() AT TIME ZONE 'UTC'",
            "mysql": "CURRENT_TIMESTAMP",
            "mariadb": "CURRENT_TIMESTAMP",
            "sqlite": "CURRENT_TIMESTAMP",
        },
    )


def dialect(spec: str) -> TypeSpec:
    """Construct a dialect-specific type from a ``"dialect.type_name"`` string.

    Example: ``t.dialect("postgresql.tsvector")``.
    """
    return DialectSpecificType(spec)


# ---- Internal: normalisation helpers --------------------------------------


def _normalize_type(value: Any) -> TypeSpec:
    """Accept either a TypeSpec instance or a TypeSpec subclass; return an
    instance. Other inputs raise TypeError so misuse fails early."""
    if isinstance(value, TypeSpec):
        return value
    if isinstance(value, type) and issubclass(value, TypeSpec):
        return value()
    raise TypeError(
        f"expected a joryu.types.TypeSpec instance or class, got {value!r}"
    )


def _sql_string_literal(s: str) -> str:
    escaped = s.replace("'", "''")
    return f"'{escaped}'"


__all__ = [
    "Array",
    "ArrayType",
    "BigInt",
    "BigIntType",
    "BigSerial",
    "BigSerialType",
    "Binary",
    "BinaryType",
    "Bool",
    "BoolType",
    "Date",
    "DateType",
    "Decimal",
    "DecimalType",
    "DialectSpecificType",
    "Double",
    "DoubleType",
    "Enum",
    "EnumType",
    "Float",
    "FloatType",
    "Int",
    "IntType",
    "Interval",
    "IntervalType",
    "Json",
    "JsonType",
    "ServerDefault",
    "SmallInt",
    "SmallIntType",
    "String",
    "StringType",
    "Serial",
    "SerialType",
    "Text",
    "TextType",
    "Time",
    "TimeType",
    "Timestamp",
    "TimestampType",
    "TypeSpec",
    "Uuid",
    "UuidType",
    "_normalize_type",
    "_norm_dialect",
    "dialect",
    "now",
]
