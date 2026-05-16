"""Configuration loader for ``joryu.toml`` (SPEC §17).

The runner and CLI both need three things resolved consistently:

* ``database_url``    — connection string to operate against.
* ``migrations_dir``  — directory containing the migration files to discover.
* ``environment``     — optional override for production-safety guards (§15).

Resolution order for the URL:

1. Explicit ``url`` argument to ``apply()`` / ``down()``.
2. ``[database] url`` in ``joryu.toml`` (``env:DATABASE_URL`` expansion is
   supported, matching the example in §17).
3. The ``DATABASE_URL`` environment variable.

A missing config file is fine — the dataclass simply contains ``None`` values
and the caller falls back to defaults.
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("joryu")

DEFAULT_CONFIG_FILENAME = "joryu.toml"
DEFAULT_MIGRATIONS_DIR = "migrations"


@dataclass
class Config:
    database_url: str | None = None
    migrations_dir: Path = Path(DEFAULT_MIGRATIONS_DIR)
    environment: str | None = None
    source_path: Path | None = None


def _resolve_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw.startswith("env:"):
        return os.environ.get(raw[len("env:") :])
    return raw


def load_config(path: Path | None = None) -> Config:
    """Load ``joryu.toml`` from ``path`` or the nearest ancestor.

    If ``path`` is ``None``, walks up from the current working directory
    looking for ``joryu.toml``. Returns an empty Config when no file is found.
    """
    target = _find_config_file(path)
    if target is None:
        return Config(
            database_url=os.environ.get("DATABASE_URL"),
            migrations_dir=Path(DEFAULT_MIGRATIONS_DIR),
        )

    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        log.warning("could not read %s: %s", target, exc)
        return Config(
            database_url=os.environ.get("DATABASE_URL"),
            migrations_dir=Path(DEFAULT_MIGRATIONS_DIR),
            source_path=target,
        )

    joryu_sec = data.get("joryu", {}) or {}
    db_sec = data.get("database", {}) or {}

    migrations_dir_raw = joryu_sec.get("migrations_dir", DEFAULT_MIGRATIONS_DIR)
    migrations_dir = Path(migrations_dir_raw)
    if not migrations_dir.is_absolute():
        migrations_dir = (target.parent / migrations_dir).resolve()

    db_url = _resolve_url(db_sec.get("url")) or os.environ.get("DATABASE_URL")

    environment = joryu_sec.get("environment")

    return Config(
        database_url=db_url,
        migrations_dir=migrations_dir,
        environment=environment,
        source_path=target,
    )


def _find_config_file(path: Path | None) -> Path | None:
    if path is not None:
        return path if path.exists() else None
    cwd = Path.cwd()
    for candidate_dir in (cwd, *cwd.parents):
        candidate = candidate_dir / DEFAULT_CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def resolve_database_url(explicit: str | None = None, config: Config | None = None) -> str:
    """Resolve the database URL using the documented precedence."""
    if explicit:
        return explicit
    cfg = config or load_config()
    if cfg.database_url:
        return cfg.database_url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    raise RuntimeError(
        "no database URL configured; pass url=..., set DATABASE_URL, or add [database] url to joryu.toml"
    )


def resolve_migrations_dir(explicit: Path | None = None, config: Config | None = None) -> Path:
    if explicit is not None:
        return explicit
    cfg = config or load_config()
    return cfg.migrations_dir


__all__ = [
    "Config",
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_MIGRATIONS_DIR",
    "load_config",
    "resolve_database_url",
    "resolve_migrations_dir",
]
