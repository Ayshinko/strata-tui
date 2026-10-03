"""STRATA-TUI - a minimal terminal launcher for Strata.

"A small interface, not another layer."

One compact, keyboard-driven terminal screen over the current Strata checkout:

  model  ->  prepare  ->  start  ->  logs

It does not build a second management stack.  There is no second server, no
reverse proxy, no daemon, no dashboard repainting the menu in the background,
and no third-party Python dependency: the launcher draws the screen with plain
ANSI sequences and reuses the Strata checkout's own logic.

Strata remains the source of truth.  Model support comes from the current
setup.py (MODELS / FAMILIES / GGUF_QUANT / gguf_dir_shards), GGUF verification
comes from tools/gguf_reader.py, and preparation is a subprocess call to the
current setup.py.  If upstream Strata adds or removes a supported quant or
family, this launcher follows the installed checkout rather than a handwritten
whitelist.

Place STRATA-TUI.py in the Strata repository root (or tools/strata_tui.py,
which resolves the root the same way).
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

_HERE = Path(__file__).resolve()
ROOT = _HERE.parent if _HERE.name == "STRATA-TUI.py" else _HERE.parent.parent


def _smash(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


SETTINGS_FILE = Path.home() / ".strata-tui.json"
RUNTIME_FILE = Path(tempfile.gettempdir()) / (
    "strata-tui-" + _smash(str(ROOT))[:10] + ".json"
)
LOG_FILE = ROOT / "strata-tui.log"
DEFAULT_CONTEXT = 65536


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import setup as strata_setup
except Exception as e:
    print("Cannot import Strata setup.py.")
    print(f"Put STRATA-TUI.py in the Strata repository root.\n\n{e}")
    raise SystemExit(1)

try:
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    from gguf_reader import GGUFFile
except Exception as e:
    print("Cannot import Strata tools/gguf_reader.py.")
    print(f"This TUI needs the current Strata repository's own GGUF reader.\n\n{e}")
    raise SystemExit(1)

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
RED = "\x1b[91m"
CYAN = "\x1b[96m"


# ---------------------------------------------------------------------------
# terminal - a static, key-driven screen.  No live dashboard, no frame caches.

CLEAR_NEXT = True


def enable_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def full_clear():
    """One explicit full clear: entering a screen, not between live frames."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def request_clear():
    """The next paint() starts with a full clear (view switch / modal prompt)."""
    global CLEAR_NEXT
    CLEAR_NEXT = True


def paint(text: str):
    """Paint one complete screen.  Clears the terminal once on a view switch,
    afterwards just moves home, writes the frame and erases leftover rows."""
    global CLEAR_NEXT
    prefix = "\x1b[2J\x1b[H" if CLEAR_NEXT else "\x1b[H"
    sys.stdout.write(prefix + text + "\x1b[J")
    sys.stdout.flush()
    CLEAR_NEXT = False


def _decode_windows_key(ch: str) -> str:
    import msvcrt
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(ch2, "")
    if ch == "\r":
        return "ENTER"
    if ch == "\x1b":
        return "ESC"
    return ch.lower()


