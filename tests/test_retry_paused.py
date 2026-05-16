"""--retry-paused polling loop (§10.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from joryu.exceptions import MigrationPaused
from joryu.runner import _parse_interval, apply, status


def _write(dir_: Path, name: str, body: str) -> Path:
    p = dir_ / f"{name}.py"
    p.write_text(body)
    return p


def test_parse_interval_units():
    assert _parse_interval("30s") == 30.0
    assert _parse_interval("5m") == 300.0
    assert _parse_interval("1h") == 3600.0
    assert _parse_interval("500ms") == 0.5
    assert _parse_interval("0") == 0.0
    assert _parse_interval(0.1) == 0.1
    assert _parse_interval(2) == 2.0


def test_parse_interval_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_interval("never")
    with pytest.raises(ValueError):
        _parse_interval("-5")
    with pytest.raises(ValueError):
        _parse_interval(-1)


def test_retry_paused_recovers_when_step_flips_ready(
    tmp_migrations_dir: Path, sqlite_url: str, tmp_path: Path
):
    """A step that pauses on the first attempt and succeeds on the second must
    complete when --retry-paused is set."""
    sentinel = tmp_path / "ready.flag"
    _write(
        tmp_migrations_dir,
        "20260101T000000_wait",
        f'''
import os
import joryu
from joryu import op

@joryu.migration(id="20260101T000000_wait")
def upgrade():
    @op.step
    def wait(conn, dialect, checkpoint):
        attempts = checkpoint.get("attempts", 0) + 1
        checkpoint.set("attempts", attempts)
        if not os.path.exists({str(sentinel)!r}):
            # Create the flag so the *next* attempt succeeds.
            open({str(sentinel)!r}, "w").close()
            raise op.PauseStep("not ready yet")
        return True
''',
    )

    apply(
        url=sqlite_url,
        migrations_dir=tmp_migrations_dir,
        retry_paused=True,
        retry_interval="0.05s",
        retry_max_attempts=5,
    )

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "applied"


def test_retry_paused_gives_up_after_max_attempts(
    tmp_migrations_dir: Path, sqlite_url: str
):
    _write(
        tmp_migrations_dir,
        "20260101T000000_always_pause",
        '''
import joryu
from joryu import op

@joryu.migration(id="20260101T000000_always_pause")
def upgrade():
    @op.step
    def wait(conn, dialect, checkpoint):
        raise op.PauseStep("never ready")
''',
    )

    with pytest.raises(MigrationPaused):
        apply(
            url=sqlite_url,
            migrations_dir=tmp_migrations_dir,
            retry_paused=True,
            retry_interval="0s",
            retry_max_attempts=3,
        )

    rows = status(url=sqlite_url, migrations_dir=tmp_migrations_dir)
    assert rows[0]["status"] == "paused"
