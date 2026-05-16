---
name: joryu
description: Drive the joryu Python migration library — generate, apply, verify, downgrade migrations from natural-language intent.
---

# joryu skill

This skill teaches the assistant to drive the `joryu` CLI on the user's
behalf. joryu is a Python 3.11+ migration library (modern alternative to
Alembic). The full spec lives in `SPEC.md` at the project root.

## Distribution note

This skill ships inside the joryu GitHub repository under
`.claude/skills/joryu/`. The project's `.gitignore` excludes `.claude/`,
so users typically copy this directory into their own project root (or
add it as a git submodule / symlink). The skill is portable: no paths
inside it are hard-coded to the joryu repo.

## When to activate

Activate when the user expresses intent that matches any of:

- "add a column / table / index / constraint to <X>"
- "create a migration for <X>"
- "generate a migration / scaffold / skeleton"
- "apply / run migrations"
- "rollback / downgrade / undo this migration"
- "complete the downgrade" / "fill in the JORYU-DOWN-HINT"
- "verify migrations" / "check for migration conflicts" (CI gate)
- "import this Alembic project" / "switch from Alembic to joryu"
- "explain this migration" / "what does migration X do"
- "snapshot the current schema"

Do not activate for application code that merely uses SQLAlchemy models
without touching `migrations/` or running `joryu` commands.

## Standard playbook

Follow this order every time, even when only one step appears to be
requested. Skipping `status` is a common cause of incorrect actions.

1. **Read state first.** Run `joryu status` to learn which migrations
   are applied / pending / failed / paused. Treat any `failed` or
   `paused` migration as a blocker; surface it to the user before
   proceeding.

2. **Get current schema context.** Run
   `joryu schema-snapshot --format=json` and read it before generating
   or completing downgrades. This is the canonical "current DB" view
   joryu itself uses.

3. **Generating a new migration.**
   - Prefer `joryu generate <slug> --empty` to scaffold a file, then
     edit the body. The auto-diff form (`joryu generate <slug>`
     without `--empty`) is fine when the user has updated their
     SQLAlchemy models and wants the diff captured.
   - Slugs are lowercase ASCII + `_`, max 60 chars (e.g. `add_users`,
     `backfill_email_normalized`).
   - The generated filename is `<UTC timestamp>_<slug>.py`. Do not
     rename it.

4. **Editing the migration body.**
   - Use `op.*` from `joryu` (see `reference/api.md`). Never write
     `sa.Column(...)` — the joryu op signature takes positional
     `(table, name, type, **kwargs)`.
   - Types live in `joryu.types as t` (e.g. `t.Text`, `t.BigInt`,
     `t.Timestamp`, `t.Json`).
   - Per-dialect SQL: `op.execute({"postgresql": "...", "mysql": "...",
     "sqlite": "...", "default": "..."})`.
   - Data migrations: `op.run_python(fn)` where `fn(conn, dialect,
     checkpoint)`. Use `checkpoint.get(key)` / `.set(key, value)` to
     make the loop resumable (see
     `templates/data_migration_resumable.py`).
   - SQLite constraint changes go inside `with op.batch("table") as
     b: ...`.

5. **Completing a downgrade.** When the user asks to "complete the
   downgrade" or fill in a stub:
   - Parse the `JORYU-DOWN-HINT:` block (see
     `reference/down_hints.md` for the full field vocabulary).
   - Cross-reference `models/*.py` for FK relationships and
     `schema-snapshot` output for dependent objects.
   - Honour `order-constraint`.
   - If `data-loss-risk: irreversible`, comment out the body, add a
     note explaining why a human must intervene, and stop. Do not
     guess a body.
   - When done, update `completion-status:` from `stub` to `complete`
     (or `partial` / `manual-review-required` if appropriate).
   - Never modify `upgrade()`.
   - The standard prompt (verbatim from SPEC §11.3):

     > Complete the downgrade() in this migration file. Use the
     > JORYU-DOWN-HINT: comments as the source of truth for what must
     > be undone. Cross-check with models/*.py for FK relationships.
     > If data-loss-risk is "irreversible", comment out the downgrade
     > body and add a clear note explaining why. Update
     > completion-status when done. Do not modify the upgrade()
     > function.

6. **CI / pre-merge gate.** Run `joryu verify`. Exit code 4 means a
   semantic conflict was detected — read the `Conflict` messages and
   suggest a rebase + `depends_on` fix (see
   `reference/conflict_taxonomy.md`).

7. **Local apply.** Run `joryu apply`. Exit codes:
   `2` migration failed, `3` paused (`PauseStep`), `4` verify,
   `5` production guard, `6` type misuse. Surface the offending
   migration id / step to the user before suggesting a fix.

8. **Importing an Alembic project.** Run
   `joryu import alembic --alembic-dir=<dir> --output-dir=migrations`.
   Add `--migrate-state` to copy the current `alembic_version` row,
   `--report` for a TODO listing. Look for
   `# JORYU-IMPORT-TODO:` comments in the output and walk the user
   through them.

9. **Show diffs first.** Before staging any new file or running
   `joryu apply`, show the user the file contents and the planned
   command. Do not run `joryu apply` automatically against a remote
   database without explicit confirmation. Do not delete migration
   files (they are append-only; checksum violations are detected by
   `joryu verify`).

10. **Editing already-applied migrations.** Forbidden by default.
    `joryu apply` / `verify` will raise a checksum mismatch. If the
    user truly intends it, run `joryu repair <id>` to update the
    stored checksum — and explain the implication before doing so.

## Useful invariants to remember

- Operations are **ensure-style** (idempotent). Re-applying a partly
  finished migration is safe; skipped steps are reported as such.
- Default `transaction_mode` is `per_step` (MySQL's implicit DDL
  commit makes per-migration transactions unreliable).
- joryu is **forward-only** for production; `joryu down` refuses
  production-like connections without `--allow-prod`.
- There is no central "sum file" — parallel PRs do not conflict on
  joryu's own state.
- A `joryu generate --from-prompt` flag does not exist and will not
  be added. The user's editor-side AI (this skill) is the
  generation interface.

## Reference files

- `reference/api.md` — `op.*` and `types` reference, plus the
  Alembic-vs-joryu contrast table.
- `reference/down_hints.md` — `JORYU-DOWN-HINT:` field vocabulary
  and standard downgrade-completion prompt.
- `reference/conflict_taxonomy.md` — semantic-conflict commutativity
  table and conflict-kind priority order.

## Templates

- `templates/empty_migration.py` — output of `joryu generate --empty`.
- `templates/data_migration_resumable.py` — resumable bulk update
  with `checkpoint`.
- `templates/per_dialect_sql.py` — per-dialect `op.execute(dict)`
  example with `transaction_mode="none"`.