def read_key(timeout: float | None = None) -> str | None:
    """Read one key.  `timeout` is used only by the operation-log page while a
    process is active; the main menu always blocks instead of auto-refreshing."""
    if os.name == "nt":
        import msvcrt
        if timeout is None:
            return _decode_windows_key(msvcrt.getwch())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return _decode_windows_key(msvcrt.getwch())
            time.sleep(0.03)
        return None

    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if timeout is not None:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], 0.03)
            if ready:
                seq = sys.stdin.read(2)
                return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT", "[D": "LEFT"}.get(seq, "ESC")
            return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# files / status

def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_settings() -> dict:
    try:
        return read_json(SETTINGS_FILE)
    except Exception:
        return {}


def save_settings(data: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def default_model_root() -> Path:
    settings = load_settings()
    if settings.get("model_root"):
        return Path(settings["model_root"])
    return Path(r"E:\Model") if os.name == "nt" else Path("/mnt/Storage/Model")


def health(port: int, timeout=0.35) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if data.get("service") == "strata" else None
    except Exception:
        return None


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def cfg_port(cfg: dict) -> int:
    try:
        return int(cfg.get("port", 8080))
    except Exception:
        return 8080


def running_status(configs) -> tuple[int, dict] | None:
    ports = {8080}
    for item in configs:
        if item.kind == "config" and item.cfg:
            ports.add(cfg_port(item.cfg))
    for p in sorted(ports):
        h = health(p)
        if h:
            return p, h
    return None


def arg_value(args: list, key: str) -> str | None:
    if key in args:
        i = args.index(key)
        if i + 1 < len(args):
            return args[i + 1]
    return None


# ---------------------------------------------------------------------------
# READY.  An installed config only means READY when the artifacts the CURRENT
# Strata needs actually exist.  Every path below comes from the config itself
# and mirrors a check Strata performs:
#   - setup.py start(): cfg["exe"] and every *.gguf arg must exist
#   - serve/server.py:  the tokenizer directory's vocab.json (+ merges/token_type)
#   - setup.py step 6:  the pack's completion markers (index.txt+experts.bin, or
#                       native_experts.txt for a native pack) and its tokenizer
#   - the engine:       the --mtp draft runtime (dense.txt, dense.bin, experts.bin)
# Only file existence is tested - never the tens-of-GB payloads.

def config_ready(cfg: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    args = list(cfg.get("args") or [])

    exe = cfg.get("exe")
    if not exe:
        problems.append("the config has no \"exe\"")
    elif not Path(exe).is_file():
        problems.append(f"the engine is missing: {exe}")

    pack = arg_value(args, "--pack")
    if not pack:
        problems.append("the config has no --pack path")
    else:
        p = Path(pack)
        if not p.is_dir():
            problems.append(f"the prepared pack is missing: {pack}")
        else:
            q2_ok = (p / "index.txt").is_file() and (p / "experts.bin").is_file()
            native_ok = (p / "native_experts.txt").is_file()
            if not (q2_ok or native_ok):
                problems.append(f"the pack is incomplete: {pack} (no index.txt+experts.bin "
                                "and no native_experts.txt)")
            if not (p / "tokenizer" / "vocab.json").is_file():
                problems.append(f"the pack tokenizer is missing: {p / 'tokenizer' / 'vocab.json'}")

    tpath = cfg.get("tokenizer")
    if tpath:
        t = Path(tpath)
        for f in ("vocab.json", "merges.txt", "token_type.json"):
            if not (t / f).is_file():
                problems.append(f"the tokenizer file is missing: {t / f}")
    else:
        problems.append("the config has no tokenizer path")

    for shard in (a for a in args if str(a).lower().endswith(".gguf")):
        if not Path(shard).is_file():
            problems.append(f"a GGUF shard is missing: {shard}")

    mtp = arg_value(args, "--mtp")
    if mtp:
        m = Path(mtp)
        if not m.is_dir():
            problems.append(f"the MTP draft layer is missing: {mtp}")
        else:
            for f in ("dense.txt", "dense.bin", "experts.bin"):
                if not (m / f).is_file():
                    problems.append(f"a MTP draft file is missing: {m / f}")

    return not problems, problems


def config_context(cfg: dict) -> int | None:
    ctx = arg_value(list(cfg.get("args") or []), "--max-context")
    try:
        return int(ctx) if ctx else None
    except ValueError:
        return None


def config_source_gguf(cfg: dict) -> Path | None:
    """The raw GGUF shards' folder when the source still exists (any *.gguf arg
    present), else None.  The config itself records where the model came from."""
    for shard in (a for a in list(cfg.get("args") or []) if str(a).lower().endswith(".gguf")):
        p = Path(shard)
        if p.parent.is_dir():
            return p.parent
    return None


# ---------------------------------------------------------------------------
# V4 GGUF verification - kept exactly as the working version.  Strata's own
# header-only reader parses metadata + tensor directory; tensor payloads are
# never read during discovery.

# The exact architecture guard compiled into Strata's engine
# (include/strata/artifact/gguf_reader.hpp::check_architecture).
ARCH_REQUIRED = {
    "general.architecture": "qwen4exp",
    "qwen4exp.block_count": 48,
    "qwen4exp.embedding_length": 2560,
    "qwen4exp.attention.head_count": 24,
    "qwen4exp.attention.head_count_kv": 2,
}
ARCH_PRESENCE_ONLY = (
    "qwen4exp.expert_count",
    "qwen4exp.expert_used_count",
)


@dataclass
class HeaderCheck:
    ok: bool
    model: str | None
    architecture: str | None
    expert_count: int | None
    expert_used_count: int | None
    tensor_count: int
    expert_types: dict[str, int]
    reason: str = ""


def _metadata_int(md: dict, key: str) -> int | None:
    v = md.get(key)
    if isinstance(v, bool):
        return None
    return int(v) if isinstance(v, (int, float)) else None


def validate_gguf_headers(folder: Path, family: str, filename_model: str | None) -> HeaderCheck:
    """Validate a candidate using Strata's own GGUF header reader.

    This never scans tensor payload bytes.  It checks:
      - GGUF v3 parseability
      - Strata's exact qwen4exp architecture contract
      - expert metadata presence
      - shard split metadata consistency
      - every shard is long enough for its tensor directory
      - expert tensor quantization, when it directly maps to a Strata model size
    """
    fam = strata_setup.FAMILIES[family]
    model_for_paths = filename_model or "Q2_0"
    shards = strata_setup.gguf_dir_shards(folder, fam, model_for_paths)

    try:
        parsed = [GGUFFile(p) for p in shards]
    except Exception as e:
        return HeaderCheck(False, None, None, None, None, 0, {}, f"GGUF header: {e}")

    if not parsed:
        return HeaderCheck(False, None, None, None, None, 0, {}, "No GGUF shards found")

    first = parsed[0]
    md = first.metadata
    arch = md.get("general.architecture")
    total_tensors = sum(len(g.tensors) for g in parsed)

    for key, want in ARCH_REQUIRED.items():
        got = md.get(key)
        if got is None:
            return HeaderCheck(False, None, str(arch) if arch else None, None, None,
                               total_tensors, {}, f"Missing metadata: {key}")
        if key == "general.architecture":
            if got != want:
                return HeaderCheck(False, None, str(got), None, None,
                                   total_tensors, {},
                                   f"Architecture is {got!r}; Strata requires {want!r}")
        else:
            try:
                if int(got) != want:
                    return HeaderCheck(False, None, str(arch), None, None,
                                       total_tensors, {},
                                       f"{key} = {got}; Strata requires {want}")
            except Exception:
                return HeaderCheck(False, None, str(arch), None, None,
                                   total_tensors, {}, f"{key} is not numeric")

    for key in ARCH_PRESENCE_ONLY:
        if key not in md:
            return HeaderCheck(False, None, str(arch), None, None,
                               total_tensors, {}, f"Missing metadata: {key}")

    expert_count = _metadata_int(md, "qwen4exp.expert_count")
    expert_used = _metadata_int(md, "qwen4exp.expert_used_count")

    n = len(parsed)
    total_declared = md.get("split.tensors.count")
    if n == 1:
        if int(md.get("split.count", 1)) > 1:
            return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                               len(first.tensors), {},
                               f"GGUF says split.count={md.get('split.count')} but only one shard was resolved")
    else:
        for i, g in enumerate(parsed):
            gmd = g.metadata
            if gmd.get("split.count") != n or gmd.get("split.no") != i:
                return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                                   total_tensors, {},
                                   f"Shard {i + 1}/{n} has inconsistent split.count / split.no")
            if total_declared is not None and gmd.get("split.tensors.count") != total_declared:
                return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                                   total_tensors, {},
                                   f"Shard {i + 1}/{n} has inconsistent split.tensors.count")
        if total_declared is not None and total_tensors != int(total_declared):
            return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                               total_tensors, {},
                               f"Shards contain {total_tensors} tensors; metadata declares {total_declared}")

    for p, g in zip(shards, parsed):
        try:
            need = g.data_start + max(
                (t.offset + (t.expected_bytes() or 0) for t in g.tensors),
                default=0,
            )
            have = p.stat().st_size
        except Exception as e:
            return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                               total_tensors, {}, f"Shard size check failed: {e}")
        if have < need:
            return HeaderCheck(False, None, str(arch), expert_count, expert_used,
                               total_tensors, {},
                               f"{p.name} is truncated: {have:,} bytes, needs at least {need:,}")

    # Routed expert encodings actually present, and the ones that are also
    # current setup.py model keys (the strongest evidence short of a filename).
    expert_types = Counter()
    for g in parsed:
        for t in g.tensors:
            if t.name.startswith("blk.") and t.name.endswith("_exps.weight"):
                expert_types[t.type_name] += 1

    detected_model = filename_model
    recognized = Counter({
        typ: count for typ, count in expert_types.items()
        if typ in strata_setup.MODELS
    })
    if detected_model is None and recognized:
        dominant_type, _ = recognized.most_common(1)[0]
        detected_model = dominant_type

    if not expert_types:
        return HeaderCheck(
            False, detected_model, str(arch), expert_count, expert_used,
            total_tensors, {},
            "Architecture metadata matches, but no routed *_exps.weight tensors were found",
        )

    return HeaderCheck(
        True,
        detected_model or filename_model,
        str(arch),
        expert_count,
        expert_used,
        total_tensors,
        dict(expert_types),
        "",
    )


