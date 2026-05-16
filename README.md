# joryu

A modern Python migration library for SQLAlchemy projects. joryu reads
diffs from SQLAlchemy models, generates Python-first migration files,
applies them with per-step idempotent semantics, and makes parallel
PRs safe by default. It is designed to be driven by either humans or
LLM coding assistants.

## Quickstart

```bash
pip install joryu

joryu init                         # write joryu.toml and create migrations/
joryu generate add_users --empty   # scaffold migrations/<timestamp>_add_users.py
# edit the file, then:
joryu apply
```

A minimal migration body:

```python
import joryu
from joryu import op, types as t

@joryu.migration(id="20260514T093000_add_users")
def upgrade():
    op.create_table(
        "users",
        op.column("id",    t.BigInt, primary_key=True, autoincrement=True),
        op.column("email", t.Text,   nullable=False, unique=True),
        op.column("created_at", t.Timestamp, server_default=t.now()),
    )

@joryu.downgrade
def downgrade():
    op.drop_table("users")
```

## What's different from Alembic

| Item                | Alembic                                                          | joryu                                                 |
|---------------------|------------------------------------------------------------------|-------------------------------------------------------|
| Add column          | `op.add_column("u", sa.Column("e", sa.Text(), nullable=False))`  | `op.add_column("u", "e", t.Text, nullable=False)`     |
| Raw SQL             | `op.execute(text("..."))`                                        | `op.execute("...")`                                   |
| Per-dialect SQL     | `if op.get_bind().dialect.name == "postgres": op.execute(...)`   | `op.execute({"postgresql": "...", "mysql": "..."})`   |
| Op semantics        | imperative (errors if it exists)                                 | ensure-style (idempotent)                             |
| Parallel PRs        | requires `alembic merge` ceremony                                | merge freely; `joryu verify` catches same-object edits |
| Interrupt / resume  | not supported (manual rollback, then rerun)                      | per-step resume; bulk ops use checkpoints             |

See `SPEC.md` §18 for the full comparison (Alembic vs Django vs joryu).

## How it works in 30 seconds

```
project/
├── models/                                      ← SQLAlchemy models (source of truth)
├── migrations/
│   ├── 20260514T093000_add_users.py             ← Python-first, declarative ops
│   ├── 20260515T101200_add_email_index.py
│   └── 20260516T120000_seed_default_roles.py
└── joryu.toml                                   ← project configuration

           generate                  apply / verify
models/  ───────────►  migrations/  ─────────────►  joryu_migrations
                                                     joryu_migration_steps
                                                       (state in the DB)
```

- **Source of truth**: the user's SQLAlchemy `MetaData`.
- **Produced artifacts**: `migrations/*.py`. No central sum file, so
  parallel PRs never collide on the migration history itself.
- **State**: the `joryu_migrations` table in the target DB stores
  per-migration checksums; `joryu_migration_steps` stores per-step
  progress and checkpoints for resumable data migrations.
- **Conflict detection**: `joryu verify` runs static analysis over
  Operations and only flags genuine non-commutative pairs as ERROR.

## CLI

```
joryu init                                  # initial setup
joryu generate <slug> [--empty]             # new migration (empty scaffold or diff)
joryu apply [--target=<id>] [--dry-run]     # apply pending migrations
joryu status                                # applied / pending / failed / paused
joryu verify                                # CI gate: checksum + semantic conflicts
joryu down [--steps=N | --to=<id>]          # dev-only rollback
joryu schema-snapshot --format=json         # current schema (for AI assistance)
joryu show <id>                             # raw migration body
joryu explain <id>                          # natural-language render
joryu mark <id> --as=applied|pending|failed # manual state correction
joryu repair <id>                           # update checksum after an intentional edit
joryu test --unit                           # in-memory SQLite apply / re-apply
joryu import alembic --alembic-dir=./alembic --output-dir=./migrations
```

Full reference: `SPEC.md` §16. Exit codes: `0` ok, `2` migration
failed, `3` paused, `4` verify failure, `5` production guard, `6`
unsupported type usage.

## Requirements

- Python **3.11+** (uses `tomllib`, `Self`, exception groups,
  `asyncio.TaskGroup`).
- `sqlalchemy >= 2.0`.
- Supported dialects: **PostgreSQL**, **MySQL / MariaDB**,
  **SQLite**. A single migration file is expected to run on all
  three (see SPEC §6 for the three-layer dialect model).

## AI integration

joryu ships an editor-side skill in `.claude/skills/joryu/` (Claude
Code / Cursor / similar). The skill teaches the assistant the
`op.*` API, the `JORYU-DOWN-HINT:` vocabulary for downgrade
completion, and the conflict taxonomy. There is intentionally **no**
`joryu generate --from-prompt` flag — the editor AI is the
generation interface.

To use it, copy the directory into your project root:

```bash
cp -r path/to/joryu/.claude/skills/joryu .claude/skills/joryu
```

Standard prompt for downgrade completion (also in
`.claude/skills/joryu/reference/down_hints.md`):

> Complete the downgrade() in this migration file. Use the
> JORYU-DOWN-HINT: comments as the source of truth for what must be
> undone. Cross-check with models/*.py for FK relationships. If
> data-loss-risk is "irreversible", comment out the downgrade body
> and add a clear note explaining why. Update completion-status when
> done. Do not modify the upgrade() function.

## CI templates

Drop-in workflows live in `examples/ci/`:

- `github_actions.yml` — PR gate (`verify` + `test --unit`) and
  `apply` on push to `main`.
- `gitlab_ci.yml` — equivalent merge-request gate plus a manual
  production `apply` job.
- `pre-commit.yaml` — local `joryu verify` on every commit, optional
  `joryu test --unit` on push.

See `examples/ci/README.md` for wiring instructions.

## Further reading

- `SPEC.md` — the full design specification (architecture,
  Operations API, multi-dialect model, parallel-PR safety, downgrade
  semantics, Alembic import).
- `.claude/skills/joryu/` — the official AI skill.
- `examples/ci/` — official CI templates.

## License

MIT.
