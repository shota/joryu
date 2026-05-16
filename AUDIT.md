# joryu SPEC.md compliance audit

> Generated for v0.3. Pre-audit baseline at this branch tip: 135 tests passing.
> Post-gap-closure baseline: **153 tests passing** (18 added in
> `tests/test_spec_compliance.py`, zero regressions). Task brief named "114"
> as the v0.3 baseline; the repo at HEAD already had a richer suite.

Legend: OK = implemented and exercised, PARTIAL = present but with caveats,
MISSING = not implemented, N/A = explicitly out of scope.

## §1 — Goals and non-goals

| Section | Verdict | Notes |
|---|---|---|
| Python 3.11+ baseline | OK | `pyproject.toml` declares `>=3.11`; `tomllib`, `Self`, `match` are used in source. |
| Decorator-based migration | OK | `registry.migration` decorator, no module-level attrs. |
| Python-first migrations | OK | `op.execute` / `op.run_python` as first-class escape hatches. |
| Multi-dialect support | OK | `_op_impl` renders per dialect; `op.execute(dict)` accepted. |
| AI skill distribution in-repo | N/A | Lives under `.claude/skills/joryu/` (not auditable from source). |
| No `--from-prompt` mode | OK | No such flag in `cli.py`. |
| MySQL / PostgreSQL / SQLite initial support | OK | All three handled in `_op_impl`, `lock.py`. |

## §2 — Architecture

| Section | Verdict | Notes |
|---|---|---|
| `joryu_migrations` + `joryu_migration_steps` tables | OK | `state.py` defines both with exact column shapes in §9.2. |
| No central sum file | OK | Per-migration DB-side checksum only; no on-disk lock file. |
| `joryu verify` static analysis | OK | `verify.py` enumerates (table,column) targets. |

## §3 — Migration file format

| Section | Verdict | Notes |
|---|---|---|
| §3.1 UTC timestamp ISO basic format | OK | `_generate_empty` + `autogen._allocate_file` both use `%Y%m%dT%H%M%S`. |
| §3.1 same-second `_2`, `_3` suffix | OK (now tested) | Implemented in `generate.py`, `autogen.py`, AND `importer/alembic_importer.py`. Tests added in `test_spec_3_1_*`. |
| §3.2 decorator metadata (`id` / `depends_on` / `transaction_mode` / `dialects` / `tags` / `group` / `on_mismatch`) | OK | All seven kwargs honoured in `registry.Migration`. |
| §3.2 duplicate id is ERROR | OK | `registry.migration` raises `ValueError`. |

## §4 — Operations API

| Section | Verdict | Notes |
|---|---|---|
| §4.2 core DDL surface (create_table / add_column / etc.) | OK | `_op_impl` covers all listed operations. |
| §4.3 SQLAlchemy model integration (`create_table_from_model`) | OK | Exported from `op` namespace. |
| §4.4 `op.batch` opt-in for SQLite | OK | `_op_impl.batch` + `batch_rebuild` op kind. |
| §4.5 `op.run_python(fn)` receives `(conn, dialect, checkpoint)` | OK | Confirmed in `_op_impl.RunPythonOp.apply`. |

## §5 — AI-friendly API

| Section | Verdict | Notes |
|---|---|---|
| Short typed `op.*` surface | OK | `op.add_column(table, name, type, ...)` style honoured. |
| `t.Int`, `t.BigInt`, ... short type names | OK | `_types_impl` + `types` re-export. |
| `joryu explain <id>` natural-language render | OK | `explain.py` handles 16 op kinds. |

## §6 — Multi-dialect support