# ---------------------------------------------------------------------------
# model discovery

@dataclass
class Item:
    kind: str                  # config | gguf
    title: str
    status: str                # READY | PREPARE | UNSUPPORTED | BROKEN
    path: Path
    cfg: dict | None = None
    family: str | None = None
    model: str | None = None
    variant: str | None = None
    context: int | None = None
    reason: str = ""
    first_gguf: Path | None = None
    prepare_dir: Path | None = None   # the folder to hand setup.py as --gguf-dir


def config_identity(path: Path) -> tuple[str | None, str | None, str | None, int | None]:
    try:
        c = strata_setup.choices_from_config(path)
        return c.get("family"), c.get("model"), c.get("variant"), c.get("context")
    except Exception:
        return None, None, None, None


def family_title(family: str | None) -> str:
    if family and family in strata_setup.FAMILIES:
        return strata_setup.FAMILIES[family].get("title", family)
    return family or "Unknown"


def config_title(cfg: dict, family, model, variant, context) -> str:
    title = f"{family_title(family)} {model}"
    if variant:
        title += f" · {variant}"
    if context:
        title += f"  ·  {context // 1024}K"
    return title


def installed_configs(conf_root: Path | None = None) -> list[Item]:
    """Installed strata-*.json configs with a REAL READY check.  A config whose
    prepared artifacts are gone is PREPARE when the source GGUF still exists
    (Enter re-runs the current setup.py) and BROKEN when it does not."""
    items = []
    conf_root = conf_root or ROOT
    for p in sorted(conf_root.glob("strata-*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            cfg = read_json(p)
            family, model, variant, context = config_identity(p)
            if not family or not model:
                items.append(Item("config", cfg.get("model_name") or p.stem, "BROKEN", p,
                                  cfg=cfg, reason=f"Config did not map to a supported setup.py model"))
                continue
            context = context or config_context(cfg)
            title = config_title(cfg, family, model, variant, context)

            ready, problems = config_ready(cfg)
            if ready:
                items.append(Item("config", title, "READY", p, cfg=cfg,
                                  family=family, model=model, variant=variant, context=context,
                                  reason=f"{p.name} · port {cfg_port(cfg)}"))
                continue

            source = config_source_gguf(cfg)
            if source is not None:
                # One clear PREPARE row: re-running the CURRENT setup.py rebuilds
                # exactly what is missing, keeping the config's identity (variant!).
                items.append(Item("config", title, "PREPARE", p, cfg=cfg,
                                  family=family, model=model, variant=variant, context=context,
                                  prepare_dir=source,
                                  reason="Prepared pack is incomplete; the source GGUF still exists - "
                                         "Enter re-runs the current Strata setup. " + "; ".join(problems)))
            else:
                items.append(Item("config", title, "BROKEN", p, cfg=cfg,
                                  family=family, model=model, variant=variant, context=context,
                                  reason="Prepared pack is incomplete; source GGUF not found. " + "; ".join(problems)))
        except Exception as e:
            items.append(Item("config", p.name, "BROKEN", p, reason=str(e)))
    return items


def quant_from_name(name: str) -> str | None:
    m = strata_setup.GGUF_QUANT.search(name)
    return m.group(1).upper() if m else None


def infer_family(folder: Path, first: Path, model: str) -> str | None:
    allowed = tuple(strata_setup.MODELS.get(model, {}).get("families", ("qwen", "swift")))
    hay = (folder.name + " " + first.name).lower()

    if len(allowed) == 1:
        return allowed[0]
    if "swift" in hay and "swift" in allowed:
        return "swift"
    if "coder" in hay and "coder" in allowed:
        return "coder"
    if "unsloth" in hay and "unsloth" in allowed:
        return "unsloth"
    if model == "UD-Q4_K_XL" and "unsloth" in allowed:
        return "unsloth"
    if model == "IQ1_M" and "coder" in allowed:
        return "coder"
    if "qwen" in allowed:
        return "qwen"
    return allowed[0] if allowed else None


def variant_hint(folder: Path, first: Path, family: str, model: str) -> str | None:
    fam = strata_setup.FAMILIES[family]
    try:
        canonical = fam["file"].format(q=model, i=1)
    except Exception:
        canonical = ""
    if first.name == canonical:
        return None

    low = (folder.name + " " + first.name).lower()
    for known in ("abliterated", "uncensored"):
        if known in low:
            return known

    simple = re.sub(r"[^a-z0-9_-]+", "-", folder.name.lower()).strip("-_")
    for token in (model.lower(), model.lower().replace("_", "-")):
        simple = simple.replace(token, "").strip("-_")
    return simple[:40] if simple else "custom"


AUX_MODEL_DIRS = {
    "mtp", "packs", "pack", "tokenizer", "tokenizers", "cache", "caches",
    "draft", "drafts", "engine", "engines", "mmproj", "vision", "tmp", "temp"
}


def top_level_model_folders(root: Path) -> list[Path]:
    if not root.exists():
        return []
    folders = [root]
    try:
        folders.extend(sorted(
            (p for p in root.iterdir()
             if p.is_dir()
             and p.name.lower() not in AUX_MODEL_DIRS
             and not p.name.startswith(".")),
            key=lambda p: p.name.lower()
        ))
    except OSError:
        pass
    return folders


def gguf_first_files(folder: Path) -> list[Path]:
    try:
        files = []
        for p in folder.glob("*.gguf"):
            low = p.name.lower()
            if "mmproj" in low:
                continue
            m = strata_setup.SHARD_NAME.search(p.name)
            if not m or m.group(1) == "00001":
                files.append(p)
        return sorted(files)
    except OSError:
        return []


def gguf_items(root: Path, covered_keys: set[tuple]) -> tuple[list[Item], list[Item]]:
    """Raw GGUF discovery with the V4 verification.  A raw GGUF is hidden only
    for an identity that is HEALTHY (READY) or already repairable (PREPARE):
    a BROKEN config must not suppress its own recovery candidate."""
    supported = []
    unsupported = []

    for folder in top_level_model_folders(root):
        for first in gguf_first_files(folder):
            filename_model = quant_from_name(first.name)
            family_guess_model = filename_model if filename_model in strata_setup.MODELS else "Q2_0"
            family = infer_family(folder, first, family_guess_model)
            if not family or family not in strata_setup.FAMILIES:
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason="Cannot identify a supported Strata family",
                    first_gguf=first, model=filename_model
                ))
                continue

            check = validate_gguf_headers(folder, family, filename_model)
            if not check.ok:
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason=check.reason,
                    first_gguf=first, model=check.model or filename_model, family=family
                ))
                continue

            model = check.model
            if not model or model not in strata_setup.MODELS:
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason=f"Header is valid qwen4exp, but quant/model {model or '?'} is not supported "
                           "by this Strata setup.py",
                    first_gguf=first, model=model, family=family
                ))
                continue

            allowed = tuple(strata_setup.MODELS[model].get("families", ("qwen", "swift")))
            if family not in allowed:
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason=f"Header is compatible, but {family} / {model} is not a supported setup.py combination",
                    first_gguf=first, family=family, model=model
                ))
                continue

            var = variant_hint(folder, first, family, model)
            if (family, model, var) in covered_keys:
                continue

            title = f"{family_title(family)} {model}"
            if var:
                title += f" · {var}"

            expert_summary = ", ".join(f"{k}:{v}" for k, v in sorted(check.expert_types.items()))
            reason = (
                f"GGUF verified: arch={check.architecture}, tensors={check.tensor_count}, "
                f"experts={check.expert_count}, expert types={expert_summary}"
            )

            supported.append(Item(
                "gguf", title, "PREPARE", folder,
                family=family, model=model, variant=var,
                first_gguf=first, reason=reason
            ))

    return supported, unsupported


