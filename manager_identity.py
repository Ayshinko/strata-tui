"""Strata Manager - the external compatibility layer that used to live in a patched setup.py.

The OFFICIAL setup.py of the checkout at STRATA_ROOT is imported as-is (a pure-Python module: no
third-party imports); it stays the single source of truth for MODELS / FAMILIES / CONTEXTS /
config parsing / hardware.  This module only adds what upstream Strata deliberately does not have
and the old Manager got by patching setup.py:

  * identity reconciliation: a custom-build config (strata-q2_0-abliterated.json) that official
    setup.py cannot name (its stem is not a known size) is mapped back to (family, model, variant)
    through manager-models.json -  friendly/API id -> strata-*.json config -> GGUF path ->
    display name.
  * custom GGUF install ("prepare"): run the OFFICIAL setup.py for the build (it writes canonical
    names), then convert the result to a variant install - its own pack folder and its own config
    with its own model_name + aliases - so two builds of the same size can coexist.  The canonical
    install (pack + config + run script) is moved aside for the run and restored afterwards, so
    nothing is ever destroyed or duplicated (renames only).  setup.py itself is never patched.
  * child_flags(): the no-console-window policy for short utility subprocesses (nvidia-smi, the
    python tools) when the Manager runs from pythonw - previously patched into setup.py.

The end of this file is a small CLI used by the Manager and the TUI to run a variant prepare in
the background:
    python manager_identity.py --prepare --root DIR --family qwen --model Q2_0
                               --gguf-dir DIR --context N --variant abliterated --log FILE
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import manager_config

HERE = Path(__file__).resolve().parent
META_FILE = HERE / "manager-models.json"
BACKUP_SUFFIX = ".bak-convert"            # canonical files set aside during a variant prepare


def child_flags() -> int:
    """Windows subprocess creation flags for short utility commands (nvidia-smi, powershell, small
    tools): CREATE_NO_WINDOW exactly when this process has NO console - a pythonw/GUI context (the
    Manager) - where a console child would flash a transient black window.  An interactive terminal
    keeps its console behavior (no flag); Linux: 0."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return 0                    # a real (or hidden) console to inherit: normal behavior
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    except Exception:
        return 0


def run(cmd, cwd=None, check=True, quiet=False, timeout=None):
    """Run a short utility command, with the no-console-window policy applied (child_flags)."""
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd,
                           stdout=subprocess.PIPE if quiet else None,
                           stderr=subprocess.STDOUT if quiet else None,
                           timeout=timeout, text=True,
                           creationflags=child_flags())
    except OSError:
        if check:
            raise
        return None
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(str(c) for c in cmd)}")
    return r


# ------------------------------------------------------------------------------------------------ identity
def models_meta() -> dict:
    """manager-models.json: friendly/API id -> {config, display_name, gguf_dir, family, quant,
    variant, aliases}."""
    try:
        data = json.loads(META_FILE.read_text(encoding="utf-8"))
        m = data.get("models") or {}
        return {k: v for k, v in m.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def meta_for_config(config_name: str) -> dict | None:
    """The metadata entry whose config field is this file name (strata-*.json), or None."""
    cfg = Path(config_name).name
    for entry in models_meta().values():
        if Path(entry.get("config") or "").name == cfg:
            return entry
    return None


def meta_for_id(friendly_id: str) -> dict | None:
    """The metadata entry whose friendly/API id is this model name."""
    return models_meta().get(friendly_id)


def variant_from_config(config_name: str) -> str | None:
    """A config's custom-build label from manager-models.json (the only source a variant identity
    has, now that official setup.py no longer carries --variant)."""
    entry = meta_for_config(config_name)
    return entry.get("variant") if entry else None


def reconcile(config_name: str, official: dict) -> dict:
    """Merge official setup.choices_from_config() with the external metadata: when official setup
    cannot name the config (a custom build's stem ends in *-<variant>), the metadata supplies the
    family, the size and the variant label.  Official answers always win when they are usable."""
    out = dict(official)
    entry = meta_for_config(config_name)
    if not entry:
        return out
    if entry.get("variant") is not None:
        out["variant"] = entry["variant"]
    if entry.get("family"):
        out["family"] = entry["family"]
    if not out.get("model") and entry.get("quant"):
        out["model"] = entry["quant"]
    return out


def display_title(family: str | None, model: str | None, variant: str | None,
                  context: int | None = None) -> str:
    """A readable title: 'Qwen3.8-Flash-Next Q2_0 Â· abliterated  Â·  64K' - shared by the Manager
    and the TUI so both present the same model identity."""
    root = manager_config.strata_root()
    try:
        sys.path.insert(0, str(root))
        import setup
        ft = setup.FAMILIES.get(family or "", {}).get("title") or family or "unknown"
    except Exception:
        ft = family or "unknown"
    title = f"{ft} {model}" if model else ft
    if variant:
        title += f" Â· {variant}"
    if context:
        title += f"  Â·  {context // 1024}K"
    return title


# ------------------------------------------------------------------------------------------------ variant prepare
def _read_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_config(path: Path, cfg: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _arg_val(args: list, key: str):
    try:
        i = args.index(key)
        return args[i + 1] if i + 1 < len(args) else None
    except ValueError:
        return None


def _replace_arg(args: list, key: str, value) -> list:
    out = list(args)
    try:
        i = out.index(key)
        out[i + 1] = str(value)
    except ValueError:
        out += [key, str(value)]
    return out


def _record_backup(root: Path, tag: str, stamp: str, moved: dict) -> dict:
    """The canonical files set aside for tag, remembered in root/logs/.  With a `stamp` it writes a
    new record; without one it returns the latest record (read-only)."""
    state = root / "logs" / f"manager-convert-{tag}.json"
    if stamp:
        try:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"tag": tag, "stamp": stamp, "moved": moved}),
                             encoding="utf-8")
        except OSError:
            pass
    try:
        return json.loads(state.read_text(encoding="utf-8")).get("moved") or {}
    except (OSError, ValueError):
        return {}


