"""Before a start: has the engine changed since the last start? If it has, list how Strata's own launch files differ from the
profile. Nothing is changed anywhere: the profile stays as it is, and Strata's files are only read.

Strata's files that an update, setup or its launcher rewrite (read here):
  <Strata>/strata-<model>.json                      setup.py writes it ("args", "user_args")
  %LOCALAPPDATA%/Strata/launch/<profile>.json      `strata launch` writes it at every start ("args")
  %APPDATA%/Strata/launch-presets/<profile>.json   the launcher's preset: "extra" (flags and environment) and "drafting"
The engine version is the active (or pinned) package in <components>/.updates/state.json.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import live_status as L
import profiles as P

SEEN_FILE = "engine-seen.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def last_seen(state: Path) -> str | None:
    data = _read(Path(state) / SEEN_FILE) or {}
    return data.get("version")


def mark_seen(state: Path, version: str) -> None:
    Path(state).mkdir(parents=True, exist_ok=True)
    tmp = Path(state) / (SEEN_FILE + ".tmp")
    tmp.write_text(json.dumps({"version": version, "at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2), encoding="utf-8")
    tmp.replace(Path(state) / SEEN_FILE)


def _overlay(args: list, user: list) -> list[str]:
    """``args`` with ``user_args`` on top, flag by flag (Strata's user_args.merge)."""
    mine = {g[0] for g in P.groups(user or [])}
    return [t for g in P.groups(args or []) if g[0] not in mine for t in g] + [str(t) for t in user or []]


def _launch_dirs() -> tuple[Path | None, Path | None]:
    local, roaming = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if os.name != "nt":
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "strata"
        return base / "launch", None
    return (Path(local) / "Strata" / "launch" if local else None,
            Path(roaming) / "Strata" / "launch-presets" if roaming else None)


def strata_sources(strata_root: Path, model_name: str, launch_dir: Path | None = None,
                   preset_dir: Path | None = None) -> list[tuple[str, list[str], dict, str | None]]:
    """(label, engine args, env, drafting) of every Strata file that describes this model."""
    out = []
    ld, pd = _launch_dirs()
    launch_dir = launch_dir if launch_dir is not None else ld
    preset_dir = preset_dir if preset_dir is not None else pd
    for path in sorted(Path(strata_root).glob("strata-*.json")):
        cfg = _read(path)
        if cfg and cfg.get("model_name") == model_name and isinstance(cfg.get("args"), list):
            out.append((str(path), _overlay(cfg["args"], cfg.get("user_args") or []), cfg.get("env") or {}, cfg.get("drafting")))
    if launch_dir and launch_dir.is_dir():
        for path in sorted(launch_dir.glob("*.json")):
            if path.name.endswith((".choices.json", ".tune.json")):
                continue
            cfg = _read(path)
            if cfg and cfg.get("model_name") == model_name and isinstance(cfg.get("args"), list):
                out.append((str(path), _overlay(cfg["args"], cfg.get("user_args") or []), cfg.get("env") or {}, cfg.get("drafting")))
    if preset_dir and preset_dir.is_dir():
        for path in sorted(preset_dir.glob("*.json")):
            pre = _read(path)
            if not pre or not isinstance(pre.get("extra"), dict):
                continue
            launch_json = (launch_dir / path.name) if launch_dir else None
            linked = _read(launch_json) if launch_json else None
            if linked and linked.get("model_name") != model_name:
                continue
            flags = [t for k, v in pre["extra"].items() if str(k).startswith("--")
                     for t in ([k] if v in (True, "", None) else [k, str(v)])]
            env = {k: str(v) for k, v in pre["extra"].items() if not str(k).startswith("--")}
            out.append((str(path) + " (preset extra)", flags, env, pre.get("drafting")))
    return out


def diff_source(profile: dict, strata_root: Path, args: list[str], env: dict, drafting: str | None,
                partial: bool = False) -> list[str]:
    """How one Strata file differs from the profile. `partial`: the file lists only some flags (a preset's "extra")."""
    want = P.engine_argv(profile, strata_root)
    lines = L.compare_args(want, args)
    if partial:
        lines = [x for x in lines if not x.startswith("missing ")]
    for k in sorted(set(profile.get("env") or {}) | set(env)):
        a, b = (profile.get("env") or {}).get(k), env.get(k)
        if a != b and not (partial and b is None):
            lines.append(f"env {k}: profile {a!r} / Strata {b!r}")
    if drafting is not None and drafting != profile.get("drafting"):
        lines.append(f"drafting: profile {profile.get('drafting')} / Strata {drafting}")
    return lines


def check(profile: dict, *, strata_root: Path, components: Path, state: Path, record: bool = True,
          launch_dir: Path | None = None, preset_dir: Path | None = None) -> dict:
    """{"version", "previous", "changed", "sources": [(label, [differences])]}. The first check only remembers the version.
    `record`: remember the version now (the caller does it after showing the report)."""
    version = P.engine_version(components)
    previous = last_seen(state)
    changed = bool(version and previous and version != previous)
    sources = []
    if changed:
        for label, args, env, drafting in strata_sources(strata_root, profile["model_name"], launch_dir, preset_dir):
            try:
                sources.append((label, diff_source(profile, strata_root, args, env, drafting, partial=label.endswith("(preset extra)"))))
            except P.ProfileError as e:
                sources.append((label, [f"could not be read: {e}"]))
    if record and version and version != previous:
        mark_seen(state, version)
    return {"version": version, "previous": previous, "changed": changed, "sources": sources}
