"""Public sync + async API entry points (§16.1).

Stub for now — the runner/CLI agent fills these in.
"""
from __future__ import annotations

from typing import Any


def apply(*args: Any, **kwargs: Any) -> None:
    from .runner import apply as _apply

    return _apply(*args, **kwargs)


async def apply_async(*args: Any, **kwargs: Any) -> None:
    from .runner import apply_async as _apply_async

    return await _apply_async(*args, **kwargs)


def down(*args: Any, **kwargs: Any) -> None:
    from .runner import down as _down

    return _down(*args, **kwargs)


async def down_async(*args: Any, **kwargs: Any) -> None:
    from .runner import down_async as _down_async

    return await _down_async(*args, **kwargs)


def status(*args: Any, **kwargs: Any):
    from .runner import status as _status

    return _status(*args, **kwargs)


def verify(*args: Any, **kwargs: Any):
    from .verify import verify as _verify

    return _verify(*args, **kwargs)


def generate(*args: Any, **kwargs: Any):
    from .generate import generate as _generate

    return _generate(*args, **kwargs)
