"""Your own launch profiles, kept outside the Strata folder so a Strata update or setup run never rewrites them.

A profile is one JSON file in the profiles folder (manager_config.profiles_dir()). From it the TUI writes a run config of
its own (in the state folder, never in Strata's) and starts Strata's serve/server.py with it: the engine itself only talks
to server.py over a pipe, so the HTTP API, the tokenizer and the picture reader need server.py.

server.py reshapes the engine arguments at every start (user_args, the drafting policy, the memory fit, saved tunings).
To keep the profile's values exactly:

* every argument is written to "args" AND "user_args" (server.py puts user_args over everything it generates; the drafting
  policy and the memory fit keep a value found in user_args);
* "drafting" is the profile's choice (dflash2 | mtp | off | auto), so the drafting policy picks the same drafter;
* the server starts with STRATA_NO_PRESET=1, so no saved tuning is put on top.

One value is still Strata's own: with "drafting": "dflash2" the drafting policy names the drafter pack itself
(strata_control/drafting.py pack_path). After a start, live_status.compare_args() checks the engine's real command line
against the profile and says what differs.

Schema ("strata-tui.profile/1"):

    name            shown in the menu
    model_name      the API model id (Strata's "model_name")
    port            the API port
    engine          the engine program, normally "component:strata-engine" (the installed or pinned package)
    drafting        dflash2 | mtp | off | auto
    model           pack, native, ple_gguf (optional), expert_profile (optional), tokenizer
    vision          {"mode": "cpu" | "gpu" | "off", "mmproj": FILE, "threads": N, "max_tokens": N}
    args            the engine options, verbatim, in argv form
    env             environment for the engine, e.g. {"STRATA_ROUTE_PRIOR": "1.5"}
    refusal         the control vector (see REFUSAL_DEFAULT)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "strata-tui.profile/1"
DRAFTING = ("dflash2", "mtp", "off", "auto")

#: The control vector Strata ships (docs/DETAILS.md "Experimental speed projection"; data/experimental-speed-projection/).
BUILTIN_VECTOR = Path("data") / "experimental-speed-projection" / "Qwen3.8-Flash-Next-experimental-speed-projection.gguf"
#: Off unless turned on. Its package describes the vector as a refusal-direction projection: it removes a safety behaviour.
#: scale: project mode removes scale * (h . v) v (engine generate.cpp load_control_vectors): 1.0 removes the direction, 0.5 half of it.
#: per_request_default: false writes "sampling": {"experimental_speed_projection": false}, so a request turns it on itself.
REFUSAL_DEFAULT = {"enabled": False, "vector": "builtin", "scale": 1.0, "layers": [4, 44], "mode": "project",
                   "dir": "per-layer", "per_request_default": True}

#: Engine options that take more than one value (Strata's strata_control/user_args.py MULTI_VALUE_FLAGS).
MULTI_VALUE = {"--control-vector-layer-range": 2}
CVEC_FLAGS = ("--control-vector", "--control-vector-scaled", "--control-vector-layer-range", "--cvec-mode", "--cvec-dir")
MODEL_FLAGS = ("--pack", "--native", "--ple-gguf", "--expert-profile")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileError(ValueError):
    """A profile that cannot be used, with the reason in plain words."""


# ------------------------------------------------------------------------------------------------- argv groups

def groups(argv: list[Any]) -> list[list[str]]:
    """``--flag value...`` groups. A token that starts with ``--`` opens a group; a multi-value option takes its count."""
    items = [str(a) for a in argv or []]
    out: list[list[str]] = []
    i = 0
    while i < len(items):
        tok = items[i]
        if not tok.startswith("--"):
            raise ProfileError(f"argument {tok!r} does not follow an option")
        if tok in MULTI_VALUE:
            n = MULTI_VALUE[tok]
            out.append(items[i:i + 1 + n])
            i += 1 + n
            continue
        g = [tok]
        i += 1
        while i < len(items) and not items[i].startswith("--"):
            g.append(items[i])
            i += 1
        out.append(g)
    return out


def flag_value(argv: list[Any], flag: str) -> str | None:
    for g in groups(argv):
        if g[0] == flag:
            return g[1] if len(g) > 1 else ""
    return None


def without(argv: list[Any], flags) -> list[str]:
    return [t for g in groups(argv) if g[0] not in set(flags) for t in g]


# ------------------------------------------------------------------------------------------------- files

def profile_id(path: Path) -> str:
    return Path(path).stem


def list_profiles(folder: Path) -> list[tuple[Path, dict | None, str]]:
    """(path, profile or None, problem) for every *.json in the folder, sorted by name."""
    out = []
    try:
        files = sorted(Path(folder).glob("*.json"), key=lambda p: p.name.casefold())
    except OSError:
        return []
    for p in files:
        try:
            prof = load(p)
            out.append((p, prof, ""))
        except (OSError, ProfileError, ValueError) as e:
            out.append((p, None, str(e)))
    return out


def load(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate(data)
    data.setdefault("refusal", {})
    data["refusal"] = {**REFUSAL_DEFAULT, **(data.get("refusal") or {})}
    return data


#: Profiles that hold the owner's verified settings: never saved over and never deleted (save a new profile instead).
PROTECTED_IDS = frozenset({"qwen3.8-iq3s-dflash2"})
TRASH = "_deleted"


def is_protected(path: Path) -> bool:
    return Path(path).stem.casefold() in {p.casefold() for p in PROTECTED_IDS}


def set_readonly(path: Path) -> None:
    """The read-only file attribute (Windows) / no write bits: a slip in an editor or another tool cannot change it silently."""
    import stat
    try:
        os.chmod(path, stat.S_IREAD | (os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH)))
    except OSError:
        pass


def soft_delete(path: Path) -> Path:
    """Moves a profile (and its .json.bak) into <profiles>/_deleted/ with a time stamp, so it can be found again. Returns where."""
    import time
    path = Path(path)
    if is_protected(path):
        raise ProfileError(f"{path.name} holds the verified settings and cannot be deleted")
    if not path.is_file():
        raise ProfileError(f"{path} does not exist")
    trash = path.parent / TRASH
    trash.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = trash / f"{path.stem}.{stamp}.json"
    n = 1
    while dest.exists():
        n += 1
        dest = trash / f"{path.stem}.{stamp}-{n}.json"
    path.replace(dest)
    bak = path.with_suffix(".json.bak")
    if bak.is_file():
        bak.replace(dest.with_suffix(".json.bak"))
    return dest


def save(path: Path, profile: dict) -> None:
    """Writes atomically; an existing file is kept beside it as <name>.json.bak. A protected profile is refused."""
    if is_protected(path):
        raise ProfileError(f"{Path(path).name} holds the verified settings and is read-only: save as a new profile instead")
    validate(profile)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        bak = path.with_suffix(".json.bak")
        bak.unlink(missing_ok=True)
        path.replace(bak)
    tmp.replace(path)


def save_as(folder: Path, new_id: str, profile: dict) -> Path:
    if not _ID.match(new_id or ""):
        raise ProfileError("a profile name may use letters, digits, '.', '_' and '-' (up to 64)")
    path = Path(folder) / f"{new_id}.json"
    if path.exists():
        raise ProfileError(f"{path.name} already exists")
    if is_protected(path):
        raise ProfileError(f"{path.name} is the verified profile's name; choose another")
    save(path, profile)
    return path


def validate(p: Any) -> None:
    if not isinstance(p, dict):
        raise ProfileError("a profile is a JSON object")
    if p.get("schema") != SCHEMA:
        raise ProfileError(f'"schema" must be "{SCHEMA}"')
    for key in ("name", "model_name"):
        if not isinstance(p.get(key), str) or not p[key].strip():
            raise ProfileError(f'"{key}" is missing')
    port = p.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ProfileError('"port" must be a number 1-65535')
    if p.get("drafting") not in DRAFTING:
        raise ProfileError(f'"drafting" must be one of {", ".join(DRAFTING)}')
    model = p.get("model")
    if not isinstance(model, dict) or not model.get("pack") or not model.get("native") or not model.get("tokenizer"):
        raise ProfileError('"model" needs pack, native and tokenizer')
    args = p.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ProfileError('"args" must be a list of strings')
    gs = groups(args)                              # raises on a stray value
    bad = [g[0] for g in gs if g[0] in MODEL_FLAGS]
    if bad:
        raise ProfileError(f'{", ".join(bad)} belong in "model", not in "args"')
    if any(g[0] in CVEC_FLAGS for g in gs):
        raise ProfileError('control vector options belong in "refusal", not in "args"')
    env = p.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ProfileError('"env" must map names to strings')
    vision = p.get("vision", {"mode": "off"})
    if not isinstance(vision, dict) or vision.get("mode", "off") not in ("cpu", "gpu", "off"):
        raise ProfileError('"vision.mode" must be cpu, gpu or off')
    if vision.get("mode", "off") != "off" and not vision.get("mmproj"):
        raise ProfileError('"vision" needs "mmproj" when it is on')
    if ("--vision" in args) != (vision.get("mode", "off") != "off"):
        raise ProfileError('"--vision" in "args" and "vision.mode" disagree')
    validate_refusal({**REFUSAL_DEFAULT, **(p.get("refusal") or {})})


def validate_refusal(r: dict) -> None:
    if not isinstance(r.get("enabled"), bool):
        raise ProfileError('"refusal.enabled" must be true or false')
    scale = r.get("scale")
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < float(scale) <= 1.0:
        raise ProfileError('"refusal.scale" must be a number above 0 and at most 1.0 (owner rule)')
    layers = r.get("layers")
    if (not isinstance(layers, list) or len(layers) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in layers)
            or not 1 <= layers[0] <= layers[1]):
        raise ProfileError('"refusal.layers" must be [first, last] with 1 <= first <= last')
    if r.get("mode") not in ("project", "add"):
        raise ProfileError('"refusal.mode" must be project or add')
    if not (r.get("dir") == "per-layer" or re.fullmatch(r"single:\d+", str(r.get("dir")))):
        raise ProfileError('"refusal.dir" must be per-layer or single:L')
    if not isinstance(r.get("vector"), str) or not r["vector"]:
        raise ProfileError('"refusal.vector" must be "builtin" or a file path')
    if not isinstance(r.get("per_request_default"), bool):
        raise ProfileError('"refusal.per_request_default" must be true or false')


# ------------------------------------------------------------------------------------------------- the run config

def vector_path(refusal: dict, strata_root: Path) -> Path:
    v = refusal.get("vector") or "builtin"
    return Path(strata_root) / BUILTIN_VECTOR if v == "builtin" else Path(v)


def refusal_args(refusal: dict, strata_root: Path) -> list[str]:
    """The engine options of the control vector (docs/DETAILS.md), or none when it is off."""
    if not refusal.get("enabled"):
        return []
    a, b = refusal["layers"]
    return ["--control-vector-scaled", f"{vector_path(refusal, strata_root)}:{float(refusal['scale']):g}",
            "--control-vector-layer-range", str(a), str(b), "--cvec-mode", refusal["mode"], "--cvec-dir", refusal["dir"]]


def model_args(model: dict) -> list[str]:
    out = ["--pack", model["pack"], "--native", model["native"]]
    if model.get("ple_gguf"):
        out += ["--ple-gguf", model["ple_gguf"]]
    if model.get("expert_profile"):
        out += ["--expert-profile", model["expert_profile"]]
    return out


def engine_argv(profile: dict, strata_root: Path) -> list[str]:
    """The engine options the profile means: the model's files, the profile's options verbatim, the control vector."""
    refusal = {**REFUSAL_DEFAULT, **(profile.get("refusal") or {})}
    return model_args(profile["model"]) + list(profile["args"]) + refusal_args(refusal, strata_root)


def active_engine_folder(components: Path) -> Path | None:
    """The engine package a start uses: the pinned one (a manual rollback), else the active one (components/.updates/state.json)."""
    try:
        state = json.loads((Path(components) / ".updates" / "state.json").read_text(encoding="utf-8-sig"))
        eng = state.get("engine") or {}
        name = eng.get("pinned") or eng.get("active")
    except (OSError, ValueError, AttributeError):
        return None
    if not name:
        return None
    folder = Path(components) / "engine" / str(name)
    return folder if folder.is_dir() else None


def engine_version(components: Path) -> str | None:
    folder = active_engine_folder(components)
    return folder.name.split("-", 1)[0] if folder else None


def run_config(profile: dict, *, strata_root: Path, components: Path, log_path: Path) -> dict:
    """Strata's run config for this profile (written by the TUI to its own state folder)."""
    validate(profile)
    argv = engine_argv(profile, strata_root)
    cfg: dict[str, Any] = {
        "exe": profile.get("engine") or "component:strata-engine",
        "args": list(argv),
        "user_args": list(argv),
        "drafting": profile["drafting"],
        "cwd": str(strata_root),
        "tokenizer": profile["model"]["tokenizer"],
        "model_name": profile["model_name"],
        "log": str(log_path),
        "lib_dirs": [],
        "port": profile["port"],
        "host": "127.0.0.1",
        "gpu": 0,
        "gpus_asked": True,
        "env": dict(profile.get("env") or {}),
    }
    vision = profile.get("vision") or {"mode": "off"}
    if vision.get("mode", "off") != "off":
        folder = active_engine_folder(components)
        exe = (folder / ("strata-vision.exe" if os.name == "nt" else "strata-vision")) if folder else None
        cfg["vision"] = {"exe": str(exe) if exe else "", "mmproj": vision["mmproj"], "model": profile["model"]["native"],
                         "gpu": vision["mode"] == "gpu", "max_tokens": int(vision.get("max_tokens", 300)),
                         "threads": int(vision.get("threads", 6))}
    refusal = {**REFUSAL_DEFAULT, **(profile.get("refusal") or {})}
    if refusal["enabled"] and not refusal["per_request_default"]:
        cfg["sampling"] = {"experimental_speed_projection": False}
    return cfg


