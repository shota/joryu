# joryu Specification (draft v1)

> A SQLAlchemy-based Python migration library, intended as a modern alternative to Alembic.
> This document is the working design spec; finalize it before implementation.

## Table of contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Architecture overview](#2-architecture-overview)
3. [Migration file format (option B: Python-first)](#3-migration-file-format-option-b-python-first)
4. [Operations API](#4-operations-api)
5. [AI-friendly API design](#5-ai-friendly-api-design)
6. [Multi-dialect support](#6-multi-dialect-support)
7. [Parallel PRs and consistency detection](#7-parallel-prs-and-consistency-detection)
8. [Autogeneration (SQLAlchemy diff)](#8-autogeneration-sqlalchemy-diff)
9. [Execution model: transactions, idempotency, locking](#9-execution-model-transactions-idempotency-locking)
10. [Failure handling and resumption](#10-failure-handling-and-resumption)
11. [Downgrade](#11-downgrade)
12. [Historical schema replay](#12-historical-schema-replay)
13. [User-defined steps and checkpoints](#13-user-defined-steps-and-checkpoints)
14. [Progress display](#14-progress-display)
15. [Production safety](#15-production-safety)
16. [CLI](#16-cli)
17. [Configuration file (`joryu.toml`)](#17-configuration-file-joryutoml)
18. [Comparison with Alembic and Django](#18-comparison-with-alembic-and-django)
19. [Alembic migration tool](#19-alembic-migration-tool)
20. [Design decisions log](#20-design-decisions-log)
21. [Appendix A: Migration examples](#appendix-a-migration-examples)

---

## 1. Goals and non-goals

### Goals
- Read diffs from SQLAlchemy models and autogenerate migration files.
- Concurrent PRs MUST NOT cause implicit ordering conflicts (parallel-safe).
- Migrations follow a simple append model: "execute everything in `joryu_migrations` that isn't yet recorded in the DB."
- Ordering can be made explicit; otherwise it defaults to timestamp ascending.
- Python's expressive power for data migrations and conditional logic is first-class.
- Raw SQL is first-class (escape hatch).
- API designed to resist LLM hallucination.
- A migration interrupted mid-flight can be safely resumed (idempotent / resumable, §9).
- Bulk data updates are checkpointable and resumable (§13.1).
- User-defined custom steps are first-class (`op.step`, §13.2).
- A migration tool for existing Alembic projects ships from day one (§19).
- Initial support for MySQL/MariaDB, PostgreSQL, and SQLite.
- A single migration file must be able to run across multiple DB engines (hard requirement).

### Non-goals
- Automated rollback for production (forward-only; dev-time rollback is separate).
- Automatic multi-tenant schema switching.
- Automatic schema-drift remediation.
- A direct generative-AI mode (`joryu generate --from-prompt "..."`) — never shipping. Instead, an official skill is distributed inside the repository (`.claude/skills/joryu/`) so the user's editor-side AI drives the joryu CLI.

### Runtime requirements
- **Python 3.11+** (uses `tomllib` from stdlib, `Self` type, Exception Groups, `asyncio.TaskGroup`, plus 3.11 performance improvements).
- 3.10 is excluded (EOL 2026-10).
- Targets modern Python style: type hints, `match` statements, PEP 604 unions (`X | Y`), `Self`.

### Official AI skill distribution

- Ship `.claude/skills/joryu/` inside the joryu GitHub repository.
- Skill contents: instructions so an AI can drive `joryu generate` / `apply` / `down` completion / `verify` and other CLI subcommands.
- Users copy the skill into their project (or use a submodule / symlink).
- A standalone VSCode extension or similar may be considered later, but the in-repo skill is the primary distribution channel.

---

## 2. Architecture overview

```
project/
├── models/                                # SQLAlchemy models (user-managed)
├── migrations/
│   ├── 20260514T093000_add_users.py
│   ├── 20260515T101200_add_email_index.py
│   └── 20260516T120000_seed_default_roles.py
└── joryu.toml                             # project configuration
```

- **Source of truth**: the user's SQLAlchemy `MetaData`.
- **Produced artifacts**: `migrations/*.py`. No central file that would block PR merges.
- **State / mutation detection**: the `joryu_migrations` table in the DB (stores per-file checksums of applied migrations).
- **Semantic conflict detection**: `joryu verify` does static analysis over Operations (§7).

---

## 3. Migration file format (option B: Python-first)

### 3.1 Naming convention

```
<UTC timestamp ISO basic>_<slug>.py
example: 20260514T093000_add_users.py
```

- The timestamp is UTC, second precision, ISO basic format (`YYYYMMDDTHHMMSS`).
- Lexicographic order equals chronological order.
- Slugs are lowercase ASCII plus `_`, up to 60 characters.
- On a same-second collision, append `_2`, `_3`, … to the filename.

Why not Django's per-app sequence (`0001_*.py`): concurrent PRs always collide on the number.
Why not Alembic's random hex: not human-readable, not sortable.

### 3.2 File body (decorator style)

```python
"""Add users table."""
import joryu
from joryu import op, types as t

@joryu.migration(
    id="20260514T093000_add_users",
    depends_on=[],                       # empty means timestamp-ascending order
    transaction_mode="per_step",         # default
    tags=["schema"],
)
def upgrade():
    op.create_table(
        "users",
        op.column("id",    t.BigInt, primary_key=True, autoincrement=True),
        op.column("email", t.Text,   nullable=False, unique=True),
        op.column("created_at", t.Timestamp, server_default=op.func.now()),
    )

@joryu.downgrade                          # optional, dev-only
def downgrade():
    op.drop_table("users")
```

**Metadata fields** (arguments to `@joryu.migration(...)`):

| Argument | Required | Description |
|---|---|---|
| `id` | yes | Must match the filename. Logical ID recorded in the state table. |
| `depends_on` | no, default `[]` | List of predecessor migration IDs. Empty means timestamp order. |
| `transaction_mode` | no, default `"per_step"` | One of `"per_migration"` / `"per_step"` / `"none"`. See §9.3. |
| `dialects` | no | Restrict to specific dialects, e.g. `["postgresql"]`. |
| `tags` | no | Arbitrary labels (for filtering). |
| `group` | no | Step-group ID. Migrations sharing a `group` are treated as one logical change (see §6). |
| `on_mismatch` | no, default `"error"` | Behavior on ensure mismatch. See §9.5.2. |

**Design rationale — why decorators**:
- Aligned with modern Python idioms (FastAPI / Typer / pytest).
- Typed arguments give IDE completion and type-checking.
- "One file, one migration" is explicit in the code (two `@joryu.migration` decorators in the same file is an ERROR).
- The Alembic-style module-level attribute approach is dropped.

---

## 4. Operations API

(Django-flavored, but SQLAlchemy-native.)

### 4.1 Design principles

- Resolve the chief frustrations with Alembic's `op.*`:
  - Verbose arguments (every column wrapped in `sa.Column(...)`).
  - Dialect-specific options scattered through the API (`postgresql_using=...` etc.).
  - The escape hatch (`op.execute`) being a second-class citizen.
- Adopt the strengths of Django's Operations classes:
  - Declarative objects let history be replayed, so any past schema state can be reconstructed.
  - Data migrations (`RunPython`) and DDL (`AddField`, etc.) live in the same list.
- Interoperate with SQLAlchemy MetaData / Column — accept model classes directly.

### 4.2 Core API

```python
from joryu import op, types as t

# DDL
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

# Escape hatches (first-class)
op.execute(sql_or_dict)                         # see §6
op.run_python(callable)                         # run arbitrary Python
op.batch(table)                                 # automate SQLite's table-rebuild
```

### 4.3 SQLAlchemy model integration

```python
from myapp.models import User           # SQLAlchemy model

def upgrade():
    op.create_table_from_model(User)    # use __table__ directly
    op.add_columns_from_model(User, only=["email", "phone"])
```

This removes the duplication of "hand-translating model columns into migration code."

### 4.4 Batch operations (SQLite support, opt-in)

SQLite does not directly support `ALTER TABLE DROP COLUMN`, constraint changes, etc.; internally a table-rebuild (create new table → copy data → rename) is required.

**Design rationale — explicit opt-in (no implicit auto-batching)**:
- A silent table-rebuild on a multi-million-row table is dangerous (copy cost, lock behavior, hidden FK toggling).
- Consistent with the ensure-semantics principle of "never change state behind the user's back."
- However `joryu generate` produces code already wrapped in `op.batch` when targeting SQLite (generation assists; execution stays explicit).

```python
def upgrade():
    with op.batch("users") as batch:
        batch.alter_column("email", nullable=False)
        batch.drop_column("legacy_field")
        batch.create_check_constraint("email_lower", "email = LOWER(email)")
    # Postgres / MySQL: plain ALTER. SQLite: table-rebuild.
```

Calling an unsupported op on SQLite without `with op.batch(...)` raises `UnsupportedOperationOnSQLite`, with a message suggesting batch.

Same idea as Alembic's `batch_alter_table`, but more explicit.

### 4.5 Data migration (`run_python`)

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text)

    def normalize(conn, dialect, checkpoint):
        # Receives a SQLAlchemy Connection. App models may be imported.
        conn.execute(text("UPDATE users SET email_normalized = LOWER(email)"))
        if dialect.name == "postgresql":
            conn.execute(text("CREATE INDEX ... USING GIN ..."))

    op.run_python(normalize)

    op.alter_column("users", "email_normalized", nullable=False)
```

- DDL and data migration sit in the same transaction (Alembic's biggest strength).
- The function receives `(connection, dialect, checkpoint)` (see §13.1 / §13.3 for `checkpoint`).
- Importing app SQLAlchemy models is fine, but note they reflect the *current* model shape — see §12.

---

## 5. AI-friendly API design

Why Alembic's `op` API is hard for LLMs to write correctly:

1. Nested `sa.Column(...)` structure (lines get long).
2. Dialect-specific kwargs scattered around (`postgresql_using=`, `mysql_engine=`, …).
3. Two-layer wrapping with `op.execute(text("..."))`.
4. Counterintuitive argument order (`op.add_column(table, sa.Column(name, type))`).

joryu eliminates these:

| Item | Alembic | joryu |
|---|---|---|
| Add column | `op.add_column("u", sa.Column("e", sa.Text(), nullable=False))` | `op.add_column("u", "e", t.Text, nullable=False)` |
| Raw SQL | `op.execute(text("..."))` | `op.execute("...")` |
| Per-dialect SQL | `if op.get_bind().dialect.name == "postgres": op.execute(...)` | `op.execute({"postgresql": "...", "mysql": "..."})` |
| Types | `sa.Integer()`, `sa.BigInteger()` | `t.Int`, `t.BigInt` (short, fully type-hinted) |

In addition:

- Every op API has type hints, so LSP completion works and LLMs can read the schema.
- `joryu generate` output uses a uniform style readable by LLMs (fixed import order, fixed argument order).
- `joryu explain <id>` renders a migration as natural-language prose (useful for both human and LLM review).

---

## 6. Multi-dialect support

> "The same migration file must run on SQLite and on MySQL."
> The library does not need to provide a unified native API for everything, but the structure must let users write portable migrations by hand.

### 6.1 The three-layer model

joryu deals with SQL across three layers:

1. **Layer 1: Operations abstractions (dialect-automatic)**
   `op.create_table`, `op.add_column`, etc. are translated to per-dialect SQL by joryu. The user does not think about dialects. This covers the bulk of cases.

2. **Layer 2: Per-dialect dispatch (`op.execute(dict)` / `op.run_python`)**
   `op.execute` accepts either a string or a dict:
   ```python
   # Single SQL — same statement on every dialect
   op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

   # Per-dialect SQL
   op.execute({
       "postgresql": "CREATE INDEX CONCURRENTLY ... USING GIN (data)",
       "mysql":      "CREATE INDEX ... ON ...((CAST(data AS CHAR(255))))",
       "sqlite":     "CREATE INDEX ... ON ...(data)",
   })

   # Default fallback
   op.execute({
       "postgresql": "CREATE INDEX CONCURRENTLY ...",
       "default":    "CREATE INDEX ...",      # used by every dialect other than postgresql
   })
   ```
   Key normalization:
   - `"postgresql"`, `"postgres"`, `"pg"` are synonyms (prefer the standard `"postgresql"`).
   - `"mysql"` and `"mariadb"` are separate keys (implementations sometimes diverge). Use `default` when sharing the same SQL between them.
   - `"sqlite"`.
   - `"default"` is used when the current dialect matches no other key.
   - If neither the current dialect nor `"default"` matches, raise ERROR.

   Or with a function:
   ```python
   def upgrade():
       d = op.dialect.name
       if d == "postgresql":
           op.execute("CREATE TYPE status AS ENUM ('a', 'b')")
       else:
           op.create_table("status_enum", op.column("value", t.Text, primary_key=True))
   ```

3. **Layer 3: Per-file dialect restriction + group**
   When a single file genuinely cannot cover both, restrict it with `dialects=` and bind it to siblings with `group=`:
   ```python
   # 20260601T120000_pg_partitions.py
   @joryu.migration(
       id="20260601T120000_pg_partitions",
       dialects=["postgresql"],
       group="20260601_partitions",          # logical group ID
       depends_on=["20260530T100000_create_events"],
   )
   def upgrade(): ...

   # 20260601T120000_mysql_partitions.py
   @joryu.migration(
       id="20260601T120000_mysql_partitions",
       dialects=["mysql"],
       group="20260601_partitions",          # same group
       depends_on=["20260530T100000_create_events"],
   )
   def upgrade(): ...
   ```
   Each environment only applies the file matching its dialect. `group=` has these effects:
   - `joryu status` displays them as one logical change.
   - Later migrations can write `depends_on=["group:20260601_partitions"]`; joryu resolves it to the ID actually applied for the current dialect.
   - `joryu verify` checks that the group has at least one file per dialect.

### 6.2 Recommended layer per use case

| Case | Recommended layer |
|---|---|
| Add column, create table, create index (type differences absorbed by joryu) | L1 |
| `JSON` vs `JSONB`, `SERIAL` vs `AUTO_INCREMENT`, etc. | L1 (joryu renders per dialect) |
| `CREATE INDEX CONCURRENTLY`, partial index, generated column | L2 (`op.execute(dict)`) |
| Data migration using dialect-specific functions (`jsonb_set` vs `JSON_SET`) | L2 (branching inside `op.run_python`) |
| Features exclusive to Postgres (`CREATE EXTENSION`, RLS, materialized view) | L3 (dialect-restricted file) |
| SQLite-only `batch` table-rebuild | L1 + `op.batch` (auto-routing) |

### 6.3 Type compatibility

The `joryu.types` module targets the cross-dialect lowest common denominator:

| `joryu.types` | Postgres | MySQL | SQLite |
|---|---|---|---|
| `Int`        | INTEGER | INT | INTEGER |
| `BigInt`     | BIGINT  | BIGINT | INTEGER |
| `Text`       | TEXT    | LONGTEXT | TEXT |
| `Json`       | JSONB   | JSON | TEXT (JSON1) |
| `Uuid`       | UUID    | CHAR(36) | TEXT |
| `Timestamp`  | TIMESTAMPTZ | TIMESTAMP | TEXT (ISO8601) |
| `Decimal(p,s)` | NUMERIC | DECIMAL | NUMERIC |
| `Bool`       | BOOLEAN | TINYINT(1) | INTEGER |

For dialect-specific types use the escape `types.dialect("postgresql.tsvector")`.

### 6.4 Test strategy (two-tier: unit / integration)

Migration behavior is verified at two tiers:

#### Unit testing (default, lightweight)

```
joryu test                                 # = joryu test --unit
joryu test --unit
```

- Apply every migration against an in-memory SQLite and then re-apply (verifying ensure semantics).
- Also run against an in-memory virtual DB (pure-Python DDL simulator) to validate Operations correctness.
- Completes in seconds. Run continuously during development and on every PR.
- Verifies syntax errors, ensure-semantics consistency, the same checks as `joryu verify`, and correct behavior of the checkpoint API.

#### Integration testing (optional, heavy)

```
joryu test --integration                                   # all configured dialects
joryu test --integration --dialects=postgresql,mysql        # explicit subset
```

- Use testcontainers to spin up real RDBMS instances and apply all migrations against them.
- Verifies dialect-specific behavior on real engines (MySQL's implicit DDL commits, PostgreSQL's transactional DDL, SQLite's table-rebuild).
- Takes minutes to tens of minutes. Run in nightly CI and before release.
- Skipped when testcontainers is unavailable (CI runners must have Docker).

#### Configuration

```toml
[joryu.test]
default_mode = "unit"                      # "unit" | "integration"
integration_dialects = ["postgresql", "mysql", "sqlite"]
postgresql_image = "postgres:16"
mysql_image = "mysql:8"
```

#### Verification scope

Common to both modes:
- All migrations apply successfully in order.
- Re-applying skips everything via ensure semantics (idempotency).
- `joryu verify` reports no semantic conflicts or drift.
- The final schema is logically equivalent across dialects (matching tables / columns / nullability / PKs).

### 6.5 Constraints (explicit)

Cross-dialect operation within a single file is supported but compatibility remains the user's responsibility in these areas:

- Dialect-specific types written directly (L2/L3).
- DDL behavior differences (MySQL's implicit commit, SQLite's constraint-change limits, Postgres's transactional DDL).
- Raw SQL strings inside data migration functions.

---

## 7. Parallel PRs and consistency detection

> **Design principle**: parallel PRs do not conflict by default. Conflicts arise only when they touch the same schema element.
> The Atlas `atlas.sum` model — which forces every PR to conflict — is rejected (it contradicts the parallel-safe requirement).

Three independent mechanisms ensure safety:

### 7.1 Mutation detection — `joryu_migrations.checksum` (DB side)

- The `joryu_migrations.checksum` column (§9.2) stores the file hash recorded at apply time.
- During `joryu apply` / `joryu verify`, if the on-disk hash of an *already-applied* file differs from the DB value, raise ERROR.
- Detects "I just retroactively edited an old migration" mistakes in production / CI.
- Unapplied files are not checked (rewrite freely while developing locally).
- No central file on disk, so PRs never conflict over it.

**Adoption elsewhere**:
- Adopted: Flyway, Liquibase, Prisma Migrate, Atlas (enterprise / schema-as-code).
- Not adopted: Alembic, Django migrations, yoyo, goose, golang-migrate, Diesel, sqlx (the classic script-oriented camp).

**Practical value**: it catches the "I just fixed a typo" accident on an old migration, unintended edits during rebase, and "apply, tweak, re-apply" flows in CI. With Alembic, this class of bug silently desynchronizes the production DB from migration history and shows up later as a fresh-environment build with a different schema. Cost is negligible (one hash, one column).

**Legitimate edits**: `joryu repair <id>` updates the checksum (an explicit channel, not a silent override).

### 7.2 Semantic conflict detection — `joryu verify` (CI side)

Each Operation can statically enumerate its touched targets as `(table, column)` pairs.
`joryu verify` scans all unapplied migrations and flags only non-commutative op pairs as ERROR. There is no warning category (warnings get ignored).

| Two parallel ops | Commutative | Decision |
|---|---|---|
| `add_column(users, A)` + `add_column(users, B)` (A≠B) | yes | **silent** |
| `add_column(users, A)` + `alter_column(users, B)` (A≠B) | yes | **silent** |
| `add_column(t1, ...)` + any change to `t2` (t1≠t2) | yes | **silent** |
| `alter_column(users, email)` + `alter_column(users, email)` | no | **ERROR** |
| `add_column(users, X)` + `drop_column(users, X)` | no | **ERROR** |
| Any change to `users` + `drop_table(users)` | no | **ERROR** |
| Any change to `users.X` + `rename_column(users, X, Y)` | no | **ERROR** |
| Any change to `users` + `rename_table(users, accounts)` | no | **ERROR** |
| One side is `op.execute(raw)` / `op.run_python(...)` | not analyzable | **silent** (human review responsibility) |

**Design rationale**: "no noise on normal workflows" is the top priority. Adding distinct columns to the same table — the common case — never trips this. Only genuinely dangerous combinations stop the build.

### 7.3 Ordering guarantee — `depends_on`

- If you need ordering, set `migration.depends_on = ["predecessor id", ...]`.
- Omitting it declares "I don't care about ordering (tiebreaker: timestamp ascending)."
- Editing applied migrations is forbidden (caught by checksum in §7.1). Only append.
- During apply, the `depends_on` DAG is topologically sorted.

### 7.4 Scenarios for parallel PRs

| Case | Outcome |
|---|---|
| Fully independent changes (A adds users, B adds orders) | No conflict; merge both, apply in order. |
| Different columns added to the same table | **Silent**; merge both, apply in order. |
| Both modify the same column | `joryu verify` ERROR. Rebase one and add `depends_on`. |
| Ordering matters (B depends on A) | Put A in B's `depends_on`. |
| A is merged and applied, then B is merged | B stays unapplied and runs on the next `joryu apply`. |
| Someone edits an applied file | `joryu apply` / `verify` reports a checksum mismatch — ERROR. Use `joryu repair` if intentional. |

---

## 8. Autogeneration (SQLAlchemy diff)

### 8.1 Command

```
joryu generate "add users table"
joryu generate "..." --empty             # create an empty template
joryu generate "..." --against=db        # compare against the current DB
joryu generate "..." --against=replay    # replay existing migrations and compare (CI-friendly)
```

1. Load the `target` from `joryu.toml` (e.g., `myapp.models:Base.metadata`).
2. Detect diffs against the comparison schema.
3. Produce a Python file as a list of Operations.

### 8.2 Comparison sources

| Mode | Compared against | Use case |
|---|---|---|
| `--against=db` (default) | The current schema of the real DB | Developer with a dev DB |
| `--against=replay` | A virtual schema obtained by replaying existing migrations in memory | CI, generation without a DB |

Alembic's `--autogenerate` requires a DB, which makes CI use awkward. joryu supports both.

### 8.3 Generated output

- Dangerous operations (`drop_table`, `drop_column`, adding NOT NULL) carry a `# WARNING: ...` comment.
- Irreversible operations are suggested to be split into a separate file.
- Cases that require data migration (NOT NULL with backfill, etc.) include an empty `op.run_python` placeholder so a human fills it in.

---

## 9. Execution model: transactions, idempotency, locking

> Alembic / Django and similar systems have only two states: "migration runs to completion" or "migration is rolled back wholesale."
> That model breaks down on multi-ten-million-row `UPDATE`s — rollback costs more than the work, long-running transactions block other queries, and MySQL implicitly commits DDL so the transactional guarantee is a lie anyway.
> joryu treats "interruption" as a first-class concept and designs Operations to be idempotent and resumable.

### 9.1 Design pillars

1. **Ensure-style Operations**: `op.add_column` etc. assert "this state must exist." If the column already exists with the desired name and type, no-op. If it exists with conflicting attributes, ERROR. Re-runnable.
2. **Per-step state tracking**: record not only which migration completed, but which *step* within a migration completed.
3. **Three transaction modes**: choose from `per_migration` / `per_step` (default) / `none`.
4. **Batched data migrations**: large updates use `op.run_python` with checkpoints (§13.1).
5. **Resume**: re-running `joryu apply` continues an interrupted migration from where it stopped.

### 9.2 State tables

```sql
CREATE TABLE joryu_migrations (
    id              VARCHAR(120) PRIMARY KEY,
    checksum        VARCHAR(80)  NOT NULL,
    status          VARCHAR(20)  NOT NULL,     -- 'running' | 'applied' | 'failed'
    started_at      TIMESTAMP    NOT NULL,
    finished_at     TIMESTAMP    NULL,
    joryu_version   VARCHAR(20)  NOT NULL,
    dialect         VARCHAR(20)  NOT NULL
);

CREATE TABLE joryu_migration_steps (
    migration_id    VARCHAR(120) NOT NULL,
    step_index      INTEGER      NOT NULL,
    op_fingerprint  VARCHAR(80)  NOT NULL,     -- hash of op kind and arguments
    status          VARCHAR(20)  NOT NULL,     -- 'running' | 'done' | 'failed'
    started_at      TIMESTAMP    NOT NULL,
    finished_at     TIMESTAMP    NULL,
    progress        TEXT         NULL,         -- checkpoint state, etc.
    PRIMARY KEY (migration_id, step_index)
);
```

- A migration with `status='applied'` is never re-executed (same as Alembic).
- `status='running'` or `'failed'` is a resume target. Replay from the first non-`done` step using `joryu_migration_steps`.
- `op_fingerprint` detects code edits at resume time (a different op at the same index is ERROR).

### 9.3 Transaction mode (three choices)

Behavior depends on `migration.transaction_mode`:

| Mode | Behavior | Best for |
|---|---|---|
| `"per_migration"` | Wrap the whole migration in one transaction | Small DDL only. When you want atomic DDL on Postgres / SQLite. |
| `"per_step"` (**default**) | Each op runs and commits in its own transaction | The majority of workloads. Completed steps survive interruption. |
| `"none"` | No transaction (rely on implicit commit) | `CREATE INDEX CONCURRENTLY`, `VACUUM`, heavy MySQL DDL. |

**Why `per_step` is the default**:
- MySQL implicitly commits DDL, so `per_migration` is a lie. Standardizing on `per_step` is more honest.
- Wrapping a multi-ten-million-row UPDATE in a single transaction is impractical (rollback cost, lock duration, binlog growth).
- Step-granular commits leave "K of N steps complete" durable in the DB, so resumption works.
- Combined with idempotent ops, re-running skips completed steps and continues from the failed one.

#### 9.3.1 Real transactional behavior per dialect

DDL transactional behavior diverges wildly by dialect. joryu does not hide this:

| Dialect | DDL atomicity | Data DML | Reality of `per_migration` | Reality of `per_step` |
|---|---|---|---|---|
| **PostgreSQL** | Fully transactional (almost all DDL is rollback-safe) | Normal | Works as advertised (except CONCURRENTLY family) | Works as advertised |
| **MySQL 8.0+ (InnoDB)** | Each DDL implicitly commits (atomic DDL is per-statement only) | Normal | **A lie**: the first DDL commits and nothing after can roll back | DDL is effectively `none`; data DML stays in-tx |
| **MariaDB** | Same (implicit commit) | Normal | A lie | Same as MySQL |
| **SQLite** | Transactional | Normal | Works as advertised | Works as advertised |

**Practical guidance**:
- On MySQL, `per_migration` rarely makes sense. Choosing it explicitly emits a warning.
- "Atomic across multiple DDLs" on MySQL is not achievable (a DB-level constraint). Split migrations finely instead (aim for "1 migration = 1 DDL").
- Bulk data UPDATE (`op.run_python` with checkpoints) works on every dialect (DML transactions are fine on MySQL).
- `per_step` is the default because, even on MySQL, it provides the minimum guarantee "step-level progress is durable."

This prevents the "I thought it worked, but on MySQL nothing rolled back" class of incident.

### 9.4 Ensure-style Operations (the heart of idempotency)

Each op declares a *desired state* and checks the current state before acting:

| Operation | Already in desired state | Partial match (e.g., same name, different type) | Missing |
|---|---|---|---|
| `add_column(t, c, type)` | **skip** | ERROR (does not silently change type) | create |
| `drop_column(t, c)` | (does not exist) **skip** | — | skip |
| `alter_column(t, c, ...)` | **skip** | execute ALTER | ERROR |
| `create_table(t, ...)` | **skip** | ERROR (column set differs) | create |
| `drop_table(t)` | (does not exist) **skip** | — | skip |
| `create_index(name, ...)` | **skip** | ERROR (definition differs) | create |
| `rename_column(t, old, new)` | new exists, old does not → **skip** | both exist → ERROR | rename old → new |

This means:
- Re-running after interruption skips successfully completed ops.
- A DB that's been partially fixed by hand can be reconciled by ensure semantics.
- The Alembic "dies because the column already exists" failure mode disappears.

**Design rationale**: Alembic issues the command "add a column." joryu states the intent "this column must exist." The difference looks small but produces resumability in practice.

"Never silently change state" is equally important: when a type mismatch is found, ERROR. Silent mutation causes incidents. To change a type, write an explicit `alter_column`.

### 9.5 Type mismatches and `on_mismatch`

#### 9.5.1 Typical mismatch sources

Real-world sources of "current state differs from desired":

| Source | Example | joryu behavior |
|---|---|---|
| Manual DDL drift | Someone added `phone VARCHAR(20)` via psql; migration expects `t.Text` | **ERROR** |
| Cross-environment drift | A hotfix only changed dev's column type; running the migration everywhere ERRORs in prod | **ERROR** |
| Parallel-PR type clash | PR1 = `Text`, PR2 = `Varchar(20)` adds the same column | Second one **ERRORs** (ideally `joryu verify` catches it earlier in CI) |
| Dialect rendering | `t.Text` is TEXT on SQLite, LONGTEXT on MySQL | **No false positive** (the type abstraction layer treats them as equal) |
| Edit of the migration itself | After apply, `Text → Varchar(255)` is rewritten | **Stopped earlier** by checksum (§7.1); ensure is never reached |

#### 9.5.2 `on_mismatch` options

Default is strict (no silent mutation) but can be relaxed locally:

```python
op.add_column("users", "phone", t.Text)                         # default: on_mismatch="error"
op.add_column("users", "phone", t.Text, on_mismatch="alter")    # explicitly reconcile
op.add_column("users", "phone", t.Text, on_mismatch="skip")     # tolerate drift
```

- `"error"` (default): stop on mismatch. Safe.
- `"alter"`: silently run `ALTER COLUMN` to reconcile. Destructive changes like VARCHAR shrinking can occur, so this is opt-in only.
- `"skip"`: log a warning and proceed. For drift-tolerant operations.

A migration-wide default via `migration.on_mismatch = "alter"` is supported (discouraged, but allowed).

### 9.6 Apply algorithm (`joryu apply`)

1. Acquire the advisory lock (§9.7).
2. Treat `status='running'` / `'failed'` rows in `joryu_migrations` as resume targets.
3. Build the set of unapplied migrations: everything not `applied` and not excluded by dialect.
4. Build the `depends_on` DAG and topologically sort (ties: timestamp ascending).
5. Process resume + unapplied items in order:
   - If no `joryu_migrations` row exists, INSERT one (`status='running'`) and verify the checksum.
   - Open a transaction according to `transaction_mode`.
   - Execute steps in order:
     - Consult `joryu_migration_steps`; skip if `done`.
     - For a `running` batched op, resume from `progress`.
     - Otherwise INSERT step (`status='running'`) → execute op → UPDATE step (`status='done'`).
     - On failure: UPDATE step (`status='failed'`), mark migration `failed`, stop.
   - When all steps finish: UPDATE migrations (`status='applied'`, `finished_at=...`).
6. Release the lock.

### 9.7 Advisory lock

Prevents concurrent processes:
- Postgres: `pg_advisory_lock`
- MySQL: `GET_LOCK`
- SQLite: `BEGIN EXCLUSIVE` or a file lock

---

## 10. Failure handling and resumption

### 10.1 Operational flow on failure

```
joryu apply
  → migration X, step 3 (run_python backfill) reaches 50% then OOM-kills
  → status='failed', steps[3].status='failed', steps[3].progress='{"cursor": 5012345}'

investigate (DB load, root cause)

joryu status              # shows X is failed and its progress
joryu apply --resume      # skip steps 1, 2; resume step 3 from the cursor
```

`apply` resumes by default; pass `--no-resume` to disable.

`joryu mark <id> --as=applied` and `joryu mark <id> --as=pending` allow manual state correction (last resort).

### 10.2 Multi-stage DDL failure (typical scenario)

Not data migration but a failure midway through multiple DDLs. Common in practice:

```python
def upgrade():
    op.add_column("users", "col1", t.Text)
    op.add_column("users", "col2", t.Text)
    op.create_index("idx_col2", "users", ["col2"])   # ← fails (duplicate index name, etc.)
    op.add_column("users", "col3", t.Text)
```

Behavior in `per_step` mode:

```
Step 1: ALTER TABLE ADD col1   → BEGIN; ...; COMMIT;  ok
Step 2: ALTER TABLE ADD col2   → BEGIN; ...; COMMIT;  ok
Step 3: CREATE INDEX idx_col2  → fails → ROLLBACK (Postgres) / implicit state (MySQL)
Step 4: ALTER TABLE ADD col3   → not executed, halted
```

**Real DB state**: col1 / col2 present, idx_col2 absent, col3 absent.
**`joryu_migration_steps`**: 1=done, 2=done, 3=failed, 4=pending.

Three recovery paths:

| Path | Action | Result |
|---|---|---|
| **A. Remove the cause and continue** | Drop the conflicting idx_col2, re-run `joryu apply` | Steps 1, 2 skip via ensure; step 3 retries; step 4 runs |
| **B. Edit the file** | Rename the index to `idx_col2_v2`, push, re-run `joryu apply` | While `failed`, checksum changes are allowed (§10.3); step 3 runs with a new fingerprint; step 4 runs |
| **C. Abandon** | `joryu mark <id> --as=pending` + manually drop col1 / col2, *or* `--as=applied` to call it done | The latter lies, but ensure semantics in later migrations will still reconcile to actual state |

### 10.3 Checksum / `op_fingerprint` policy in failed state

| State | File checksum change | Step `op_fingerprint` change |
|---|---|---|
| `applied` | **forbidden** (§7.1); requires `joryu repair` | — |
| `failed` | **allowed** (fix + push is the normal flow) | Before the failed step: must match. From the failed step onward: changeable; new content is executed. |
| `running` | should not happen (concurrency blocked by advisory lock) | — |
| Not yet started | free | — |

Erroring on a fingerprint change *before* the failed step matters: silently rewriting step 1 and re-running would find col1 already present, skip the step via ensure, and silently lose the new intent.

### 10.4 Behavior when a failed / paused migration exists

If migration X is `failed` or `paused`, should a different migration Y be allowed to run?

- **Default: halt.** If any migration is `failed` or `paused`, `joryu apply` does nothing and asks the user to consult `joryu status`. Exit code is `2` if any `failed` exists, or `3` if only `paused` (both present: `2` wins — `failed` takes priority).
- **Explicit `failed` override**: `joryu apply --continue-past-failed` runs only migrations not transitively dependent (via `depends_on`) on the failed one. Does not apply to `paused` (paused is recoverable, so the design expects resumption, not bypass).
- **`paused` handling**: `joryu apply --retry-paused [--retry-interval=N]` retries paused migrations. If still paused after retries, exit with code 3. There is no automatic bypass for subsequent migrations; to skip explicitly, transition the migration to `failed` via `joryu mark <id> --as=failed` and then use `--continue-past-failed`.
- Rationale: failure is an exceptional event; running other migrations unnoticed would complicate the state.

### 10.5 Interactive recovery from a half-failed state

When step 2 was a `run_python` that completed 50%, and step 3 raised — `joryu apply --resume` prompts for the recovery strategy:

```
$ joryu apply
Migration 20260620T030000_backfill_email_normalized is in failed state.

  ✓ step 1 add_column(users.email_normalized)  done
  ⚠ step 2 run_python(backfill)                done at last_id=15003421 (30%)
  ✗ step 3 alter_column(... NOT NULL)          failed: NotNullViolation

How would you like to proceed?
  [1] Resume from step 3 (re-run only failed/pending steps)
  [2] Restart from step 2 (re-run from a chosen step)  ← prompt for step number
  [3] Restart from step 1 (full restart, clears all checkpoints)
  [4] Skip step 3 and continue (mark as skipped)
  [5] Abort (do nothing)

Choose [1-5]:
```

- Default is `[1]` (resume).
- `--non-interactive --on-failure=resume|restart|abort` selects behavior for CI / automation.
- `[3]` clears all checkpoints; ensure-style ops still skip on re-run, but `run_python` starts from scratch — safe only if the user's WHERE clause is idempotent.
- `[4]` skip is recorded as metadata and visible in `joryu status`.

---

## 11. Downgrade

> "Reverse order and drop everything" does not work in practice. FKs, indexes, and dependencies require a specific order. Alembic's auto-generated `downgrade` rarely runs as-is.
> joryu discourages downgrade in production and structures dev-only downgrade so AI can complete it.

### 11.1 Why naive reverse order fails

Typical failures:
- The inverse of `create_index` (a `drop_index`) cannot drop the index because an FK references it.
- The inverse of `create_table` cannot drop the table because another table has an FK pointing at it.
- The inverse of `add_column NOT NULL` cannot drop the column because a view, FK, or index still references it.
- The inverse of a data migration (`run_python` that transforms values) is fundamentally unwritable (information loss).

Alembic's auto-generated downgrade ignores these and emits naïve reverse order, so in production people either rewrite it by hand or skip downgrade entirely.

### 11.2 The `JORYU-DOWN-HINT:` structured-comment spec (frozen in v1)

> **Language policy**: HINT field names and enum values are English (per the language policy in CLAUDE.md). The goal is stable AI parsing.

`joryu generate` produces both an upgrade and a downgrade skeleton plus structured hints. Completion is left to AI or human. Hints are YAML-like key-value lines prefixed with `JORYU-DOWN-HINT:`:

```python
def downgrade():
    # JORYU-DOWN-HINT: schema-impact:
    #   - drop_column: users.email_normalized
    #   - drop_index: idx_users_email_normalized
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: high
    # JORYU-DOWN-HINT: data-loss-reason: column data cannot be reconstructed from remaining schema
    # JORYU-DOWN-HINT: order-constraint:
    #   - drop_index before drop_column
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: stub

    op.drop_index("idx_users_email_normalized", "users")
    op.drop_column("users", "email_normalized")
```

**Field vocabulary (v1, frozen)**:

| Field | Type | Description | Required |
|---|---|---|---|
| `schema-impact` | list of `<verb>: <target>` | What the downgrade removes/restores. Verbs: `drop_table`, `drop_column`, `drop_index`, `drop_constraint`, `drop_view`, `drop_enum`, `restore_column_type`, `restore_nullable`, `restore_default`, `restore_data` | yes |
| `cross-references` | list of `<kind>: <name> -> <target>` | Other DB objects in the current schema that reference what we're dropping. Kinds: `foreign_key`, `index`, `view`, `materialized_view`, `trigger`, `policy`. Empty list `[]` if none detected | yes |
| `data-loss-risk` | enum: `none` / `low` / `medium` / `high` / `irreversible` | Whether running downgrade loses data. `irreversible` means the downgrade cannot recover the original state even with the migration code | yes |
| `data-loss-reason` | string | Free-text explanation. Required when `data-loss-risk >= medium` | conditional |
| `order-constraint` | list of `<a> before <b>` | Operations that must run in a specific order within the downgrade | no |
| `requires-app-knowledge` | bool | True if the downgrade depends on facts not derivable from schema alone (e.g., business invariants, external system state) | yes |
| `app-knowledge-needed` | list of strings | Specific unknowns the human/AI must resolve. Required when `requires-app-knowledge: true` | conditional |
| `completion-status` | enum: `stub` / `partial` / `complete` / `manual-review-required` | Set by generator (`stub`), updated by AI/human as they edit. CI can warn on `stub` if `joryu down` is part of the workflow | yes |
| `manual-steps` | list of strings | Non-SQL steps (e.g., "restart application servers", "purge cache") that must accompany the downgrade | no |

**Verb vocabulary for `schema-impact`**:
- `drop_table: <table>` / `drop_column: <table>.<column>` / `drop_index: <name>` / `drop_constraint: <name>` / `drop_view: <name>` / `drop_enum: <name>`
- `restore_column_type: <table>.<column> from <new_type> to <old_type>`
- `restore_nullable: <table>.<column>` / `restore_default: <table>.<column>`
- `restore_data: <table>.<column>` (when downgrade attempts to undo a data transformation)

**Cross-reference kinds**:
- `foreign_key: <fk_name> -> <referenced_table>.<col>`
- `index: <index_name> -> <table>.<columns>`
- `view: <view_name> -> <table>.<col>`
- `materialized_view: <name> -> ...`
- `trigger: <name> -> <table>`
- `policy: <name> -> <table>` (RLS)

**Generator responsibilities**:
- `schema-impact`, `order-constraint`: derived mechanically from upgrade ops.
- `cross-references`: scan the current DB schema (or the virtual schema reconstructed via `--against=replay`) at generation time.
- `data-loss-risk`: heuristic — `drop_column` / `drop_table` / `run_python` → `high`; pure index/constraint drop → `low`; schema-only changes that don't touch data → `none`.
- `requires-app-knowledge`: `true` if the migration contains `run_python` / `op.execute(raw_sql)`.

**AI responsibilities** (when completing downgrade):
- Read `schema-impact` to plan drop order (respecting `order-constraint`).
- Read `cross-references` to add drops for dependent objects.
- If `data-loss-risk: irreversible`, comment out the downgrade body and let a human decide.
- On completion, update `completion-status: complete`.

### 11.3 AI inputs

When an AI tool (e.g., Claude Code) completes a downgrade, it combines:

1. The `JORYU-DOWN-HINT:` structured fields.
2. SQLAlchemy model definitions under `models/` (FKs, relationships).
3. The current DB schema as emitted by `joryu schema-snapshot --format=json`.
4. The body of `upgrade()` in the same migration.

**Standard AI prompt** (shipped in the README):

```
Complete the downgrade() in this migration file. Use the JORYU-DOWN-HINT: comments
as the source of truth for what must be undone. Cross-check with models/*.py for
FK relationships. If data-loss-risk is "irreversible", comment out the downgrade
body and add a clear note explaining why. Update completion-status when done.
Do not modify the upgrade() function.
```

### 11.4 Positioning of downgrade (explicit)

| Environment | Downgrade usage | Recommendation |
|---|---|---|
| Local dev | OK (experimentation, branch switching) | Normal workflow |
| Staging | Limited (often breaks on FKs etc.) | With caution |
| Production | **Discouraged** | Use PITR / backups instead |

`joryu down` refuses production-like connections unless `--allow-prod` is passed (production detection is by DSN / config). Accident prevention.

---

## 12. Historical schema replay

As in Django, joryu can reconstruct the schema at any past point because Operations are declarative.
Inside a data-migration function, refer to the schema as it was at that migration:

```python
def upgrade():
    OldUser = op.historical_model("users")    # the schema at this migration
    def backfill(conn, dialect, checkpoint):
        for row in conn.execute(select(OldUser).where(OldUser.c.email.is_(None))):
            ...
    op.run_python(backfill)
```

This avoids the Alembic-style accident "I imported `models.User` and it had drifted to the current shape, breaking my migration."

### 12.1 Replay strategy: Operations replay (no snapshot files)

joryu uses Operations-replay. No snapshot JSON files (the Drizzle approach):
- At `joryu generate` time, replay every Operation up to (but excluding) the target migration into an in-memory virtual schema to obtain "current state."
- Apply the target migration's ops on top and compute the diff.
- Without snapshot files, PRs don't conflict on snapshots and git diffs stay clean.

### 12.2 Raw SQL / `run_python` handling

`op.execute(raw_sql)` and `op.run_python(...)` are opaque to the virtual schema. For migrations that contain them, the user adds a **declarative hint**:

```python
def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.declare_schema_change(extension_added="pgcrypto")     # hint for replay

    op.execute("ALTER TABLE users ADD COLUMN api_key TEXT GENERATED ALWAYS AS (...) STORED")
    op.declare_schema_change(
        column_added=("users", "api_key", t.Text, {"nullable": False, "generated": True})
    )

    def transform(conn, dialect, checkpoint):
        conn.execute(text("UPDATE users SET ..."))
    op.run_python(transform)
    # Data-only changes don't affect the schema, so no declare needed.
```

**Without a hint**:
- Replay treats the migration as an opaque box; subsequent `historical_model()` calls are pinned to the pre-migration state.
- `joryu generate` warns: "historical schema may be stale after migration X due to undeclared raw SQL."
- It does not error (most `run_python` calls are data-only and don't affect schema).

`op.declare_schema_change(...)` uses a subset of the same vocabulary as the other `op.*` calls.

**v1 frozen vocabulary**:

| Category | Keywords |
|---|---|
| Column | `column_added`, `column_dropped`, `column_altered`, `column_renamed` |
| Table | `table_added`, `table_dropped`, `table_renamed` |
| Index | `index_added`, `index_dropped` |
| Constraint | `constraint_added`, `constraint_dropped` (kind=`fk` \| `unique` \| `check`) |
| Extension (PG) | `extension_added`, `extension_dropped` |
| Enum | `enum_added`, `enum_dropped`, `enum_value_added` |
| View | `view_added`, `view_dropped` |
| Materialized View | `materialized_view_added`, `materialized_view_dropped` |
| Trigger | `trigger_added`, `trigger_dropped` |
| RLS Policy | `policy_added`, `policy_dropped` |
| Sequence | `sequence_added`, `sequence_dropped` |
| Schema (PG) | `schema_added`, `schema_dropped` |

This vocabulary is frozen in v1 (no breaking changes). Backward-compatible additions are allowed. Multiple keywords can be passed in one call:
```python
op.declare_schema_change(
    column_added=("users", "api_key", t.Text, {"nullable": False, "generated": True}),
    index_added=("idx_users_api_key", "users", ["api_key"], {"unique": True}),
)
```

Tuple shape per keyword:

| Keyword | Tuple shape |
|---|---|
| `column_added` | `(table, column_name, type, opts: dict)` |
| `column_dropped` | `(table, column_name)` |
| `column_altered` | `(table, column_name, {"old": ..., "new": ...})` — `old` / `new` are dicts of `{"type": ..., "nullable": ..., "default": ...}` |
| `column_renamed` | `(table, old_name, new_name)` |
| `table_added` | `(table_name, {"columns": [...], "options": {...}})` |
| `table_dropped` | `(table_name,)` |
| `table_renamed` | `(old_name, new_name)` |
| `index_added` | `(index_name, table, [columns], opts: dict)` |
| `index_dropped` | `(index_name, table)` |
| `constraint_added` | `(constraint_name, table, {"kind": "fk" \| "unique" \| "check", ...kind-specific fields})` — fk: `referred_table` / `referred_columns` / `local_columns`; unique: `columns`; check: `expression` |
| `constraint_dropped` | `(constraint_name, table, {"kind": ...})` |
| `extension_added` / `extension_dropped` | `(name,)` |
| `enum_added` | `(name, [labels])` |
| `enum_dropped` | `(name,)` |
| `enum_value_added` | `(name, label, {"before": neighbor} \| {"after": neighbor} \| {})` |
| `view_added` / `materialized_view_added` | `(name, sql)` |
| `view_dropped` / `materialized_view_dropped` | `(name,)` |
| `trigger_added` | `(name, table, definition_sql)` |
| `trigger_dropped` | `(name, table)` |
| `policy_added` | `(name, table, definition_sql)` |
| `policy_dropped` | `(name, table)` |
| `sequence_added` / `sequence_dropped` | `(name,)` |
| `schema_added` / `schema_dropped` | `(name,)` |

For multiple instances of the same keyword in one call, pass a list of tuples (`column_added=[(...), (...)]`).

**Intentionally omitted from v1**: `index_renamed` (no `op.rename_index` exists in the Operations API). If renaming an index via raw SQL is needed, a dedicated declaration (analogous to `column_renamed`) will be considered in v2+.

---

## 13. User-defined steps and checkpoints

### 13.1 Resumable data migrations (`op.run_python` + checkpoints)

> **Design rationale**: joryu does *not* provide a general-purpose `batched_update` API.
> Reasons:
> - The library cannot statically verify that WHERE clauses and indexes are consistent. A "use this, you're safe" API would mislead.
> - Batching strategy depends on data shape and index structure (cursor / range / ctid / SKIP LOCKED). Any general API leaks.
> - The batching loop itself is small. The library's real value is checkpoint persistence and resumption.
>
> Instead, joryu provides only the checkpoint infrastructure; the user writes the batching loop.

#### 13.1.1 The `op.run_python` checkpoint API

The `fn` passed to `op.run_python(fn)` receives `(connection, dialect, checkpoint)`. `checkpoint` is a dict-like object persisted to `joryu_migration_steps.progress`:

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text, nullable=True)

    def backfill(conn, dialect, checkpoint):
        cursor = checkpoint.get("last_id", 0)
        while True:
            rows = conn.execute(text(
                "SELECT id, email FROM users "
                "WHERE id > :c AND email_normalized IS NULL "
                "ORDER BY id LIMIT 10000"
            ), {"c": cursor}).fetchall()
            if not rows:
                return
            conn.execute(text(
                "UPDATE users SET email_normalized = LOWER(email) "
                "WHERE id = ANY(:ids)"
            ), {"ids": [r.id for r in rows]})
            cursor = rows[-1].id
            checkpoint.set("last_id", cursor)   # ← commit + persist
```

`checkpoint.set(key, value)` behavior:
- Writes the value to `joryu_migration_steps.progress`.
- Commits under joryu's control (in the same transaction as the user's batch UPDATE, or immediately after; see §13.1.3).
- On restart, `checkpoint` is preloaded with the persisted values before `fn` runs.

#### 13.1.2 Idempotency guidelines (responsibility of the `run_python` author)

The body of `run_python` is arbitrary Python; idempotency is the user's responsibility — joryu cannot enforce it. Practical guidance:

| Pattern | Good | Bad |
|---|---|---|
| **Filter "unprocessed only" with WHERE** | `WHERE email_normalized IS NULL` | `UPDATE users SET email_normalized = LOWER(email)` (rescans every row on re-run) |
| **Use ON CONFLICT / WHERE NOT EXISTS on inserts** | `INSERT ... ON CONFLICT DO NOTHING` | bare `INSERT` (duplicates on re-run) |
| **Cursor-based ordered progression** | `id > :cursor ORDER BY id` | offset/limit alone (skips or duplicates on interruption) |
| **Side effects only inside the DB** | DB UPDATE only | HTTP calls, email sends, external APIs (re-fire on resume) |
| **Deterministic computation** | Same input, same output | `random()`, current-time-dependent values (drift on resume) |
| **Checkpoint per batch** | `checkpoint.set` on each batch | Only `set` after the whole loop (progress lost on interruption) |

**Index hint**: a predicate like `WHERE email_normalized IS NULL AND id > :cursor` will do a full scan every iteration unless an index on `(email_normalized, id)` (or at least `id`) exists. When writing `run_python`, create the supporting indexes in an earlier step:

```python
def upgrade():
    op.add_column("users", "email_normalized", t.Text, nullable=True)
    # Partial index narrowing unprocessed rows (drop after backfill).
    op.create_index("tmp_users_unnormalized", "users", ["id"],
                    where="email_normalized IS NULL")
    op.run_python(backfill)
    op.drop_index("tmp_users_unnormalized")
    op.alter_column("users", "email_normalized", nullable=False)
```

#### 13.1.3 Checkpoint and batch transaction relationship

`checkpoint.set()` is committed in the *same* transaction as the immediately preceding DML:

```python
# User code:
conn.execute(UPDATE...)        # batch UPDATE
checkpoint.set("last_id", x)   # ← joryu issues COMMIT here
```

This prevents "UPDATE ran but checkpoint wasn't saved, so the next run double-processes the same rows." An idempotent WHERE makes duplicates harmless, but this provides a defensive guarantee.

When the user needs explicit transaction control, set `transaction_mode = "none"` and manage transactions manually:

```python
migration.transaction_mode = "none"

def backfill(conn, dialect, checkpoint):
    while ...:
        with conn.begin():
            conn.execute(UPDATE...)
            checkpoint.set("last_id", x)   # commit happens at with-exit
```

### 13.2 User-defined steps (`op.step`)

> A migration is a sequence of step ops, but any user code can register as a step.
> Completion logic is customizable, and resume / pause behave the same as built-in steps.

#### 13.2.1 Basics

```python
@op.step
def wait_for_replication(conn, dialect, checkpoint):
    """Wait for prior DDL to propagate to a replica."""
    if checkpoint.get("ready"):
        return True

    lag = conn.execute(text("SHOW SLAVE STATUS")).scalar()
    if lag < 1:
        checkpoint.set("ready", True)
        return True
    raise op.PauseStep(f"replica lag={lag}s, retry later")
```

`op.step` supports three call forms:
```python
@op.step                                       # bare decorator
def my_func(conn, dialect, checkpoint): ...

@op.step(name="...", description="...")        # decorator factory (with metadata)
def my_func(conn, dialect, checkpoint): ...

op.step(my_func, name="...", description="...")  # direct call (register an existing function)
```

**Return value**: every form returns the original function unchanged (registration is a side effect that appends to the registry). This means:
- The decorated function is callable directly from tests (`my_func(conn, dialect, checkpoint)`).
- `inspect.iscoroutinefunction(my_func)` still works (needed for the sync/async dispatch in §13.2.2).
- The type signature of `@op.step` and `@op.step(...)` doesn't differ (the decorator factory also returns `Callable[F, F]`).

- `name`: identifier and display name in `joryu status` (defaults to the function name).
- `description`: 1-line description returned by `describe()` (defaults to the docstring's first line, or `name`).
- Dispatch logic at runtime:
  1. If there is one positional argument and it is callable, treat it as **bare decorator or direct call** and register it immediately (the two are semantically identical — `@op.step` and `op.step(fn)` are equivalent registration calls).
  2. If there are no positional arguments and only kwargs (`name=` / `description=`), treat it as a **decorator factory** and return a wrapper that accepts the function.
  3. Anything else raises `TypeError`.

#### 13.2.2 Signature and return values

Signature: `fn(conn, dialect, checkpoint) -> bool | None`
- **Sync functions are first-class**: the common case (SQLAlchemy Core/ORM queries).
- **`async def` is supported**: required when waiting on external I/O (HTTP, replication wait, etc.).
- Dispatch: `inspect.iscoroutinefunction(fn)` → True runs on the **anyio** runner; False runs synchronously.
- Arguments may be received as `*args, **kwargs` and ignored, or as a zero-arg `fn()`.
- Sync and async steps may coexist in the same migration.

```python
# Sync (typical, SQLAlchemy)
@op.step
def normalize_user_data(conn, dialect, checkpoint):
    rows = conn.execute(select(User).where(User.normalized.is_(None))).all()
    for r in rows:
        conn.execute(update(User).where(User.id == r.id).values(normalized=r.name.lower()))
    return True

# Async (external I/O)
@op.step
async def wait_for_webhook(conn, dialect, checkpoint):
    async with httpx.AsyncClient() as c:
        r = await c.get("https://...")
    return r.status_code == 200
```

**Runtime choice**: internally uses **anyio** (not raw asyncio). Reasons:
- Supports both asyncio and trio, future-proof.
- Clean structured concurrency via TaskGroup et al.
- Interop: sync code can be invoked from the async runner via `anyio.from_thread.run_sync`.

**Caller event-loop assumptions**: joryu's CLI / API assumes a synchronous calling context; async steps start their own loop with `anyio.run(...)`.
- If the caller is already on an async runtime (FastAPI background task, `pytest-anyio`, Jupyter, etc.) and invokes `joryu.apply()`, `anyio.run()` fails with `RuntimeError: nested event loops`.
- For that case, use `await joryu.apply_async(...)` (shipped in v1). `apply_async` reuses the caller's loop and `await`s steps directly.
- Patches like `nest_asyncio` are *not* adopted (they create hard-to-debug behavior).

Completion semantics. A step signals its outcome either by returning a value or by raising one of two control-flow exceptions. joryu's step runner catches `PauseStep` and `SkipStep` specifically *before* the generic exception handler; any other exception propagates as failure.

| Outcome | How produced | Meaning | Step status |
|---|---|---|---|
| return `True` / `None` | `return True` or `return` | Done | `done` |
| return `False` | `return False` | Not done, but not an error (retry later) | stays `pending`, re-runs next apply |
| `op.PauseStep(reason)` | `raise op.PauseStep(...)` | External wait (halt migration, resume on rerun) | step `pending`, migration `paused` |
| `op.SkipStep(reason)` | `raise op.SkipStep(...)` | Skip this step and continue | step `skipped` |
| Any other exception | `raise SomeError(...)` | Failure | step `failed`, migration `failed` |

`PauseStep` and `SkipStep` are *raised*, not returned. Returning a `PauseStep` / `SkipStep` instance has no special meaning and will be treated as a truthy return (i.e. `done`).

#### 13.2.3 SQL session

- `conn` is passed in (sync: SQLAlchemy `Connection`; async: `AsyncConnection`).
- If you need your own engine, `op.get_engine()` exposes the same connection info.
- Transaction control follows the migration's `transaction_mode`. To open an explicit transaction inside a step:
  - sync step: `with conn.begin():`
  - async step: `async with conn.begin():` (`AsyncConnection` only supports the async context manager; the sync form raises `TypeError`).

#### 13.2.4 Difference from regular ops

| Aspect | Regular op (`add_column`, …) | `op.step` |
|---|---|---|
| Static analysis (`joryu verify`) | yes | no (opaque, same as `run_python`) |
| Auto schema-impact recording | yes | no (use `op.declare_schema_change`) |
| Ensure semantics | yes | user-implemented |
| Completion criterion | non-exceptional return ⇒ done | controlled by return value |
| `PauseStep` support | no | yes |

### 13.3 Checkpoint API reference

#### 13.3.1 Methods

```python
checkpoint.get(key, default=None)         # fetch (None-safe)
checkpoint.set(key, value)                # single-key update (atomic UPDATE + commit)
checkpoint.update({k1: v1, k2: v2})       # multi-key atomic update in one UPDATE
checkpoint.clear()                        # erase all progress (restart from scratch)
checkpoint.snapshot()                     # read-only dict of the full state
```

#### 13.3.2 Persistence

- Stored as JSON text in `joryu_migration_steps.progress`.
- `set` / `update` internally run `BEGIN; UPDATE joryu_migration_steps SET progress=... WHERE ...; COMMIT;`.
- Concurrent writers are prevented by the advisory lock (§9.7) — single-writer guarantee.

#### 13.3.3 Transaction relationship

`set` / `update` commit in the *same* transaction as the user's immediately preceding DML:
```python
conn.execute(UPDATE users SET ...)        # user DML
checkpoint.set("cursor", x)               # ← COMMIT happens here
```
This prevents "DML ran but checkpoint missed, so re-run double-processes."

When `transaction_mode = "none"`, the user manages transactions:
```python
with conn.begin():
    conn.execute(UPDATE...)
    checkpoint.set("cursor", x)            # commit at with-exit
```

#### 13.3.4 Serializable types

JSON-compatible types only:
- `str`, `int`, `float`, `bool`, `list`, `dict`, `None`
- `datetime` / `date` → `isoformat()` string (automatic).
- `Decimal` → string (automatic).
- Anything else (custom objects, etc.) is an ERROR.

A custom encoder/decoder hook (`migration.checkpoint_codec`) may be considered in v1.1+.

#### 13.3.5 Size and operations

- Soft limit: 1 MB per step. Exceeding it warns.
- For large data, use a dedicated table. The checkpoint should hold only "next position to process" markers (cursors), not bulk data.
- First `get` lazy-loads; subsequent reads are memory-cached.

---

## 14. Progress display

> When a migration is running, it must be visible what is happening: is it stuck, advancing, on which op?
> joryu enumerates every step before executing any op (`upgrade()` is split into a registration phase and an execution phase), so step number, current op, next op, and progress can be displayed in real time.

### 14.1 The two-phase execution model

1. **Registration phase**: call `upgrade()` once to register all `op.*` calls in the registry (no real DB access). Step count, op kinds, and arguments are determined.
2. **Execution phase**: execute registered steps in order. Each step emits start / done / progress events.

This allows displaying "5 steps total" up front and rendering accurate progress bars.

**`op.dialect` during the registration phase**:
- Registration does not hold a real connection; `op.dialect` behaves as a read-only value carrying the target dialect name (`postgresql` / `mysql` / `sqlite`). This means `if op.dialect.name == "postgresql":` branching evaluates correctly during registration.
- Only the dialect name from connection info is available. Real-query operations such as `conn.execute(...)` raise `RuntimeError` during registration (real DB access is confined to the execution phase).
- The bodies of `op.run_python` / `op.step` are *not* called during registration (only the function objects are appended to the registry). So if a `run_python` / `op.step` body branches on dialect, the step count is fixed but the internal op count is not known up front. This is reflected in `describe()` output (e.g., "run_python: backfill (internal progress unavailable until execution)").
- Writing side effects (external API calls, file I/O) into the body of `upgrade()` would execute them during registration. This is forbidden and detected statically by `joryu verify` (§7).

### 14.2 Display modes

| Mode | Trigger | Output |
|---|---|---|
| **auto** (default) | No flag. Auto-selects interactive on TTY, plain otherwise | Environment-dependent |
| **interactive** | `--interactive` or auto + TTY | Rich-style live progress (step bar + current/next op) |
| **plain** | `--plain` or auto + non-TTY | Line-oriented log, one event per line |
| **json** | `--json` | JSONL structured log (external monitoring) |
| **quiet** | `--quiet` | Only on failure |

CLI flags are mutually exclusive (`--interactive` / `--plain` / `--json` / `--quiet` — at most one). Combining them is ERROR. Without a flag, `auto` is used.

**Scope**: the progress flags are accepted by the three long-running subcommands `joryu apply`, `joryu test`, and `joryu import alembic`. Other subcommands (`status`, `show`, `verify`, etc.) return quickly and do not accept these flags (passing one is ERROR). Only `--json` is accepted by every command and emits results as JSONL (for external monitoring).

**Relationship between `--interactive` and `--non-interactive`**: these are orthogonal controls.
- `--interactive` / `--plain` / `--json` / `--quiet` control the **progress display mode** (§14.2).
- `--non-interactive` suppresses the **failure-recovery prompt** (§10.5); it has no effect on progress display.
- Combining them is allowed (e.g., `joryu apply --non-interactive --interactive` means "CI mode, but render rich progress" — a valid combination).

### 14.3 Display examples

**Interactive (TTY)**:
```
joryu apply

▶ 20260620T030000_backfill_email_normalized  (5 steps, transaction_mode=per_step)
  ✓ 1/5  add_column users.email_normalized               (0.02s)
  ✓ 2/5  create_index tmp_users_unnormalized              (1.4s)
  ◐ 3/5  run_python backfill                              (00:42, 30% — last_id=15003421)
  · 4/5  drop_index tmp_users_unnormalized                pending
  · 5/5  alter_column users.email_normalized NOT NULL     pending
```

**Plain (CI)**:
```
[joryu] applying 20260620T030000_backfill_email_normalized (5 steps)
[joryu]   step 1/5: add_column users.email_normalized
[joryu]   step 1/5: done (0.02s)
[joryu]   step 2/5: create_index tmp_users_unnormalized
[joryu]   step 2/5: done (1.4s)
[joryu]   step 3/5: run_python backfill (starting)
[joryu]   step 3/5: progress last_id=5000000 (10%)
[joryu]   step 3/5: progress last_id=10000000 (20%)
```

**JSON**:
```jsonl
{"event":"migration_start","id":"...","steps":5,"transaction_mode":"per_step"}
{"event":"step_start","step":1,"op":"add_column","description":"users.email_normalized","next":"create_index tmp_users_unnormalized"}
{"event":"step_done","step":1,"duration_ms":20}
{"event":"step_progress","step":3,"progress":{"last_id":5000000,"percent":10}}
{"event":"step_done","step":3,"duration_ms":234000}
{"event":"migration_done","id":"...","duration_ms":237500}
```

### 14.4 Progress reporting API

Every op has a `describe() -> str` method returning a one-line description (auto-generated for built-in ops, customizable for `op.step`).

Inside `run_python` / `op.step`, progress can be reported explicitly:

```python
@op.step
def backfill(conn, dialect, checkpoint):
    batch_size = 5_000
    # Fix `total` only on first run and persist it in checkpoint.
    # Prevents shrinkage on resume when the WHERE filters out processed rows.
    total = checkpoint.get("total")
    if total is None:
        total = conn.execute(text("SELECT COUNT(*) FROM users WHERE ...")).scalar()
        checkpoint.set("total", total)
    done = checkpoint.get("done", 0)
    cursor = checkpoint.get("last_id", 0)
    while done < total:
        # ... batch work that advances `cursor` and processes up to batch_size rows.
        # Measure the actual rows processed and add to done (the last batch may be smaller).
        processed = run_one_batch(conn, cursor, batch_size)  # returns (new_cursor, rows_done)
        cursor, rows_done = processed
        done += rows_done
        checkpoint.update({"last_id": cursor, "done": done})
        checkpoint.report(percent=done * 100 // total,
                          message=f"processed {done}/{total}")
        if rows_done == 0:
            break  # safety net: no rows left to process
    return True
```

`checkpoint.report(...)` behavior:
- TTY mode: refresh the progress bar (rate-limited to at most once per 100 ms).
- Plain mode: emit a log line at most once per second.
- JSON mode: emit a `step_progress` event.
- The value is not persisted (display-only). To persist, also call `checkpoint.set(...)`.

Custom description for `op.step`:
```python
@op.step(description="wait for replication lag < 1s")
def wait_for_replication(conn, dialect, checkpoint):
    ...
```

---

## 15. Production safety

Policy for avoiding accidental destructive commands (like `joryu down`) on production:

### 15.1 Heuristic detection

The DB connection string is treated as **local** when any of the following hold:
- `localhost` / `127.0.0.1` / `::1`
- Hostname contains `.local`, `local-`, or `-local`.
- A file path (SQLite).
- `host.docker.internal`
- Environment variable `JORYU_ENV` is `local` / `dev` / `test`.

Otherwise the connection is treated as **production-like** and `joryu down` requires `--allow-prod`.

### 15.2 Explicit declaration

Declare via a config file or init API:
```python
import joryu
joryu.set_environment("production")    # explicit; overrides heuristics
```
Or in `joryu.toml`:
```toml
[joryu]
environment = "production"            # local / staging / production
```

When `environment != "local"`:
- `joryu down`: `--allow-prod` is required.
- `joryu apply`: `--continue-past-failed` shows a stricter confirmation prompt.
- `joryu mark`: confirmation prompt.

### 15.3 Documentation

A dedicated section in the manual covers production detection: the limits of the heuristic, the recommendation to declare explicitly, and CI/CD integration. Transparent detection is essential because both false positives and false negatives are harmful.

---

## 16. CLI

```
joryu init                              # initial setup
joryu generate <slug> [--empty] [--against=db|replay]
joryu apply [--target=<id>] [--dry-run] [--no-resume] [--continue-past-failed]
                                        #   [--non-interactive --on-failure=resume|restart|abort]
                                        #   [--retry-paused --retry-interval=30s]
                                        #   [--interactive | --plain | --json | --quiet]  # progress mode (§14, default auto)
joryu status                            # list applied / pending / failed / paused, with step progress
joryu down [--steps=N | --to=<id>] [--allow-prod]   # dev-only; production-like connections require explicit allow
joryu schema-snapshot [--format=json|sql]           # emit the current schema for AI assistance
joryu verify                            # CI: mutation detection + semantic conflict detection (§7)
joryu repair <id>                       # update the checksum of an applied migration
joryu mark <id> --as=applied|pending|failed       # manual state correction (last resort)
joryu mark <id>.<step> --as=done|pending|skipped  # correct an individual step
joryu show <id>                         # show details
joryu explain <id>                      # natural-language rendering (AI assistance)
joryu test [--unit | --integration] [--dialects=postgresql,mysql,sqlite]
joryu import alembic --alembic-dir=./alembic --output-dir=./migrations
                                        #   [--migrate-state] [--drop-alembic-table] [--report]
```

### 16.1 Python API entry points

A Python API equivalent to the CLI is shipped in v1. Use cases: test harnesses, deploy scripts, and embedding in async web frameworks.

```python
import joryu

# Sync API (equivalent to the CLI; internally uses anyio.run)
joryu.apply(target=None, dry_run=False, no_resume=False, continue_past_failed=False,
            non_interactive=False, on_failure="resume",
            retry_paused=False, retry_interval="30s",
            output="auto")      # "auto" | "interactive" | "plain" | "json" | "quiet" — see §14.2
joryu.status()
joryu.down(steps=None, to=None, allow_prod=False)
joryu.verify()
joryu.generate(slug, empty=False, against="db")

# Async API (when the caller is already on an event loop)
await joryu.apply_async(...)    # same signature as apply, requires await
await joryu.down_async(steps=None, to=None, allow_prod=False)  # same signature as down
```

- The sync API starts its own loop via `anyio.run(...)`. Calling it from an existing loop raises `RuntimeError`.
- The async API reuses the caller's loop and `await`s steps directly.
- Both follow the same exit-code convention as the CLI (§16.2) and signal failure through the exceptions in §16.3.

### 16.2 Exit codes

Shared by CLI and API:

| Code | Meaning | Corresponding exception (Python API) |
|---|---|---|
| `0` | Success | — |
| `1` | General error (unexpected exception, argument errors, etc.) | `JoryuError` (base) |
| `2` | Migration failure (a step errored during apply) | `MigrationFailed` |
| `3` | Migration paused (`PauseStep` raised, or still paused after `--retry-paused`, or halted by an existing paused migration) | `MigrationPaused` |
| `4` | Verify failure (drift / conflict detected) | `VerificationFailed` |
| `5` | Rejected by production guard (production detected, `--allow-prod` not passed) | `ProductionGuardError` |

### 16.3 Public exception classes

Importable directly from the `joryu` package:

```python
joryu.JoryuError                     # base of every exception
├── MigrationFailed(migration_id, step_index, step_name, cause)
├── MigrationPaused(migration_id, step_index, step_name, reason)
├── VerificationFailed(conflicts: list[Conflict])
└── ProductionGuardError(detected_env: Literal["staging", "production", "production-like"], host: str | None)
```

- `MigrationFailed.cause` holds the original exception as an attribute (not just via exception chaining).
- `MigrationPaused` is raised at the top level when `PauseStep` is *raised* from inside `op.step` or `op.run_python` (paired with the step-level signal in §13.2.2; `PauseStep` is never returned, only raised).
- `VerificationFailed.conflicts` is a list of Conflict objects (see §7).
- `ProductionGuardError.detected_env`:
  - If declared explicitly (§15.2 `joryu.set_environment(...)` or `joryu.toml`), the declared string (`"staging"` / `"production"`).
  - If detected heuristically as production-like, `"production-like"` (local detection does not raise).
  - `host` is the hostname extracted from the connection string (or `None` when not extractable, e.g., a SQLite file path).
- Every exception returns a human-readable summary from `str(exc)`.

---

## 17. Configuration file (`joryu.toml`)

```toml
[joryu]
migrations_dir = "migrations"

[metadata]
target = "myapp.models:Base.metadata"

[database]
url = "env:DATABASE_URL"

[generate]
include_schemas = ["public"]
exclude_tables  = ["spatial_ref_sys"]

[dialects]
# Dialects to exercise during development (joryu test default).
test_targets = ["postgresql", "mysql", "sqlite"]
```

---

## 18. Comparison with Alembic and Django

| Item | Alembic | Django | joryu |
|---|---|---|---|
| Primary language | Python | Python | Python |
| Migration ID | random hex | per-app sequence | UTC timestamp |
| Ordering model | linked list (`down_revision`) | per-app sequence + cross-app deps | DAG (`depends_on` set) |
| Parallel PRs | requires `alembic merge` ceremony | numbering collisions → `makemigrations --merge` | independent changes merge freely; same-object changes caught by `joryu verify` |
| Autogeneration | DB required | from app models | from models (DB optional) |
| History replay | impossible (imperative ops) | possible (declarative Operations) | possible (declarative Operations) |
| Data migration | colocated | colocated via `RunPython` | colocated via `op.run_python` |
| Multi-dialect | hand-written branching | partial absorption via Django ORM | explicit three-layer model |
| Escape hatch | `op.execute(text(...))` | `RunSQL("...")` | `op.execute("..." \| dict)` first-class |
| Consistency check | none | none | `joryu verify` (DB checksum + Operations static analysis) |
| Forward-only | not designed | de facto | explicit |
| **Op semantics** | imperative (dies if it exists) | imperative | **Ensure-style (idempotent)** |
| **Interrupt / resume** | not supported (rollback by hand, then rerun) | same | **per-step resume; batched ops use checkpoints** |
| **Transactions** | 1 migration = 1 tx (fixed) | DB-dependent | `per_migration` / `per_step` (default) / `none` |
| **Bulk data updates** | hand-written (no resume) | hand-written (no resume) | user writes the loop; library provides a checkpoint API for resume |

---

## 19. Alembic migration tool

> joryu ships the Alembic migration tool (`joryu import alembic`) from the v1 release to minimize switching cost for existing Alembic users.

### 19.1 Approach

```
joryu import alembic --alembic-dir=./alembic --output-dir=./migrations
```

#### Phase 1: structural conversion (automatic)
- Scan all `versions/*.py`.
- Convert the `revision` / `down_revision` linked list into a `depends_on` DAG.
- Normalize filenames to `<timestamp>_<slug>.py` (random hex → inferred timestamp by parsing `alembic history`; the original hex is preserved in `tags`).
- Rewrite `op.add_column(..., sa.Column(name, type, ...))` → `op.add_column(..., name, type, ...)`.
- Rewrite `op.execute(text("..."))` → `op.execute("...")`.
- Wrap `upgrade()` / `downgrade()` with `@joryu.migration` / `@joryu.downgrade`.

#### Phase 2: heuristic conversion (semi-automatic, with confirmation prompts)
- `op.batch_alter_table(...)` → `with op.batch(...)`.
- `if op.get_bind().dialect.name == ...` → suggested rewrite to `op.execute({dialect: ...})`.
- Detect `op.execute("CONCURRENTLY ...")` and suggest `transaction_mode="none"`.
- Long-form data migrations (`op.execute("UPDATE ...")`) → suggest extracting into `op.run_python`.

#### Phase 3: manual-review residue (left as comments)
- Code that cannot be auto-converted is left as-is with a `# JORYU-IMPORT-TODO: ...` comment.
- `joryu import alembic --report` produces a TODO listing.
- Dialect-specific kwargs inside `op.create_table` (`postgresql_using=`, `mysql_engine=`) are preserved as-is; joryu provides compat wrappers.

### 19.2 State table handover

```
joryu import alembic --migrate-state
```

- Read the current revision from the existing `alembic_version` table.
- INSERT into the new `joryu_migrations` all corresponding migration IDs as "applied."
- The `alembic_version` table is preserved (for rollback). Drop it explicitly with `joryu import alembic --drop-alembic-table`.

### 19.3 Side-by-side operation (gradual migration)

Supports a transition period where Alembic and joryu run side by side:
- `joryu apply` does not read `alembic_version` (independent).
- During the transition, run both CLIs in sequence.
- Recommended flow: import → freeze Alembic on every PR → run joryu only → drop `alembic_version`.

### 19.4 Constraints

- Alembic-specific ops like `op.bulk_insert(...)` have no direct equivalent; they must be rewritten as `op.run_python` by hand.
- Branch labels and multiple heads collapse into a single DAG (merge revisions become migrations with multiple parents in `depends_on`).
- Custom migration templates are not converted.

---

## 20. Design decisions log

Resolved decisions (with section pointers):

- [x] **Migration declaration style**: `@joryu.migration(...)` decorator (§3.2).
- [x] **`op.execute(dict)` key normalization**: `postgresql` / `mysql` / `mariadb` / `sqlite` are independent keys; `default` exists; a bare string is also accepted (§6.1).
- [x] **`op.batch` triggering**: explicit opt-in; `generate` auto-wraps code on SQLite targets (§4.4).
- [x] **History replay granularity**: Operations replay as the primary mechanism; `run_python` / raw SQL annotated with `op.declare_schema_change()` (§12).
- [x] **Logical group across dialects**: bind with the `group=` parameter (§6.1 Layer 3).
- [x] **Direct generative-AI mode**: not shipping (permanent); ship an official skill instead (§1 non-goals).
- [x] **Alembic migration tool**: shipped from v1 (§19).
- [x] **`joryu test` implementation**: two tiers — unit (in-memory) + integration (testcontainers) (§6.4).
- [x] **Half-failed state handling**: interactive 5-choice prompt (§10.5).
- [x] **Production detection**: heuristic + explicit declaration (§15).
- [x] **Checkpoint API**: fully specified in §13.3.
- [x] **User-defined steps (`op.step`)**: specified in §13.2.
- [x] **Python version**: 3.11+ (§1 runtime requirements).
- [x] **AI skill distribution**: shipped in-repo at `.claude/skills/joryu/` (§1 official AI skill distribution).
- [x] **`op.declare_schema_change` vocabulary frozen in v1** (§12.2, "v1 vocabulary" sub-table): all entities (column / table / index / constraint / extension / enum / view / materialized_view / trigger / policy / sequence / schema) support `add` and `drop`; `altered` is defined only for `column`; `renamed` is defined for `column` and `table`; `index_renamed` is intentionally omitted (raw SQL path).
- [x] **Alembic import edge cases** (§19):
  - branch labels: ignored (dropped),
  - multiple heads: imported as parallel leaves,
  - merge revisions: multiple parents in `depends_on`,
  - `bulk_insert`: stubbed as `op.run_python` with a TODO comment,
  - custom templates: skipped with a warning.
- [x] **CI templates**: official GitHub Actions / GitLab CI / pre-commit templates under `examples/ci/`.
- [x] **`op.step` async runtime**: abstracted via **anyio** (§13.2.2).
- [x] **Sync `op.step` as a first-class case** (§13.2.2): SQLAlchemy Core/ORM is the expected dominant pattern.
- [x] **Real-time progress display**: two-phase (registration → execution), TTY/plain/JSON modes, `checkpoint.report()` API (§14).

---

## Appendix A: Migration examples

### A.1 Simple DDL (dialect-automatic)

```python
"""Add users table."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260514T093000_add_users")
def upgrade():
    op.create_table(
        "users",
        op.column("id",    t.BigInt, primary_key=True, autoincrement=True),
        op.column("email", t.Text,   nullable=False, unique=True),
        op.column("created_at", t.Timestamp, server_default=op.func.now()),
    )

@joryu.downgrade
def downgrade():
    op.drop_table("users")
```

### A.2 Includes data migration

```python
"""Normalize emails and enforce NOT NULL."""
import joryu
from joryu import op, types as t
from sqlalchemy import text

@joryu.migration(
    id="20260517T140000_normalize_emails",
    depends_on=["20260514T093000_add_users"],
)
def upgrade():
    op.add_column("users", "email_normalized", t.Text)

    def backfill(conn, dialect, checkpoint):
        conn.execute(text("UPDATE users SET email_normalized = LOWER(email) WHERE email_normalized IS NULL"))

    op.run_python(backfill)
    op.alter_column("users", "email_normalized", nullable=False)
```

### A.3 Per-dialect SQL (Layer 2)

```python
"""Add GIN index on settings JSON column."""
import joryu
from joryu import op

@joryu.migration(
    id="20260520T100000_settings_index",
    transaction_mode="none",     # uses CREATE INDEX CONCURRENTLY
)
def upgrade():
    op.execute({
        "postgresql": "CREATE INDEX CONCURRENTLY users_settings_idx ON users USING GIN (settings)",
        "mysql":      "CREATE INDEX users_settings_idx ON users ((CAST(settings AS CHAR(255))))",
        "sqlite":     "CREATE INDEX users_settings_idx ON users (json_extract(settings, '$'))",
    })

@joryu.downgrade
def downgrade():
    op.execute("DROP INDEX users_settings_idx")
```

### A.4 Dialect-restricted file + group (Layer 3)

```python
"""Postgres-only: enable pgcrypto and create RLS policy."""
import joryu
from joryu import op

@joryu.migration(
    id="20260525T093000_pg_rls",
    dialects=["postgresql"],
    group="20260525_rls",
    depends_on=["20260514T093000_add_users"],
)
def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY users_self ON users USING (id = current_setting('app.user_id')::bigint)")
```

### A.5 SQLite-compatible constraint change (batch)

```python
"""Make users.email NOT NULL."""
import joryu
from joryu import op

@joryu.migration(id="20260601T120000_email_not_null")
def upgrade():
    with op.batch("users") as batch:
        batch.alter_column("email", nullable=False)
    # Postgres / MySQL: ALTER TABLE users ALTER COLUMN email SET NOT NULL
    # SQLite: create new table → copy data → rename
```

### A.6 Directly from a SQLAlchemy model

```python
"""Create orders table from model."""
import joryu
from joryu import op
from myapp.models import Order

@joryu.migration(id="20260605T140000_add_orders")
def upgrade():
    op.create_table_from_model(Order)

@joryu.downgrade
def downgrade():
    op.drop_table("orders")
```

### A.7 Bulk update (resumable, user-written batching loop)

A safe production-grade UPDATE on tens of millions of rows. joryu provides only the checkpoint infrastructure; the user writes the loop. On interruption, resume from the saved cursor.

```python
"""Backfill users.email_normalized for 50M rows."""
import joryu
from joryu import op, types as t
from sqlalchemy import text

BATCH_SIZE = 10_000

@joryu.migration(
    id="20260620T030000_backfill_email_normalized",
    depends_on=["20260619T120000_add_email_normalized_col"],
    transaction_mode="per_step",   # default, but explicit
)
def upgrade():
    # Step 1: ensure semantics — skipped if already present.
    op.add_column("users", "email_normalized", t.Text, nullable=True)

    # Step 2: partial index for narrowing unprocessed rows (drop after backfill).
    op.create_index("tmp_users_unnormalized", "users", ["id"],
                    where="email_normalized IS NULL")

    # Step 3: user-written batching loop; joryu persists the checkpoint.
    def backfill(conn, dialect, checkpoint):
        cursor = checkpoint.get("last_id", 0)
        while True:
            rows = conn.execute(text(
                "SELECT id FROM users "
                "WHERE id > :c AND email_normalized IS NULL "
                "ORDER BY id LIMIT :n"
            ), {"c": cursor, "n": BATCH_SIZE}).fetchall()
            if not rows:
                return
            conn.execute(text(
                "UPDATE users SET email_normalized = LOWER(email) "
                "WHERE id = ANY(:ids)"
            ), {"ids": [r.id for r in rows]})
            cursor = rows[-1].id
            checkpoint.set("last_id", cursor)   # commit + persist

    op.run_python(backfill)

    # Step 4: drop the temporary index.
    op.drop_index("tmp_users_unnormalized")

    # Step 5: with every row populated, enforce NOT NULL.
    op.alter_column("users", "email_normalized", nullable=False)
```

**Interruption / resume scenario**:
```
$ joryu apply
→ step 3 reaches 30% (15M/50M rows) and is OOM-killed
→ DB state: status='failed', steps[3].progress='{"last_id": 15003421}'

$ joryu status
20260620T030000_backfill_email_normalized  failed
  ✓ step 1 (add_column)         done
  ✓ step 2 (create_index)       done
  ⚠ step 3 (run_python)         failed at last_id=15003421 (30%)
  · step 4 (drop_index)         pending
  · step 5 (alter_column)       pending

$ joryu apply
→ steps 1 and 2 skip; step 3 resumes from last_id=15003421; steps 4 and 5 execute
```

### A.8 Mid-failure on multi-stage DDL (common case)

```python
"""Add three columns and an index on col2."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260622T100000_add_columns_and_idx")
def upgrade():
    op.add_column("users", "col1", t.Text)
    op.add_column("users", "col2", t.Text)
    op.create_index("idx_col2", "users", ["col2"])
    op.add_column("users", "col3", t.Text)
```

**Run 1: step 3 fails → remove the cause → continue**

```
$ joryu apply
→ steps 1 and 2 complete (col1, col2 added)
→ step 3 (CREATE INDEX idx_col2) fails: index 'idx_col2' already exists
→ migration failed, step 4 not executed

$ joryu status
20260622T100000_add_columns_and_idx  failed
  ✓ step 1 add_column(users.col1)
  ✓ step 2 add_column(users.col2)
  ✗ step 3 create_index(idx_col2)        ← failed
  · step 4 add_column(users.col3)        pending

# Fix: drop or rename the existing idx_col2.

$ joryu apply
→ steps 1 and 2 skip (ensure: already present)
→ step 3 retries → success
→ step 4 runs → success
→ applied
```

**Run 2: edit the file in response to the failure**

Rename the index to `idx_users_col2` and commit. Re-run `joryu apply`:

- Migration is `failed`, so a checksum change is allowed (§10.3).
- Step 1 and 2 fingerprints unchanged → skip.
- Step 3 fingerprint changes → execute with the new content (`idx_users_col2`).
- Step 4 executes → done.

### A.9 Ensure-style behavior example

```python
"""Add phone column — re-runnable safely."""
import joryu
from joryu import op, types as t

@joryu.migration(id="20260625T100000_add_phone")
def upgrade():
    # First run: column added.
    # Subsequent runs: already exists with matching type → skip (ensure semantics).
    # If someone manually added phone INTEGER, this errors on type mismatch.
    op.add_column("users", "phone", t.Text, nullable=True)
```

### A.10 User-defined step (`op.step`) and PauseStep

```python
"""Wait for replication lag before continuing."""
import joryu
from joryu import op
from sqlalchemy import text

@joryu.migration(id="20260701T100000_replication_aware_change")
def upgrade():
    op.add_column("users", "feature_flag", t.Bool, server_default="false")

    @op.step
    def wait_for_replication(conn, dialect, checkpoint):
        if checkpoint.get("ready"):
            return True
        lag = conn.execute(text("SELECT EXTRACT(EPOCH FROM (NOW() - pg_last_xact_replay_timestamp()))")).scalar()
        if lag is None or lag < 1.0:
            checkpoint.set("ready", True)
            return True
        raise op.PauseStep(f"replica lag={lag:.1f}s, retry later")

    op.alter_column("users", "feature_flag", nullable=False)
```