def all_items(model_root: Path, show_unsupported: bool, conf_root: Path | None = None) -> tuple[list[Item], int]:
    configs = installed_configs(conf_root)
    covered = {
        (x.family, x.model, x.variant)
        for x in configs
        if x.status in ("READY", "PREPARE") and x.family and x.model
    }
    supported, unsupported = gguf_items(model_root, covered)
    items = configs + supported
    if show_unsupported:
        items += unsupported
    return items, len(unsupported)


# ---------------------------------------------------------------------------
# prepare / run

def normal_python() -> str:
    p = Path(sys.executable)
    if os.name == "nt" and p.name.lower() == "pythonw.exe":
        q = p.with_name("python.exe")
        if q.exists():
            return str(q)
    return str(p)


def prepare_command(item: Item) -> list[str]:
    """The CURRENT Strata setup.py's own prepare workflow, not a reimplementation."""
    gguf_dir = item.prepare_dir or item.path
    ctx = item.context or DEFAULT_CONTEXT
    contexts = list(getattr(strata_setup, "CONTEXTS", [DEFAULT_CONTEXT]))
    if 65536 in contexts:
        ctx = 65536
    elif ctx not in contexts and contexts:
        ctx = contexts[0]

    cmd = [
        normal_python(), str(ROOT / "setup.py"),
        "--family", item.family,
        "--model", item.model,
        "--gguf-dir", str(gguf_dir),
        "--context", str(ctx),
        "--no-start",
        "--yes",
    ]
    if item.variant:
        cmd += ["--variant", item.variant]
    return cmd


