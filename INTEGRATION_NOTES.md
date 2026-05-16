# Wave 2 — `virtual_schema` / `autogen` / `downhint` integration notes

This document is a hand-off note for whoever merges the three Wave-2 sub-agents
together. Only one tiny coordination patch is needed; everything else is
self-contained behind existing public APIs.

## Patches the integrator must apply

### 1. Wire `op.historical_model` to the new replayer

File: `src/joryu/_op_impl.py`

The v0.1 stub:

```python
def historical_model(table_name: str) -> None:
    # TODO(v0.2): replay the operations history into an in-memory MetaData and
    # surface the table-as-of-this-migration as a SQLAlchemy Table. v0.1 returns
    # None so callers can fall back to direct conn.execute(text(...)).
    return None
```

should become:

```python
def historical_model(table_name: str):
    from .virtual_schema import historical_table
    return historical_table(table_name)
```

The new return type is `VTable | None` (see `joryu.virtual_schema.VTable`),
which carries `columns: dict[str, VColumn]`. Callers that previously branched
on `is None` still work.

### 2. Runner sets the "currently-executing migration" contextvar

File: `src/joryu/runner.py` (Wave-2 sibling owns this file)

Around each migration's execution-phase loop, the runner should call:

```python
from ._runtime import set_current_migration, reset_current_migration

token = set_current_migration(migration.id)
try:
    # ... apply ops ...
finally:
    reset_current_migration(token)
```

Until this lands, `op.historical_model("...")` falls back to "everything
declared so far," which is the safe over-approximation (callers see a schema
that includes the migration currently being executed). Wiring the contextvar
gives callers the documented "schema as of *just before* this migration."

## Files added by this agent

- `src/joryu/virtual_schema.py` — VirtualSchema dataclasses + replayer + the
  `historical_table(name)` helper used by `op.historical_model`.
- `src/joryu/autogen.py` — `diff_schemas` / `render_migration` /
  `generate_diff` / `metadata_to_virtual_schema` / `emit_down_hints`.
- `src/joryu/downhint.py` — `DownHints` dataclass + `parse_hints` / `emit_hints`.
- `src/joryu/_runtime.py` — `set_current_migration` / `get_current_migration`
  contextvar helpers.
- `tests/test_virtual_schema.py`, `tests/test_autogen.py`,
  `tests/test_downhint.py` — 24 new tests, all green against the v0.1 baseline.

## Files rewritten

- `src/joryu/generate.py` — keeps the `--empty` path identical (it's now
  `_generate_empty` internally), and adds the autogen path when a `target=`
  MetaData is passed. The CLI shim continues to pass `empty=True` so existing
  behaviour is preserved.

## Contract assumptions

- `op.declare_schema_change(...)` keyword shapes follow §12.2 verbatim. The
  replayer tolerates tuples one shorter than the spec when the trailing field
  is an "opts" / "spec" dict (defaults to `{}`).
- `metadata_to_virtual_schema()` stores SQLAlchemy types as their `str(t.type)`
  rendering, not as joryu `TypeSpec` instances — sufficient for the v0.2 diff
  which is structural (column presence / absence) rather than type-deep.
  Deep-type comparison is the AlterColumnOp lane (sibling agent).
- `emit_down_hints` derives `cross-references=[]` — the AI completion prompt
  (§11.3) fills it in from `models/`. The v0.2 generator does not introspect
  the bound metadata for FK graphs.
- Replay is best-effort: malformed declare entries log nothing and skip,
  rather than aborting `joryu generate --against=replay`.

## What is NOT implemented (deferred)

- Deep type-mismatch diffs in `diff_schemas` (would conflict with the
  AlterColumnOp sibling agent).
- `cross-references` field population in `emit_down_hints` (requires the
  live FK / index graph from the bound metadata).
- `index_renamed` and other v2 vocabulary in `declare_schema_change` —
  intentionally omitted in v1 per §12.2 note.

---

# Wave 2 — Alembic importer + `joryu test --unit` integration notes

This section is owned by the importer / testing sub-agent. Files added are
self-contained; CLI wiring is the only coordination point.

## Spec sections implemented

- §19 (Alembic migration tool), Phase 1 in full + lightweight Phase 2/3
  rewrites + §19.2 state handover (`--migrate-state`, `--drop-alembic-table`).
- §6.4 unit-tier (`joryu test --unit`): in-memory SQLite apply, re-apply for
  ensure-semantics idempotency, then `joryu.verify` conflict detection.
  Integration tier raises `NotImplementedError` (testcontainers — v0.3).

## Files added

- `src/joryu/importer/__init__.py` — re-exports `ImportReport`, `import_alembic`.
- `src/joryu/importer/alembic_importer.py` — the converter. stdlib + sqlalchemy
  only; **does NOT import alembic**. Parses `versions/*.py` with `ast` and uses
  text substitution for the function bodies (Phase 1) plus a handful of
  Phase 2/3 best-effort rewrites (`batch_alter_table` -> `op.batch`, dialect
  `if` block annotation, `op.bulk_insert` stubbed as `op.run_python`).
