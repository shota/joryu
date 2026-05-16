"""Discover and import migration files from a directory.

Each ``*.py`` file under ``migrations_dir`` is expected to contain a single
``@joryu.migration(...)`` decorated function (§3.2). Importing the file is what
registers the migration into ``registry.MIGRATIONS``; this module is just the
"walk the directory in deterministic order and import" wrapper.

Files starting with ``_`` (e.g. ``__init__.py``) and a top-level
``conftest.py`` are skipped.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Iterable

from .registry import MIGRATIONS, Migration

log = logging.getLogger("joryu")


def discover_migration_files(migrations_dir: Path) -> list[Path]:
    """Return migration files in import order (sorted by filename).

    Filename ordering matches the timestamp-prefixed naming convention in §3.1
    so a lexical sort is also chronologically correct.
    """
    if not migrations_dir.exists():
        raise FileNotFoundError(f"migrations directory does not exist: {migrations_dir}")
    if not migrations_dir.is_dir():
        raise NotADirectoryError(f"migrations path is not a directory: {migrations_dir}")
    files = []
    for path in sorted(migrations_dir.iterdir()):
        if path.suffix != ".py":
            continue
        if path.name.startswith("_") or path.name == "conftest.py":
            continue
        files.append(path)
    return files


def _import_file(path: Path) -> None:
    # Use a synthetic module name so that two migrations sharing a stem (which
    # would be a bug anyway) at least surface as a clear ValueError from the
    # @migration decorator rather than a silent reimport.
    mod_name = f"joryu_migrations.{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load migration file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise


def load_migrations(migrations_dir: Path) -> dict[str, Migration]:
    """Import every migration file under ``migrations_dir``.

    Returns the populated ``MIGRATIONS`` mapping. Already-imported migrations
    are not re-imported (Python's module cache handles that), but newly
    discovered ones are appended.
    """
    files = discover_migration_files(migrations_dir)
    for path in files:
        mod_name = f"joryu_migrations.{path.stem}"
        if mod_name in sys.modules:
            continue
        log.debug("loading migration file %s", path)
        _import_file(path)
    return MIGRATIONS


def loaded_migrations() -> Iterable[Migration]:
    """Return every Migration currently registered in this process."""
    return MIGRATIONS.values()


__all__ = ["discover_migration_files", "load_migrations", "loaded_migrations"]
