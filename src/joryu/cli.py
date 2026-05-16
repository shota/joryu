"""joryu command-line interface (§16).

The CLI is intentionally a thin wrapper: every subcommand defers to the
public API (``joryu.api``) or to small in-module helpers. Imports of the
runner / state / loader / config modules happen lazily inside command
bodies so that:

* a still-being-built module does not break CLI module load, and
* sub-agents can rename internal helpers without breaking the CLI.

Exit codes follow §16.2.
"""
from __future__ import annotations

import json as _json
import sys
import traceback
from pathlib import Path

import click

# Exit codes -- mirror §16.2.
EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_MIGRATION_FAILED = 2
EXIT_MIGRATION_PAUSED = 3
EXIT_VERIFY_FAILED = 4
EXIT_PROD_GUARD = 5
EXIT_TYPE_MISUSE = 6


# ---------------------------------------------------------------------------
# Exception -> exit-code mapping
# ---------------------------------------------------------------------------


def _exit_for_exception(exc: BaseException) -> int:
    """Map a joryu exception to the CLI exit code (§16.2)."""
    # Late import — exceptions module is pinned but use a safe import anyway.
    from .exceptions import (
        MigrationFailed,
        MigrationPaused,
        ProductionGuardError,
        UnsupportedTypeUsage,
        VerificationFailed,
    )

    if isinstance(exc, VerificationFailed):
        return EXIT_VERIFY_FAILED
    if isinstance(exc, MigrationFailed):
        return EXIT_MIGRATION_FAILED
    if isinstance(exc, MigrationPaused):
        return EXIT_MIGRATION_PAUSED
    if isinstance(exc, ProductionGuardError):
        return EXIT_PROD_GUARD
    if isinstance(exc, UnsupportedTypeUsage):
        return EXIT_TYPE_MISUSE
    return EXIT_GENERAL


def _die(exc: BaseException, *, message: str | None = None, exit_code: int | None = None) -> None:
    """Print an error message and exit with the proper code."""
    code = exit_code if exit_code is not None else _exit_for_exception(exc)
    text = message if message is not None else f"{type(exc).__name__}: {exc}"
    click.echo(text, err=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Progress-mode argument validation
# ---------------------------------------------------------------------------


def _resolve_progress_mode(
    interactive: bool, plain: bool, json_mode: bool, quiet: bool
) -> str:
    """Return a §14.2 mode string, ensuring mutual exclusion."""
    chosen = [
        name
        for name, flag in (
            ("interactive", interactive),
            ("plain", plain),
            ("json", json_mode),
            ("quiet", quiet),
        )
        if flag
    ]
    if len(chosen) > 1:
        joined = ", ".join(f"--{n}" for n in chosen)
        raise click.UsageError(
            f"progress-mode flags are mutually exclusive (got {joined})"
        )
    if chosen:
        return chosen[0]
    return "auto"


# ---------------------------------------------------------------------------
# Production-guard confirmation prompts (§15.2)
# ---------------------------------------------------------------------------


def _resolve_url_from_config() -> str | None:
    """Best-effort: resolve the DB URL exactly as the runner would.

    Returns ``None`` if the config is missing or the URL cannot be resolved
    (in which case the env defaults to ``"local"`` and the prompt is
    suppressed).
    """
    try:
        from .config import load_config, resolve_database_url
        cfg = load_config()
        return resolve_database_url(None, cfg)
    except Exception:
        return None


def _detect_env() -> tuple[str, str | None]:
    """Return ``(env, host)`` for the current configured URL."""
    from . import env as env_module
    url = _resolve_url_from_config()
    return env_module.detect_environment(url)


def _confirm_prod_action(action_phrase: str, flag_name: str) -> bool:
    """Prompt the operator before performing ``action_phrase`` on a
    production-like environment. Returns ``True`` if the run may proceed
    (local env, or the user typed ``y``)."""
    env_name, host = _detect_env()
    if env_name == "local":
        return True
    prompt = (
        f"Production-like environment detected ({env_name}@{host}). "
        f"{action_phrase}?"
    )
    try:
        return click.confirm(prompt, default=False)
    except click.Abort:
        return False


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group(help="joryu — Python migration library (§16).")
@click.version_option(message="%(version)s", package_name="joryu")
def cli() -> None:
    pass


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


_DEFAULT_JORYU_TOML = """\
[joryu]
migrations_dir = "migrations"

[metadata]
# target = "myapp.models:Base.metadata"

[database]
url = "env:DATABASE_URL"

[generate]
include_schemas = ["public"]
exclude_tables  = []

[dialects]
test_targets = ["postgresql", "mysql", "sqlite"]
"""


@cli.command("init", help="Create joryu.toml and migrations/ in the current directory.")
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("migrations"),
    show_default=True,
    help="Where migration files will live.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing joryu.toml.")