- `src/joryu/testing.py` — `run_unit_tests` + `UnitTestReport`.
- `tests/test_importer.py` (11 tests) and `tests/test_unit_testing.py`
  (5 tests). All green.

## Patches the integrator must apply to `src/joryu/cli.py`

The current stub implementations in `cli.py` (DO NOT touch yourself if you
own the importer/testing lane) should be replaced with calls to the new APIs.
Apply these two one-line drop-ins.

### A. `joryu import alembic` — replace the body of `import_alembic_cmd`

Replace the last two lines of `import_alembic_cmd` with:

```python
    from .importer import import_alembic as _import_alembic
    try:
        rep = _import_alembic(
            alembic_dir=alembic_dir,
            output_dir=output_dir,
            migrate_state=migrate_state,
            drop_alembic_table=drop_alembic_table,
        )
    except BaseException as exc:
        _die(exc)
        return
    click.echo(f"converted: {rep.files_converted}, skipped: {rep.files_skipped}")
    if report:
        for path, msg in rep.todos:
            click.echo(f"  TODO {path}: {msg}")
    sys.exit(EXIT_OK)
```

(The CLI already declares `--alembic-dir`, `--output-dir`, `--migrate-state`,
`--drop-alembic-table`, `--report`. The `--url` argument is not yet exposed;
add it if you want CLI-side state handover, otherwise the Python API
`from joryu.importer import import_alembic` works today.)

### B. `joryu test --unit` — replace the body of `test_cmd`

```python
    if integration:
        click.echo("joryu test: integration mode is v0.3 (testcontainers)", err=True)
        sys.exit(EXIT_OK)
    from .testing import run_unit_tests
    try:
        rep = run_unit_tests(migrations_dir=Path("migrations"))
    except BaseException as exc:
        _die(exc)
        return
    click.echo(
        f"applied {rep.applied}/{rep.total} in {rep.duration_ms}ms; "
        f"conflicts={len(rep.conflicts)} failures={len(rep.failed)}"
    )
    for mid, err in rep.failed:
        click.echo(f"  FAIL {mid}: {err}", err=True)
    for c in rep.conflicts:
        click.echo(f"  CONFLICT {c.message}", err=True)
    sys.exit(EXIT_OK if rep.ok else EXIT_VERIFY_FAILED)
```

The Python API is also usable directly today:

```python
from joryu.importer import import_alembic
from joryu.testing import run_unit_tests
```

## Contract assumptions

- The Alembic importer only handles `version_table='alembic_version'` (the
  default). Custom version tables would need a flag — out of scope for v0.2.
- The `revision` and `down_revision` module-level assignments must be string
  literals; computed values (rare in practice) are not parsed.
- Filenames collide-by-second using the source file mtime (UTC). When the
  mtime would yield a duplicate `<timestamp>_<slug>` stem, `_2` / `_3` / …
  suffixes are appended per §3.1.
- Original Alembic hex is preserved in `tags=["alembic:<hex>"]` so existing
  `alembic history` greps still find migrations.
- `op.bulk_insert(...)` becomes a stubbed `op.run_python(lambda conn, dialect,
  checkpoint: None)` plus a `# JORYU-IMPORT-TODO:` comment — there is no
  generic translator (per §19.4).
- `run_unit_tests` uses a throw-away on-disk SQLite under a `TemporaryDirectory`
  (NOT `sqlite:///:memory:`), because the runner opens and closes connections
  and `:memory:` creates a fresh DB per connection. The semantics are the
  same as the spec's "in-memory SQLite" requirement.
- `run_unit_tests` calls `reset_registry()` + purges `joryu_migrations.*`
  modules so the call is idempotent across invocations in a single Python
  process.
- Integration tier (`dialect != "sqlite"`) raises `NotImplementedError`; the
  v0.3 testcontainers implementation will replace that branch.

## What is NOT implemented (deferred)

- Phase 2 confirmation prompts: rewrites that need a human in the loop are
  emitted as `# JORYU-IMPORT-TODO:` comments and aggregated in
  `ImportReport.todos`. No interactive UI yet.
- `op.execute("CONCURRENTLY ...")` -> `transaction_mode="none"` auto-detection.
- Branch labels and merge revisions: the spec calls for collapsing them; v0.2
  treats every `down_revision` value (string, tuple, or list) as `depends_on`
  entries but does not emit a separate "merge migration."
- Custom Alembic templates / non-standard `versions/` layouts.
- `op.add_column(..., sa.Column(..., server_default=sa.text("now()")))` —
  the `server_default` kwarg is passed through as a Python expression, so the
  emitted file will contain `server_default=sa.text('now()')`. Users will
  need to clean this up by hand (or use `t.now()`).

---

