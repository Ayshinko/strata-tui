"""STRATA-TUI - a minimal terminal launcher for Strata.

"A small interface, not another layer."

A compact, keyboard-driven terminal interface for an installed Strata checkout:

  model  ->  prepare  ->  start  ->  logs

It does not build a second management stack.  There is no second server, no
reverse proxy, no daemon, and no third-party Python dependency (the main menu's
one live line is polled from Strata's own /metrics and engine log); the launcher draws the screen with plain
ANSI sequences and reuses the official Strata checkout's own logic.

Strata remains the source of truth.  Model support comes from the checkout's
setup.py (MODELS / FAMILIES / GGUF_QUANT / gguf_dir_shards), GGUF verification
comes from its tools/gguf_reader.py, and preparation is a subprocess call to its
setup.py.  If upstream Strata adds or removes a supported quant or family, this
launcher follows the installed checkout rather than a handwritten whitelist.

Your own launch profiles (profiles.py) are the exception on purpose: they live outside the Strata folder,
so an update or a setup run cannot rewrite the engine options you verified; Strata's server still runs them.

STRATA-TUI is a standalone companion to Strata. It reads the installed Strata
capabilities and configuration, and never patches official source files. Custom
variant identity and load helpers live in companion modules in this checkout.
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
TUI_DIR = _HERE.parent                 # this checkout and its local configuration/helpers


def _smash(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


if str(TUI_DIR) not in sys.path:
    sys.path.insert(0, str(TUI_DIR))
import manager_config as _mcfg        # noqa: E402
import manager_identity               # noqa: E402
import vram as _vram                  # noqa: E402  (LOAD MODEL VRAM helpers)

ROOT = _mcfg.strata_root()            # the OFFICIAL Strata checkout this launcher drives

SETTINGS_FILE = Path.home() / ".strata-tui.json"
RUNTIME_FILE = Path(tempfile.gettempdir()) / (
    "strata-tui-" + _smash(str(ROOT))[:10] + ".json"
)
LOG_FILE = TUI_DIR / "logs" / "strata-tui.log"
DEFAULT_CONTEXT = 65536
DEFAULT_KV_VRAM = 32768
DEFAULT_KV_RAM = 32768

try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import setup as strata_setup
except Exception as e:
    print("Cannot import Strata setup.py (the official checkout at STRATA_ROOT).")
    print(f"STRATA-TUI checkout: {TUI_DIR}; set STRATA_ROOT in {TUI_DIR / 'manager.env'}.")
    print()
    print(e)
    raise SystemExit(1)

try:
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    from gguf_reader import GGUFFile
except Exception as e:
    print("Cannot import Strata tools/gguf_reader.py (the official checkout).")
    print(f"This TUI needs the official Strata repository's own GGUF reader.\n\n{e}")
    raise SystemExit(1)

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
RED = "\x1b[91m"
CYAN = "\x1b[96m"
GRAY = "\x1b[90m"
SPINNER = "|/-\\"          # ASCII spinner: renders everywhere
SPINNER_SECONDS = 0.25   # status-bar animation while an operation runs


# ---------------------------------------------------------------------------
# terminal - a key-driven screen; the main menu repaints only when its text changes.

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
    # Clear every rendered row before advancing; CSI J only clears below the cursor.
    # This prevents stale suffixes from longer prior frames surviving in-line.
    frame = "\n".join(line + "\x1b[K" for line in text.splitlines())
    if text.endswith("\n"):
        frame += "\n"
    sys.stdout.write(prefix + frame + "\x1b[J")
    sys.stdout.flush()
    CLEAR_NEXT = False


def _decode_windows_key(ch: str) -> str:
    import msvcrt
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "S": "DEL"}.get(ch2, "")
    if ch == "\r":
        return "ENTER"
    if ch == "\x1b":
        return "ESC"
    return ch.lower()


def read_key(timeout: float | None = None) -> str | None:
    """Read one key, or None after `timeout` seconds (None: wait).  The main menu waits one second at a time so its
    live line can refresh; the operation-log page uses it while a process is active."""
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
                if seq == "[3":                      # Delete: ESC [ 3 ~
                    sys.stdin.read(1)
                    return "DEL"
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


def _kv_allocation_key(item) -> str:
    """Stable per-config key in the existing per-user TUI settings file."""
    try:
        return str(Path(item.path).expanduser().resolve()).casefold()
    except (OSError, TypeError, ValueError):
        return str(item.path).casefold()


def load_kv_allocation(item) -> dict | None:
    allocations = load_settings().get("kv_allocations")
    if not isinstance(allocations, dict):
        return None
    value = allocations.get(_kv_allocation_key(item))
    if not isinstance(value, dict):
        return None
    vram, ram = value.get("kv_vram"), value.get("kv_ram")
    if (isinstance(vram, int) and not isinstance(vram, bool) and vram >= 0 and
            isinstance(ram, int) and not isinstance(ram, bool) and ram >= 0):
        return {"kv_vram": vram, "kv_ram": ram,
                "context": vram + ram}
    return None


def save_kv_allocation(item, kv_vram: int, kv_ram: int) -> None:
    """Persist allocations on explicit Load, alongside existing user settings."""
    settings = load_settings()
    allocations = settings.get("kv_allocations")
    if not isinstance(allocations, dict):
        allocations = {}
    allocations[_kv_allocation_key(item)] = {
        "kv_vram": kv_vram, "kv_ram": kv_ram,
        "context": kv_vram + kv_ram,
    }
    settings["kv_allocations"] = allocations
    save_settings(settings)


def initial_kv_allocation(item) -> dict:
    """Use this model's saved choice, or the product's ordinary 32K + 32K default."""
    saved = load_kv_allocation(item)
    if saved is not None:
        return saved
    return {"kv_vram": DEFAULT_KV_VRAM, "kv_ram": DEFAULT_KV_RAM,
            "context": DEFAULT_KV_VRAM + DEFAULT_KV_RAM}


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

def config_ready(cfg: dict, config_path: Path | None = None) -> tuple[bool, list[str]]:
    if cfg.get("arch") in {d.get("arch") for d in official_families().values() if d.get("arch")}:
        if config_path is None:
            return False, ["Own-engine readiness requires its saved config path"]
        try:
            from strata_control import doctor
            report = doctor.inspect(config_path)
            problems = [f"{p['name']}: {p.get('path') or p.get('reason', 'missing')}"
                        for p in report['components']
                        if p.get('required') and p['status'] in ('missing', 'invalid')]
            if cfg.get("arch") == "deepseek41" and not problems:
                args = list(cfg.get("args") or [])
                raw_pack = arg_value(args, "--pack")
                if not raw_pack:
                    problems.append("DeepSeek config has no --pack path")
                else:
                    exe = doctor._location(cfg['exe'], cfg, config_path)
                    pack = doctor._location(raw_pack, cfg, config_path)
                    if exe.name.lower() not in ('strata-dsv41.exe', 'strata-dsv41'):
                        problems.append("DeepSeek requires the separate strata-dsv41 engine")
                    elif not (pack / 'tensors.txt').is_file() or not (pack / 'ds4.txt').is_file():
                        problems.append(f"DeepSeek pack is incomplete: {pack} (ds4.txt / tensors.txt missing)")
                    else:
                        good, why = strata_setup.dsv41_pack_info(exe, pack, cfg.get('lib_dirs'))
                        if not good:
                            problems.append("DeepSeek pack check: " + why.strip()[-600:])
            return not problems, problems
        except Exception as exc:
            return False, [f"Official component check: {exc}"]
    problems: list[str] = []
    args = list(cfg.get("args") or [])

    exe = cfg.get("exe")
    if not exe:
        problems.append("the config has no \"exe\"")
    elif str(exe).startswith("component:"):
        try:
            from strata_control import engine_component
            resolved = engine_component.expand(exe, cfg, full=False)
            if not Path(resolved).is_file():
                problems.append(f"the engine is missing: {resolved}")
        except Exception as exc:
            problems.append(f"Official engine component check: {exc}")
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


def config_vision(cfg: dict) -> str:
    """The vision state recorded in a config, for the load-options page:
    Off when there is no vision section, GPU / CPU when the encoder is on
    (the encoder always runs on a device; "Auto" is stored as GPU)."""
    vis = cfg.get("vision")
    if isinstance(vis, dict):
        return "GPU" if vis.get("gpu") else "CPU"
    return "Off"


def config_source_gguf(cfg: dict) -> Path | None:
    """The raw GGUF shards' folder when the source still exists (any *.gguf arg
    present), else None.  The config itself records where the model came from."""
    for shard in (a for a in list(cfg.get("args") or []) if str(a).lower().endswith(".gguf")):
        p = Path(shard)
        if p.parent.is_dir():
            return p.parent
    if cfg.get('arch') == 'deepseek41':
        pack = arg_value(list(cfg.get('args') or []), '--pack')
        if pack:
            try:
                with (Path(pack) / 'ds4.txt').open(encoding='utf-8') as manifest:
                    for line in manifest.read(65536).splitlines():
                        if line.startswith('model_dir '):
                            source = Path(line[len('model_dir '):].strip())
                            if source.is_dir():
                                return source
            except OSError:
                pass
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


def validate_own_headers(folder: Path, family: str, model: str) -> HeaderCheck:
    """Published own-engine sets: official header readers + pinned sizes.

    This is compatibility discovery, not provenance attestation. Official setup
    still verifies SHA-256 and the backend's pack/smoke checks on Prepare.
    """
    definition = official_families()[family]
    details = official_models(family)[model]
    names = details.get("shards")
    if not isinstance(names, list):
        names = [definition["file"].format(q=model, i=1)]
    pick = getattr(strata_setup, "dsv41_pick", lambda d, n: d / n)
    shards = [pick(folder, n) for n in names]
    arch = definition.get("arch")
    try:
        from strata_control.gguf_preflight import inspect_metadata
        parsed = []
        summary = Counter()
        tensors = 0
        for i, path in enumerate(shards):
            if not path.is_file():
                raise ValueError(f"missing shard: {path.name}")
            pins = definition.get("sha256", {})
            pin = pins.get(path.name)
            if pin is None or path.stat().st_size != pin[0]:
                raise ValueError(f"{path.name}: size does not match the official pinned file")
            if arch == "deepseek41":
                # The generic preflight does not know DeepSeek's integer Engram
                # metadata or MXFP8. Use this backend's installed pack reader.
                from tools.dsv41 import pack_gguf as backend
                g = backend.Gguf(path)
                if g.version != 3:
                    raise ValueError("DeepSeek requires GGUF v3")
                problem = backend.short(g)
                if problem:
                    raise ValueError(problem)
                md = g.kv
                types = Counter()
                for name, typ, dims, offset in g.tensors:
                    if typ not in backend.TYPES and typ not in (26, 27):
                        raise ValueError(f"{path.name}: unsupported DeepSeek tensor type {typ}")
                    if "_exps" in name:
                        if typ not in backend.TYPES:
                            raise ValueError(f"{path.name}: unsupported expert encoding {typ}")
                        types[backend.TYPES[typ][0]] += 1
                report = {"architecture": md.get("general.architecture"),
                          "expert_tensor_types": dict(types), "tensor_count_scanned": len(g.tensors)}
            else:
                report = inspect_metadata(path)
                md = GGUFFile(path).metadata
            if i == 0 and report.get("architecture") != arch:
                raise ValueError(f"Architecture is {report.get('architecture')!r}; {family} requires {arch!r}")
            if report.get("architecture") not in (None, arch):
                raise ValueError(f"{path.name}: inconsistent architecture")
            if len(shards) > 1 and (md.get("split.no") != i or md.get("split.count") != len(shards)):
                raise ValueError(f"{path.name}: inconsistent split metadata")
            parsed.append(md)
            summary.update(report["expert_tensor_types"])
            tensors += report["tensor_count_scanned"]
        if not summary:
            raise ValueError("No routed expert tensors in the published model set")
        md = parsed[0]
        return HeaderCheck(True, model, arch, _metadata_int(md, arch + ".expert_count"),
                           _metadata_int(md, arch + ".expert_used_count"), tensors, dict(summary))
    except (Exception, SystemExit) as exc:
        return HeaderCheck(False, model, arch, None, None, 0, {}, str(exc))


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
    definition = official_families()[family]
    if "models" in definition:
        return validate_own_headers(folder, family, filename_model)
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
        if not g.tensors:
            # a metadata-only shard (Unsloth v3.1's first shard: 0 tensors) ends with its header; the aligned data start
            # past it is not file content, so there is nothing to check beyond the header that was just read
            continue
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
    kind: str                  # profile | config | gguf
    title: str
    status: str                # READY | PREPARE | UNSUPPORTED | BROKEN
    path: Path                 # (a profile: the run config the TUI writes for it in the state folder)
    cfg: dict | None = None
    family: str | None = None
    model: str | None = None
    variant: str | None = None
    context: int | None = None
    reason: str = ""
    first_gguf: Path | None = None
    prepare_dir: Path | None = None   # the folder to hand setup.py as --gguf-dir
    profile_path: Path | None = None  # your own profile file (kind "profile")
    profile: dict | None = None


def config_identity(path: Path) -> tuple[str | None, str | None, str | None, int | None]:
    """(family, model, variant, context) of a strata-*.json config: the official setup.py's own
    read-back, completed with manager-models.json for a custom build official setup cannot name
    (its stem ends in a variant label)."""
    try:
        c = manager_identity.reconcile(path.name, strata_setup.choices_from_config(path))
        if not c.get('family') or not c.get('model'):
            # Some installer versions uppercase a mixed-case catalog key (Gyro-S)
            # and fail their own lookup. Match only an exact official config tag.
            for family, definition in official_families().items():
                for model in official_models(family):
                    tag = 'strata-' + definition.get('tag', '') + model
                    if path.stem.casefold() == tag.casefold():
                        c['family'], c['model'] = family, model
        return c.get("family"), c.get("model"), c.get("variant"), c.get("context")
    except Exception:
        return None, None, None, None


def official_families() -> dict:
    """All installer routes, not only Flash-Next's packed GGUF families."""
    return {**getattr(strata_setup, "FAMILIES", {}),
            **getattr(strata_setup, "OWN_PATH_FAMILIES", {}),
            **getattr(strata_setup, "MULTI_FILE_FAMILIES", {})}