def prepare(item: Item) -> subprocess.Popen | None:
    """Start setup.py for this model.  Reuses the stored family/model/variant/
    context; the logs page shows the real command and output."""
    if item.status != "PREPARE" or not (item.family and item.model):
        return None

    cmd = prepare_command(item)

    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log.write(f"\n\n--- PREPARE {time.strftime('%Y-%m-%d %H:%M:%S')} {item.title} ---\n")
    log.write(" ".join(f'"{x}"' if " " in x else x for x in cmd) + "\n\n")
    log.flush()
    try:
        process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    except Exception as e:
        log.write(f"Prepare failed to start: {e}\n")
        log.close()
        return None
    log.close()
    return process


def runtime_load() -> dict | None:
    try:
        return read_json(RUNTIME_FILE)
    except Exception:
        return None


def runtime_save(data: dict):
    try:
        RUNTIME_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def runtime_clear():
    try:
        RUNTIME_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def append_log(message: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(message.rstrip() + "\n")
    except OSError:
        pass


def start_item(item: Item) -> tuple[subprocess.Popen, int] | None:
    if item.kind != "config" or item.status != "READY" or not item.cfg:
        append_log("This model is not ready. Prepare it first.")
        return None

    port = cfg_port(item.cfg)
    h = health(port)
    if h:
        append_log(f"Strata is already running on {port}: {h.get('model', 'unknown')}")
        return None
    if port_open(port):
        append_log(f"Port {port} is occupied by another program. Nothing was changed.")
        return None

    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log.write(f"\n\n--- START {time.strftime('%Y-%m-%d %H:%M:%S')} {item.path.name} ---\n")
    log.flush()

    cmd = [
        normal_python(),
        str(ROOT / "serve" / "server.py"),
        "--engine", "strata",
        "--config", str(item.path),
        "--port", str(port),
    ]

    kwargs = dict(cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)

    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        p = subprocess.Popen(cmd, **kwargs)
    except Exception as e:
        log.close()
        append_log(f"Start failed: {e}")
        return None
    finally:
        try:
            log.close()
        except Exception:
            pass

    runtime_save({
        "pid": p.pid,
        "port": port,
        "config": str(item.path),
        "model_name": item.cfg.get("model_name") or item.path.stem,
    })

    append_log(f"Starting {item.cfg.get('model_name', item.path.stem)} on :{port}")
    return p, port


def stop_owned():
    rt = runtime_load()
    if not rt:
        append_log("Nothing stopped: current Strata was not started by this TUI.")
        return

    pid = int(rt.get("pid", 0) or 0)
    port = int(rt.get("port", 8080) or 8080)
    expected = rt.get("model_name")
    h = health(port)

    if h and expected and h.get("model") not in (expected, None):
        append_log(f"Safety stop: :{port} is now {h.get('model')}; left it running.")
        runtime_clear()
        return

    if not h and not port_open(port):
        runtime_clear()
        append_log("Recorded Strata was already stopped.")
        return

    append_log(f"Stopping {expected or 'Strata'}…")
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=12, creationflags=flags
            )
            time.sleep(1.5)
            if health(port):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=12, creationflags=flags
                )
        else:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(1.5)
            if health(port):
                os.killpg(pid, signal.SIGKILL)

        runtime_clear()
        append_log(f"Stopped {expected or 'Strata'}.")
    except Exception as e:
        append_log(f"Stop failed: {e}")


