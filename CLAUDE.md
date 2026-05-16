# joryu — Project Guidelines for Claude

## Language policy

- **All artifacts in this repository are written in English** by default. This includes:
  - Source code, comments, docstrings
  - Documentation (READMEs, design docs, ADRs, contributor guides)
  - Commit messages, PR descriptions
  - Issue titles and bodies
  - Error messages and log output
  - CLI help text
  - Generated migration file boilerplate (including `JORYU-DOWN-HINT:` comments)
- **Sole exception**: `SPEC.md` is written in Japanese while the design is being negotiated with the maintainer. Once the spec is frozen, it will be translated to English alongside the rest of the docs.
- When asked to "write a doc / readme / comment / message," produce English output unless the request explicitly mentions `SPEC.md` or asks for Japanese.
- When editing code or docs, do not introduce mixed-language comments — match the language of the surrounding file.

## Project context

joryu is a Python migration library, intended as a modern alternative to Alembic. The full design is in `SPEC.md`. Key invariants Claude should preserve when working in this repo:

- **Python 3.11+** baseline (uses `tomllib`, `Self`, Exception Groups, `asyncio.TaskGroup`).
- **Decorator-based migration declaration**: `@joryu.migration(id=..., depends_on=...)`, not module-level attributes.
- **Python-first migration files** with `op.execute()` / `op.run_python()` escape hatches (not SQL-first).
- **Ensure-style operations**: `op.add_column` etc. are idempotent — they assert desired state, not imperatively mutate.
- **Per-step transaction mode is the default**, because MySQL's implicit DDL commit makes per-migration transactions a lie.
- **Forward-only by default**; `down` is dev-only and uses `JORYU-DOWN-HINT:` structured comments so AI tools can complete the rollback.
- **Parallel-PR safe**: no central sum file. Conflicts are detected via Operations static analysis (`joryu verify`) and DB-side per-migration checksums.
- **Checkpoint-based resumability**: data migrations use `op.run_python(fn)` with a `checkpoint` object; the library persists progress to `joryu_migration_steps.progress` and resumes on rerun.
- **User-defined steps via `op.step`** are first-class (sync + async, custom completion via return value, `PauseStep` / `SkipStep` exceptions).
- **Multi-dialect first**: a single migration file should run on PostgreSQL, MySQL, and SQLite. Where they diverge, use `op.execute({dialect: sql, ...})` or limit the file with `dialects=[...]` + `group=...`.
- **AI skill is shipped in-repo** at `.claude/skills/joryu/`; never add a `--from-prompt` flag to the CLI.
- **Alembic import path** (`joryu import alembic`) ships from v1.
