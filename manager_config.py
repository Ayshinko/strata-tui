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

KEYS = ("STRATA_ROOT", "STRATA_API", "MANAGER_PORT", "STRATA_COMPONENTS_DIR", "STRATA_TUI_PROFILES", "STRATA_TUI_STATE",
        "STRATA_TUI_VERIFIED")
DEFAULTS = {k: "" for k in KEYS}
DEFAULT_PORT = 8275
DEFAULT_API = "http://127.0.0.1:8080"
#: your own launch profiles and the TUI's generated run configs and logs: outside the Strata folder, so an update never touches them
DEFAULT_PROFILES = r"C:\AI\strata-tui\profiles" if os.name == "nt" else "~/.local/share/strata-tui/profiles"
DEFAULT_STATE = r"C:\AI\strata-tui\state" if os.name == "nt" else "~/.local/share/strata-tui/state"


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


def components_dir() -> Path:
    """Strata's component folder (the engine packages and components/.updates/state.json)."""
    value = load().get("STRATA_COMPONENTS_DIR", "").strip()
    return Path(value).expanduser() if value else strata_root() / "components"


def profiles_dir() -> Path:
    """Your own launch profiles (STRATA_TUI_PROFILES)."""
    return Path(load().get("STRATA_TUI_PROFILES", "").strip() or DEFAULT_PROFILES).expanduser()


def state_dir() -> Path:
    """The TUI's generated run configs, engine logs and remembered engine version (STRATA_TUI_STATE)."""
    return Path(load().get("STRATA_TUI_STATE", "").strip() or DEFAULT_STATE).expanduser()


def verified_dir() -> Path:
    """The read-only copy of the verified settings (STRATA_TUI_VERIFIED; default: beside the profiles folder)."""
    value = load().get("STRATA_TUI_VERIFIED", "").strip() if "STRATA_TUI_VERIFIED" in KEYS else ""
    return Path(value).expanduser() if value else profiles_dir().parent / "verified"