def stop_in_background() -> threading.Thread:
    append_log(f"\n--- STOP REQUEST {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def worker():
        stop_owned()

    thread = threading.Thread(target=worker, name="strata-stop", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# operation log page - the automatic destination of Prepare / Start / Stop.
# It clears ONCE on entry (no main-menu ghosting), then appends new output
# while a process is active and stops updating once the operation finishes.

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class LogView:
    """Tails several files at once, remembering each file's position, so the
    page only repaints when there actually is new output."""

    def __init__(self, sources):
        self.sources = [Path(s) for s in sources]
        self.pos = {}
        for s in self.sources:
            try:
                self.pos[s] = s.stat().st_size
            except OSError:
                self.pos[s] = 0
        self.lines: deque[str] = deque(maxlen=600)

    def read(self) -> bool:
        """Append any new output; True when something changed."""
        new = False
        for s in self.sources:
            try:
                size = s.stat().st_size
            except OSError:
                continue
            start = self.pos.get(s, 0)
            if size < start:
                start = 0                      # the file was replaced or truncated
            if size <= start:
                continue
            try:
                with open(s, "rb") as f:
                    f.seek(start)
                    raw = f.read(size - start).decode("utf-8", errors="replace")
            except OSError:
                continue
            self.pos[s] = size
            for line in raw.splitlines():
                line = ANSI_RE.sub("", line).rstrip()
                if line:
                    self.lines.append(line)
            new = True
        return new


def clip_line(s: str, width: int) -> str:
    if width <= 8:
        return s[:width]
    return s if len(s) <= width else s[:width - 1] + "…"


def log_frame(status: str, lines) -> str:
    width = max(60, min(150, shutil.get_terminal_size((120, 40)).columns - 2))
    visible = list(lines)[-max(20, shutil.get_terminal_size((120, 40)).lines - 9):]
    body = [BOLD + "STRATA LOG" + RESET, "", CYAN + status + RESET, ""]
    if visible:
        body.extend(clip_line(line, width) for line in visible)
    else:
        body.append(DIM + "(no output yet)" + RESET)
    body += ["", DIM + "─" * min(88, width) + RESET,
             "", "B / Esc  Back", "R        Refresh"]
    return "\n".join(body) + "\n"


def log_page(status: str, sources=(LOG_FILE,), active=None, finished=None,
             record_finished: bool = True):
    """Operation output view.  `active` is a callable that is True while the
    operation runs; `finished` produces the final status line.  Auto-updates
    stop the moment the operation finishes; B / Esc returns to the menu."""
    request_clear()
    view = LogView(sources)
    last_status = None
    last_lines = None
    force = True

    while True:
        if active is not None and not active():
            end = finished() if callable(finished) else finished
            if end is None:
                end = status
            if record_finished and end:
                append_log(end)
            status = end
            active = None

        changed = view.read()
        animate = active is not None and (changed or force)
        if animate or status != last_status:
            last_status = status
            last_lines = list(view.lines)
            paint(log_frame(status, last_lines))
            force = False

        key = read_key(0.5 if active is not None else None)
        if key in ("b", "esc"):
            request_clear()                     # the menu clears once on return
            return
        if key == "r":
            force = True
            last_status = None


# ---------------------------------------------------------------------------
# UI - main menu.  Rendered once, redrawn only after a user action.

def status_color(status: str) -> str:
    return {
        "READY": GREEN,
        "PREPARE": YELLOW,
        "UNSUPPORTED": RED,
        "BROKEN": RED,
    }.get(status, "")


def render_menu(items: list[Item], pos: int, model_root: Path, unsupported_count: int,
                show_unsupported: bool, notice: str):
    running = running_status([x for x in items if x.kind == "config"])
    lines: list[str] = []

    lines.append(BOLD + "STRATA" + RESET)
    if running:
        port, h = running
        lines.append(GREEN + "● Running" + RESET + f"  {h.get('model', 'unknown')}  :{port}")
    else:
        lines.append(DIM + "○ Stopped" + RESET)

    if notice:
        lines.append(CYAN + "  " + notice + RESET)

    lines.append("")
    lines.append(DIM + f"Model folder: {model_root}" + RESET)
    lines.append("")

    if not items:
        lines.append("  No installed Strata configs or supported GGUF models found.")
    else:
        for i, item in enumerate(items):
            mark = ">" if i == pos else " "
            col = status_color(item.status)
            source = "CONFIG" if item.kind == "config" else "GGUF"
            lines.append(
                f"{BOLD if i == pos else ''}{mark} "
                f"{item.title[:64]:64} "
                f"{col}{item.status:11}{RESET} "
                f"{DIM}{source}{RESET}"
            )

    lines.append("")
    if items:
        cur = items[pos]
        if cur.reason:
            lines.append(DIM + "  " + cur.reason[:135] + RESET)
        else:
            lines.append(DIM + f"  {cur.path}" + RESET)

    if unsupported_count:
        state = "shown" if show_unsupported else "hidden"
        lines.append(DIM + f"  {unsupported_count} unsupported GGUF item(s) {state} · press U to toggle" + RESET)

    lines.append("")
    lines.append("↑/↓ Select   Enter Start/Prepare   R Refresh   X Stop   C Chat   M Monitor   F Folder   U Unsupported   Q Quit")
    paint("\n".join(lines) + "\n")


def choose_model_root(current: Path) -> Path:
    full_clear()
    print(BOLD + "MODEL FOLDER" + RESET)
    print()
    raw = input(f"Path [{current}]: ").strip().strip('"')
    request_clear()
    if not raw:
        return current
    p = Path(raw)
    if not p.exists():
        print()
        print(RED + "That folder does not exist." + RESET)
        print(DIM + "Press any key to return…" + RESET)
        read_key()
        request_clear()
        return current
    settings = load_settings()
    settings["model_root"] = str(p)
    save_settings(settings)
    return p


def open_strata(fragment: str, items: list[Item]):
    run = running_status([x for x in items if x.kind == "config"])
    port = run[0] if run else 8080
    webbrowser.open(f"http://127.0.0.1:{port}/{fragment}")


def main():
    enable_ansi()
    model_root = default_model_root()
    show_unsupported = False
    items, unsupported_count = all_items(model_root, show_unsupported)
    pos = 0
    notice = ""

    while True:
        pos = max(0, min(pos, max(0, len(items) - 1)))
        render_menu(items, pos, model_root, unsupported_count, show_unsupported, notice)
        notice = ""
        key = read_key()                          # blocking: the menu never redraws on its own

        if key == "UP" and items:
            pos = (pos - 1) % len(items)
        elif key == "DOWN" and items:
            pos = (pos + 1) % len(items)
        elif key == "ENTER" and items:
            item = items[pos]
            request_clear()
            if item.kind == "config" and item.status == "READY":
                started = start_item(item)
                if started:
                    process, port = started
                    cfg = item.cfg or {}
                    engine_log = Path(cfg["log"]) if cfg.get("log") else None
                    sources = [LOG_FILE] + ([engine_log] if engine_log else [])
                    log_page(
                        f"Starting {item.title}…",
                        sources=sources,
                        active=lambda: process.poll() is None and health(port) is None,
                        finished=lambda: (
                            f"Ready: http://127.0.0.1:{port}/v1"
                            if health(port) else
                            f"Start process exited before becoming ready (exit {process.poll()})."
                        ),
                    )
                else:
                    log_page("Start did not begin.", sources=[LOG_FILE])
            elif item.status == "PREPARE":
                process = prepare(item)
                if process:
                    log_page(
                        f"Preparing {item.title}…",
                        sources=[LOG_FILE],
                        active=lambda: process.poll() is None,
                        finished=lambda: (
                            "Prepare finished."
                            if process.returncode == 0 else
                            f"Prepare failed (exit {process.returncode})."
                        ),
                    )
                else:
                    log_page("Prepare failed to start.", sources=[LOG_FILE])
            else:
                log_page(item.reason or f"{item.status}: no action available.", sources=[])
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key == "r":
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key == "x":
            request_clear()
            stop_thread = stop_in_background()
            log_page(
                "Stopping…",
                sources=[LOG_FILE],
                active=stop_thread.is_alive,
                finished=lambda: "Stopped.",
                record_finished=False,
            )
            items, unsupported_count = all_items(model_root, show_unsupported)
        elif key == "c":
            open_strata("#chat", items)
        elif key == "m":
            open_strata("#monitor", items)
        elif key == "f":
            model_root = choose_model_root(model_root)
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key == "u":
            show_unsupported = not show_unsupported
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key in ("q", "esc"):
            full_clear()
            return


if __name__ == "__main__":
    main()