def init_cmd(migrations_dir: Path, force: bool) -> None:
    try:
        toml_path = Path("joryu.toml")
        if toml_path.exists() and not force:
            click.echo("joryu.toml already exists (pass --force to overwrite)", err=True)
            sys.exit(EXIT_GENERAL)
        toml_path.write_text(_DEFAULT_JORYU_TOML)
        migrations_dir.mkdir(parents=True, exist_ok=True)
        click.echo(f"wrote {toml_path}")
        click.echo(f"created {migrations_dir}/")
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _die(exc)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


@cli.command("generate", help="Create a new migration file (v0.1: --empty only).")
@click.argument("slug")
@click.option("--empty", is_flag=True, help="Create an empty migration template.")
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("migrations"),
    show_default=True,
)
def generate_cmd(slug: str, empty: bool, migrations_dir: Path) -> None:
    if not empty:
        click.echo(
            "joryu generate: only --empty is supported in v0.1 "
            "(autogen requires SQLAlchemy diff, not wired up yet)",
            err=True,
        )
        sys.exit(EXIT_GENERAL)
    try:
        from .generate import generate as _generate
        path = _generate(slug, empty=True, migrations_dir=migrations_dir)
        click.echo(str(path))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        _die(exc)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@cli.command("apply", help="Apply pending migrations.")