def missing_files(profile: dict, strata_root: Path) -> list[str]:
    """Files the profile names that are not on this computer (only existence; nothing is read)."""
    out = []
    m = profile["model"]
    for key in ("pack", "native", "ple_gguf", "expert_profile", "tokenizer"):
        if m.get(key) and not Path(m[key]).exists():
            out.append(f"model.{key}: {m[key]}")
    v = profile.get("vision") or {}
    if v.get("mode", "off") != "off" and not Path(v["mmproj"]).exists():
        out.append(f"vision.mmproj: {v['mmproj']}")
    for flag in ("--draft-model", "--mtp-draft-vocab"):
        val = flag_value(profile["args"], flag)
        if val and not Path(val).exists():
            out.append(f"{flag}: {val}")
    val = flag_value(profile["args"], "--mtp")
    if val and not val.isdigit() and not Path(val).exists():
        out.append(f"--mtp: {val}")
    r = {**REFUSAL_DEFAULT, **(profile.get("refusal") or {})}
    if r["enabled"] and not vector_path(r, strata_root).is_file():
        out.append(f"refusal vector: {vector_path(r, strata_root)}")
    return out


def refusal_label(profile: dict) -> str:
    r = {**REFUSAL_DEFAULT, **(profile.get("refusal") or {})}
    if not r["enabled"]:
        return "off"
    vec = "builtin" if r["vector"] == "builtin" else Path(r["vector"]).name
    a, b = r["layers"]
    per = "" if r["per_request_default"] else ", per request off"
    return f"on x{float(r['scale']):g} L{a}-{b} {vec}{per}"