def _bail_out(tag: str, root: Path) -> None:
    """Move the canonical install (strata-<tag>.json + its pack + its run script) aside for the
    duration of an official setup run, so the run cannot destroy it.  Renames only."""
    cfg_path = root / f"strata-{tag}.json"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = {"config": None, "pack": None, "run": None}

    if not cfg_path.is_file():
        _record_backup(root, tag, stamp, moved)
        return
    try:
        cfg = _read_config(cfg_path)             # OSError/ValueError: no canonical install yet
        pack = Path(_arg_val(cfg.get("args") or [], "--pack") or "")
        if pack.is_dir():
            bak = Path(str(pack) + f"{BACKUP_SUFFIX}-{stamp}")
            if not bak.exists():
                pack.rename(bak)
                moved["pack"] = (str(pack), str(bak))
        run_bat = root / f"run-{tag}.bat"
        if run_bat.is_file():
            bak = Path(str(run_bat) + f"{BACKUP_SUFFIX}-{stamp}")
            run_bat.rename(bak)
            moved["run"] = (str(run_bat), str(bak))
        bak = Path(str(cfg_path) + f"{BACKUP_SUFFIX}-{stamp}")
        if not bak.exists():
            cfg_path.rename(bak)
            moved["config"] = (str(cfg_path), str(bak))
    except OSError as e:
        raise RuntimeError(f"could not set the canonical {tag} install aside: {e}")
    _record_backup(root, tag, stamp, moved)


def _restore(root: Path, tag: str) -> None:
    """Put the canonical install back after a variant prepare (rename-based: no copies).  Any fresh
    file/dir the run left at a canonical name is disposable (it was built this run) and is dropped."""
    rec = _record_backup(root, tag, "", {})
    for slot, v in (rec or {}).items():
        if not isinstance(v, (tuple, list)) or len(v) != 2 or not v[1]:
            continue
        orig, bak = v
        if not Path(bak).exists():
            continue
        if Path(orig).exists():
            Path(orig).unlink()
        Path(bak).rename(orig)


def _same_gguf_source(cfg_path: Path, gguf_dir: str) -> bool:
    """Does the config's --native shard already live in this folder?"""
    cfg = _read_config(cfg_path)
    native = _arg_val(cfg.get("args") or [], "--native")
    return bool(native) and Path(native).resolve().parent == Path(gguf_dir).resolve()


