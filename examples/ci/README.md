# joryu CI templates

Drop-in CI configuration for the three platforms joryu supports
out of the box. Each file is a copy-paste template; tweak paths
and secret names to fit your project.

## Files

| File                | Platform              | Purpose                                                                                                          |
|---------------------|-----------------------|------------------------------------------------------------------------------------------------------------------|
| `github_actions.yml`| GitHub Actions        | PR gate: `joryu verify` + `joryu test --unit`. Deploy job on push to `main`: `joryu apply`.                       |
| `gitlab_ci.yml`     | GitLab CI/CD          | MR gate: `verify` + `test:unit`. Manual `apply:production` job for controlled rollouts.                          |
| `pre-commit.yaml`   | pre-commit framework  | Local pre-commit hook: `joryu verify`. Optional pre-push hook: `joryu test --unit`.                              |

## Where to put each file

```text
github_actions.yml   →  .github/workflows/joryu.yml
gitlab_ci.yml        →  .gitlab-ci.yml          (or include into an existing one)
pre-commit.yaml      →  .pre-commit-config.yaml
```

## Secrets / environment variables

The PR-gate jobs (`verify`, `test --unit`) need **no** DB access —
they run entirely in-memory. Only the deploy / apply job needs:

| Variable        | Where                              | What                                                                                  |
|-----------------|------------------------------------|---------------------------------------------------------------------------------------|
| `DATABASE_URL`  | GitHub: repo secret; GitLab: CI/CD variable | SQLAlchemy URL of the target environment. Read by joryu's default `joryu.toml`.        |

Optional: set the GitHub Actions job's `environment:` to one that
requires approval before secrets are exposed (recommended for
production).

## Exit codes (SPEC §16.2)

| Code | Meaning                | Typical CI response                                                |
|------|------------------------|--------------------------------------------------------------------|
| 0    | Success                | —                                                                  |
| 1    | General error          | Investigate logs.                                                  |
| 2    | Migration failed       | Block deploy; re-run `joryu apply` after fix (ensure semantics).   |
| 3    | Migration paused       | Block deploy; check the paused step's reason in `joryu status`.    |
| 4    | Verify failure         | Block merge; rebase + add `depends_on`.                            |
| 5    | Production guard       | The job ran without `--allow-prod` against a production-like DSN.  |
| 6    | Unsupported type usage | Fix the migration (e.g. `Serial` requires `primary_key=True`).     |

## Notes

- The GitHub Actions workflow comments on the PR when `verify` or
  `test --unit` fails. Customize the messages to match your team's
  norms.
- The GitLab `apply:production` job is `when: manual` on purpose —
  joryu's philosophy is "forward-only, explicit deploys." If your
  workflow auto-applies on every merge, change it to `when: on_success`
  but keep `environment:` set so deploys are auditable.
- The pre-commit `joryu-test-unit` hook is on `stages: [push]` so
  the per-commit feedback loop stays fast.

## Further reading

- `SPEC.md` §16 — full CLI reference.
- `SPEC.md` §7 — what `joryu verify` actually checks.
- `SPEC.md` §20 — CI templates are part of the v1 deliverable.
