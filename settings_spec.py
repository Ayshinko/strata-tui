"""The settings menu's items: each one's engine option, allowed values, verified default, and what it does and costs.

Every name, value and limit below was read from the Strata checkout (engine help and parser in src/program/generate.cpp,
docs/DETAILS.md, setup.py, serve/server.py, strata_control/drafting.py). Where the source gives no limit, the range is the
one the owner chose (2026-11-05) and is marked "owner range".

The verified defaults are the shipped default profile (profiles-default/qwen3.8-iq3s-dflash2.json), never edited here.
Edits change a copy of a profile dict; the TUI saves it to the profile file (never to Strata's files).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import profiles as P

HERE = Path(__file__).resolve().parent
#: The shipped template of the verified settings; the copy the menu compares with is the read-only verified_file().
TEMPLATE_FILE = HERE / "profiles-default" / "qwen3.8-iq3s-dflash2.json"
VERIFIED_FILE = TEMPLATE_FILE        # kept for the menu's header line; see verified_file()


def verified_file() -> Path:
    """The read-only verified settings (<verified dir>/qwen3.8-iq3s-dflash2.json), made once from the template.
    The file is the reference: an edit to the template later does not change it."""
    try:
        import manager_config
        folder = manager_config.verified_dir()
    except Exception:  # noqa: BLE001 - no manager.env (tests elsewhere): the template is the reference
        return TEMPLATE_FILE
    path = folder / TEMPLATE_FILE.name
    if not path.is_file():
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(TEMPLATE_FILE.read_bytes())
            P.set_readonly(path)
        except OSError:
            return TEMPLATE_FILE
    return path

#: The MTP drafter's options as Strata's setup writes them (setup.py); not verified by the owner on this PC.
MTP_SETUP = {"--spec": "4", "--spec-min-p": "0.5"}
#: Flags the drafter choice owns (strata_control/drafting.py DRAFT_FLAGS and MTP_ONLY_FLAGS).
DRAFT_FLAGS = ("--draft", "--draft-model", "--draft-fallback", "--draft-conf", "--draft-cap", "--draft-rule",
               "--draft-mtp-spec", "--draft-mtp-min-p")
MTP_FLAGS = ("--mtp", "--mtp-q4", "--mtp-hnorm", "--mtp-window", "--mtp-draft-vocab", "--spec-min-p")

CONTEXT_MAX = 262144          # docs/DETAILS.md: trained on 262,144 positions; more needs rope scaling, which the TUI does not manage
CONTEXT_MIN = 4096            # the engine's own default --max-context (generate.cpp help)
KV_RESIDENT_MIN = 20480       # engine help "(min 20480)" (owner chose the help text over the code's 4096)
REFUSAL_LAST_LAYER = 47       # data/experimental-speed-projection/README.md: direction.1 .. direction.47


class Invalid(ValueError):
    """A value the item refuses, with the reason."""


def verified() -> dict:
    return P.load(verified_file())


# ------------------------------------------------------------------------------------------------- argv helpers

def get_flag(prof: dict, flag: str) -> str | None:
    return P.flag_value(prof["args"], flag)


def has_flag(prof: dict, flag: str) -> bool:
    return any(g[0] == flag for g in P.groups(prof["args"]))


def set_flag(prof: dict, flag: str, value: str | None) -> None:
    """Sets ``flag value`` in place of the old group (kept at its position), appends it when new, removes it on None.
    ``value == ""`` sets a flag without a value."""
    out, done = [], False
    for g in P.groups(prof["args"]):
        if g[0] != flag:
            out.append(g)
        elif value is not None and not done:
            out.append([flag] + ([value] if value != "" else []))
            done = True
    if value is not None and not done:
        out.append([flag] + ([value] if value != "" else []))
    prof["args"] = [t for g in out for t in g]


def _int(text: str, what: str) -> int:
    try:
        return int(str(text).strip().replace(",", "").replace("_", ""))
    except ValueError:
        raise Invalid(f"{what}: '{text}' is not a whole number") from None


def _float(text: str, what: str) -> float:
    try:
        return float(str(text).strip())
    except ValueError:
        raise Invalid(f"{what}: '{text}' is not a number") from None


# ------------------------------------------------------------------------------------------------- items

@dataclass
class Item:
    key: str
    group: str
    label: str                      # the option as Strata names it
    desc: str                       # what it does and what it costs
    get: Callable[[dict], str]      # the value shown (and compared with the verified default)
    put: Callable[[dict, str], None] | None = None   # sets a value (raises Invalid); None: shown only
    choices: tuple = ()             # presets; free input is allowed when `free`
    free: bool = False
    hint: str = ""                  # allowed values, for the input prompt
    locked: str = ""                # why it cannot be changed here
    when: Callable[[dict], bool] = field(default=lambda p: True)    # shown only in this state
    restore: Callable[[dict, dict], None] | None = None             # put the verified value back (default: put(get(verified)))


def _flag_item(key, group, flag, desc, *, choices=(), free=False, hint="", check=None, when=lambda p: True, unset="(not set)"):
    def get(p):
        v = get_flag(p, flag)
        return unset if v is None else v

    def put(p, text):
        text = str(text).strip()
        value = check(text, p) if check else text
        set_flag(p, flag, value)

    def restore(p, v):
        set_flag(p, flag, get_flag(v, flag))

    return Item(key, group, flag, desc, get, put, choices, free, hint, when=when, restore=restore)


def _check_context(text, p):
    n = _int(text, "--max-context")
    if not CONTEXT_MIN <= n <= CONTEXT_MAX:
        raise Invalid(f"--max-context must be {CONTEXT_MIN}-{CONTEXT_MAX} (above {CONTEXT_MAX} needs rope scaling, not managed here)")
    kr = get_flag(p, "--kv-resident")
    if kr and kr.isdigit() and int(kr) > n:
        raise Invalid(f"--kv-resident ({kr}) is larger than this context; lower it first")
    return str(n)


def _check_kv_resident(text, p):
    n = _int(text, "--kv-resident")
    ctx = int(get_flag(p, "--max-context") or 4096)
    if n != 0 and not KV_RESIDENT_MIN <= n <= ctx:
        raise Invalid(f"--kv-resident must be 0 (all of the K/V in VRAM) or {KV_RESIDENT_MIN}-{ctx} (the context)")
    if n != 0 and has_flag(p, "--kv-grow"):
        raise Invalid("--kv-grow is on, and the engine runs it only with --kv-resident 0: turn --kv-grow off first")
    return str(n)


def _set_kv_grow(p, text):
    on = _onoff(text, "--kv-grow")
    kr = get_flag(p, "--kv-resident")
    if on and kr not in (None, "0"):
        raise Invalid(f"--kv-grow needs --kv-resident 0 (the whole K/V in VRAM); with --kv-resident {kr} the engine turns it off "
                      "by itself (generate.cpp: 'kv-grow is off')")
    set_flag(p, "--kv-grow", "" if on else None)


def _check_kv(text, p):
    if text not in ("fp16", "int8", "q4_0", "k8v4"):
        raise Invalid("--kv takes fp16, int8, q4_0 or k8v4 (the engine refuses anything else)")
    return text


def _check_prefill(text, p):
    if text in ("auto", "auto:16384", "auto:32768"):
        return text
    n = _int(text, "--prefill")
    if not 1024 <= n <= 32768:
        raise Invalid("--prefill: auto, auto:16384, auto:32768, or a chunk of 1024-32768 tokens (owner range)")
    return str(n)


def _check_reserve(text, p):
    n = _int(text, "--vram-reserve-mib")
    if not 0 <= n <= 4096:
        raise Invalid("--vram-reserve-mib must be 0-4096 MiB (owner range)")
    return str(n)


def _check_expert_cache(text, p):
    if text == "auto":
        return text
    n = _int(text, "--expert-cache")
    if n < 0:
        raise Invalid("--expert-cache takes auto or a number of experts >= 0 (0 = none on the GPU)")
    return str(n)


def _check_spec(text, p):
    n = _int(text, "--spec")
    if not 2 <= n <= 8:
        raise Invalid("--spec must be 2-8 (the engine refuses a window above 8 tokens)")
    return str(n)


def _check_conf(text, p):
    x = _float(text, "--draft-conf")
    if not 0 <= x <= 1:
        raise Invalid("--draft-conf must be 0-1 (0 = use --spec-min-p)")
    return f"{x:g}"


def _check_min_p(text, p):
    x = _float(text, "--spec-min-p")
    if not 0 <= x <= 1:
        raise Invalid("--spec-min-p must be 0-1")
    return f"{x:g}"


def _check_file(flag):
    def check(text, p):
        text = text.strip().strip('"')
        if not Path(text).is_file():
            raise Invalid(f"{flag}: no such file: {text}")
        return text
    return check


# --- the drafter choice: rewrites the drafter's own flags as a group

def drafting_of(p: dict) -> str:
    return p.get("drafting", "auto")


def set_drafting(p: dict, mode: str, v: dict | None = None) -> None:
    """dflash2 / mtp / off, with the drafter's options of that choice. A switch stashes the options it removes in the profile
    ("drafter_options"), so switching back brings the person's own values back; else the verified ones (dflash2) or setup's (mtp)."""
    if mode not in ("dflash2", "mtp", "off"):
        raise Invalid("the drafter is dflash2, mtp or off")
    v = v or verified()
    old = drafting_of(p)
    stash = p.setdefault("drafter_options", {})
    owned = set(DRAFT_FLAGS) | set(MTP_FLAGS)
    if old in ("dflash2", "mtp"):
        stash[old] = [t for g in P.groups(p["args"]) if g[0] in owned or (g[0] == "--spec") for t in g]
    spec_now = get_flag(p, "--spec")
    p["args"] = [t for g in P.groups(p["args"]) if g[0] not in owned for t in g]
    if mode == "dflash2":
        src = stash.get("dflash2") or [t for g in P.groups(v["args"]) if g[0] in owned or g[0] == "--spec" for t in g]
    elif mode == "mtp":
        vocab = get_flag(v, "--mtp-draft-vocab")
        src = stash.get("mtp") or ["--mtp", str(Path(vocab).parent), "--spec", MTP_SETUP["--spec"],
                                   "--spec-min-p", MTP_SETUP["--spec-min-p"]]
    else:
        src = ["--spec", spec_now] if spec_now else []
    for g in P.groups(src):
        if g[0] == "--spec":
            set_flag(p, "--spec", g[1])
        else:
            p["args"] += g
    if mode == "off" and spec_now and not has_flag(p, "--spec"):
        set_flag(p, "--spec", spec_now)
    p["drafting"] = mode


# --- vision

def vision_mode(p: dict) -> str:
    return (p.get("vision") or {}).get("mode", "off")


def set_vision_on(p: dict, text: str, v: dict | None = None) -> None:
    on = _onoff(text, "--vision")
    vis = p.setdefault("vision", {})
    if on:
        if vis.get("mode", "off") == "off":
            vv = (v or verified()).get("vision") or {}
            vis["mode"] = vis.get("last_mode") or "cpu"
            vis.setdefault("mmproj", vv.get("mmproj"))
            vis.setdefault("threads", vv.get("threads", 6))
            vis.setdefault("max_tokens", vv.get("max_tokens", 300))
        if not vis.get("mmproj"):
            raise Invalid("no mmproj file is set for the picture reader")
        set_flag(p, "--vision", "")
    else:
        if vis.get("mode", "off") != "off":
            vis["last_mode"] = vis["mode"]
        vis["mode"] = "off"
        set_flag(p, "--vision", None)


def _onoff(text: str, what: str) -> bool:
    t = str(text).strip().lower()
    if t in ("on", "1", "true", "yes", "开"):
        return True
    if t in ("off", "0", "false", "no", "关"):
        return False
    raise Invalid(f"{what}: on or off")


def set_vision_device(p: dict, text: str) -> None:
    if vision_mode(p) == "off":
        raise Invalid("turn --vision on first")
    if text not in ("cpu", "gpu"):
        raise Invalid("the picture reader runs on cpu or gpu")
    p["vision"]["mode"] = text


def set_vision_tokens(p: dict, text: str) -> None:
    n = _int(text, "image tokens")
    if not 0 <= n <= 4096:
        raise Invalid("image tokens must be 0-4096 (0 = no limit; owner range)")
    p.setdefault("vision", {})["max_tokens"] = n


# --- environment

def _route_prior_put(p: dict, text: str) -> None:
    x = _float(text, "STRATA_ROUTE_PRIOR")
    if not 0 <= x <= 2.0:
        raise Invalid("STRATA_ROUTE_PRIOR must be 0-2.0 (0 = off; owner range)")
    p.setdefault("env", {})["STRATA_ROUTE_PRIOR"] = f"{x:g}"


# --- refusal vector

def _refusal(p):
    return {**P.REFUSAL_DEFAULT, **(p.get("refusal") or {})}


def set_refusal_on(p, text):
    p["refusal"] = {**_refusal(p), "enabled": _onoff(text, "refusal vector")}


def set_refusal_scale(p, text):
    t = str(text).strip().lower()
    if t in ("off", "关", "0"):
        p["refusal"] = {**_refusal(p), "enabled": False}
        return
    x = _float(text, "strength")
    if not 0 < x <= 1.0:
        raise Invalid("strength must be above 0 and at most 1.0 (0 or 'off' turns the vector off)")
    p["refusal"] = {**_refusal(p), "scale": x}


def set_refusal_vector(p, text):
    t = str(text).strip().strip('"')
    if t != "builtin" and not Path(t).is_file():
        raise Invalid(f"no such file: {t} (or 'builtin' for Strata's own vector)")
    p["refusal"] = {**_refusal(p), "vector": t or "builtin"}


def set_refusal_layers(p, text):
    parts = str(text).replace("-", " ").replace(",", " ").split()
    if len(parts) != 2:
        raise Invalid("layers: two numbers, first and last (e.g. 4 44)")
    a, b = _int(parts[0], "first layer"), _int(parts[1], "last layer")
    if not 1 <= a <= b <= REFUSAL_LAST_LAYER:
        raise Invalid(f"layers must satisfy 1 <= first <= last <= {REFUSAL_LAST_LAYER} (the vector has direction.1-47)")
    p["refusal"] = {**_refusal(p), "layers": [a, b]}


def set_port(p, text):
    n = _int(text, "port")
    if not 1024 <= n <= 65535:
        raise Invalid("port must be 1024-65535")
    p["port"] = n


def _is(mode):
    return lambda p: drafting_of(p) == mode


def _copy_key(path):
    def restore(p, v):
        cur, src = p, v
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
            src = src.get(k) or {}
        if path[-1] in src:
            cur[path[-1]] = copy.deepcopy(src[path[-1]])
        else:
            cur.pop(path[-1], None)
    return restore


ITEMS: list[Item] = [
    # 一 上下文与 KV
    _flag_item("max_context", "1 Context & KV", "--max-context",
               "最长对话（token）。越大能装的对话越长，但 KV 占的显存/内存越多，专家缓存槽位越少。",
               choices=("32768", "65536", "131072", "262144"), free=True, hint=f"{CONTEXT_MIN}-{CONTEXT_MAX}", check=_check_context),
    _flag_item("kv", "1 Context & KV", "--kv",
               "KV 存储精度。int8 是 fp16 的一半显存；q4_0 再减半、精度略低；k8v4 = int8 K + q4_0 V（816 B/格，int8 是 1,056）。",
               choices=("fp16", "int8", "q4_0", "k8v4"), check=_check_kv),
    _flag_item("kv_resident", "1 Context & KV", "--kv-resident",
               "每个 QSA 层留在显存的 KV 格数，其余放锁页内存；省下的显存给专家缓存。0 = 全部放显存。太小时长对话要从内存读 KV。",
               free=True, hint=f"0 or {KV_RESIDENT_MIN}-<max-context>", check=_check_kv_resident, unset="0 (not set)"),
    Item("kv_grow", "1 Context & KV", "--kv-grow",
         "KV 只为用到的格子占显存，其余先给专家缓存，随对话变长再还回去。引擎只在单卡、有 --expert-profile、且 --kv-resident 0（KV 全放显存）时启用，否则自己关掉。",
         lambda p: "on" if has_flag(p, "--kv-grow") else "off", _set_kv_grow, choices=("off", "on"),
         restore=lambda p, v: set_flag(p, "--kv-grow", "" if has_flag(v, "--kv-grow") else None)),
    _flag_item("prefill", "1 Context & KV", "--prefill",
               "读提示时的分块大小。auto = 专家缓存能借出的最大块（上限 8192）；auto:16384/32768 允许更大块，读长提示更快但借走更多专家槽位。",
               choices=("auto", "auto:16384", "auto:32768"), free=True, hint="auto | auto:16384 | auto:32768 | 1024-32768",
               check=_check_prefill),
    # 二 视觉
    Item("vision", "2 Vision", "--vision",
         "接收图片。开着时图像编码器常驻（CPU 版占内存），关掉省资源但不能看图。",
         lambda p: "on" if vision_mode(p) != "off" else "off", set_vision_on, choices=("on", "off"),
         restore=lambda p, v: set_vision_on(p, "on" if vision_mode(v) != "off" else "off", v)),
    Item("vision_device", "2 Vision", "vision reader",
         "图像编码器跑在 CPU 还是 GPU。24 GB 以下的 N 卡默认 CPU：GPU 编码器约占 850 个专家槽位（约 5% 解码速度），CPU 编码每张图更慢。",
         lambda p: vision_mode(p), set_vision_device, choices=("cpu", "gpu"),
         when=lambda p: vision_mode(p) != "off",
         restore=lambda p, v: set_vision_device(p, vision_mode(v)) if vision_mode(p) != "off" else None),
    Item("vision_tokens", "2 Vision", "image tokens (vision.max_tokens)",
         "一张图最多变成多少 token。默认 CPU 300 / GPU 1024；越多细节越清楚（小字、图表），编码越慢、占上下文越多。0 = 不限。",
         lambda p: str((p.get("vision") or {}).get("max_tokens", "300 (cpu default)")), set_vision_tokens,
         choices=("300", "512", "1024"), free=True, hint="0-4096 (0 = no limit)",
         when=lambda p: vision_mode(p) != "off", restore=_copy_key(("vision", "max_tokens"))),
    # 三 投机解码
    Item("drafting", "3 Drafting", "drafter",
         "猜测后续 token 的方式。dflash2 = 块草稿器；mtp = 模型自带的草稿层（参数取 Strata setup 的默认值，未经你验证）；off = 不用草稿器（仍做 prompt lookup）。",
         drafting_of, lambda p, t: set_drafting(p, t), choices=("dflash2", "mtp", "off"),
         restore=lambda p, v: set_drafting(p, drafting_of(v), v)),
    Item("draft_model", "3 Drafting", "--draft-model",
         "DFlash2 的草稿器文件。", lambda p: get_flag(p, "--draft-model") or "(not set)", when=_is("dflash2"),
         locked="Strata 选 dflash2 时会按数据目录自己挑选草稿包（drafting.py pack_path），这里改了也会被它换掉；启动后状态行会核对实际用的包。"),
    _flag_item("mtp_draft_vocab", "3 Drafting", "--mtp-draft-vocab",
               "草稿头使用的 token 子集文件（DFlash2 需要）。", free=True, hint="an existing file",
               check=_check_file("--mtp-draft-vocab"), when=_is("dflash2")),
    _flag_item("spec", "3 Drafting", "--spec",
               "每次验证最多猜几个 token。大一点猜中时更快，猜错时白算更多。",
               choices=("4", "6", "8"), free=True, hint="2-8", check=_check_spec, when=lambda p: drafting_of(p) != "off"),
    _flag_item("draft_conf", "3 Drafting", "--draft-conf",
               "DFlash2 的信心门槛：猜测的累计概率低于它就截断窗口。高 = 窗口短、命中率高；低 = 窗口长、白算多。0 = 用 --spec-min-p。",
               choices=("0.35", "0.5", "0.8"), free=True, hint="0-1", check=_check_conf, when=_is("dflash2")),
    Item("draft_fallback", "3 Drafting", "--draft-fallback",
         "DFlash2 启动失败时是否退回 MTP。", lambda p: get_flag(p, "--draft-fallback") or "(not set)", when=_is("dflash2"),
         locked="经 Strata 的 server 启动时，选 dflash2 一律写 --draft-fallback none（drafting.py engine_flags），mtp 无法通过它生效。"),
    _flag_item("spec_min_p", "3 Drafting", "--spec-min-p",
               "MTP 草稿层把窗口再延长一格所需的把握。高 = 窗口短、命中率高。", free=True, hint="0-1",
               check=_check_min_p, when=_is("mtp")),
    # 四 显存与专家
    _flag_item("vram_reserve", "4 VRAM & experts", "--vram-reserve-mib",
               "给其他程序/驱动留的显存。留得多更稳（不容易显存吃满变慢），但专家缓存槽位更少。",
               free=True, hint="0-4096", check=_check_reserve),
    _flag_item("expert_cache", "4 VRAM & experts", "--expert-cache",
               "放在显存里的专家数。auto = 填满剩余显存；固定数字可留出显存给别的程序。",
               choices=("auto",), free=True, hint="auto or a number >= 0 (format checked only)", check=_check_expert_cache),
    Item("route_prior", "4 VRAM & experts", "STRATA_ROUTE_PRIOR",
         "路由偏向已在显存里的专家（环境变量）。小显存卡解码更快；Strata 文档提到长 agent 会话可能出现重复、结果变弱（#4402）。0 = 关。",
         lambda p: (p.get("env") or {}).get("STRATA_ROUTE_PRIOR", "(not set = off)"), _route_prior_put,
         choices=("0", "1.5", "2.0"), free=True, hint="0-2.0",
         restore=_copy_key(("env", "STRATA_ROUTE_PRIOR"))),
    # 五 拒答控制
    Item("refusal", "5 Refusal vector", "refusal vector",
         "去掉模型的拒答方向（Strata 自带的 experimental speed projection）。模型拒答大幅减少——这是去掉一项安全行为，输出由你负责；每 token 约慢 0.2-0.4%。",
         lambda p: "on" if _refusal(p)["enabled"] else "off", set_refusal_on, choices=("off", "on"),
         restore=lambda p, v: set_refusal_on(p, "on" if _refusal(v)["enabled"] else "off")),
    Item("refusal_vector", "5 Refusal vector", "vector file",
         "向量来源：builtin = Strata 自带（data/experimental-speed-projection/），或外部 controlvector GGUF。",
         lambda p: _refusal(p)["vector"], set_refusal_vector, choices=("builtin",), free=True, hint="builtin or a file path",
         restore=lambda p, v: set_refusal_vector(p, _refusal(v)["vector"])),
    Item("refusal_scale", "5 Refusal vector", "strength",
         "去掉多少拒答方向：1.0 全部去掉，0.5 去掉一半。越大拒答越少、对普通回答影响也越大。",
         lambda p: f"{float(_refusal(p)['scale']):g}", set_refusal_scale, choices=("0.5", "0.75", "1.0"), free=True,
         hint="above 0 to 1.0 (0/off turns it off)", restore=lambda p, v: set_refusal_scale(p, str(_refusal(v)["scale"]))),
    Item("refusal_layers", "5 Refusal vector", "--control-vector-layer-range",
         "在哪些层后去掉该方向。自带向量的文档设置是 4-44；范围越大效果越强、对普通回答影响也越大。",
         lambda p: "{} {}".format(*_refusal(p)["layers"]), set_refusal_layers, free=True, hint=f"first last, 1-{REFUSAL_LAST_LAYER}",
         restore=lambda p, v: set_refusal_layers(p, "{} {}".format(*_refusal(v)["layers"]))),
    # 六 其他
    Item("port", "6 Other", "port", "API 端口。改了之后客户端（以及 manager.env 的 STRATA_API）要用新端口。",
         lambda p: str(p["port"]), set_port, choices=("8080",), free=True, hint="1024-65535",
         restore=lambda p, v: set_port(p, str(v["port"]))),
]


def visible(prof: dict) -> list[Item]:
    return [i for i in ITEMS if i.when(prof)]


def differs(item: Item, prof: dict, v: dict) -> bool:
    return item.get(prof) != item.get(v)


def apply(item: Item, prof: dict, text: str) -> dict:
    """A copy of the profile with the item set to `text` (raises Invalid). The whole profile is validated too."""
    if item.locked or item.put is None:
        raise Invalid(item.locked or "this item is shown only")
    new = copy.deepcopy(prof)
    item.put(new, text)
    try:
        P.validate(new)
    except P.ProfileError as e:
        raise Invalid(str(e)) from None
    return new


def restore(item: Item, prof: dict, v: dict | None = None) -> dict:
    v = v or verified()
    new = copy.deepcopy(prof)
    if item.locked:
        return new
    if item.restore:
        item.restore(new, v)
    elif item.put:
        item.put(new, item.get(v))
    return new


def restore_all(prof: dict, v: dict | None = None) -> dict:
    """The verified settings, keeping this profile's name. (The model files and anything not in the menu come back too.)"""
    v = v or verified()
    new = copy.deepcopy(v)
    new["name"] = prof["name"]
    return new


def changed_items(prof: dict, v: dict | None = None) -> list[str]:
    v = v or verified()
    return [i.label for i in ITEMS if i.when(prof) or i.when(v) if i.get(prof) != i.get(v)]


def same(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