def prepare_variant(args) -> int:
    """The background preparation of a custom GGUF build, completely external:

      1. move the canonical install (strata-<tag>.json, its pack, its run script) aside
      2. run the OFFICIAL setup.py (--family --model --gguf-dir --context --no-start --yes);
         it writes canonical names, which is exactly what it knows
      3. convert: rename the fresh pack to packs/<tag>-<variant>, write the variant config
         (strata-<tag>-<variant>.json) with its own model_name + aliases + log
      4. restore the canonical install (renames only - nothing is copied or deleted)

    setup.py is never patched; official Strata updates cannot conflict with this.  The model files
    in --gguf-dir are only read.  Returns the process exit code (0 = installed)."""
    root = Path(args.root).expanduser().resolve()
    if not (root / "setup.py").is_file():
        print(f"  [x] {root} is no Strata checkout (no setup.py)")
        return 1
    if not re_allowed(args.variant or ""):
        print("  [x] --variant needs letters, digits, '-' and '_' only")
        return 1
    variant = (args.variant or "").strip().lower().replace(" ", "-")
    if not variant:
        print("  [x] --variant is empty")
        return 1
    sys.path.insert(0, str(root))
    import setup
    fam = setup.FAMILIES[args.family]
    tag = (fam["tag"] + args.model).lower()
    variant_cfg = root / f"strata-{tag}-{variant}.json"

    if variant_cfg.exists():
        try:
            same = _same_gguf_source(variant_cfg, args.gguf_dir)
        except (OSError, ValueError):
            same = False
        if same:
            print(f"  [ok] {variant_cfg.name} already points at {args.gguf_dir} - nothing to do")
            return 0
        print(f"  [x] another build already occupies {variant_cfg.name}; choose another --variant "
              "label (or fix manager-models.json)")
        return 1

    try:
        _bail_out(tag, root)
    except Exception as e:
        print(f"  [x] prepare failed: {e} - nothing was changed")
        return 1
    try:
        cmd = [sys.executable, str(root / "setup.py"), "--family", args.family, "--model", args.model,
               "--gguf-dir", str(Path(args.gguf_dir)), "--context", str(args.context),
               "--vision", args.vision, "--no-start", "--yes"]
        log = open(args.log, "a", encoding="utf-8", errors="replace") if args.log else None
        try:
            p = subprocess.Popen(cmd, cwd=str(root), stdout=log, stderr=subprocess.STDOUT)
            rc = p.wait()
        finally:
            if log:
                log.close()
        if rc != 0:
            print(f"  [x] official setup.py exited {rc} - see {args.log}; the canonical install "
                  "was restored, nothing changed")
            _restore(root, tag)
            return rc
        _convert_fresh(tag, variant, root)
        _restore(root, tag)
    except Exception as e:
        try:
            _restore(root, tag)
        except Exception:
            pass
        print(f"  [x] prepare failed: {e} - the canonical install was restored, nothing changed")
        return 1
    print(f"  [ok] {variant}'s build installed: strata-{tag}-{variant}.json")
    return 0


def re_allowed(variant: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9_-]+", variant))


def _convert_fresh(tag: str, variant: str, root: Path) -> None:
    """Turn the just-written canonical install (fresh pack + config from the official run) into the
    variant install: its own pack folder and config, with its own model_name and aliases, and the
    canonical log path.  The pack is RENAMED (same volume: no copy, no extra disk use)."""
    setup_dir = str(root)
    sys.path.insert(0, setup_dir)
    import setup
    cfg_path = root / f"strata-{tag}.json"
    cfg = _read_config(cfg_path)
    args = list(cfg.get("args") or [])
    pack = Path(_arg_val(args, "--pack") or "")
    fam_name = next((f for f, d in setup.FAMILIES.items()
                     if d["tag"] and tag.startswith(d["tag"])), "qwen")
    fam = setup.FAMILIES[fam_name]
    model = (tag[len(fam["tag"]):] if tag.startswith(fam["tag"]) else tag).upper()
    if model not in setup.MODELS:
        model = tag.split("-")[-1].upper()

    variant_pack = pack.with_name(pack.name + "-" + variant) if pack.name else pack
    if pack.is_dir():
        if variant_pack.exists():
            raise RuntimeError(f"the pack folder {variant_pack} already exists - another build of "
                               f"this size and label already uses it")
        pack.rename(variant_pack)
    else:
        raise RuntimeError(f"setup.py finished but wrote no pack at {pack}")

    model_name = f"{fam['name']}-{model.lower()}-{variant}"
    canonical_id = f"{fam['name']}-{model.lower()}"
    cfg["args"] = _replace_arg(args, "--pack", str(variant_pack))
    cfg["tokenizer"] = str(variant_pack / "tokenizer")
    cfg["model_name"] = model_name
    cfg["aliases"] = [canonical_id]
    cfg["log"] = str(root / f"strata-{tag}-{variant}.log")
    _write_config(root / f"strata-{tag}-{variant}.json", cfg)


def _drop_arg(args: list, key: str) -> list:
    """Remove a `--key [value]` pair (a value is everything up to the next flag)."""
    out, i = [], 0
    while i < len(args):
        if args[i] == key:
            if i + 1 < len(args) and not str(args[i + 1]).startswith("--"):
                i += 2
            else:
                i += 1
            continue
        out.append(args[i])
        i += 1
    return out


