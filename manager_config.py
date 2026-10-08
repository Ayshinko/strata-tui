"""Strata Manager - the manager's own configuration.

Single source of truth for the three paths/ports the whole manager uses:
  STRATA_ROOT     the official Strata checkout (setup.py, strata-*.json configs, serve/)
  STRATA_API      the Strata server's own API origin (proxied under MANAGER_PORT)
  MANAGER_PORT    this manager's own local port

The values are read from manager.env next to this file (KEY=VALUE, '#' comments).
Environment variables with the same names override the file (used by tests and the
launchers).  Nothing is hard-coded in the program: manager.env is the only place the
paths/ports live.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "manager.env"

KEYS = ("STRATA_ROOT", "STRATA_API", "MANAGER_PORT")
DEFAULTS = {k: "" for k in KEYS}
DEFAULT_PORT = 8275
DEFAULT_API = "http://127.0.0.1:8080"


def _parse_env(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load() -> dict:
    """The configuration: manager.env, overridden by environment variables."""
    cfg = dict(DEFAULTS)
    try:
        cfg.update(_parse_env(ENV_FILE.read_text(encoding="utf-8")))
    except OSError:
        pass
    for k in KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg["MANAGER_PORT"] = cfg["MANAGER_PORT"] or str(DEFAULT_PORT)
    cfg["STRATA_API"] = cfg["STRATA_API"] or DEFAULT_API
    return cfg


def manager_root() -> Path:
    """This manager's folder (where manager.env, gui/, STRATA-TUI.py live)."""
    return HERE


def strata_root() -> Path:
    """The official Strata checkout this manager manages, resolved and checked."""
    p = Path(load()["STRATA_ROOT"].strip()).expanduser().resolve()
    if not (p / "setup.py").is_file():
        raise RuntimeError(
            f"STRATA_ROOT={p} is not a Strata checkout (no setup.py). "
            f"Set the real path in {ENV_FILE}.")
    return p


def manager_port() -> int:
    try:
        return int(load()["MANAGER_PORT"])
    except (TypeError, ValueError):
        return DEFAULT_PORT


def strata_api() -> str:
    return load()["STRATA_API"] or DEFAULT_API