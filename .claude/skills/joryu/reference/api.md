# joryu op / types reference

This page is a quick reference for assistants writing migration bodies.
The canonical spec is `SPEC.md` chapters 4, 5, and 6.3.

## Import shape

Every migration starts with this exact import block:

```python
import joryu
from joryu import op, types as t
```

If the migration uses raw SQLAlchemy `text()` (inside a `run_python`
body), add `from sqlalchemy import text`.

## Decorator

```python
@joryu.migration(
    id="<UTC timestamp>_<slug>",   # must match the filename stem
    depends_on=[],                 # list of migration ids; [] = timestamp order
    transaction_mode="per_step",   # "per_step" (default) | "per_migration" | "none"
    tags=[],                       # optional labels
    dialects=None,                 # None = all dialects; ["postgresql"] etc.
    group=None,                    # logical group across dialect-restricted files
)
def upgrade(): ...

@joryu.downgrade
def downgrade(): ...
```

Use `transaction_mode="none"` for ops that cannot run inside a
transaction (`CREATE INDEX CONCURRENTLY`, etc.).

## DDL ops

```python
op.create_table(name, *columns, **table_kwargs)
op.drop_table(name)
op.rename_table(old, new)

op.add_column(table, name, type, **column_kwargs)
op.drop_column(table, name)
op.alter_column(table, name, type=None, nullable=None, server_default=...)
op.rename_column(table, old, new)

op.create_index(name, table, columns, unique=False, concurrent=False, where=None)
op.drop_index(name, table=None)

op.create_unique_constraint(name, table, columns)
op.create_check_constraint(name, table, condition)
op.create_foreign_key(name, source_table, ref_table, source_cols, ref_cols, **fk_kwargs)
op.drop_constraint(name, table)
```

Column construction inside `create_table` uses `op.column(...)`:

```python
op.create_table(
    "users",
    op.column("id",    t.BigInt,    primary_key=True, autoincrement=True),
    op.column("email", t.Text,      nullable=False, unique=True),
    op.column("created_at", t.Timestamp, server_default=t.now()),
)
```

## Escape hatches (first-class)

```python
op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")   # plain string

op.execute({                                            # per-dialect dict
    "postgresql": "...",
    "mysql":      "...",
    "mariadb":    "...",     # optional; falls back to "default"
    "sqlite":     "...",
    "default":    "...",     # used when no specific key matches
})

op.run_python(fn)                                       # fn(conn, dialect, checkpoint)

with op.batch("users") as batch:                        # SQLite-safe table rebuild
    batch.alter_column("email", nullable=False)
    batch.drop_column("legacy_field")
```

Dialect key synonyms: `postgresql` / `postgres` / `pg` map to
`postgresql`; prefer the canonical form. `mysql` and `mariadb` are
distinct keys.

## Model integration

```python
from myapp.models import Order

op.create_table_from_model(Order)
op.add_columns_from_model(User, only=["email", "phone"])
op.historical_model("users")    # the schema as of this migration (use inside run_python)
```

## User-defined steps and checkpoints

```python
@op.step
def wait_for_replication(conn, dialect, checkpoint):
    if checkpoint.get("ready"):
        return True
    lag = conn.execute(text("SELECT ...")).scalar()
    if lag is None or lag < 1.0:
        checkpoint.set("ready", True)
        return True
    raise op.PauseStep(f"replica lag={lag:.1f}s, retry later")
```

- Return value or `op.SkipStep` controls completion semantics.
- `op.PauseStep` is raised (never returned); the migration enters
  `paused` state and resumes on the next `joryu apply`.

## Types (`from joryu import types as t`)

| `t.*`                | Postgres            | MySQL / MariaDB       | SQLite                 |
|----------------------|---------------------|-----------------------|------------------------|
| `SmallInt`           | SMALLINT            | SMALLINT              | INTEGER                |
| `Int`                | INTEGER             | INT                   | INTEGER                |
| `BigInt`             | BIGINT              | BIGINT                | INTEGER                |
| `Serial` / `BigSerial` | SERIAL / BIGSERIAL | INT/BIGINT AUTO_INCREMENT | INTEGER PRIMARY KEY |
| `Float`              | REAL                | FLOAT                 | REAL                   |
| `Double`             | DOUBLE PRECISION    | DOUBLE                | REAL                   |
| `Decimal(p, s)`      | NUMERIC(p, s)       | DECIMAL(p, s)         | NUMERIC                |
| `Bool`               | BOOLEAN             | TINYINT(1)            | INTEGER (0/1)          |
| `String(n)`          | VARCHAR(n)          | VARCHAR(n)            | TEXT                   |
| `Text`               | TEXT                | LONGTEXT              | TEXT                   |
| `Binary(n=None)`     | BYTEA               | VARBINARY(n)/LONGBLOB | BLOB                   |
| `Date`               | DATE                | DATE                  | TEXT (ISO 8601)        |
| `Time`               | TIME                | TIME                  | TEXT (ISO 8601)        |
| `Timestamp`          | TIMESTAMPTZ         | TIMESTAMP             | TEXT (UTC `YYYY-MM-DD HH:MM:SS`) |
| `Interval`           | INTERVAL            | unsupported (ERROR)   | unsupported (ERROR)    |
| `Json`               | JSONB               | JSON                  | TEXT                   |
| `Uuid`               | UUID                | CHAR(36)              | TEXT                   |
| `Enum(*labels, name=)` | native ENUM type | inline `ENUM(...)`    | TEXT + CHECK           |
| `Array(inner)`       | `inner[]`           | unsupported (ERROR)   | unsupported (ERROR)    |

Column modifiers (kwargs on `op.add_column` / `op.column`):

| Kwarg                | Meaning                                                    |
|----------------------|------------------------------------------------------------|
| `nullable=True/False`| NULL / NOT NULL (default True so new columns are backfill-safe) |
| `default=<value>`    | Python literal rendered as a SQL literal                  |
| `server_default=<expr>` | SQL expression string or `t.now()` (mutually exclusive with `default`) |
| `generated="<sql>"`  | `GENERATED ALWAYS AS (<sql>) STORED` (PG / MySQL / SQLite) |
| `primary_key=True`   | PK; required for `Serial` / `BigSerial`                   |
| `unique=True`        | Adds `uq_<table>_<column>` index                          |
| `comment="..."`      | PG / MySQL only; dropped silently on SQLite               |

Dialect-only types use the escape `t.dialect("postgresql.tsvector")`.
Wrap their use in a `dialects=[...]` migration or per-dialect branch.

## Why this API differs from Alembic

| Item                | Alembic                                                          | joryu                                                 |
|---------------------|------------------------------------------------------------------|-------------------------------------------------------|
| Add column          | `op.add_column("u", sa.Column("e", sa.Text(), nullable=False))`  | `op.add_column("u", "e", t.Text, nullable=False)`     |
| Raw SQL             | `op.execute(text("..."))`                                        | `op.execute("...")`                                   |
| Per-dialect SQL     | `if op.get_bind().dialect.name == "postgres": op.execute(...)`  | `op.execute({"postgresql": "...", "mysql": "..."})`   |
| Types               | `sa.Integer()`, `sa.BigInteger()`                                | `t.Int`, `t.BigInt`                                   |
| Op semantics        | imperative (errors if it exists)                                 | ensure-style (idempotent assertion)                   |
| Transaction default | per-migration (a lie on MySQL)                                   | per-step                                              |

The goal is that LSP completion and short argument lists keep
LLM-written migrations syntactically correct on the first try.