@click.option("--target", default=None, help="Stop after applying this migration id.")
@click.option("--dry-run", is_flag=True, help="Plan only; do not touch the DB.")
@click.option("--no-resume", is_flag=True, help="Do not resume an interrupted migration.")
@click.option(
    "--continue-past-failed",
    is_flag=True,
    help="Continue applying even if a prior migration is in failed state.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Disable the interactive recovery prompt (§10.5).",
)
@click.option(
    "--on-failure",
    type=click.Choice(["resume", "restart", "abort"]),
    default="resume",
    show_default=True,
    help="Behavior under --non-interactive when a failed/paused row exists.",
)
@click.option("--retry-paused", is_flag=True, help="Retry a paused migration.")
@click.option(
    "--retry-interval",
    default="30s",
    show_default=True,
    help="Polling interval used with --retry-paused.",
)
@click.option("--interactive", "interactive_flag", is_flag=True, help="Force interactive progress.")
@click.option("--plain", "plain_flag", is_flag=True, help="Force plain (line-oriented) progress.")
@click.option("--json", "json_flag", is_flag=True, help="Emit JSONL progress events to stdout.")
@click.option("--quiet", "quiet_flag", is_flag=True, help="Suppress progress except on failure.")
def apply_cmd(
    target: str | None,
    dry_run: bool,
    no_resume: bool,
    continue_past_failed: bool,
    non_interactive: bool,
    on_failure: str,
    retry_paused: bool,
    retry_interval: str,
    interactive_flag: bool,
    plain_flag: bool,
    json_flag: bool,
    quiet_flag: bool,
) -> None:
    try:
        output = _resolve_progress_mode(
            interactive_flag, plain_flag, json_flag, quiet_flag
        )
    except click.UsageError as exc:
        click.echo(str(exc), err=True)
        sys.exit(EXIT_GENERAL)

    # §15.2: --continue-past-failed on a production-like environment requires
    # an explicit confirmation. --non-interactive suppresses the prompt so
    # CI/CD callers can still pass through.
    if continue_past_failed and not non_interactive:
        if not _confirm_prod_action(
            "Continue past the failed migration",
            "--continue-past-failed",
        ):
            click.echo("aborted by user", err=True)
            sys.exit(EXIT_PROD_GUARD)

    try:
        from . import api
    except ImportError as exc:
        click.echo(f"joryu apply: api not available yet ({exc})", err=True)
        sys.exit(EXIT_GENERAL)

    kwargs = {
        "target": target,
        "dry_run": dry_run,
        "no_resume": no_resume,
        "continue_past_failed": continue_past_failed,
        "non_interactive": non_interactive,
        "on_failure": on_failure,
        "retry_paused": retry_paused,
        "retry_interval": retry_interval,
        "output": output,
    }
    # Filter out kwargs the (possibly-still-evolving) runner doesn't accept.
    try:
        import inspect
        from . import runner as _runner
        sig = inspect.signature(_runner.apply)
        accepted = {
            name
            for name, p in sig.parameters.items()
            if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
        }
        # If the runner uses **kwargs, accept everything.
        if not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in accepted}
    except (ImportError, ValueError, TypeError):
        # If signature introspection fails, just pass everything.
        pass

    try:
        api.apply(**kwargs)
    except NotImplementedError:
        click.echo("joryu apply: runner not implemented yet", err=True)
        sys.exit(EXIT_OK)
    except TypeError as exc:
        # Runner sub-agent may not yet accept all kwargs. Surface a friendly
        # message rather than a Python traceback.
        click.echo(f"joryu apply: incompatible runner signature ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    except SystemExit:
        raise
    except BaseException as exc:
        _die(exc)
        return  # unreachable
    sys.exit(EXIT_OK)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@cli.command("verify", help="Static conflict detection (§7.2).")
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the migrations directory (defaults to joryu.toml).",
)
def verify_cmd(migrations_dir: Path | None) -> None:
    try:
        from .verify import verify as _verify
        conflicts = _verify(migrations_dir)
    except NotImplementedError:
        click.echo("joryu verify: not implemented yet", err=True)
        sys.exit(EXIT_OK)
    except BaseException as exc:
        _die(exc)
        return

    if conflicts:
        for c in conflicts:
            click.echo(c.message, err=True)
        sys.exit(EXIT_VERIFY_FAILED)
    click.echo("ok")
    sys.exit(EXIT_OK)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command("status", help="Show applied / pending / failed / paused migrations.")
@click.option("--json", "json_flag", is_flag=True, help="Emit JSON.")
def status_cmd(json_flag: bool) -> None:
    try:
        from . import api
        rows = api.status()
    except NotImplementedError:
        click.echo("joryu status: not implemented yet", err=True)
        sys.exit(EXIT_OK)
    except ImportError as exc:
        click.echo(f"joryu status: not wired up ({exc})", err=True)
        sys.exit(EXIT_OK)
    except BaseException as exc:
        _die(exc)
        return

    if rows is None:
        rows = []

    if json_flag:
        click.echo(_json.dumps(rows, default=str))
        return

    if not rows:
        click.echo("(no migrations)")
        return

    # Best-effort table rendering: rows may be list[dict] or list[Migration].
    for row in rows:
        if isinstance(row, dict):
            mid = row.get("id", "?")
            status = row.get("status", "?")
            click.echo(f"{mid}\t{status}")
        else:
            click.echo(str(row))


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------