# Wave 2 — Deep mismatch / SQLite alter / interactive recovery integration notes

This section is owned by the AddColumnOp + AlterColumnOp + interactive-recovery
sub-agent. Files added are self-contained; the only CLI coordination point is
exposing the new `non_interactive` / `on_failure` flags on `joryu apply`.

## Spec sections implemented

- §9.4 + §9.5: deep type-mismatch and nullability detection inside
  `AddColumnOp.apply`, honouring `on_mismatch` (`"error"` / `"alter"` /
  `"skip"`) with per-op override winning over the migration-wide default.
- §A.5 + §4.4: `op.alter_column` on SQLite now auto-wraps into a single-op
  `BatchTableRebuildOp` — users no longer need to write `with op.batch(...)`
  manually for the common nullable / type change. `op.batch` continues to
  work for multi-op rebuilds and remains the documented form.
- §10.5: half-failed recovery. The runner detects `status='failed'` rows up
  front and either prompts the operator (TTY + `non_interactive=False`) or
  picks programmatically (`non_interactive=True, on_failure="resume"|
  "restart"|"abort"`). All five §10.5 menu actions are wired:
  `resume` / `restart_from` / `restart_all` / `skip_step` / `abort`.

## Files modified

- `src/joryu/_op_impl.py` — added `_type_matches`, `_canon_type`,
  `_TYPE_SYNONYMS`; rewrote `AddColumnOp.apply`; added the SQLite
  auto-batch path inside `AlterColumnOp.apply`.
- `src/joryu/runner.py` — added `RecoveryDecision`, `_prompt_recovery`,
  `_resolve_recovery`, `_apply_recovery_pre`; threaded `non_interactive` /
  `on_failure` from `apply()` into `_apply_core` and `_apply_one`.

## Files added

- `tests/test_ensure_mismatch.py` (4 tests)
- `tests/test_alter_column_sqlite.py` (1 test)
- `tests/test_recovery.py` (4 tests)

## Patches the integrator must apply to `src/joryu/cli.py`

`joryu apply` must accept the new flags and pass them through. Suggested
click options:

```python
@click.option("--non-interactive", is_flag=True, default=False,
              help="Suppress the §10.5 failure-recovery prompt.")
@click.option("--on-failure", type=click.Choice(["resume", "restart", "abort"]),
              default="resume",
              help="Non-interactive failure strategy (see §10.5).")
```

…and forwarded to `runner.apply(non_interactive=..., on_failure=...)`.
Default behaviour when neither flag is set: TTY -> prompt; non-TTY -> behave
as `non_interactive=True, on_failure="resume"`.

## Contract assumptions

- The deep type comparator is intentionally lenient (synonym table +
  affinity-aware buckets) to avoid spurious ERRORs on rerun. Width changes
  (e.g. `VARCHAR(255)` -> `VARCHAR(512)`) only fire when *both* sides
  carry an explicit length and they differ. The risk is documented inline
  in `_TYPE_SYNONYMS`; users who need strict width invariants should write
  an explicit `op.alter_column`.
- The migration-wide `on_mismatch` (set via `@joryu.migration(on_mismatch=
  "alter")`) is respected: when the per-op default is still `"error"` but
  the migration default is something else, the migration default wins.
- `on_mismatch="alter"` on SQLite raises (mirrors the existing
  `alter_column` SQLite hint) — the operator must wrap the change in
  `op.batch(...)` for a table rebuild.
- The SQLite auto-batch path inside `AlterColumnOp.apply` constructs a
  fresh `BatchTableRebuildOp` *inside* `apply` so that the registered op
  (and therefore the conflict-detection target list and fingerprint) is
  still `AlterColumnOp`. `joryu verify` continues to see `alter_column` on
  the affected target — no surprise behaviour for static analysis.
- Recovery decisions are applied *before* `_apply_one` runs:
  - `restart_all`: deletes every step row for the migration.
  - `restart_from`: rewrites step rows `>= step_index` to `pending` and
    clears their progress.
  - `skip_step`: marks the failed step as `skipped`; the existing
    "previously skipped; not re-running" branch in `_apply_one` then
    advances naturally.
  - `abort`: raises `MigrationFailed("<recovery-abort>")` so the CLI exits
    with the existing failure code.
- `non_interactive=True` is the default for the `apply()` Python API so
  library callers do not block on a hidden prompt. The CLI should default
  to `False` so a human at a terminal gets the §10.5 menu.

## What is NOT implemented (deferred)

- Deep mismatch detection for `CreateTableOp` (only the column-presence
  check from v0.1 is in place; extending it to nullability / types would
  duplicate the `AddColumnOp` logic and is left to v0.3).
- A dry-run preview of the recovery decision (the prompt acts
  immediately). For CI use the `non_interactive=True` knobs above.
- Skipping multiple consecutive failed steps in one prompt round —
  `skip_step` skips exactly the currently-failed step.