def official_models(family: str) -> dict:
    definition = official_families().get(family, {})
    if "models" in definition:
        return definition["models"]
    return {m: d for m, d in strata_setup.MODELS.items()
            if family in d.get("families", ("qwen", "swift"))}


def official_file_identity(first: Path) -> tuple[str, str] | None:
    """Only published base-model names; Engram/drafters are not model choices."""
    for family, definition in official_families().items():
        if definition.get("ready"):
            continue
        for model, details in official_models(family).items():
            shards = details.get("shards")
            if isinstance(shards, list):
                names = [shards[0]] if shards else []
                names += [getattr(strata_setup, "DSV41_LEGACY_NAMES", {}).get(n) for n in names[:]]
            elif "file" in definition:
                names = [definition["file"].format(q=model, i=1) if "models" in definition
                         else strata_setup.model_file(definition, model, 1)]
            else:
                names = []
            if first.name in names:
                return family, model
    return None


def family_title(family: str | None) -> str:
    return official_families().get(family, {}).get("title", family or "Unknown")


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

            ready, problems = config_ready(cfg, p)
            if ready:
                items.append(Item("config", title, "READY", p, cfg=cfg,
                                  family=family, model=model, variant=variant, context=context,
                                  reason=f"{p.name} · port {cfg_port(cfg)}"))
                continue

            source = config_source_gguf(cfg)
            if official_families().get(family, {}).get('ready'):
                repo_name = official_models(family)[model].get('repo', '').replace('/', '--')
                raw_pack = arg_value(list(cfg.get('args') or []), '--pack')
                if repo_name and raw_pack:
                    source = next((p.parent for p in Path(raw_pack).parents
                                   if p.name == repo_name and p.parent.is_dir()), source)
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


def setup_variants_supported() -> bool:
    """The custom --variant builds setup.py used to offer are gone from official Strata; the variant
    knowledge now lives entirely outside it (manager-models.json / manager_identity).  Kept as a named
    check so a NEW official setup.py that removes --variant never breaks this launcher: variants keep
    working from the external metadata instead."""
    return True


def quant_from_name(name: str) -> str | None:
    m = strata_setup.GGUF_QUANT.search(name)
    return m.group(1).upper() if m else None


def infer_family(folder: Path, first: Path, model: str) -> str | None:
    identity = official_file_identity(first)
    if identity:
        return identity[0]
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
    """A custom GGUF folder's own label, read from its name (external logic: the variant knowledge
    that used to require a patched setup.py).  None for the exact published name."""
    fam = strata_setup.FAMILIES[family]
    try:
        canonical = strata_setup.model_file(fam, model, 1)
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
            identity = official_file_identity(first)
            auxiliary_family = next((f for f, d in official_families().items()
                                     if any(first.name in (n, getattr(strata_setup, 'DSV41_LEGACY_NAMES', {}).get(n))
                                            for md in d.get('models', {}).values() for n in md.get('shared', []))), None)
            if auxiliary_family and not identity:
                unsupported.append(Item('auxiliary', first.name, 'AUXILIARY', folder,
                                        family=auxiliary_family, first_gguf=first,
                                        reason=f'Official shared artifact for {family_title(auxiliary_family)}; not a base model. '
                                               'Strata resolves it during preparation.'))
                continue
            filename_model = identity[1] if identity else quant_from_name(first.name)
            family_guess_model = filename_model if filename_model in strata_setup.MODELS else "Q2_0"
            family = identity[0] if identity else infer_family(folder, first, family_guess_model)
            if not family or family not in official_families():
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
            if not model or model not in official_models(family):
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason=f"Header is valid qwen4exp, but quant/model {model or '?'} is not supported "
                           "by this Strata setup.py",
                    first_gguf=first, model=model, family=family
                ))
                continue

            allowed = (family,) if "models" in official_families()[family] else tuple(
                strata_setup.MODELS[model].get("families", ("qwen", "swift")))
            if family not in allowed:
                unsupported.append(Item(
                    "gguf", first.name, "UNSUPPORTED", folder,
                    reason=f"Header is compatible, but {family} / {model} is not a supported setup.py combination",
                    first_gguf=first, family=family, model=model
                ))
                continue

            var = None if "models" in official_families()[family] else variant_hint(folder, first, family, model)
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

            if family in getattr(strata_setup, 'MULTI_FILE_FAMILIES', {}):
                reason = f'Native preparation required ({check.architecture}); SHA-256 verification and pack check pending.'
                if check.architecture == 'deepseek41':
                    from types import SimpleNamespace
                    args = SimpleNamespace(gguf_dir=str(folder), models_dir=str(ROOT / 'models'))
                    definition = official_families()[family]
                    shared_dir = strata_setup.dsv41_shared_dir(args, definition)
                    pending = []
                    if strata_setup.dsv41_exe(ROOT / 'engine') is None:
                        pending.append('separate DeepSeek engine')
                    for name in official_models(family)[model].get('shared', []):
                        artifact = strata_setup.dsv41_find_shared(args, name, folder, shared_dir)
                        if not artifact.is_file():
                            pending.append(name)
                    reason += (' Engram/DSpark shared inputs missing: ' + ', '.join(pending)
                               if pending else ' Engram/DSpark shared inputs present; official verification still required.')
                    if pending:
                        reason += '; official setup may download these prerequisites.'

            supported.append(Item(
                "gguf", title, "PREPARE", folder,
                family=family, model=model, variant=var,
                first_gguf=first, reason=reason
            ))

    return supported, unsupported


def ready_pack_items(root: Path, covered_keys: set[tuple]) -> list[Item]:
    """Catalog-defined non-GGUF inputs (NVFP4/QAD); no recursive weight scan."""
    items = []
    for family, definition in official_families().items():
        if not definition.get('ready'):
            continue
        for model in official_models(family):
            if (family, model, None) in covered_keys:
                continue
            files = strata_setup.ready_needed(model, root)
            paths = [(strata_setup.ready_where(root, f), f) for f in files]
            prepared = strata_setup.prepared_done(model, root) if strata_setup.MODELS[model].get('prepare') else False
            if not prepared and not any(p.is_file() for p, _ in paths):
                continue
            missing = [p.name for p, f in paths if not p.is_file() or p.stat().st_size != f[4]]
            reason = ('Official local pack inputs; ' +
                      (f'{len(missing)} missing/incomplete file(s); setup may download required files: ' + ', '.join(missing[:3])
                       if missing else 'files present; official setup still verifies hashes and prepares the config'))
            items.append(Item('pack', f'{family_title(family)} {model}', 'PREPARE', root,
                              family=family, model=model, reason=reason))
    return items


def all_items(model_root: Path, show_unsupported: bool, conf_root: Path | None = None) -> tuple[list[Item], int]:
    configs = installed_configs(conf_root)
    covered = {
        (x.family, x.model, x.variant)
        for x in configs
        if x.status in ("READY", "PREPARE") and x.family and x.model
    }
    supported, unsupported = gguf_items(model_root, covered)
    items = profile_items() + configs + supported + ready_pack_items(model_root, covered)
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
    """The CURRENT official Strata setup.py's own prepare workflow, not a reimplementation.  A custom
    build (variant) goes through the external manager_identity helper instead: official setup writes
    the canonical names, the helper converts the result into the variant's own config + pack and
    restores the canonical install - setup.py is never patched, nothing is duplicated."""
    gguf_dir = item.prepare_dir or item.path
    if official_families().get(item.family, {}).get('ready'):
        return [normal_python(), str(ROOT / 'setup.py'),
                '--family', item.family, '--model', item.model,
                '--models-dir', str(gguf_dir),
                *(['--context', str(item.context)] if item.context else []),
                '--no-start', '--yes']
    if "models" in official_families().get(item.family, {}):
        # Own-engine installers own their defaults and integrity/pack recipe.
        # Never force the Flash-Next 64K or variant wrapper onto another backend.
        raw_pack = arg_value(list((item.cfg or {}).get('args') or []), '--pack')
        data_dir = Path(raw_pack).parent.parent if raw_pack else None
        return [normal_python(), str(ROOT / "setup.py"),
                "--family", item.family, "--model", item.model,
                "--gguf-dir", str(gguf_dir),
                *(["--data-dir", str(data_dir)] if data_dir else []),
                *(["--context", str(item.context)] if item.context else []),
                "--no-start", "--yes"]
    ctx = item.context or DEFAULT_CONTEXT
    contexts = list(getattr(strata_setup, "CONTEXTS", [DEFAULT_CONTEXT]))
    if 65536 in contexts:
        ctx = 65536
    elif ctx not in contexts and contexts:
        ctx = contexts[0]

    if item.variant:
        return [
            normal_python(), str(TUI_DIR / "manager_identity.py"), "--prepare",
            "--root", str(ROOT),
            "--family", item.family,
            "--model", item.model,
            "--gguf-dir", str(gguf_dir),
            "--context", str(ctx),
            "--variant", item.variant,
            "--log", str(LOG_FILE),
        ]
    return [
        normal_python(), str(ROOT / "setup.py"),
        "--family", item.family,
        "--model", item.model,
        "--gguf-dir", str(gguf_dir),
        "--context", str(ctx),
        "--no-start",
        "--yes",
    ]


def setup_install_dir() -> Path:
    """Where Strata's own launcher points setup (STRATA_INSTALL_DIR): its catalog's install root (strata_cli/launch/catalog.py
    install_root), which for a source checkout is the checkout itself. ROOT when that cannot be read."""
    try:
        from strata_cli.launch.catalog import install_root
        return Path(install_root(None, ROOT))
    except Exception:  # noqa: BLE001 - an older checkout without the launcher: setup's own home is ROOT
        return ROOT


def setup_env(cmd: list) -> dict | None:
    """The environment of a setup.py run, the way Strata's launcher runs it (strata_cli/launch/runner.py): installer mode
    STRATA_CONTROL_INSTALL=1, so setup only prepares the model - no Python packages, no engine download or build; the run
    config names "component:strata-engine" (the components folder's engine). None (inherit) for any command that is not
    this checkout's setup.py."""
    try:
        is_setup = len(cmd) > 1 and Path(cmd[1]).resolve() == (ROOT / "setup.py").resolve()
    except OSError:
        is_setup = False
    if not is_setup:
        return None
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
               STRATA_CONTROL_INSTALL="1", STRATA_INSTALL_DIR=str(setup_install_dir()),
               STRATA_COMPONENTS_DIR=str(COMPONENTS_DIR))
    env.pop("STRATA_NO_ACQUIRE", None)      # preparing is the explicit step that may download model files
    env.pop("STRATA_NEED_ENGINE", None)     # (as the launcher: a model can be prepared before its engine is installed)
    return env


def _spawn_setup(cmd: list, header: str) -> subprocess.Popen | None:
    """Run the current setup.py with output appended to the shared log (in Strata's installer mode, see setup_env)."""
    env = setup_env(cmd)
    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log.write(f"\n\n--- {header} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.write(" ".join(f'"{x}"' if " " in x else x for x in cmd) + "\n")
    if env is not None:
        log.write(f"(installer mode: STRATA_CONTROL_INSTALL=1, STRATA_INSTALL_DIR={env['STRATA_INSTALL_DIR']})\n")
    log.write("\n")
    log.flush()
    try:
        process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env)
    except Exception as e:
        log.write(f"{header} failed to start: {e}\n")
        log.close()
        return None
    log.close()
    return process


