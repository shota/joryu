"""``joryu generate`` (§8).

This module is the thin public façade. ``--empty`` keeps its v0.1 behaviour
(scaffold an empty migration); supplying ``target=`` routes through the real
autogenerator in :mod:`joryu.autogen`.
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy import MetaData


def generate(
    slug: str,
    *,
    empty: bool = False,
    against: str = "db",
    migrations_dir: str | Path = "migrations",
    target: "MetaData | None" = None,
    url: str | None = None,
) -> Path:
    """Create a new migration file.

    - ``empty=True`` or ``target is None`` → emit an empty scaffold (the v0.1
      behaviour). This keeps existing CLI / API callers working.
    - Otherwise diff ``target`` against the live DB / replay and write a
      filled-in migration via :func:`joryu.autogen.generate_diff`.
    """
    if empty or target is None:
        return _generate_empty(slug, migrations_dir=migrations_dir)
    from .autogen import generate_diff
    return generate_diff(
        slug,
        target=target,
        against=against,
        migrations_dir=migrations_dir,
        url=url,
    )


def _generate_empty(slug: str, *, migrations_dir: str | Path) -> Path:
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^a-z0-9_]+", "_", slug.lower()).strip("_")[:60] or "migration"
    name = f"{ts}_{safe}"
    dir_path = Path(migrations_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{name}.py"
    suffix = 2
    while file_path.exists():
        file_path = dir_path / f"{name}_{suffix}.py"
        suffix += 1
    body = f'''"""TODO: describe this migration."""
import joryu
from joryu import op, types as t


@joryu.migration(id={file_path.stem!r})
def upgrade():
    pass


@joryu.downgrade
def downgrade():
    # JORYU-DOWN-HINT: schema-impact: []
    # JORYU-DOWN-HINT: cross-references: []
    # JORYU-DOWN-HINT: data-loss-risk: none
    # JORYU-DOWN-HINT: requires-app-knowledge: false
    # JORYU-DOWN-HINT: completion-status: stub
    pass
'''
    file_path.write_text(body)
    return file_path