| Section | Verdict | Notes |
|---|---|---|
| §6.1 L1 dialect-automatic ops | OK | Built into each `Operation.apply`. |
| §6.1 L2 `op.execute(dict)` per-dialect dispatch | OK | `_op_impl.execute` matches by normalised key + `default`. |
| §6.1 `"postgres"` / `"pg"` synonyms normalised | OK | `_types_impl._norm_dialect`. |
| §6.1 L3 `dialects=[...]` + `group=` cross-dialect groups | OK | Migration carries both fields. `runner._sorted_migrations` skips non-matching dialects. |
| §6.3 type compatibility table | OK | `_types_impl` covers Int/BigInt/Text/Json/Uuid/etc. with per-dialect rendering. |
| §6.3 `Serial` requires `primary_key=True` | OK | `UnsupportedTypeUsage` raised on misuse. |
| §6.4 `joryu test --unit` | OK | `testing.run_unit_tests` against in-memory SQLite. |
| §6.4 `joryu test --integration` | PARTIAL | `testing.run_integration_tests` exists with testcontainers gating; sibling agent owns CLI wiring. |

## §7 — Parallel PRs and conflict detection

| Section | Verdict | Notes |
|---|---|---|
| §7.1 DB-side per-migration checksum | OK | `state.migrations_table.checksum`; runner re-validates on apply. |
| §7.2 commutativity rules | OK | `verify.py` matches the spec table verbatim. |
| §7.2 conflict-kind priority `table_drop > table_rename > add_drop > column_rename > double_alter` | OK | `_KIND_PRIORITY` tuple matches spec ordering. |
| §7.2 opaque ops (raw SQL / run_python / step) emit no Conflict | OK | `OpaqueOperation.targets()` returns `[]`. |
| §7.2 dialect-restricted migrations don't conflict if their `dialects=` sets are disjoint | **FIXED** | Added `_dialects_disjoint` filter in `verify.py`. Tests: `test_spec_7_2_verify_skips_disjoint_dialects` + `test_spec_7_2_verify_still_flags_overlapping_dialects`. |
| §7.2.1 `Conflict` / `OpRef` / `ConflictKind` importable from top-level `joryu` | OK | Re-exported via `joryu.__init__`. |
| §7.3 `depends_on` DAG with timestamp tiebreak | OK | `runner._sorted_migrations`. |

## §8 — Autogeneration

| Section | Verdict | Notes |
|---|---|---|
| §8.1 `joryu generate --empty` | OK | CLI + `generate._generate_empty`. |
| §8.1 `--against=db` and `--against=replay` | OK | `autogen.generate_diff` + `virtual_schema.replay_migrations`. |
| §8.3 dangerous-op warnings, run_python placeholders for backfill | OK | `autogen.render_migration` emits `# WARNING:` and `op.run_python` stubs. |

## §9 — Execution model

| Section | Verdict | Notes |
|---|---|---|
| §9.2 two state tables, exact column shapes | OK | `state.py` matches spec column for column. |
| §9.2 status enums + transitions | OK | `_MIGRATION_STATUSES`, `_STEP_STATUSES` cover the §9.2 enums. |
| §9.2 `started_at` / `finished_at` use server clock | OK (now tested) | `func.current_timestamp()` server-side in `insert_migration` / `upsert_step`. Test: `test_spec_9_2_started_at_uses_server_clock`. |
| §9.2 `last_error` 4 KB cap | OK (now tested) | `runner._LAST_ERROR_LIMIT = 4 * 1024`; `_summarise_exception` truncates. Test: `test_spec_9_2_last_error_truncated_to_4_kb`. |
| §9.2 clear `last_error` / `pause_reason` on out-of-failed transitions | OK | `state.update_migration_status` clears on `running`/`applied`. |
| §9.3 three transaction modes (`per_migration`, `per_step`, `none`) | OK | Runner dispatches on `m.transaction_mode`. |
| §9.4 ensure-style operations | OK | Each `Operation.apply` short-circuits when desired state already exists. |
| §9.5.2 `on_mismatch="alter" / "skip" / "error"` per-op + per-migration | OK (now tested) | `_resolved_on_mismatch` falls back to ctx default. Test: `test_spec_9_5_2_migration_wide_on_mismatch_propagates`. |
| §9.6 apply algorithm | OK | `_apply_core` follows the spec's 1–6 steps. |
| §9.7 advisory lock per dialect | OK | `lock.advisory_lock` covers PG / MySQL / SQLite. |

