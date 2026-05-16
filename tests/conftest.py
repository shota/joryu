"""Shared test fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make sure the in-repo `src/` layout is importable even outside an editable install.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _purge_migration_modules() -> None:
    for name in list(sys.modules):
        if name.startswith("joryu_migrations."):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _reset_joryu_registry():
    """Each test starts with an empty migration registry."""
    from joryu.registry import reset_registry

    reset_registry()
    _purge_migration_modules()
    yield
    reset_registry()
    _purge_migration_modules()


@pytest.fixture
def tmp_migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"
