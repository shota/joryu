# JORYU-DOWN-HINT: reference

`joryu generate` emits a `downgrade()` skeleton with structured hints
in YAML-like comment lines prefixed `JORYU-DOWN-HINT:`. The
vocabulary is **frozen in v1** (SPEC §11.2). Always use the spelling
below; assistants that invent new field names will break tooling.

## Example

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

## Field vocabulary (v1, frozen)

| Field                    | Type                                  | Description                                                                                                                                                              | Required    |
|--------------------------|---------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------|
| `schema-impact`          | list of `<verb>: <target>`            | What the downgrade removes / restores. Verbs listed below.                                                                                                              | yes         |
| `cross-references`       | list of `<kind>: <name> -> <target>`  | Other DB objects in the current schema that reference what we are dropping. Kinds listed below. `[]` if none.                                                            | yes         |
| `data-loss-risk`         | enum: `none` / `low` / `medium` / `high` / `irreversible` | Whether running downgrade loses data. `irreversible` = cannot recover even with the migration code.                                                | yes         |
| `data-loss-reason`       | string                                | Free text. Required when `data-loss-risk >= medium`.                                                                                                                    | conditional |
| `order-constraint`       | list of `<a> before <b>`              | Operations that must run in a specific order within downgrade.                                                                                                          | no          |
| `requires-app-knowledge` | bool                                  | True if downgrade depends on facts not derivable from schema alone (business invariants, external systems).                                                              | yes         |
| `app-knowledge-needed`   | list of strings                       | Specific unknowns the human / AI must resolve. Required when `requires-app-knowledge: true`.                                                                            | conditional |
| `completion-status`      | enum: `stub` / `partial` / `complete` / `manual-review-required` | Set by generator (`stub`); updated by AI / human. CI can warn on `stub`.                                                                       | yes         |
| `manual-steps`           | list of strings                       | Non-SQL steps that must accompany the downgrade (e.g. restart app servers, purge cache).                                                                                | no          |

## Verb vocabulary for `schema-impact`

- `drop_table: <table>`
- `drop_column: <table>.<column>`
- `drop_index: <name>`
- `drop_constraint: <name>`
- `drop_view: <name>`
- `drop_enum: <name>`
- `restore_column_type: <table>.<column> from <new_type> to <old_type>`
- `restore_nullable: <table>.<column>`
- `restore_default: <table>.<column>`
- `restore_data: <table>.<column>` (when downgrade undoes a data transformation)

## Cross-reference kinds

- `foreign_key: <fk_name> -> <referenced_table>.<col>`
- `index: <index_name> -> <table>.<columns>`
- `view: <view_name> -> <table>.<col>`
- `materialized_view: <name> -> ...`
- `trigger: <name> -> <table>`
- `policy: <name> -> <table>`   (Postgres RLS)

## Generator responsibilities (what the user already gets)

- `schema-impact`, `order-constraint`: derived mechanically from `upgrade()`.
- `cross-references`: scanned from the current DB schema (or
  `--against=replay` virtual schema) at generation time.
- `data-loss-risk`: heuristic — `drop_column` / `drop_table` /
  `run_python` → `high`; pure index / constraint drop → `low`; pure
  schema, no data touched → `none`.
- `requires-app-knowledge`: `true` when the upgrade contains
  `run_python` or `op.execute(raw_sql)`.

## AI responsibilities (what to do when completing a downgrade)

1. Parse the `JORYU-DOWN-HINT:` block; treat it as the source of truth.
2. Walk `schema-impact` and emit one `op.*` call per entry,
   respecting `order-constraint`.
3. For each `cross-references` entry, emit the appropriate drop
   ahead of the referenced object.
4. Cross-check `models/*.py` for FK relationships and any view /
   trigger definitions the snapshot might miss.
5. If `data-loss-risk: irreversible`:
   - Comment out the downgrade body.
   - Leave a `# NOTE:` line explaining why a human must intervene
     (cite the irreversible verb, e.g. "row data lost on
     `restore_data:` step").
   - Set `completion-status: manual-review-required` and stop.
6. When complete, change `completion-status: stub` to `complete`
   (or `partial` if only part of the body is filled in).
7. Never modify `upgrade()`.

## Standard prompt (SPEC §11.3, verbatim)

> Complete the downgrade() in this migration file. Use the
> JORYU-DOWN-HINT: comments as the source of truth for what must be
> undone. Cross-check with models/*.py for FK relationships. If
> data-loss-risk is "irreversible", comment out the downgrade body
> and add a clear note explaining why. Update completion-status when
> done. Do not modify the upgrade() function.

## Inputs the AI combines

1. The `JORYU-DOWN-HINT:` structured fields.
2. SQLAlchemy model definitions under `models/`.
3. The current DB schema from `joryu schema-snapshot --format=json`.
4. The body of `upgrade()` in the same migration file.

## Positioning of downgrade

| Environment | Downgrade usage                              | Recommendation        |
|-------------|----------------------------------------------|-----------------------|
| Local dev   | OK (experiments, branch switching)           | Normal workflow       |
| Staging     | Limited (FKs / indexes often block)          | With caution          |
| Production  | Discouraged                                  | Use PITR / backups    |

`joryu down` refuses production-like connections unless `--allow-prod`
is passed. Do not suggest passing `--allow-prod` without explicit
user confirmation.