def prepare(item: Item) -> subprocess.Popen | None:
    """Start setup.py for this model.  Reuses the stored family/model/variant/
    context; the logs page shows the real command and output."""
    if item.status != "PREPARE" or not (item.family and item.model):
        return None
    return _spawn_setup(prepare_command(item), f"PREPARE {item.title}")


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
    if item.kind not in ("config", "profile") or item.status != "READY" or not item.cfg:
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
    if item.kind == "profile":
        # your profile is the configuration: no saved Strata tuning is put on top (server.py apply_saved_tuning)
        env = os.environ.copy()
        env["STRATA_NO_PRESET"] = "1"
        kwargs["env"] = env
    if item.family == 'deepseek41':
        import deepseek_settings as ds
        try:
            ds.validate_copy_config(item.cfg)
        except ValueError as error:
            log.close()
            append_log('Incompatible engine configuration: ' + str(error))
            return None
        budget = ds.flag_value(ds.effective(item.cfg), '--expert-copy-gb')
        if budget is not None and budget != 'auto':
            if not getattr(item, '_copy_budget_session_confirmed', False):
                log.close()
                append_log('Manual Copy Budget not armed: use Performance > Load & Start and confirm risk.')
                return None
            item._copy_budget_session_confirmed = False
            env = os.environ.copy()
            env['DS4_MANUAL_COPY_SESSION'] = '1'  # child only; never saved to JSON or system environment
            kwargs['env'] = env

    kwargs["stdin"] = subprocess.DEVNULL
    if os.name == "nt":
        # The way Strata's own launcher starts its server (strata_control/runtime.py): no console at all. A hidden console
        # (CREATE_NEW_CONSOLE + SW_HIDE, used here before) measured ~37% slower decode on 2026-11-06 with the same engine
        # options and environment: TUI 79.0 vs Strata Terminal 117.6-126.1 tok/s on the same 2169-token first prompt.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
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

    try:
        from strata_control import procs
        identity = procs.identify(p.pid)
    except Exception:
        identity = None
    runtime_save({
        "pid": p.pid,
        "process_token": identity.token if identity else None,
        "process_cmd": list(identity.cmd) if identity else None,
        "port": port,
        "config": str(item.path),
        "model_name": item.cfg.get("model_name") or item.path.stem,
        "profile": str(item.profile_path) if item.profile_path else None,
        "engine_log": item.cfg.get("log"),
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
    token, command = rt.get('process_token'), rt.get('process_cmd')
    try:
        from strata_control import procs
        same = bool(token and command and procs.is_same(pid, token, command))
    except Exception:
        same = False
    if not same:
        append_log('Nothing stopped: TUI process identity is absent or stale; '
                   'a server owned by official Strata is left running. Use its owner to stop it.')
        runtime_clear()
        return
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
            if health(port) and procs.is_same(pid, token, command):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=12, creationflags=flags
                )
        else:
            os.killpg(pid, signal.SIGTERM)
            time.sleep(1.5)
            if health(port) and procs.is_same(pid, token, command):
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

    def note(self, text: str):
        """Insert one synthesized line into the visible log (page-local, never
        written to the file) - e.g. the MODEL READY banner."""
        self.lines.append(text)


def clip_line(s: str, width: int) -> str:
    if width <= 8:
        return s[:width]
    return s if len(s) <= width else s[:width - 1] + "…"


def _short(s, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _pad(s: str, width: int) -> str:
    """Pad a possibly-ANSI row to the full width so a shorter frame can never
    leave ghosting from an earlier, longer one."""
    return s + " " * max(0, width - len(ANSI_RE.sub("", s)))


# (kind, label, port) -> status-bar color
STATUS_STYLE = {
    "LOADING": YELLOW,
    "RUNNING": GREEN,
    "PREPARING": CYAN,
    "STOPPING": YELLOW,
    "STOPPED": GREEN,
    "PREPARED": GREEN,
    "ERROR": RED,
    "DONE": GREEN,
}


def _status_text(state, spinner: str | None, width: int) -> str:
    """The plain status-bar text: LOADING/RUNNING/PREPARING/STOPPING/...
    `state` is (kind, label, port, extra) - extra carries e.g. "64K" or
    "64K · Vision Off" on the LOADING / PREPARING rows."""
    kind, label, port, extra = state
    label = _short(label, 48)
    extra = _short(extra, 32) if extra else ""
    if kind == "LOADING":
        where = f" · {extra}" if extra else (f" · :{port}" if port else "")
        return f"LOADING · {label}{where} · {spinner}"
    if kind == "RUNNING":
        return f"RUNNING · {label} · :{port} · READY"
    if kind == "PREPARING":
        where = f" · {extra}" if extra else ""
        return f"PREPARING · {label}{where} · {spinner}"
    if kind == "STOPPING":
        return f"STOPPING · {label} · {spinner}"
    if kind == "STOPPED":
        return f"STOPPED · {label}"
    if kind == "PREPARED":
        return f"PREPARED · {label}"
    if kind == "DONE":
        return f"DONE · {label}"
    return f"ERROR · {_short(label, width - 8)}"


def _finish_record(state) -> str | None:
    """One short line recorded in the log when an operation changes state."""
    kind, label, port, _extra = state
    if kind == "RUNNING":
        return f"ready: http://127.0.0.1:{port}/v1"
    if kind == "PREPARED":
        return "prepare finished."
    if kind == "STOPPED":
        return "stopped."
    if kind == "ERROR":
        return f"error: {label}"
    return f"{kind.lower()}: {label}"


def build_log_frame(state, lines, tick: int, busy: bool, hint: str | None,
                    running: bool) -> str:
    """One log-page frame:
      row 1  status bar (fixed, always on top)
      row 2  dim separator
      row 3  (busy only) one dim note, e.g. the long-prepare hint
      ...    log tail, auto-scrolled to the newest output
      last   one dim key-hint line (C / M greyed out when not running)
    """
    width = max(60, min(150, shutil.get_terminal_size((120, 40)).columns - 2))
    rows = max(12, shutil.get_terminal_size((120, 40)).lines - 1)
    kind, label, _port, _extra = state

    spinner = SPINNER[tick % len(SPINNER)] if busy else None
    color = STATUS_STYLE.get(kind, "")
    out = [_pad(color + BOLD + _status_text(state, spinner, width) + RESET, width),
           _pad(DIM + "─" * width + RESET, width)]
    if hint and busy:
        out.append(_pad(GRAY + "  " + hint + RESET, width))

    log_rows = rows - len(out) - 2            # minus the blank row and the keys line
    visible = list(lines)[-max(log_rows, 1):]
    if visible:
        out.extend(_pad(clip_line(line, width), width) for line in visible)
    elif kind == "ERROR" and label:
        out.append(DIM + "  " + _short(label, width) + RESET)
    else:
        out.append(DIM + "(no output yet)" + RESET)

    keys = "[B/Esc] Back   [R] Refresh   "
    keys += (
        "[C] Chat   [M] Monitor"
        if running else
        GRAY + "[C] Chat   [M] Monitor" + RESET
    )
    out += ["", _pad(DIM + keys + RESET, width)]
    return "\n".join(out) + "\n"


def log_page(kind: str, label: str = "", port=None, extra: str = "",
             sources=(LOG_FILE,), active=None, finished=None,
             record_finished: bool = True, banner: str | None = None,
             hint: str | None = None):
    """Operation log page: fixed status bar on top, log tail in the middle,
    one dim key hint at the bottom.

    `state` is (kind, label, port, extra); kind is one of LOADING / RUNNING /
    PREPARING / STOPPING / STOPPED / PREPARED / ERROR / DONE.  While `active`
    (a callable) is True the status bar shows a spinner.  When nothing new has
    arrived, only the one status line is repainted (no whole-terminal redraw);
    new log text repaints the full page.  `finished()` returns the ending
    state, or None to keep waiting (e.g. a setup phase handing over to serve).
    `banner` is shown once in the log when the state turns RUNNING; `hint` is
    one dim line shown while active.  B / Esc returns; C / M open Chat /
    Monitor once the model is RUNNING.
    """
    request_clear()
    view = LogView(sources)
    state = (kind, label, port, extra)
    width = max(60, min(150, shutil.get_terminal_size((120, 40)).columns - 2))
    tick = 0
    last_frame = None
    last_state = None
    last_lines = None
    last_spinner = None
    force = True
    banner_done = False

    while True:
        if active is not None and not active():
            end = finished() if callable(finished) else finished
            if end:
                if isinstance(end, str):
                    end = ("DONE", str(end), None)
                else:
                    end = tuple(end)
                state = (end + (None,) * 4)[:4]
                if record_finished:
                    rec = _finish_record(state)
                    if rec:
                        append_log(rec)
                active = None                        # a final state: done
            # else: a phase ended but the operation continues (setup -> serve)

        changed = view.read()
        if state[0] == "RUNNING" and banner and not banner_done:
            view.note(banner)
            banner_done = True
        tick += 1
        busy = active is not None
        spinner = SPINNER[tick % len(SPINNER)] if busy else None
        lines_snap = list(view.lines)

        if (busy and not changed and not force and state == last_state
                and lines_snap == last_lines):
            # only the spinner moves: rewrite just the one status line
            if spinner != last_spinner:
                color = STATUS_STYLE.get(state[0], "")
                row = _pad(color + BOLD + _status_text(state, spinner, width) + RESET, width)
                sys.stdout.write("\x1b[1;1H" + row + "\x1b[K")
                sys.stdout.flush()
                last_spinner = spinner
        else:
            frame = build_log_frame(
                state, lines_snap, tick, busy,
                hint=hint if busy else None,
                running=(state[0] == "RUNNING"),
            )
            if force or frame != last_frame:
                paint(frame)
                last_frame = frame
                last_state = state
                last_lines = lines_snap
                last_spinner = spinner
                force = False

        key = (read_key(SPINNER_SECONDS if busy else None) or "").lower()
        if key in ("b", "esc"):
            request_clear()                     # the menu clears once on return
            return
        if key == "r":
            force = True
        elif key == "c" and state[0] == "RUNNING" and state[2]:
            webbrowser.open(f"http://127.0.0.1:{state[2]}/#chat")
        elif key == "m" and state[0] == "RUNNING" and state[2]:
            webbrowser.open(f"http://127.0.0.1:{state[2]}/#monitor")


# ---------------------------------------------------------------------------
# UI - main menu.  Redrawn after a key, or when the running model's live line changes.

def status_color(status: str) -> str:
    return {
        "READY": GREEN,
        "PREPARE": YELLOW,
        "UNSUPPORTED": RED,
        "BROKEN": RED,
    }.get(status, "")


_LAST_MENU: str | None = None


def render_menu(items: list[Item], pos: int, model_root: Path, unsupported_count: int,
                show_unsupported: bool, notice: str) -> bool:
    """Builds the menu and paints it when it differs from what is on screen (the live line refreshes it). True when painted."""
    global _LAST_MENU
    live = LIVE.get() if LIVE is not None else None
    if LIVE is None:
        running = running_status([x for x in items if x.kind == "config"])
        live = (running[0], running[1], "", []) if running else None
    lines: list[str] = []

    lines.append(BOLD + "STRATA" + RESET)
    if live:
        port, h, line, diffs = live
        lines.append(GREEN + "● Running" + RESET + f"  {h.get('model', 'unknown')}  :{port}")
        if line:
            lines.append("  " + (YELLOW if diffs else CYAN) + line + RESET)
        for d in diffs[:4]:
            lines.append(YELLOW + "  ! " + d + RESET)
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
            source = {'profile': 'PROFILE', 'config': 'CONFIG', 'gguf': 'GGUF', 'pack': 'PACK',
                      'auxiliary': 'AUX'}.get(item.kind, item.kind.upper())
            refusal = ""
            if item.kind == "profile" and item.profile:
                r = item.profile["refusal"]
                refusal = (f"  {YELLOW}refusal-vector ON x{float(r['scale']):g}{RESET}" if r["enabled"]
                           else f"  {DIM}refusal-vector off{RESET}")
            lines.append(
                f"{BOLD if i == pos else ''}{mark} "
                f"{item.title[:64]:64} "
                f"{col}{item.status:11}{RESET} "
                f"{DIM}{source}{RESET}{refusal}"
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
        lines.append(DIM + f"  {unsupported_count} non-launchable item(s) {state} · press U to toggle" + RESET)

    lines.append("")
    lines.append("↑/↓ Select   Enter Profile/Start/Prepare   E Settings   Del Delete profile   R Refresh   X Stop   C Chat   M Monitor   B Benchmark   F Folder   U Unsupported   Q Quit")
    frame = "\n".join(lines) + "\n"
    if frame == _LAST_MENU and not CLEAR_NEXT:
        return False
    paint(frame)
    _LAST_MENU = frame
    return True


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


# ---------------------------------------------------------------------------
# load options - READY + Enter opens this small pre-load page instead of
# starting directly.  Only choosing Load actually starts the model.

VISION_FLAGS = {"Off": "no", "Auto": "yes", "GPU": "gpu", "CPU": "cpu"}
CONTEXT_CHOICES = [(32768, "32K"), (65536, "64K"), (131072, "128K"), (262144, "262K")]


def ctx_label(ctx) -> str:
    if isinstance(ctx, int) and ctx > 0:
        return f"{ctx // 1024}K"
    return "?"


def choose_context_frame(pos: int) -> str:
    names = [name for _t, name in CONTEXT_CHOICES] + ["Custom"]
    lines = [BOLD + "CONTEXT" + RESET, ""]
    lines += [("> " if i == pos else "  ") + name for i, name in enumerate(names)]
    lines += ["", DIM + "↑/↓ Select   Enter Choose   Esc Back" + RESET]
    return "\n".join(lines) + "\n"


def prompt_context(current: int | None) -> int | None:
    """Custom context as an integer.  No clamping: the value is passed through
    to the current setup.py exactly as typed (Esc / empty keeps the value)."""
    while True:
        full_clear()
        print(BOLD + "CUSTOM CONTEXT" + RESET)
        print()
        raw = input(f"Context tokens [{current}]: ").strip()
        request_clear()
        if not raw:
            return current
        try:
            v = int(raw)
        except ValueError:
            v = None
        if v is None or v <= 0:
            print(RED + f"{raw!r} is not a positive integer." + RESET)
            print(DIM + "Press any key to try again, or B / Esc to keep the current value." + RESET)
            if (read_key() or "").lower() in ("b", "esc"):
                return current
            continue
        return v


def choose_context_page(current: int | None) -> int | None:
    """Context chooser; returns the chosen token count or None on Esc."""
    pos = len(CONTEXT_CHOICES)                  # Custom, the fallback
    if isinstance(current, int):
        for i, (tokens, _name) in enumerate(CONTEXT_CHOICES):
            if tokens == current:
                pos = i
                break
    count = len(CONTEXT_CHOICES) + 1
    while True:
        paint(choose_context_frame(pos))
        key = (read_key() or "").lower()
        if key == "up":
            pos = (pos - 1) % count
        elif key == "down":
            pos = (pos + 1) % count
        elif key in ("esc", "b"):
            return None
        elif key == "enter":
            if pos < len(CONTEXT_CHOICES):
                return CONTEXT_CHOICES[pos][0]
            return prompt_context(current)


def choose_vision_frame(pos: int) -> str:
    names = list(VISION_FLAGS)
    lines = [BOLD + "VISION" + RESET, ""]
    lines += [("> " if i == pos else "  ") + name for i, name in enumerate(names)]
    lines += ["", DIM + "↑/↓ Select   Enter Choose   Esc Back" + RESET]
    return "\n".join(lines) + "\n"


def choose_vision_page(current: str) -> str | None:
    """Vision chooser; returns Off / Auto / GPU / CPU or None on Esc."""
    names = list(VISION_FLAGS)
    pos = names.index(current) if current in names else 0
    while True:
        paint(choose_vision_frame(pos))
        key = (read_key() or "").lower()
        if key == "up":
            pos = (pos - 1) % len(names)
        elif key == "down":
            pos = (pos + 1) % len(names)
        elif key in ("esc", "b"):
            return None
        elif key == "enter":
            return names[pos]


def load_options_frame(item, ctx, vision, reserve, kv_vram, estimate, pos: int) -> str:
    """The single pre-load configuration screen.  Context and estimate are read-only."""
    est_text = _vram.format_estimate(estimate)
    if estimate is None:
        est_text = DIM + est_text + RESET
    rows = [
        ("TOTAL CONTEXT", f"{ctx:>10,}"),
        ("Vision", vision),
        ("VRAM reserve", f"[ {reserve:>6,} ] MiB"),
        ("KV in VRAM", f"[ {kv_vram:>7,} ]"),
        ("KV in RAM", f"[ {_vram.kv_ram(ctx, kv_vram):>7,} ]"),
        ("Estimated experts", est_text),
    ]
    lines = [BOLD + "STRATA — LOAD MODEL" + RESET, "",
             DIM + _short(item.title, 60) + RESET, ""]
    for i, (name, value) in enumerate(rows):
        if i == 2:
            lines.append("")
        lines.append(("> " if pos == i else "  ") + f"{name:<21} {value}")
    lines.append("")
    for i, action in enumerate(("Load", "Back"), start=len(rows)):
        lines.append(("> " if pos == i else "  ") + action)
    lines += ["", DIM + "↑/↓ Select   Enter Edit/Confirm   Esc Back" + RESET]
    return "\n".join(lines) + "\n"


def load_options_page(item, ctx, vision, reserve, kv_vram, calibration) -> tuple:
    """Edit temporary settings; context is the sum of editable KV allocations."""
    row_count = 8
    pos = 0
    kv_ram = _vram.kv_ram(ctx, kv_vram)
    kv_format = _vram.model_kv_format(item.cfg or {})
    while True:
        ctx = _vram.kv_total(kv_vram, kv_ram)
        estimate = _vram.estimate_experts(ctx, kv_vram, reserve, kv_format, calibration)
        paint(load_options_frame(item, ctx, vision, reserve, kv_vram, estimate, pos))
        key = (read_key() or "").lower()
        if key == "up":
            pos = (pos - 1) % row_count
        elif key == "down":
            pos = (pos + 1) % row_count
        elif key in ("esc", "b"):
            return ("back", vision, reserve, kv_vram, ctx)
        elif key == "enter":
            if pos == 1:
                new = choose_vision_page(vision)
                if new is not None:
                    vision = new
            elif pos == 2:
                new = _vram_prompt_int("VRAM RESERVE (MiB)", reserve, "MiB",
                                       _vram.validate_reserve_mib)
                if new is not None:
                    reserve = new
            elif pos == 3:
                new = _vram_prompt_int("KV IN VRAM (tokens)", kv_vram, "",
                                       _vram.validate_kv_vram)
                if new is not None:
                    kv_vram = new
                    ctx = _vram.kv_total(kv_vram, kv_ram)
            elif pos == 4:
                new = _vram_prompt_int("KV IN RAM (tokens)", kv_ram, "",
                                       _vram.validate_kv_ram)
                if new is not None:
                    kv_ram = new
                    ctx = _vram.kv_total(kv_vram, kv_ram)
            elif pos == 6:
                return ("load", vision, reserve, kv_vram, ctx)
            elif pos == 7:
                return ("back", vision, reserve, kv_vram, ctx)


def build_load_plan(item, ctx, vision, current_ctx, current_vision):
    """The load plan.  None = start the existing config directly (nothing
    changed).  Otherwise the CURRENT official setup.py command that applies the new
    context / vision first - setup.py keeps the finished pack, so it updates
    the config instead of rebuilding the model.  A custom build (variant) is NEVER
    re-prepared through the canonical slot: its settings are applied to its own
    config file directly (manager_identity --apply-settings), so the variant
    stays a separate model and official Strata is never patched."""
    if ctx == current_ctx and vision == current_vision:
        return None
    if item.variant:
        return _variant_settings_cmd(item, ctx, vision)
    src = config_source_gguf(item.cfg or {})
    if src is None:
        return None                        # cannot re-run setup.py without the source
    return [normal_python(), str(ROOT / "setup.py"),
            "--family", item.family,
            "--model", item.model,
            "--gguf-dir", str(src),
            "--context", str(ctx),
            "--vision", VISION_FLAGS.get(vision, "no"),
            "--no-start", "--yes"]


def _variant_settings_cmd(item, ctx, vision) -> list[str]:
    """A custom build's settings change: edit its OWN config file directly (external
    helper), keeping the variant's separate identity (its model_name/aliases/pack)
    - official setup.py would write the canonical slot instead, which this must never do."""
    cmd = [normal_python(), str(TUI_DIR / "manager_identity.py"), "--apply-settings",
           "--root", str(ROOT), "--config", item.path.name]
    if item.cfg is not None:
        cmd += ["--save-backup"]
    if ctx:
        cmd += ["--context", str(ctx)]
    cmd += ["--vision", str(VISION_FLAGS.get(vision, "no"))]
    return cmd


def build_vram_settings_cmd(item, cfg, kv_vram, reserve, context=None) -> list[str]:
    """Build the final residency/reserve edit after context setup has completed."""
    kv_arg, reserve_arg = _vram.effective_args(cfg, kv_vram, reserve, context=context)
    cmd = [normal_python(), str(TUI_DIR / "manager_identity.py"),
           "--apply-settings", "--root", str(ROOT), "--config", item.path.name,
           "--kv-resident", str(kv_arg),
           "--vram-reserve-mib", str(reserve_arg)]
    return cmd


def load_extra(ctx, vision) -> str:
    bits = []
    if ctx:
        bits.append(f"{ctx // 1024}K")
    bits.append(f"Vision {vision}")
    return " · ".join(bits)


class LoadRunner:
    """Run ordered pre-start commands, then start and health-check the server."""

    def __init__(self, item: Item, pre_commands=()):
        self.item = item
        self.port = cfg_port(item.cfg or {})
        self.pre_commands = list(pre_commands or ())
        self.command_index = 0
        self.phase = "setup" if self.pre_commands else "serve"
        self.setup_proc = None
        self.serve = None                 # (Popen, port) - or False after the attempt
        self.started = False

    def model_name(self):
        return (self.item.cfg or {}).get("model_name") or self.item.path.stem

    def alive(self) -> bool:
        if self.phase == "setup":
            if self.setup_proc is None:
                command = self.pre_commands[self.command_index]
                self.setup_proc = _spawn_setup(
                    command, f"LOAD-SETTINGS {self.command_index + 1}/{len(self.pre_commands)} "
                    f"{self.item.title}")
                if self.setup_proc is None:
                    self.phase = "fail"
                    return False
            return self.setup_proc.poll() is None
        if self.phase == "serve":
            if not self.started:
                self.serve = start_item(self.item) or False
                self.started = True
            if self.serve is False:
                return False
            proc, port = self.serve
            return proc.poll() is None and health(port) is None
        return False                        # "fail" phase

    def finish(self):
        if self.phase == "setup":
            rc = self.setup_proc.returncode if self.setup_proc is not None else -1
            if rc != 0:
                return ("ERROR", f"settings update failed (exit {rc}) - see the log", None)
            self.command_index += 1
            self.setup_proc = None
            if self.command_index >= len(self.pre_commands):
                self.phase = "serve"
            return None
        if self.phase == "fail":
            return ("ERROR", "settings update did not start - see the log", None)
        live = health(self.port)
        if live and live.get('model') not in (None, self.model_name()):
            return ('ERROR', f":{self.port} is serving {live.get('model')}; {self.model_name()} not started", self.port)
        if self.serve is False:
            if live:
                return ("RUNNING", self.model_name(), self.port)
            return ("ERROR", "start did not begin - see the log", self.port)
        proc, port = self.serve
        if health(port):
            return ("RUNNING", self.model_name(), port)
        return ("ERROR", f"start exited ({proc.poll()}) while loading - see the log", port)


def cell_clip(text: str, width: int) -> str:
    """Bound a backend screen by terminal cells without splitting ANSI codes."""
    import unicodedata
    tokens = re.findall(r'\x1b\[[0-9;?]*[A-Za-z]|.', text)
    sizes = [0 if t.startswith('\x1b') or unicodedata.combining(t) else
             2 if unicodedata.east_asian_width(t) in ('W', 'F') else 1 for t in tokens]
    if sum(sizes) <= width:
        return text
    out, used = [], 0
    for token, size in zip(tokens, sizes):
        if used + size > max(0, width - 1):
            break
        out.append(token)
        used += size
    return ''.join(out) + '…' + (RESET if '\x1b' in text else '')


def deepseek_input(prompt, max_len=64):
    """Small raw-key editor so Esc cancels without waiting for Enter on Windows."""
    if not sys.stdin.isatty():
        try:
            value = input(prompt)
            return None if '\x1b' in value else value
        except (EOFError, KeyboardInterrupt):
            return None
    value = ''
    while True:
        width = max(1, shutil.get_terminal_size((120, 40)).columns - 2)
        sys.stdout.write('\r' + cell_clip(prompt + value, width) + '\x1b[K')
        sys.stdout.flush()
        key = read_key() or ''
        if key.lower() == 'esc' or key == '\x03':
            return None
        if key.lower() == 'enter':
            return value
        if key in ('\b', '\x7f'):
            value = value[:-1]
        elif len(key) == 1 and key.isprintable() and len(value) < max_len:
            value += key


def deepseek_confirm(title, text):
    """A small yes/no frame; Enter confirms the first choice, Esc/B keeps editing."""
    width = max(1, shutil.get_terminal_size((120, 40)).columns - 2)
    import textwrap
    pos = 0
    request_clear()
    while True:
        lines = [BOLD + title + RESET, '']
        lines += textwrap.wrap(text, width)
        lines += ['', ('> ' if pos == 0 else '  ') + 'Yes',
                  ('> ' if pos == 1 else '  ') + 'No',
                  '', '↑/↓ Select   Enter Confirm   Esc Cancel']
        paint('\n'.join(cell_clip(line, width) for line in lines) + '\n')
        key = (read_key() or '').lower()
        if key in ('up', 'down'):
            pos = 1 - pos
        elif key in ('enter', 'y'):
            return pos == 0
        elif key in ('esc', 'b', 'n'):
            return False


def deepseek_edit_field(args, changes, label, flag, ds, width):
    """One option's value editor.  Esc discards ALL pending edits; blank keeps the current
    value.  Returns 'discard' on Esc (whole page abandoned), otherwise None."""
    import textwrap
    error = ''
    while True:
        current = changes.get(flag, ds.current_value(args, flag))
        lines = [BOLD + label.upper() + RESET, '']
        lines += textwrap.wrap(ds.HELP[flag], width)
        lines += textwrap.wrap(error, width) if error else ['']
        lines += textwrap.wrap('Blank = keep current; cancel = return. auto = omit the flag '
                               '(only --dspark and --mmap-experts take the literal auto).', width)
        paint('\n'.join(cell_clip(line, width) for line in lines) + '\n')
        raw = deepseek_input('Value [' + ds.display_value(flag, current) + ']: ')
        request_clear()
        if raw is None:
            return 'discard'
        raw = raw.strip()
        if not raw or raw.lower() == 'cancel':
            return None
        try:
            value = ds.parse_value(flag, raw)
        except ValueError as exc:
            error = 'Invalid: ' + str(exc)
            continue
        if value == ds.current_value(args, flag):
            changes.pop(flag, None)
        else:
            changes[flag] = value
        return None


def deepseek_save(item, changes, ds):
    """Write the pending edits with a verified backup, mark Restart Required and never
    restart.  Refuses to touch a foreign model serving the config's port."""
    cfg = item.cfg or {}
    port = cfg_port(cfg)
    live = health(port)
    if live and live.get('model') not in (None, cfg.get('model_name')):
        return False, RED + f"ERROR: :{port} is serving {live.get('model')}; nothing saved" + RESET
    if not changes and not cfg.get('tui_deepseek_reload_required'):
        return True, 'Nothing changed since the last save.'
    try:
        new_cfg, _updated = ds.persist(item, changes, running=True)
    except (OSError, ValueError, TypeError) as exc:
        return False, RED + 'Save failed: ' + str(exc) + RESET
    item.cfg = new_cfg
    return True, 'Saved. Restart Required until the engine reloads — nothing was restarted.'


def deepseek_load_options_page(item):
    """DeepSeek Performance settings screen.  Every option shows the effective value;
    Auto means the flag is omitted (the engine's own default).  Actions: Save (marks
    Restart Required, never restarts), Load & Start (returns the pending changes so the
    caller starts the engine), Reset to Default, Reload Configuration, Back.  Unsaved
    edits are never silently dropped: Esc and Back ask first."""
    import deepseek_settings as ds
    cfg = item.cfg or {}
    args = ds.effective(cfg)
    changes = {}
    notice = ''
    pos = 0
    actions = ['Save', 'Load & Start', 'Reset to Default', 'Reload Configuration', 'Back']
    fields = ds.fields_for_config(cfg)
    while True:
        width = max(1, shutil.get_terminal_size((120, 40)).columns - 2)
        status = (RED + BOLD + '● Restart Required' + RESET if cfg.get('tui_deepseek_reload_required')
                  else YELLOW + '● Unsaved changes' + RESET if changes
                  else GREEN + '● Saved' + RESET)
        lines = [BOLD + 'STRATA — DEEPSEEK PERFORMANCE' + RESET, '', item.title,
                 '  Engine: ' + str(cfg.get('exe') or '') + '   ' + status,
                 '', BOLD + '  Performance settings' + RESET]
        for i, (label, flag) in enumerate(fields):
            if i == len(ds.PERFORMANCE):
                lines += ['', BOLD + '  Advanced' + RESET]
            value = changes.get(flag, ds.current_value(args, flag))
            lines.append(('> ' if pos == i else '  ') + label + ': ' + ds.display_value(flag, value))
        lines += ['', BOLD + '  Actions' + RESET]
        for i, label in enumerate(actions, len(fields)):
            lines.append(('> ' if pos == i else '  ') + label)
        if notice:
            lines += ['', CYAN + notice + RESET]
        lines += ['', '←/→ choices · Enter edit/confirm · ↑/↓ select · Esc Back',
                  'Save marks Restart Required and never restarts; edits are pending until saved.']
        paint('\n'.join(cell_clip(line, width) for line in lines) + '\n')
        key = (read_key() or '').lower()
        total = len(fields) + len(actions)
        if key in ('up', 'down'):
            pos = (pos + (1 if key == 'down' else -1)) % total
            notice = ''
        elif key in ('left', 'right') and pos < len(fields):
            label, flag = fields[pos]
            if flag in ds.ENUMS:                  # dropdown-style cycling for the choices
                base = ds.current_value(args, flag)
                choices = ds.ENUMS[flag]
                index = choices.index(changes.get(flag, base)) if changes.get(flag, base) in choices else 0
                index = (index + (1 if key == 'right' else -1)) % len(choices)
                value = choices[index]
                if value == base:
                    changes.pop(flag, None)
                else:
                    changes[flag] = value
                notice = ''
        elif key in ('esc', 'b'):
            if changes and not deepseek_confirm('DISCARD EDITS',
                    'There are unsaved changes. Discard them all and leave the settings?'):
                continue
            return None
        elif key == 'enter':
            if pos < len(fields):
                label, flag = fields[pos]
                if deepseek_edit_field(args, changes, label, flag, ds, width) == 'discard':
                    return None
                notice = ''
                continue
            action = actions[pos - len(fields)]
            if action == 'Back':
                if changes and not deepseek_confirm('DISCARD EDITS',
                        'There are unsaved changes. Discard them all and leave the settings?'):
                    continue
                return None
            if action == 'Save':
                saved, notice = deepseek_save(item, changes, ds)
                if saved:
                    cfg = item.cfg
                    args = ds.effective(cfg)
                    changes = {}
            elif action == 'Load & Start':
                try:
                    pending = ds.edited_config(cfg, changes)
                    ds.validate_copy_config(pending)
                    budget = ds.flag_value(ds.effective(pending), '--expert-copy-gb')
                    if budget is not None and budget != 'auto' and not deepseek_confirm(
                            'HIGH MEMORY RISK', 'Manual expert copies can exhaust RAM and freeze Windows. '
                            'Start this experiment now? No boot/autostart is configured.'):
                        continue
                except (ValueError, OSError) as error:
                    notice = str(error)
                    continue
                item._copy_budget_session_confirmed = budget is not None and budget != 'auto'
                return changes
            elif action == 'Reset to Default':
                changes = {flag: ds.default_option(flag) for _, flag in fields}
                notice = 'All options set to their engine defaults — Save to apply.'
            elif action == 'Reload Configuration':
                if changes and not deepseek_confirm('DISCARD EDITS',
                        'Discard unsaved changes and reload the saved configuration from disk?'):
                    continue
                cfg = read_json(item.path)
                item.cfg = cfg
                args = ds.effective(cfg)
                changes = {}
                notice = 'Configuration reloaded from disk.'



def benchmark_detail(item, record):
    import deepseek_bench as bench
    offset = 0
    notice = ''
    while True:
        height = max(6, shutil.get_terminal_size((120, 40)).lines - 8)
        import textwrap
        width = max(30, shutil.get_terminal_size((120, 40)).columns - 2)
        lines = [part for line in bench.detail_lines(record)
                 for part in (textwrap.wrap(line, width=width, replace_whitespace=False) or [''])]
        offset = max(0, min(offset, max(0, len(lines) - height)))
        paint('\n'.join([BOLD + 'STRATA — SNAPSHOT DETAILS' + RESET, ''] +
                        lines[offset:offset + height] + ['', notice,
                        '↑/↓ Scroll · R Restore Configuration · D Delete Snapshot · B/Esc Back']) + '\n')
        key = (read_key() or '').lower()
        if key in ('esc', 'b'):
            return
        if key in ('up', 'down'):
            offset += 1 if key == 'down' else -1
        elif key == 'r' and deepseek_confirm('RESTORE CONFIGURATION',
                'Restore verified performance launch parameters? Current JSON will be backed up. No engine restart.'):
            try:
                paths = bench.restore_result(item, record)
                notice = 'Restored; Restart Required (not restarted). Backup: ' + str(Path(item.path).parent / '.deepseek-backups')
            except Exception as error:
                notice = str(error)
        elif key == 'd' and deepseek_confirm('DELETE SNAPSHOT', 'Delete this snapshot from History? Original data remains archived in JSONL.'):
            try:
                bench.delete_result(record)
                return
            except OSError as error:
                notice = str(error)


def benchmark_page(item):
    """DeepSeek benchmark screen.  Reads the engine's own logged numbers (decode/prefill
    tok/s, expert-cache store statistics, VRAM placement), samples the engine's RAM, shows
    the configuration under test, and keeps a saved history for comparison.  Everything is
    manual: nothing runs, restarts or writes to the engine here."""
    import deepseek_settings as ds
    import deepseek_bench as bench
    cfg = item.cfg or {}
    log = Path(cfg.get('log') or '')
    notice = ''
    pos = 0
    actions = ['Snapshot & Save Result', 'Refresh Stats', 'Back']
    while True:
        stats = bench.parse_log(log) if log.is_file() else {}
        cfg = item.cfg or {}
        memory = bench.memory_snapshot(cfg)
        results = bench.visible_results()
        pos = min(pos, len(actions) + len(results) - 1)
        best = bench.best_decode(results)
        width = max(60, min(150, shutil.get_terminal_size((120, 40)).columns - 2))
        lines = [BOLD + 'STRATA — DEEPSEEK BENCHMARK' + RESET, '', item.title,
                 '  Engine: ' + bench.engine_version(cfg) + '   log: ' + (log.name if log else '—'),
                 '', BOLD + '  Current run (from the engine log)' + RESET]
        decode = stats.get('decode_tok_s')
        prefill = stats.get('prefill_tok_s')
        lines.append('  Decode          ' +
                     (f'{decode:>8.2f} tok/s' if isinstance(decode, (int, float)) else '—'))
        lines.append('  Prefill         ' +
                     (f'{prefill:>8.1f} tok/s' if isinstance(prefill, (int, float)) else '—'))
        lines.extend(bench.memory_lines(memory))
        vram_used = None
        if 'vram_weights_gb' in stats and 'vram_experts_gb' in stats:
            vram_used = stats['vram_weights_gb'] + stats['vram_experts_gb'] + stats.get('vram_caches_gb', 0)
        lines.append('  VRAM used       ' +
                     (f'{vram_used:>8.2f} GB (weights + experts + caches)' if vram_used is not None else '—'))
        if 'vram_free_mib' in stats:
            lines.append(f"  VRAM free       {stats['vram_free_mib']:>8d} MiB (CUDA0, after reserve)")
        cpu = stats.get('cpu_lane_pct')
        if cpu is not None:
            lines.append(f"  CPU lane        {cpu:>8.1f}% of the experts")
        if stats.get('ram_store_gb') is not None or stats.get('store'):
            lines += ['  Expert memory store (engine reported):']
            if stats.get('ram_store_gb') is not None:
                store_policy = {0: 'lru', 1: 'lfu', 2: 'lfulru'}.get(stats.get('ram_store_policy'), '?')
                lines.append(f"    RAM store: {stats.get('ram_store_slots', '?')} slots · "
                             f"{stats['ram_store_gb']:.1f} GB cap · policy {store_policy}")
            if 'store_warm_experts' in stats:
                lines.append(f"    warm-up: {stats['store_warm_experts']} of the profile's hottest "
                             f"experts ({stats['store_warm_gb']:.1f} GB)")
            store = stats.get('store')
            if store:
                lines.append(f"    {store['hits_pct']:.1f}% of the CPU lane's experts were in RAM · "
                             f"{store['read_mb_per_token']:.1f} MB read a token")
                lines.append(f"    slots {store['slots']} of {store['slots_max']} · "
                             f"{store['evictions']} evictions · {store['failed_reads']} failed reads")
        lines += ['', BOLD + '  Configuration under test' + RESET]
        launch = bench.launch_evidence(cfg.get('log'))
        lines.extend('  ' + line for line in bench.configuration_lines(launch.get('args') or [], stats))
        lines += ['', BOLD + '  History · L latest / D best Decode / P best Prefill · Enter Details' + RESET,
                  '      Time Decode Prefill Context DSpark GPU Experts CPU Copy GB']
        lines += ['  Snapshot JSON: ' + str(bench.results_path().resolve())] + bench.comparison(results)
        if not results:
            lines.append('  No saved results yet — Snapshot & Save Result records one.')
        else:
            selected = max(0, pos - len(actions))
            first = max(0, selected - 5)
            for index, record in enumerate(results[first:first + 6], first):
                is_best = (isinstance(record.get('decode_tok_s'), (int, float)) and best is not None
                           and abs(record['decode_tok_s'] - best) < 1e-9)
                best_pf = max((r['prefill_tok_s'] for r in results if isinstance(r.get('prefill_tok_s'), (int, float))), default=None)
                marks = ('L' if index == 0 else '-') + ('D' if is_best else '-') + ('P' if best_pf is not None and record.get('prefill_tok_s') == best_pf else '-')
                line = marks + ' ' + bench.result_row(record, width - 6)
                if is_best:
                    line = GREEN + line + RESET
                lines.append(('> ' if pos == len(actions) + index else '  ') + cell_clip(line, width))
        lines += ['', BOLD + '  Actions' + RESET]
        for i, label in enumerate(actions):
            lines.append(('> ' if pos == i else '  ') + label)
        if notice:
            lines += ['', CYAN + notice + RESET]
        lines += ['', 'Enter Confirm · R Refresh · B/Esc Back',
                  'Benchmark is manual: it never runs or restarts the engine.']
        paint('\n'.join(cell_clip(line, width) for line in lines) + '\n')
        key = (read_key() or '').lower()
        if key in ('up', 'down'):
            pos = (pos + (1 if key == 'down' else -1)) % (len(actions) + len(results))
            notice = ''
        elif key in ('r',):
            notice = 'Stats re-read from the engine log.'
        elif key in ('esc', 'b'):
            return
        elif key == 'enter':
            if pos >= len(actions):
                benchmark_detail(item, results[pos - len(actions)])
                continue
            if pos == 0:
                record = bench.save_result(cfg)
                if record is None:
                    notice = RED + 'Could not write the results file.' + RESET
                else:
                    decode_text = ('%.2f tok/s' % record['decode_tok_s']) \
                        if isinstance(record.get('decode_tok_s'), (int, float)) else '—'
                    notice = f"Saved result {record.get('ts', '')} · decode {decode_text} · " \
                             f"prefill {record.get('prefill_tok_s') or '—'}"
            elif pos == 1:
                notice = 'Stats refreshed.'
            else:
                return


def own_load_options_page(item) -> bool:
    """Own engines retain their native saved settings, never Qwen KV formulas."""
    cfg = item.cfg or {}
    args = list(cfg.get('args') or [])
    ctx = config_context(cfg)
    pos = 0
    while True:
        lines = [BOLD + 'STRATA — LOAD MODEL' + RESET, '', item.title, '',
                 f"  Context              {ctx:,}" if ctx else '  Context              Engine default',
                 f"  Engine               {Path(cfg.get('exe') or '').name}",
                 '  Memory / experts     Engine-managed',
                 f"  Drafting             {arg_value(args, '--dspark') or arg_value(args, '--mtp') or 'Engine default'}",
                 '', '  Saved backend settings are kept unchanged.',
                 '  Use official Strata settings to tune this backend.', '',
                 ('> ' if pos == 0 else '  ') + 'Load',
                 ('> ' if pos == 1 else '  ') + 'Back', '',
                 '↑/↓ Select   Enter Confirm   Esc Back']
        width = max(20, shutil.get_terminal_size((120, 40)).columns - 2)
        paint('\n'.join(cell_clip(line, width) for line in lines) + '\n')
        key = (read_key() or '').lower()
        if key in ('up', 'down'):
            pos = 1 - pos
        elif key in ('esc', 'b'):
            return False
        elif key == 'enter':
            return pos == 0


def start_via_options(item):
    """READY + Enter: load-options page first; only 'Load' actually starts."""
    if item.family == 'deepseek41' and (item.cfg or {}).get('arch') == 'deepseek41':
        changes = deepseek_load_options_page(item)
        if changes is None:
            return
        cfg = item.cfg or {}
        port = cfg_port(cfg)
        live = health(port)
        if live and live.get('model') not in (None, cfg.get('model_name')):
            log_page('ERROR', f":{port} is serving {live.get('model')}; nothing saved or started", port)
            return
        try:
            import deepseek_settings as ds
            item.cfg, updated = ds.persist(item, changes, running=bool(live))
            item.context = config_context(item.cfg)
        except (OSError, ValueError, TypeError) as exc:
            log_page('ERROR', 'DeepSeek settings not applied: ' + str(exc), port)
            return
        if live:
            if changes or item.cfg.get('tui_deepseek_reload_required'):
                log_page('RELOAD REQUIRED', 'Settings saved for next start; not applied to the running model. '
                         'Stop/reload using its owner (official Terminal or this TUI). Nothing was stopped.', port)
            else:
                log_page('RUNNING', cfg.get('model_name') or item.title, port)
            return
        runner = LoadRunner(item)
        cfg = item.cfg or {}
        log_page('LOADING', cfg.get('model_name') or item.title, port,
                 extra=ctx_label(config_context(cfg)), sources=[LOG_FILE],
                 active=runner.alive, finished=runner.finish,
                 banner=f'==== MODEL READY · http://127.0.0.1:{port} ====')
        return
    if "models" in official_families().get(item.family, {}):
        if not own_load_options_page(item):
            return
        cfg = item.cfg or {}
        runner = LoadRunner(item)
        port = cfg_port(cfg)
        sources = [LOG_FILE] + ([Path(cfg['log'])] if cfg.get('log') else [])
        log_page('LOADING', cfg.get('model_name') or item.title, port,
                 extra=ctx_label(config_context(cfg)), sources=sources,
                 active=runner.alive, finished=runner.finish,
                 banner=f'==== MODEL READY · http://127.0.0.1:{port} ====')
        return
    cfg = item.cfg or {}
    current_ctx = config_context(cfg)
    current_vision = config_vision(cfg)
    vision = current_vision or "Off"
    reserve = _vram.model_reserve_mib(cfg)
    allocation = initial_kv_allocation(item)
    ctx = allocation["context"]
    kv_vram = allocation["kv_vram"]
    calibration = _vram.calibration_from_log(cfg.get("log") or "")

    action, vision, reserve, kv_vram, ctx = load_options_page(
        item, ctx, vision, reserve, kv_vram, calibration)
    if action != "load":
        return
    kv_ram = ctx - kv_vram
    save_kv_allocation(item, kv_vram, kv_ram)

    port = cfg_port(cfg)
    model_name = cfg.get("model_name") or item.path.stem
    live = health(port)
    if live:
        actual = live.get('model') or 'unknown'
        if actual != model_name:
            log_page('ERROR', f':{port} is serving {actual}; {model_name} not started', port,
                     sources=[LOG_FILE])
        else:
            log_page("RUNNING", model_name, port,
                     banner=f"==== MODEL READY · http://127.0.0.1:{port} ====")
        return

    setup_cmd = build_load_plan(item, ctx, vision, current_ctx, current_vision)
    changed = (ctx != current_ctx) or (vision != current_vision)
    if changed and setup_cmd is None:
        # never silently start with settings we could not apply
        log_page("ERROR", "settings changed, but the model's GGUF folder was not found - "
                          "nothing was changed", port, sources=[LOG_FILE])
        return
    current_kv_arg, current_reserve_arg = _vram.effective_args(
        cfg, _vram.model_kv_vram(cfg, current_ctx), _vram.model_reserve_mib(cfg),
        context=current_ctx)
    selected_kv_arg, selected_reserve_arg = _vram.effective_args(
        cfg, kv_vram, reserve, context=ctx)
    vram_changed = ((selected_kv_arg, selected_reserve_arg) !=
                    (current_kv_arg, current_reserve_arg))
    pre_commands = []
    if setup_cmd:
        pre_commands.append(setup_cmd)
    # Always apply explicit VRAM choices after setup when setup may rewrite defaults.
    if setup_cmd or vram_changed:
        pre_commands.append(build_vram_settings_cmd(item, cfg, kv_vram, reserve, context=ctx))
    if pre_commands:
        backup = _vram_backup(item.path)
        if backup:
            append_log(f"Load settings: timestamped backup {backup}")
    runner = LoadRunner(item, pre_commands)
    sources = [LOG_FILE]
    engine_log = cfg.get("log")
    if engine_log:
        sources.append(Path(engine_log))
    log_page(
        "LOADING", model_name, port, extra=load_extra(ctx, vision),
        sources=sources, active=runner.alive, finished=runner.finish,
        banner=f"==== MODEL READY · http://127.0.0.1:{port} ====",
    )


def _vram_prompt_int(label, current, unit, ok):
    """Full-screen numeric prompt.  Returns the new int, or None on cancel."""
    unit_txt = (" " + unit) if unit else ""
    while True:
        full_clear()
        print(BOLD + label + RESET)
        print()
        raw = input(f"Value [{current:,}]{unit_txt}: ").strip()
        request_clear()
        if not raw:
            return None
        try:
            v = int(raw)
        except ValueError:
            v = None
        accepted = v is not None and ok(v)
        if not accepted:
            print(RED + f"{raw!r} is not a valid value here." + RESET)
            print(DIM + "Press any key to try again, or B / Esc to cancel." + RESET)
            if (read_key() or "").lower() in ("b", "esc"):
                return None
            continue
        return v


def _vram_backup(path: Path) -> str:
    """Timestamped backup before the first config write this session."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.stem}.vram-{stamp}.bak")
    try:
        shutil.copy2(path, bak)
    except OSError:
        return ""
    return bak.name


# ---------------------------------------------------------------------------
# your own launch profiles (profiles.py): kept outside the Strata folder, so an update or a setup run never rewrites them.
# The TUI writes a run config from the profile into its own state folder and starts Strata's serve/server.py with it.

import profiles as _prof            # noqa: E402
import live_status as _live         # noqa: E402
import update_check as _upd         # noqa: E402

PROFILES_DIR = _mcfg.profiles_dir()
STATE_DIR = _mcfg.state_dir()
COMPONENTS_DIR = _mcfg.components_dir()
DEFAULT_PROFILES_DIR = TUI_DIR / "profiles-default"
REFUSAL_WARNING = ("The vector's package describes it as a refusal-direction projection: with it on, the model declines far "
                   "fewer requests (a safety behaviour is removed) and its answers shift slightly. You are responsible for "
                   "what it writes. Strata measured 0.2-0.4% more time per token.")


def seed_profiles():
    """The verified profile is always there (copied from the read-only verified file when missing) and read-only; an empty
    folder also gets the other shipped profiles. An existing file is never replaced."""
    try:
        PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        empty = not any(PROFILES_DIR.glob("*.json"))
        for pid in _prof.PROTECTED_IDS:
            dst = PROFILES_DIR / f"{pid}.json"
            if not dst.exists():
                shutil.copyfile(_set.verified_file(), dst)
            _prof.set_readonly(dst)
        if empty:
            for src in sorted(DEFAULT_PROFILES_DIR.glob("*.json")):
                dst = PROFILES_DIR / src.name
                if not dst.exists():
                    shutil.copyfile(src, dst)
    except OSError:
        pass


def profile_run_paths(pid: str) -> tuple[Path, Path]:
    """(the run config the TUI writes, the engine log) of one profile, both in the state folder."""
    return STATE_DIR / "run" / f"{pid}.json", STATE_DIR / "logs" / f"{pid}.engine.log"


def profile_item(path: Path, prof: dict | None, problem: str = "") -> Item:
    pid = _prof.profile_id(path)
    run_path, engine_log = profile_run_paths(pid)
    if prof is None:
        return Item(kind="profile", title=f"[profile] {pid}", status="BROKEN", path=run_path,
                    reason=f"{path.name}: {problem}", profile_path=path)
    try:
        cfg = _prof.run_config(prof, strata_root=ROOT, components=COMPONENTS_DIR, log_path=engine_log)
        missing = _prof.missing_files(prof, ROOT)
    except (_prof.ProfileError, OSError) as e:
        cfg, missing = None, [str(e)]
    reason = ("missing " + "; ".join(missing)) if missing else \
        f"{path}  ·  {prof['drafting']}  ·  refusal vector {_prof.refusal_label(prof)}" + \
        ("  ·  read-only (verified): E then A saves a copy" if _prof.is_protected(path) else "  ·  Del moves it to _deleted")
    return Item(kind="profile", title=f"[profile] {prof['name']}", status="BROKEN" if missing or cfg is None else "READY",
                path=run_path, cfg=cfg, reason=reason, profile_path=path, profile=prof)


def _protected_model_missing(path: Path, prof: dict | None) -> bool:
    """A protected (verified) profile whose model files are gone: it cannot be deleted, so the menu hides it
    instead. The file itself is left untouched and the row comes back once the model files are restored."""
    if prof is None or not _prof.is_protected(path):
        return False
    try:
        return bool(_prof.missing_files(prof, ROOT))
    except (_prof.ProfileError, OSError):
        return False


def profile_items() -> list[Item]:
    seed_profiles()
    return [profile_item(p, prof, problem) for p, prof, problem in _prof.list_profiles(PROFILES_DIR)
            if not _protected_model_missing(p, prof)]


def write_run_config(item: Item) -> None:
    item.path.parent.mkdir(parents=True, exist_ok=True)
    Path(item.cfg["log"]).parent.mkdir(parents=True, exist_ok=True)
    tmp = item.path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(item.cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(item.path)


def update_report_page(report: dict) -> bool:
    """The engine changed since the last start: how Strata's own files differ from the profile. Enter starts with the
    profile unchanged, Esc cancels. Nothing is changed anywhere."""
    width = max(60, shutil.get_terminal_size((120, 40)).columns - 2)
    lines = [BOLD + YELLOW + f"Engine changed: {report['previous']} -> {report['version']}" + RESET, "",
             "Your profile is used as it is. Strata's own launch files now differ from it as follows",
             DIM + "(for review only: nothing was changed; Strata puts its drafter flags on at start, per its \"drafting\")" + RESET, ""]
    if not report["sources"]:
        lines.append("  No Strata file for this model was found.")
    for label, diffs in report["sources"]:
        lines.append(BOLD + "  " + label + RESET)
        lines += [("    " + d) for d in diffs] or [DIM + "    same as the profile" + RESET]
        lines.append("")
    lines += ["", "Enter Start with the profile   Esc Cancel"]
    request_clear()
    paint("\n".join(cell_clip(x, width) for x in lines) + "\n")
    while True:
        key = (read_key() or "").lower()
        if key == "enter":
            return True
        if key in ("esc", "b", "q"):
            return False


def start_profile(item: Item) -> None:
    prof = item.profile
    report = _upd.check(prof, strata_root=ROOT, components=COMPONENTS_DIR, state=STATE_DIR, record=False)
    if report["changed"] and not update_report_page(report):
        return
    if report["version"]:
        _upd.mark_seen(STATE_DIR, report["version"])
    port = prof["port"]
    live = health(port)
    if live:
        actual = live.get("model") or "unknown"
        log_page("ERROR" if actual != prof["model_name"] else "RUNNING",
                 f":{port} is already serving {actual}; stop it first to start this profile", port)
        return
    try:
        item.cfg = _prof.run_config(prof, strata_root=ROOT, components=COMPONENTS_DIR, log_path=Path(item.cfg["log"]))
        write_run_config(item)
    except (OSError, _prof.ProfileError) as e:
        log_page("ERROR", f"run config not written: {e}", port)
        return
    append_log(f"Profile {item.profile_path} -> {item.path} (engine options: {' '.join(_prof.engine_argv(prof, ROOT))})")
    runner = LoadRunner(item)
    log_page("LOADING", prof["model_name"], port,
             extra=f"profile {_prof.profile_id(item.profile_path)} · refusal vector {_prof.refusal_label(prof)}",
             sources=[LOG_FILE, Path(item.cfg["log"])], active=runner.alive, finished=runner.finish,
             banner=f"==== MODEL READY · http://127.0.0.1:{port} ====")


def _wrap_argv(argv: list[str], width: int) -> list[str]:
    out, cur = [], ""
    for g in _prof.groups(argv):
        part = " ".join(g)
        if cur and len(cur) + 1 + len(part) > width:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}".strip()
    return out + ([cur] if cur else [])


def profile_page(item: Item) -> None:
    """One profile: what it starts, the refusal vector, start / edit / save as."""
    notice = ""
    while True:
        try:
            prof = _prof.load(item.profile_path)
        except (OSError, ValueError) as e:
            log_page("ERROR", f"{item.profile_path.name}: {e}", None, sources=[])
            return
        item = profile_item(item.profile_path, prof)
        width = max(60, min(160, shutil.get_terminal_size((120, 40)).columns - 2))
        r = prof["refusal"]
        lines = [BOLD + "PROFILE  " + prof["name"] + RESET, DIM + str(item.profile_path) + RESET]
        if notice:
            lines.append(CYAN + notice + RESET)
        lines += ["", f"Model     {prof['model_name']}   port {prof['port']}   drafting {prof['drafting']}   "
                      f"vision {(prof.get('vision') or {}).get('mode', 'off')}",
                  f"Env       {' '.join(f'{k}={v}' for k, v in (prof.get('env') or {}).items()) or '-'}", "Options"]
        lines += ["  " + x for x in _wrap_argv(prof["args"], width - 4)]
        state = (GREEN + "ON" + RESET) if r["enabled"] else (DIM + "off" + RESET)
        vec = _prof.vector_path(r, ROOT)
        lines += ["", BOLD + "Refusal vector  " + RESET + state +
                  f"   strength x{float(r['scale']):g}   layers {r['layers'][0]}-{r['layers'][1]}   mode {r['mode']}  dir {r['dir']}",
                  f"  vector {'builtin: ' if r['vector'] == 'builtin' else ''}{vec}" + ("" if vec.is_file() else RED + "  (not found)" + RESET),
                  f"  per request: {'on unless a request sends experimental_speed_projection=false' if r['per_request_default'] else 'off unless a request sends experimental_speed_projection=true'}"]
        if r["enabled"]:
            lines.append(YELLOW + "  " + REFUSAL_WARNING + RESET)
        if item.status != "READY":
            lines += ["", RED + item.reason + RESET]
        lines += ["", "Enter Start   E Settings (all options)   T Refusal on/off   P Per-request default   A Save as   Esc Back",
                  DIM + "Changes are saved to the profile (the old file is kept as .json.bak) and apply at the next start." + RESET]
        request_clear()
        paint("\n".join(cell_clip(x, width) for x in lines) + "\n")
        key = (read_key() or "").lower()
        notice = ""
        new = json.loads(json.dumps(prof))
        nr = new["refusal"]
        if key in ("esc", "b", "q"):
            return
        if key == "enter":
            if item.status == "READY":
                start_profile(item)
                return
            notice = "not startable: " + item.reason
            continue
        if key in ("t", "p") and _prof.is_protected(item.profile_path):
            notice = "the verified profile is read-only: E opens its settings, A there saves your changes as a new profile"
            continue
        if key == "t":
            if not nr["enabled"] and not deepseek_confirm("Turn the refusal vector on?", REFUSAL_WARNING):
                continue
            nr["enabled"] = not nr["enabled"]
        elif key == "e":
            item = settings_page(item.profile_path) or item
            continue
        elif key == "p":
            nr["per_request_default"] = not nr["per_request_default"]
        elif key == "a":
            sys.stdout.write("\n")
            v = deepseek_input("New profile file name (letters, digits, . _ -; Esc cancels): ")
            if v is None:
                continue
            title = deepseek_input("Shown name (Enter keeps a copy of this one): ", max_len=120)
            copy = json.loads(json.dumps(prof))
            copy["name"] = title.strip() if title and title.strip() else prof["name"] + " (copy)"
            try:
                path = _prof.save_as(PROFILES_DIR, v.strip(), copy)
            except (_prof.ProfileError, OSError) as e:
                notice = str(e)
                continue
            item = profile_item(path, copy)
            notice = f"saved as {path.name}; now editing it"
            continue
        else:
            continue
        try:
            _prof.save(item.profile_path, new)
            notice = "saved"
        except (_prof.ProfileError, OSError) as e:
            notice = f"not saved: {e}"


# ---------------------------------------------------------------------------
# settings menu (settings_spec.py): every option of a profile, its verified default, what it does and costs

import settings_spec as _set        # noqa: E402


def profile_running(path: Path) -> int | None:
    """The port when the model on it was started by this TUI from this profile file, else None."""
    rt = runtime_load() or {}
    try:
        same = rt.get("profile") and Path(rt["profile"]).resolve() == Path(path).resolve()
    except OSError:
        same = False
    port = int(rt.get("port") or 0)
    return port if same and port and health(port) else None


def launch_command_lines(prof: dict, run_path: Path, width: int) -> list[str]:
    """What a start runs: the server (with its environment) and the engine command the server builds from the profile."""
    folder = _prof.active_engine_folder(COMPONENTS_DIR)
    exe = (folder / ("strata.exe" if os.name == "nt" else "strata")) if folder else Path("<engine not found>")
    env = {"STRATA_NO_PRESET": "1", **(prof.get("env") or {})}
    lines = [BOLD + "Environment" + RESET] + [f"  {k}={v}" for k, v in env.items()]
    lines += [BOLD + "Server" + RESET,
              f"  {normal_python()} {ROOT / 'serve' / 'server.py'} --engine strata --config {run_path} --port {prof['port']}",
              BOLD + "Engine (started by the server)" + RESET, f"  {exe} --serve"]
    lines += ["    " + x for x in _wrap_argv(_prof.engine_argv(prof, ROOT), width - 6)]
    if (prof.get("vision") or {}).get("mode", "off") != "off":
        v = prof["vision"]
        lines.append(f"  picture reader: {v['mode']}, up to {v.get('max_tokens', 300)} tokens a picture, mmproj {v['mmproj']}")
    if prof.get("drafting") == "dflash2":
        lines.append(DIM + "  (Strata picks the DFlash2 pack and writes --draft-fallback none itself; the live line checks the real "
                           "command after the start)" + RESET)
    return lines


def confirm_page(title: str, body: list[str], yes: str = "Enter Confirm", no: str = "Esc Cancel") -> bool:
    width = max(60, shutil.get_terminal_size((120, 40)).columns - 2)
    request_clear()
    paint("\n".join(cell_clip(x, width) for x in [BOLD + title + RESET, ""] + body + ["", f"{yes}   {no}"]) + "\n")
    while True:
        key = (read_key() or "").lower()
        if key in ("enter", "y"):
            return True
        if key in ("esc", "b", "n", "q"):
            return False


def delete_profile(item: Item | None) -> str:
    """Del on a profile row: after a confirmation, the file moves to <profiles>/_deleted/ (found again there). Returns the notice."""
    if item is None or item.kind != "profile" or item.profile_path is None:
        return "Del deletes a profile: select a [profile] row first"
    path = item.profile_path
    if _prof.is_protected(path):
        return f"{path.name} holds the verified settings and cannot be deleted"
    rt = runtime_load() or {}
    try:
        mine = bool(rt.get("profile")) and Path(rt["profile"]).resolve() == path.resolve()
    except OSError:
        mine = False
    if mine and health(int(rt.get("port") or 0)):
        return f"{path.name} is running on :{rt.get('port')}: stop it first (X), then delete it"
    if not confirm_page("Delete this profile?",
                        [f"File: {path}", f"It moves to: {path.parent / _prof.TRASH}\\ (with a time stamp; move it back to restore it)",
                         f"Name: {(item.profile or {}).get('name', path.stem)}"],
                        yes="Enter Delete", no="Esc Keep"):
        return "nothing deleted"
    try:
        dest = _prof.soft_delete(path)
    except (_prof.ProfileError, OSError) as e:
        return f"not deleted: {e}"
    append_log(f"Profile {path} moved to {dest}")
    return f"moved to {dest}"


def restart_profile(path: Path) -> None:
    """Stops the model this TUI started, then starts the profile again."""
    rt = runtime_load() or {}
    port = int(rt.get("port") or 0)
    request_clear()
    paint(BOLD + f"Stopping {rt.get('model_name') or 'Strata'} on :{port} ..." + RESET + "\n")
    stop_owned()
    for _ in range(40):
        if not health(port):
            break
        time.sleep(0.25)
    if health(port):
        log_page("ERROR", f"still running on :{port}; not restarted - see the log", port)
        return
    try:
        prof = _prof.load(path)
    except (OSError, ValueError) as e:
        log_page("ERROR", f"{path.name}: {e}", None, sources=[])
        return
    item = profile_item(path, prof)
    if item.status != "READY":
        log_page("ERROR", item.reason, None, sources=[])
        return
    start_profile(item)


def _settings_input(it, cur: str) -> str | None:
    """Asks for a value: a letter picks a preset, free text is a value (when the item takes one)."""
    letters = "abcdefgh"
    presets = "   ".join(f"[{letters[i]}] {c}" for i, c in enumerate(it.choices))
    allowed = ("type a value: " + it.hint) if it.free else ""
    sys.stdout.write("\n")
    v = deepseek_input(f"{it.label} (now {cur})  {presets}  {allowed}  Esc cancels: ", max_len=400)
    if v is None:
        return None
    v = v.strip()
    if len(v) == 1 and v.lower() in letters[:len(it.choices)]:
        return it.choices[letters.index(v.lower())]
    if not it.free and v not in it.choices:
        raise _set.Invalid(f"{it.label}: choose one of {', '.join(it.choices)}")
    return v


def settings_page(path: Path):
    """All of a profile's options. Edits stay in memory until S saves them (after the full launch command is shown)."""
    try:
        saved = _prof.load(path)
    except (OSError, ValueError) as e:
        log_page("ERROR", f"{Path(path).name}: {e}", None, sources=[])
        return None
    verified = _set.verified()
    prof = json.loads(json.dumps(saved))
    pos, notice, error = 0, "", ""
    while True:
        width = max(70, min(170, shutil.get_terminal_size((120, 40)).columns - 2))
        items = _set.visible(prof)
        pos = max(0, min(pos, len(items) - 1))
        dirty = not _set.same(prof, saved)
        head = [BOLD + "SETTINGS  " + prof["name"] + RESET + (YELLOW + "   * unsaved changes" + RESET if dirty else ""),
                DIM + f"{path}   ·   verified defaults (read-only): {_set.verified_file()}" + RESET, ""]
        # every row of the list (group headings too); `sel` is the row of the selected option
        rows, sel, group = [], 0, None
        short = lambda s, n: s if len(s) <= n else "…" + s[-(n - 1):]      # paths: keep the file name end
        for i, it in enumerate(items):
            if it.group != group:
                group = it.group
                rows.append(CYAN + group + RESET)
            cur, dflt = it.get(prof), it.get(verified)
            diff = cur != dflt
            mark = ">" if i == pos else " "
            val = (YELLOW + BOLD if diff else "") + f"{short(cur, 28):28}" + RESET
            tag = (DIM + "locked " + RESET) if it.locked else (YELLOW + "changed" + RESET if diff else "       ")
            if i == pos:
                sel = len(rows)
            rows.append(f"{BOLD if i == pos else ''}{mark} {_set.title(it)[:22]:22} {DIM}{it.label[:26]:26}{RESET} {tag} {val} "
                        f"{DIM}default {short(dflt, 22)}{RESET}")
        it = items[pos]
        foot = ["", BOLD + _set.title(it) + RESET + f" ({it.label})  " + it.desc]
        if it.locked:
            foot.append(DIM + "  locked: " + it.locked + RESET)
        else:
            opts = ", ".join(it.choices)
            foot.append(DIM + "  values: " + (opts + (" or " if it.free and opts else "") if opts else "") +
                        (it.hint if it.free else "") + RESET)
        if error:
            foot.append(RED + "  " + error + RESET)
        if notice:
            foot.append(CYAN + "  " + notice + RESET)
        foot += ["", "↑/↓ Select   Enter Change   D Restore this one   R Restore all   S Save   A Save as new profile   Esc Back"]
        # the list scrolls inside what the terminal can show, so the top rows are never pushed off screen
        height = shutil.get_terminal_size((120, 40)).lines
        room = max(5, height - len(head) - len(foot) - 3)       # 2 for the "more" markers, 1 so the last line does not scroll
        if len(rows) <= room:
            shown, above, below = rows, 0, 0
        else:
            top = min(max(0, sel - room // 2), len(rows) - room)
            if top > 0 and rows[top - 1].startswith(CYAN) and sel < top - 1 + room:   # keep a group heading with its first option
                top -= 1
            shown, above, below = rows[top:top + room], top, len(rows) - (top + room)
        body = ([DIM + f"  ↑ {above} more" + RESET] if above else [""]) + shown + \
               ([DIM + f"  ↓ {below} more" + RESET] if below else [""])
        lines = head + body + foot
        request_clear()
        paint("\n".join(cell_clip(x, width) for x in lines) + "\n")
        key = (read_key() or "").lower()
        notice, error = "", ""
        if key == "up":
            pos = (pos - 1) % len(items)
        elif key == "down":
            pos = (pos + 1) % len(items)
        elif key == "enter":
            if it.locked:
                error = "locked: " + it.locked
                continue
            try:
                text = _settings_input(it, it.get(prof))
                if text is None:
                    continue
                new = _set.apply(it, prof, text)
                if it.key == "refusal" and new["refusal"]["enabled"] and not prof["refusal"]["enabled"]:
                    if not deepseek_confirm("Turn the refusal vector on?", REFUSAL_WARNING):
                        continue
                prof = new
                notice = f"{it.label} = {it.get(prof)} (not saved yet)"
            except _set.Invalid as e:
                error = f"refused: {e}"
        elif key == "d":
            if it.locked:
                error = "locked: " + it.locked
                continue
            try:
                prof = _set.restore(it, prof, verified)
                notice = f"{it.label} back to the verified {it.get(prof)} (not saved yet)"
            except _set.Invalid as e:
                error = f"refused: {e}"
        elif key == "r":
            if confirm_page("Restore every option to the verified defaults?",
                            ["Changed now: " + (", ".join(_set.changed_items(prof, verified)) or "nothing")]):
                prof = _set.restore_all(prof, verified)
                notice = "all options back to the verified defaults (not saved yet)"
        elif key in ("s", "a"):
            try:
                _prof.validate(prof)
            except _prof.ProfileError as e:
                error = f"cannot save: {e}"
                continue
            target, new_name = path, None
            if key == "s" and _prof.is_protected(path):
                error = "the verified profile is read-only: A saves these settings as a new profile"
                continue
            if key == "a":
                sys.stdout.write("\n")
                fid = deepseek_input("New profile file name (letters, digits, . _ -; e.g. long-chat; Esc cancels): ")
                if fid is None:
                    continue
                title = deepseek_input("Shown name (e.g. Long chat; Enter = file name): ", max_len=120)
                new_name = (title or "").strip() or fid.strip()
                target = PROFILES_DIR / f"{fid.strip()}.json"
            out = json.loads(json.dumps(prof))
            if new_name:
                out["name"] = new_name
            run_path = profile_run_paths(_prof.profile_id(target))[0]
            body = [f"Profile file: {target}", "Changed from the verified defaults: " +
                    (", ".join(_set.changed_items(out, verified)) or "none"), ""] + launch_command_lines(out, run_path, width)
            if not confirm_page("Save these settings? A start will run:", body, yes="Enter Save"):
                continue
            try:
                if key == "a":
                    target = _prof.save_as(PROFILES_DIR, _prof.profile_id(target), out)
                else:
                    _prof.save(target, out)
            except (_prof.ProfileError, OSError) as e:
                error = f"not saved: {e}"
                continue
            path, saved, prof = target, out, json.loads(json.dumps(out))
            notice = f"saved to {target.name} (the old file is kept as .json.bak)"
            port = profile_running(target)
            if port is not None:
                if confirm_page("Restart the engine now?",
                                [f"{prof['model_name']} is running on :{port} from this profile with the old settings.",
                                 "The new settings apply only after a restart (the conversation cache is lost)."],
                                yes="Enter Restart now", no="Esc Later"):
                    restart_profile(target)
                    return profile_item(target, _prof.load(target))
                notice += "; the running engine keeps the old settings until it is restarted"
            elif health(prof["port"]):
                notice += f"; :{prof['port']} is serving a model this TUI did not start from this profile - stop it before starting this one"
        elif key in ("esc", "b", "q"):
            if dirty and not confirm_page("Leave without saving?", ["Changed (not saved): " +
                                          ", ".join(i.label for i in _set.ITEMS if i.get(prof) != i.get(saved))],
                                          yes="Enter Discard changes", no="Esc Keep editing"):
                continue
            try:
                return profile_item(path, _prof.load(path))
            except (OSError, ValueError):
                return None


class LiveStatus:
    """The main menu's live line: polled in the background every `interval` seconds, so a slow answer never holds a key."""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.lock = threading.Lock()
        self.value: tuple | None = None          # (port, health, status line, arg differences) or None when nothing runs
        self._logs: dict[tuple, str | None] = {}   # (port, model) -> engine log of a server started elsewhere (looked up once)
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _probe(self):
        rt = runtime_load() or {}
        ports = sorted({int(rt.get("port") or 8080), 8080})
        for port in ports:
            h = health(port)
            if not h:
                continue
            prof = None
            if rt.get("profile") and int(rt.get("port") or 0) == port:
                try:
                    prof = _prof.load(Path(rt["profile"]))
                except (OSError, ValueError):
                    prof = None
            engine_log = rt.get("engine_log") if int(rt.get("port") or 0) == port else None
            if not engine_log:                   # started elsewhere (Strata's Terminal or app): find its run config's log
                key = (port, h.get("model"))
                if key not in self._logs:
                    local = os.environ.get("LOCALAPPDATA")
                    folders = [STATE_DIR / "run", ROOT] + ([Path(local) / "Strata" / "launch"] if local else [])
                    self._logs[key] = _live.engine_log_of_server(port, folders)
                engine_log = self._logs[key]
            snap = _live.snapshot(port, engine_log, prof, ROOT if prof else None)
            return port, h, _live.status_line(snap), snap.get("diffs") or []
        return None

    def _run(self):
        while not self._stop.is_set():
            try:
                value = self._probe()
            except Exception:                    # noqa: BLE001 - the menu keeps working without a status line
                value = None
            with self.lock:
                self.value = value
            self._stop.wait(self.interval)

    def get(self):
        with self.lock:
            return self.value


LIVE: LiveStatus | None = None


def main():
    global LIVE
    enable_ansi()
    LIVE = LiveStatus()
    model_root = default_model_root()
    show_unsupported = False
    items, unsupported_count = all_items(model_root, show_unsupported)
    pos = 0
    notice = ""

    while True:
        pos = max(0, min(pos, max(0, len(items) - 1)))
        render_menu(items, pos, model_root, unsupported_count, show_unsupported, notice)
        key = (read_key(1.0) or "").lower()       # the running model's live line refreshes once a second
        if not key:
            continue                              # nothing pressed: repaint only if the live line changed
        notice = ""
        if key not in ("up", "down"):
            request_clear()                       # another page may be on screen when we come back

        if key == "up" and items:
            pos = (pos - 1) % len(items)
        elif key == "down" and items:
            pos = (pos + 1) % len(items)
        elif key == "enter" and items:
            item = items[pos]
            request_clear()
            if item.kind == "profile" and item.profile is not None:
                profile_page(item)
                request_clear()
            elif item.kind == "config" and item.status == "READY":
                start_via_options(item)
            elif item.status == "PREPARE":
                process = prepare(item)
                if process:
                    pname = f"{family_title(item.family)} {item.model}"
                    if item.variant:
                        pname += f" · {item.variant}"
                    extra = (ctx_label(item.context) if item.context else
                             'Backend default' if 'models' in official_families().get(item.family, {})
                             else ctx_label(DEFAULT_CONTEXT))
                    log_page(
                        "PREPARING", pname, None, extra=extra,
                        sources=[LOG_FILE],
                        active=lambda: process.poll() is None,
                        finished=lambda: (
                            ("PREPARED", pname, None)
                            if process.returncode == 0 else
                            ("ERROR", f"prepare failed (exit {process.returncode}) - see the log", None)
                        ),
                        hint="Preparing the model pack... this may take several minutes.",
                    )
                else:
                    log_page("ERROR", "prepare failed to start", None, sources=[LOG_FILE])
            else:
                log_page("ERROR", item.reason or f"{item.status}: no action available",
                         None, sources=[])
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key == "r":
            items, unsupported_count = all_items(model_root, show_unsupported)
            pos = 0
        elif key == "e":
            cur = items[pos] if items else None
            if cur is not None and cur.kind == "profile" and cur.profile is not None:
                settings_page(cur.profile_path)
                items, unsupported_count = all_items(model_root, show_unsupported)
            else:
                notice = "E opens the settings of a profile: select a [profile] row first"
        elif key == "del":
            cur = items[pos] if items else None
            notice = delete_profile(cur)
            items, unsupported_count = all_items(model_root, show_unsupported)
        elif key == "x":
            request_clear()
            rt = runtime_load() or {}
            expected = rt.get("model_name") or "Strata"
            stop_port = int(rt.get("port", 8080) or 8080)
            stop_thread = stop_in_background()
            log_page(
                "STOPPING", expected, None,
                sources=[LOG_FILE],
                active=stop_thread.is_alive,
                finished=lambda: (
                    ("STOPPED", expected, None)
                    if not health(stop_port) else
                    ("ERROR", "still running - see the log", stop_port)
                ),
                record_finished=False,
            )
            items, unsupported_count = all_items(model_root, show_unsupported)
        elif key == "c":
            open_strata("#chat", items)
        elif key == "m":
            open_strata("#monitor", items)
        elif key == "b":
            request_clear()
            target = None
            if items:
                cur = items[pos]
                if cur.kind == "config" and cur.family == "deepseek41" and cur.status == "READY":
                    target = cur
                else:
                    for cand in items:
                        if cand.kind == "config" and cand.family == "deepseek41" and cand.status == "READY":
                            target = cand
                            break
            if target is None:
                log_page("ERROR", "No installed READY DeepSeek 4.1 config to benchmark",
                         None, sources=[])
            else:
                benchmark_page(target)
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