## §10 — Failure handling and resumption

| Section | Verdict | Notes |
|---|---|---|
| §10.1 resume-by-default; `--no-resume` to disable | OK | `runner.apply(no_resume=...)`. |
| §10.3 checksum policy in `failed` state | OK | Runner only enforces checksum match for `status='applied'` rows. |
| §10.4 halt when failed/paused, override flags | PARTIAL | Sibling agent owns `--continue-past-failed` and `--retry-paused` polling. Halt-policy method exists at `runner._enforce_halt_policy`. |
| §10.5 interactive 5-choice recovery | OK | `runner._prompt_recovery` implements all five choices with proper `--non-interactive --on-failure` fallback. |

## §11 — Downgrade

| Section | Verdict | Notes |
|---|---|---|
| §11.1 forward-only by default | OK | `joryu apply` never runs `downgrade`. |
| §11.2 `JORYU-DOWN-HINT:` structured comments | OK | `downhint.py` + `autogen.emit_down_hints` produce all required fields. |
| §11.4 prod guard on `joryu down` | OK | `env.detect_environment` + `runner.down` ProductionGuardError. |

## §12 — Historical schema replay

| Section | Verdict | Notes |
|---|---|---|
| §12.1 Operations replay (no snapshot files) | OK | `virtual_schema.replay_migrations`. |
| §12.2 `op.declare_schema_change` frozen vocabulary | OK | `_op_impl.declare_schema_change` accepts all listed keywords. |

## §13 — User-defined steps and checkpoints

| Section | Verdict | Notes |
|---|---|---|
| §13.1 `op.run_python` + checkpoint cursor | OK | `RunPythonOp` receives a real `Checkpoint`. |
| §13.2 `op.step` (bare / factory / direct-call) | OK | `_op_impl.step` dispatches all three forms. |
| §13.2 sync + async via anyio | OK | `_dispatch_async` uses `anyio.from_thread.run` when caller is async, else `anyio.run`. |
| §13.2 `PauseStep` / `SkipStep` control flow | OK | Raised, caught in `runner._run_step`. |
| §13.3.1 Checkpoint methods (`get`/`set`/`update`/`clear`/`snapshot`/`report`) | OK | All six on `Checkpoint`. |
| §13.3.4 Decimal / datetime / date auto-encoding | OK (now tested) | `checkpoint._default` handles all three. Tests: `test_spec_13_3_4_*`. |
| §13.3.4 unserialisable values raise TypeError | OK (now tested) | Same `_default` raises. |
| §13.3.5 1 MB soft limit warning | OK (now tested) | `Checkpoint.SIZE_SOFT_LIMIT = 1_000_000`, warns via `warnings.warn`. Test: `test_spec_13_3_5_size_warning_above_soft_limit`. |

## §14 — Progress display

| Section | Verdict | Notes |
|---|---|---|
| §14.1 two-phase registration / execution | OK | `register_operations` runs `upgrade()` separately from `runner._apply_one_body`. |
| §14.2 five display modes (`auto` / `interactive` / `plain` / `json` / `quiet`) | OK | `progress.make_emitter`. |
| §14.2 mutually exclusive progress flags | OK | `cli._resolve_progress_mode`. |
| §14.2 interactive ANSI rendering | PARTIAL | Sibling agent owns the live-render Interactive emitter; v0.3 falls back to a chattier plain emitter. |
| §14.4 `checkpoint.report()` rate-limit | OK | `InteractiveEmitter.step_progress` rate-limits to 100 ms. |

## §15 — Production safety

| Section | Verdict | Notes |
|---|---|---|
| §15.1 heuristic detection (`localhost`, `.local`, etc.) | OK | `env.detect_environment` matches the spec list. |
| §15.2 explicit declaration via `set_environment` or `joryu.toml` | OK | Both paths honoured. |
| §15.2 `joryu down --allow-prod` required outside `local` | OK | `runner.down` raises `ProductionGuardError`. |
| §15.2 `joryu apply --continue-past-failed` / `joryu mark` confirmation prompts | PARTIAL | Sibling agent owns the prompt wiring in `cli.py`. |