def apply_settings(args) -> int:
    """Apply a load-options change (context / vision) to ONE strata config file directly - the same
    surgical edit the Manager makes - so a custom build's settings change never re-runs official
    setup.py (which would rewrite the canonical slot of its size).  setup.py is only read (for the
    rope factor / Vision tables); it is never run or patched."""
    root = Path(args.root).expanduser().resolve()
    cfg_path = root / Path(args.config).name
    if not cfg_path.is_file():
        print(f"  [x] no such config: {cfg_path}")
        return 1
    sys.path.insert(0, str(root))
    import setup
    try:
        cfg = _read_config(cfg_path)
        a2 = list(cfg.get("args") or [])
        if args.context:
            ctx = int(args.context)
            a2 = _replace_arg(a2, "--max-context", str(ctx))
            if ctx <= 8192:
                a2 = _drop_arg(a2, "--kv")
            elif _arg_val(a2, "--kv") is None:
                a2 = _replace_arg(a2, "--kv", "int8")
            if ctx < 65536:
                a2 = _drop_arg(a2, "--kv-resident")
            if ctx > 262144 and _arg_val(a2, "--rope-scaling") is None:
                a2 = _replace_arg(a2, "--rope-scaling", "yarn")
                a2 = _replace_arg(a2, "--rope-scale", f"{setup.derived_factor(ctx):g}")
        # An omitted --vision means this is a VRAM-only surgical update.
        # Explicit callers still pass no/gpu/cpu and retain the old behavior.
        vision = (args.vision or "").lower()
        if vision in ("no", "off", "none"):
            cfg.pop("vision", None)
            a2 = _drop_arg(_drop_arg(a2, "--vision"), "--vram-reserve-mib")
        elif vision in ("gpu", "yes", "cpu"):
            cur = cfg.get("vision")
            if not isinstance(cur, dict):
                print("  [x] Vision is not installed for this model (the config has no encoder section);")
                print("      run the official SETUP.bat --vision for this size with the encoder installed first.")
                return 1
            mode = "gpu" if vision in ("gpu", "yes") else "cpu"
            cur = dict(cur)
            cur["gpu"] = (mode == "gpu")
            cur["max_tokens"] = setup.VISION[mode]["max_tokens"]
            cfg["vision"] = cur
            if "--vision" not in a2:
                a2 = a2 + ["--vision"]
            a2 = _replace_arg(a2, "--vram-reserve-mib", str(setup.VISION[mode]["reserve_mib"]))
        if getattr(args, "kv_resident", None) is not None:   # LOAD MODEL: KV residency
            if int(args.kv_resident) > 0:
                a2 = _replace_arg(a2, "--kv-resident", str(int(args.kv_resident)))
            else:
                a2 = _drop_arg(a2, "--kv-resident")
        if getattr(args, "vram_reserve_mib", None) is not None:   # VRAM kept free
            reserve = int(args.vram_reserve_mib)
            if reserve >= 0:
                a2 = _replace_arg(a2, "--vram-reserve-mib", str(reserve))
        cfg["args"] = a2
        tmp = cfg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cfg_path)
        print(f"  [ok] {cfg_path.name}: context/vision updated (model_name + aliases kept)")
        return 0
    except Exception as e:
        print(f"  [x] could not update {cfg_path.name}: {e}")
        return 1


# ------------------------------------------------------------------------------------------------ CLI
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prepare", action="store_true", help="prepare a custom GGUF build externally")
    ap.add_argument("--apply-settings", action="store_true",
                    help="apply context/vision to one config file directly (variant models)")
    ap.add_argument("--root", default="")
    ap.add_argument("--config", default="")
    ap.add_argument("--context", type=int, default=0)
    ap.add_argument("--vision", default="")
    ap.add_argument("--kv-resident", type=int, default=None,
                    help="VRAM KV residency in tokens (0 = drop it; otherwise set --kv-resident)")
    ap.add_argument("--vram-reserve-mib", type=int, default=None,
                    help="VRAM kept free in MiB (set --vram-reserve-mib)")
    ap.add_argument("--save-backup", action="store_true")
    g = ap.add_argument_group("prepare")
    g.add_argument("--family", default="qwen")
    g.add_argument("--model", default="Q2_0")
    g.add_argument("--gguf-dir", default="")
    g.add_argument("--variant", default="")
    g.add_argument("--log", default="")
    a = ap.parse_args()
    if a.prepare:
        if not a.gguf_dir:
            ap.error("--prepare needs --gguf-dir DIR")
        a.root = a.root or str(manager_config.strata_root())
        a.context = a.context or 65536
        a.vision = a.vision or "no"
        return prepare_variant(a)
    if a.apply_settings:
        a.root = a.root or str(manager_config.strata_root())
        return apply_settings(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