@cli.command("down", help="Roll back the last N migrations (dev-only).")
@click.option("--steps", type=int, default=None, help="Number of migrations to roll back.")
@click.option("--to", "to_id", default=None, help="Roll back down to this migration id.")
@click.option("--allow-prod", is_flag=True, help="Permit running against a production-like DB.")
@click.option("--yes", is_flag=True, help="Skip the interactive confirmation prompt (§15.2).")
def down_cmd(steps: int | None, to_id: str | None, allow_prod: bool, yes: bool) -> None:
    if steps is None and to_id is None:
        click.echo("joryu down: pass --steps=N or --to=<id>", err=True)
        sys.exit(EXIT_GENERAL)
    if steps is not None and to_id is not None:
        click.echo("joryu down: --steps and --to are mutually exclusive", err=True)
        sys.exit(EXIT_GENERAL)
    # §15.2: even with --allow-prod, ask for explicit consent unless --yes
    # was passed (CI escape hatch).
    if allow_prod and not yes:
        env_name, host = _detect_env()
        if env_name != "local":
            prompt = (
                f"Production-like environment detected ({env_name}@{host}). "
                f"Roll back migrations?"
            )
            try:
                if not click.confirm(prompt, default=False):
                    click.echo("aborted by user", err=True)
                    sys.exit(EXIT_PROD_GUARD)
            except click.Abort:
                click.echo("aborted by user", err=True)
                sys.exit(EXIT_PROD_GUARD)
    try:
        from . import api
        kwargs = {"steps": steps, "to": to_id, "allow_prod": allow_prod}
        try:
            import inspect
            from . import runner as _runner
            sig = inspect.signature(_runner.down)
            if not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
                accepted = {
                    n for n, p in sig.parameters.items()
                    if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
                }
                kwargs = {k: v for k, v in kwargs.items() if k in accepted}
        except (ImportError, ValueError, TypeError):
            pass
        api.down(**kwargs)
    except NotImplementedError:
        click.echo("joryu down: runner not implemented yet", err=True)
        sys.exit(EXIT_OK)
    except TypeError as exc:
        click.echo(f"joryu down: incompatible runner signature ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    except BaseException as exc:
        _die(exc)
        return
    sys.exit(EXIT_OK)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@cli.command("show", help="Show details for a single migration.")
@click.argument("migration_id")
def show_cmd(migration_id: str) -> None:
    try:
        from .registry import MIGRATIONS
    except ImportError as exc:
        click.echo(f"joryu show: registry unavailable ({exc})", err=True)
        sys.exit(EXIT_OK)

    # Optionally load from disk if a loader exists.
    try:
        from .loader import load_migrations  # type: ignore
        load_migrations(Path("migrations"))
    except Exception:
        pass

    m = MIGRATIONS.get(migration_id)
    if m is None:
        click.echo(f"joryu show: unknown migration id {migration_id!r}", err=True)
        sys.exit(EXIT_GENERAL)

    click.echo(f"id:               {m.id}")
    click.echo(f"depends_on:       {m.depends_on}")
    click.echo(f"transaction_mode: {m.transaction_mode}")
    click.echo(f"dialects:         {m.dialects}")
    click.echo(f"tags:             {m.tags}")
    click.echo(f"group:            {m.group}")
    click.echo(f"on_mismatch:      {m.on_mismatch}")
    click.echo(f"file:             {m.file_path}")
    click.echo(f"registered:       {m.registered}")
    if m.registered:
        click.echo(f"steps:            {len(m.operations)}")
        for i, op in enumerate(m.operations):
            click.echo(f"  {i}: {op.kind} — {op.describe()}")


# ---------------------------------------------------------------------------
# mark
# ---------------------------------------------------------------------------


_MIGRATION_STATES = ("applied", "pending", "failed", "paused")
_STEP_STATES = ("done", "pending", "skipped")


@cli.command("mark", help="Manual state correction (last resort). See §16.")
@click.argument("identifier")
@click.option("--as", "as_state", required=True, help="Target state.")
@click.option("--reason", default=None, help="Required when marking a migration paused.")
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Skip the production-guard confirmation prompt (§15.2).",
)
def mark_cmd(identifier: str, as_state: str, reason: str | None, non_interactive: bool) -> None:
    # §15.2: manual state corrections on production are dangerous.
    if not non_interactive:
        env_name, host = _detect_env()
        if env_name != "local":
            prompt = (
                f"Manual state correction on production-like environment "
                f"({env_name}@{host}). Mark {identifier} as {as_state}?"
            )
            try:
                if not click.confirm(prompt, default=False):
                    click.echo("aborted by user", err=True)
                    sys.exit(EXIT_PROD_GUARD)
            except click.Abort:
                click.echo("aborted by user", err=True)
                sys.exit(EXIT_PROD_GUARD)
    is_step = "." in identifier
    try:
        from . import state  # type: ignore
    except ImportError:
        state = None  # type: ignore[assignment]

    if is_step:
        if as_state not in _STEP_STATES:
            click.echo(
                f"joryu mark: step state must be one of {_STEP_STATES} (got {as_state!r})",
                err=True,
            )
            sys.exit(EXIT_GENERAL)
        mid, _, step_str = identifier.partition(".")
        try:
            step_index = int(step_str)
        except ValueError:
            click.echo(f"joryu mark: invalid step index {step_str!r}", err=True)
            sys.exit(EXIT_GENERAL)

        if state is None or not hasattr(state, "mark_step"):
            click.echo("joryu mark: state module not wired up yet", err=True)
            sys.exit(EXIT_OK)
        try:
            state.mark_step(mid, step_index, as_state)
            click.echo(f"marked {mid}.{step_index} as {as_state}")
        except BaseException as exc:
            _die(exc)
        return

    # Migration-level mark
    if as_state not in _MIGRATION_STATES:
        click.echo(
            f"joryu mark: migration state must be one of {_MIGRATION_STATES} "
            f"(got {as_state!r})",
            err=True,
        )
        sys.exit(EXIT_GENERAL)
    if as_state == "paused" and not reason:
        click.echo("joryu mark: --as=paused requires --reason=\"...\"", err=True)
        sys.exit(EXIT_GENERAL)

    if state is None or not hasattr(state, "mark_migration"):
        click.echo("joryu mark: state module not wired up yet", err=True)
        sys.exit(EXIT_OK)
    try:
        state.mark_migration(identifier, as_state, reason=reason)
        click.echo(f"marked {identifier} as {as_state}")
    except BaseException as exc:
        _die(exc)


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