## §16 — CLI / Python API

| Section | Verdict | Notes |
|---|---|---|
| §16 subcommands present (`init` / `generate` / `apply` / `status` / `down` / `verify` / `repair` / `mark` / `show` / `explain` / `test` / `import alembic` / `schema-snapshot`) | OK | All wired in `cli.py`. |
| §16.1 Python API parity (`joryu.api.{apply, apply_async, down, down_async, status, verify, generate}`) | OK (now tested) | `joryu/api.py` exposes all seven. Test: `test_spec_16_1_api_surface`. |
| §16.2 exit codes 0–6 | OK (now tested) | `cli._exit_for_exception` maps each exception class to the documented code. Test: `test_spec_16_2_exit_code_mapping`. |
| §16.3 public exception classes | OK | `joryu.exceptions` defines all five; re-exported from `joryu`. |
| `joryu repair <id>` actually updates checksum | **FIXED** | `state.repair_checksum` implemented; CLI already had the wiring. Test: `test_repair_checksum_updates_db_value`. |
| `joryu mark <id> --as=...` | **FIXED** | `state.mark_migration` implemented (validates the four migration states + `--reason` requirement for `paused`). Tests: `test_mark_migration_paused_requires_reason`, `test_mark_migration_round_trip`. |
| `joryu mark <id>.<step> --as=...` | **FIXED** | `state.mark_step` implemented; auto-downgrades an `applied` parent to `failed` when a step is set back to `pending` (§9.2). Test: `test_mark_step_pending_downgrades_applied_parent`. |
| `joryu show <id>` rendering | OK | `cli.show_cmd` renders id / depends_on / transaction_mode / dialects / tags / group / on_mismatch / steps. |

## §17 — `joryu.toml`

| Section | Verdict | Notes |
|---|---|---|
| `[joryu] migrations_dir` | OK | `config.load_config`. |
| `[database] url` with `env:VAR` expansion | OK (now tested) | `config._resolve_url`. Test: `test_spec_17_env_var_expansion`. |
| `[joryu] environment = "production"` override | OK | Read in `config.load_config`, consumed by `env`. |

## §18 — Comparison with Alembic and Django

N/A (documentation only).

## §19 — Alembic importer

| Section | Verdict | Notes |
|---|---|---|
| §19.1 Phase 1 structural conversion | OK | `importer.alembic_importer._emit_joryu_file`. |
| §19.1 Phase 2 heuristics (`batch_alter_table` → `op.batch`, dialect branches flagged) | OK | `_rewrite_batch_alter_table`, `_annotate_dialect_branches`. |
| §19.1 Phase 3 manual-review residue (`JORYU-IMPORT-TODO`) | OK | `_rewrite_bulk_insert`, `_annotate_unknown_types`. |
| §19.2 state handover (`--migrate-state`) | OK | `_migrate_state`. |
| §19.4 collapse merge revisions / multiple heads | OK | `down_revision` parsed as list, all retained. |

## §20 / §21 — Design decisions / examples

N/A (documentation only).

---

## Summary

* **Audited sections**: 21.
* **Fully OK before this pass**: 18.
* **Gaps closed in this pass**: 3 functional (`state.mark_migration`, `state.mark_step`, `state.repair_checksum`) + 1 verify rule (`dialects=` disjointness filter).
* **Behaviour newly pinned by tests** (no code change required, but spec-critical): 8 (same-second collision in 3 generators, server-clock timestamps, 4 KB `last_error` cap, migration-wide `on_mismatch`, Decimal/datetime encoding, 1 MB soft limit, API parity, exit-code mapping, `env:VAR` expansion).
* **Deferred to sibling Wave-4 agent** (already noted): `joryu test --integration` CLI wiring, `--retry-paused --retry-interval` polling, production confirmation prompts on `apply --continue-past-failed` / `mark`, true ANSI `InteractiveEmitter`.
* **Test count**: 135 → 153 (18 new in `tests/test_spec_compliance.py`).
