"""Production-safety / environment detection (§15)."""
from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlparse

EnvName = Literal["local", "staging", "production"]

_LOCAL_HINT_HOSTS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")
_LOCAL_ENV_VALUES = ("local", "dev", "test")

_declared_environment: EnvName | None = None


def set_environment(env: EnvName) -> None:
    """Explicitly declare the environment (§15.2). Overrides heuristics."""
    global _declared_environment
    if env not in ("local", "staging", "production"):
        raise ValueError(f"unknown environment {env!r}")
    _declared_environment = env


def get_declared_environment() -> EnvName | None:
    return _declared_environment


def detect_environment(url: str | None) -> tuple[EnvName, str | None]:
    """Return (env, host). Falls back to heuristics if not declared."""
    if _declared_environment is not None:
        host = _extract_host(url) if url else None
        return _declared_environment, host
    env_var = os.environ.get("JORYU_ENV")
    if env_var and env_var in _LOCAL_ENV_VALUES:
        return "local", _extract_host(url) if url else None
    if url is None:
        return "local", None
    host = _extract_host(url)
    if host is None:
        # SQLite file path / in-memory — treat as local.
        return "local", None
    if host in _LOCAL_HINT_HOSTS:
        return "local", host
    if ".local" in host or "local-" in host or "-local" in host:
        return "local", host
    # Otherwise we treat as production-like.
    return "production", host


def _extract_host(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.hostname:
        return parsed.hostname
    # SQLite often looks like "sqlite:///path/to.db" or "sqlite:///:memory:".
    return None