@cli.command("repair", help="Update the stored checksum of an applied migration.")
@click.argument("migration_id")
def repair_cmd(migration_id: str) -> None:
    try:
        from . import state  # type: ignore
    except ImportError:
        click.echo("joryu repair: state module not wired up yet", err=True)
        sys.exit(EXIT_OK)
    if not hasattr(state, "repair_checksum"):
        click.echo("joryu repair: state.repair_checksum not implemented yet", err=True)
        sys.exit(EXIT_OK)
    try:
        state.repair_checksum(migration_id)
        click.echo(f"repaired checksum for {migration_id}")
    except BaseException as exc:
        _die(exc)


# ---------------------------------------------------------------------------
# Stubbed subcommands (v0.1 placeholders)
# ---------------------------------------------------------------------------


@cli.command("schema-snapshot", help="Emit the current schema (§16).")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "sql"]),
    default="json",
    show_default=True,
)
@click.option(
    "--against",
    type=click.Choice(["db", "replay"]),
    default="db",
    show_default=True,
    help="Comparison source: live DB or replayed migrations.",
)
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("migrations"),
    show_default=True,
)
@click.option(
    "--url",
    default=None,
    help="Database URL (required when --against=db).",
)
def schema_snapshot_cmd(
    fmt: str, against: str, migrations_dir: Path, url: str | None
) -> None:
    try:
        from .snapshot import snapshot
    except ImportError as exc:
        click.echo(f"joryu schema-snapshot: not available ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    try:
        out = snapshot(
            url=url,
            migrations_dir=migrations_dir,
            against=against,  # type: ignore[arg-type]
            fmt=fmt,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        click.echo(f"joryu schema-snapshot: {exc}", err=True)
        sys.exit(EXIT_GENERAL)
    except BaseException as exc:
        _die(exc)
        return
    click.echo(out)
    sys.exit(EXIT_OK)


@cli.command("explain", help="Render a migration as English prose (§16).")
@click.argument("migration_id")
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("migrations"),
    show_default=True,
)
def explain_cmd(migration_id: str, migrations_dir: Path) -> None:
    try:
        from .explain import explain as _explain
    except ImportError as exc:
        click.echo(f"joryu explain: not available ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    try:
        out = _explain(migration_id, migrations_dir=migrations_dir)
    except KeyError as exc:
        click.echo(f"joryu explain: {exc.args[0] if exc.args else exc}", err=True)
        sys.exit(EXIT_GENERAL)
    except BaseException as exc:
        _die(exc)
        return
    click.echo(out)
    sys.exit(EXIT_OK)


@cli.command("test", help="Run migration tests (§6.4). --unit shipped; --integration is v0.3.")
@click.option("--unit", is_flag=True)
@click.option("--integration", is_flag=True)
@click.option("--dialects", default=None)
@click.option(
    "--migrations-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("migrations"),
    show_default=True,
)
def test_cmd(unit: bool, integration: bool, dialects: str | None,
             migrations_dir: Path) -> None:
    if integration:
        try:
            from .testing import run_integration_tests
        except ImportError as exc:
            click.echo(f"joryu test: integration runner not available ({exc})", err=True)
            sys.exit(EXIT_GENERAL)
        selected = (
            tuple(d.strip() for d in dialects.split(",") if d.strip())
            if dialects
            else ("postgresql", "mysql", "sqlite")
        )
        try:
            rep = run_integration_tests(
                migrations_dir=migrations_dir, dialects=selected
            )
        except SystemExit:
            raise
        except BaseException as exc:
            _die(exc)
            return
        ok = True
        for dialect, sub in rep.per_dialect.items():
            tag = "OK" if sub.ok else "FAIL"
            click.echo(
                f"[{tag}] {dialect}: applied {sub.applied}/{sub.total} in "
                f"{sub.duration_ms}ms; conflicts={len(sub.conflicts)} "
                f"failures={len(sub.failed)}"
            )
            for mid, err in sub.failed:
                click.echo(f"  FAIL {dialect} {mid}: {err}", err=True)
            for c in sub.conflicts:
                click.echo(f"  CONFLICT {dialect} {c.message}", err=True)
            if not sub.ok:
                ok = False
        for dialect, reason in rep.skipped:
            click.echo(f"[SKIP] {dialect}: {reason}")
        sys.exit(EXIT_OK if ok else EXIT_VERIFY_FAILED)

    try:
        from .testing import run_unit_tests
    except ImportError as exc:
        click.echo(f"joryu test: unit runner not available ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    try:
        rep = run_unit_tests(migrations_dir=migrations_dir)
    except SystemExit:
        raise
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


@cli.group("import", help="Import migrations from other tools.")
def import_group() -> None:
    pass


@import_group.command("alembic", help="Import an Alembic migrations directory (v1 feature).")
@click.option("--alembic-dir", type=click.Path(path_type=Path), default=Path("alembic"))
@click.option("--output-dir", type=click.Path(path_type=Path), default=Path("migrations"))
@click.option("--migrate-state", is_flag=True)
@click.option("--drop-alembic-table", is_flag=True)
@click.option("--report", is_flag=True)
@click.option("--url", default=None, help="DB URL for state handover (--migrate-state).")
def import_alembic_cmd(
    alembic_dir: Path,
    output_dir: Path,
    migrate_state: bool,
    drop_alembic_table: bool,
    report: bool,
    url: str | None,
) -> None:
    try:
        from .importer import import_alembic as _import_alembic
    except ImportError as exc:
        click.echo(f"joryu import alembic: importer not available ({exc})", err=True)
        sys.exit(EXIT_GENERAL)
    try:
        rep = _import_alembic(
            alembic_dir=alembic_dir,
            output_dir=output_dir,
            migrate_state=migrate_state,
            drop_alembic_table=drop_alembic_table,
            url=url,
        )
    except SystemExit:
        raise
    except BaseException as exc:
        _die(exc)
        return
    click.echo(f"converted: {rep.files_converted}, skipped: {rep.files_skipped}")
    if report:
        for path, msg in rep.todos:
            click.echo(f"  TODO {path}: {msg}")
    sys.exit(EXIT_OK)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point referenced by ``pyproject.toml`` (joryu.cli:main)."""
    cli(standalone_mode=True)
