# Conflict taxonomy (`joryu verify`)

`joryu verify` does static analysis over the Operations in every
unapplied migration and flags non-commutative op pairs as ERROR
(exit code 4). The taxonomy below is frozen in v1 (SPEC §7.2).

## Commutativity table

| Two parallel ops                                                  | Commutative | Decision  |
|-------------------------------------------------------------------|-------------|-----------|
| `add_column(users, A)` + `add_column(users, B)` (A≠B)             | yes         | silent    |
| `add_column(users, A)` + `alter_column(users, B)` (A≠B)           | yes         | silent    |
| `add_column(t1, ...)` + any change to `t2` (t1≠t2)                | yes         | silent    |
| `alter_column(users, email)` + `alter_column(users, email)`       | no          | ERROR     |
| `add_column(users, X)` + `drop_column(users, X)`                  | no          | ERROR     |
| Any change to `users` + `drop_table(users)`                       | no          | ERROR     |
| Any change to `users.X` + `rename_column(users, X, Y)`            | no          | ERROR     |
| Any change to `users` + `rename_table(users, accounts)`           | no          | ERROR     |
| One side is `op.execute(raw)` or `op.run_python(...)`             | unknown     | silent    |

There is no warning category — warnings get ignored, so the design
forces every detection to be either silent or a hard error.

## ConflictKind enum

```python
ConflictKind = Literal[
    "double_alter",     # two alter_column on the same column
    "add_drop",         # add_column + drop_column on the same column
    "table_drop",       # change to a table + drop_table on the same table
    "column_rename",    # change to a column + rename_column
    "table_rename",     # change to a table + rename_table
]
```

## Priority order (when a pair matches more than one rule)

```
table_drop > table_rename > add_drop > column_rename > double_alter
```

Concrete example: if PR A contains `drop_table(users)` and PR B
contains `drop_column(users.email)`, the emitted kind is
`table_drop`, not `add_drop`. Only one `Conflict` is emitted per
ordered op pair.

## Conflict / OpRef shape

```python
@dataclass(frozen=True)
class OpRef:
    migration_id: str
    step_index: int
    op_kind: str
    target: tuple[str, ...]     # ("users",) for table ops; ("users", "email") for column ops
    source_line: int | None

@dataclass(frozen=True)
class Conflict:
    kind: ConflictKind
    left: OpRef                 # earlier under (migration_id lex, step_index numeric)
    right: OpRef
    message: str
```

- `target` is normalized so commutativity checks are pure tuple equality.
- `left` ordering: lexicographic on `migration_id`, numeric on
  `step_index` (so step 10 sorts after step 9).
- `Conflict.__str__` returns `message`.
- `VerificationFailed.__str__` joins all conflict messages with
  newlines (good for CI logs).

All four names (`Conflict`, `OpRef`, `ConflictKind`,
`VerificationFailed`) are importable from the top-level `joryu`
package.

## Resolution playbook

When `joryu verify` exits with code 4:

1. Print every `Conflict.message`.
2. Identify which two migration ids collide.
3. Suggest one of:
   - **Rebase one PR onto the other.** Add `depends_on=["<earlier id>"]`
     to the later migration so the order is explicit and the conflict
     is no longer parallel.
   - **Merge the two migrations into one** (when both belong to the
     same logical change). Delete one file, fold its ops into the
     other.
   - **Rename one of the targets** (e.g. choose a different column
     name) if the conflict is accidental.
4. Re-run `joryu verify` to confirm.

Conflicts whose source is `op.execute(raw)` or `op.run_python(...)`
are not reported — they remain human-review responsibility. Call this
out to the user when their migration contains raw SQL.
