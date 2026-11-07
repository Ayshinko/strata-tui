"""What the running engine really does: read from Strata's /metrics and the engine log, never assumed from a config.

* the drafter: the engine log's own lines ("DFlash2 drafter on", "strata mtp: draft layer loaded"). /metrics has no drafter
  field (the web app's About page says "MTP drafts up to N tokens" for any --spec, DFlash2 included);
* the control vector: /metrics engine.cvec ("project:4-44", no scale) and the engine log's "control vector mode = ..." line;
  the scale is read from the command line the server logged;
* tok/s: /metrics live.tok_s while generating, the last request's decode_tok_s, and the run's average from the totals;
* the command line: the "engine started:" line the server writes into the engine log, compared flag by flag with the profile.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

import profiles as P

_START = re.compile(r"engine started: (\S+) --serve (.*)$")
_VERSION = re.compile(r"session is up \(engine ([0-9.]+)\)")
_DF2 = re.compile(r"DFlash2 drafter on\b")
_MTP = re.compile(r"strata mtp: draft layer loaded\b")
_CVEC = re.compile(r"control vector mode = (\w+), dir = (\S+), layers (\d+)\.\.(\d+) \((\d+) steered\)")
_SLOTS = re.compile(r"expert cache auto: .*-> (\d+) slots")
_FALLBACK = re.compile(r"Drafting: (MTP|off) \(([^)]*)\)")


def fetch_json(port: int, path: str, timeout: float = 0.6) -> dict | None:
    """GET http://127.0.0.1:<port><path>; STRATA_API_KEY is sent when it is set. None on any failure."""
    req = Request(f"http://127.0.0.1:{port}{path}")
    key = os.environ.get("STRATA_API_KEY", "").strip()
    if key and key.lower() != "none":
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_tail(path: Path | str | None, max_bytes: int = 4 << 20) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def last_start(text: str) -> str:
    """The engine log from its last "engine started:" line on ("" when there is none)."""
    i = text.rfind("engine started:")
    if i < 0:
        return ""
    return text[text.rfind("\n", 0, i) + 1:]


def parse_start(segment: str) -> dict:
    """What one engine start's log says: version, drafter, control vector, expert slots, the command line."""
    out: dict = {"version": None, "drafter": "unknown", "cvec": None, "slots": None, "argv": None, "exe": None}
    for line in segment.splitlines():
        m = _START.search(line)
        if m and out["argv"] is None:
            out["exe"] = m.group(1)
            out["argv"] = m.group(2).split()          # the server joins with spaces; paths with spaces would split (none here)
            continue
        m = _VERSION.search(line)
        if m:
            out["version"] = m.group(1)
        if _DF2.search(line):
            out["drafter"] = "dflash2"
        elif _MTP.search(line) and out["drafter"] != "dflash2":
            out["drafter"] = "mtp"
        m = _CVEC.search(line)
        if m:
            out["cvec"] = {"mode": m.group(1), "dir": m.group(2), "first": int(m.group(3)), "last": int(m.group(4)),
                           "steered": int(m.group(5))}
        m = _SLOTS.search(line)
        if m:
            out["slots"] = int(m.group(1))
    if out["drafter"] == "unknown" and out["version"] and out["argv"] is not None:
        if "--draft" not in out["argv"] and "--mtp" not in out["argv"]:
            out["drafter"] = "off"
    if out["argv"]:
        scaled = P.flag_value(out["argv"], "--control-vector-scaled")
        if scaled:
            out["cvec_scale"] = scaled.rsplit(":", 1)[-1]
    return out


def server_note(server_log_text: str) -> str:
    """The server's own "Drafting: MTP (reason)" line after the last start, if any."""
    seg = server_log_text[server_log_text.rfind("--- START"):] if "--- START" in server_log_text else server_log_text
    m = None
    for m in _FALLBACK.finditer(seg):
        pass
    return f"{m.group(1)} ({m.group(2)})" if m else ""


def compare_args(want: list[str], got: list[str]) -> list[str]:
    """The differences between the profile's engine options and the engine's real command line, one line each."""
    w = {g[0]: g[1:] for g in P.groups(want)}
    try:
        gg = P.groups(got)
    except P.ProfileError:
        return ["the engine's command line could not be read"]
    g = {x[0]: x[1:] for x in gg}
    out = []
    for flag in w:
        if flag not in g:
            out.append(f"missing {flag} {' '.join(w[flag])}".rstrip())
        elif [s.casefold() for s in w[flag]] != [s.casefold() for s in g[flag]]:
            out.append(f"changed {flag}: {' '.join(w[flag])} -> {' '.join(g[flag])}")
    for flag in g:
        if flag not in w:
            out.append(f"added {flag} {' '.join(g[flag])}".rstrip())
    return out


def metrics_view(m: dict | None) -> dict:
    """The parts of /metrics the TUI shows."""
    if not m:
        return {}
    eng = m.get("engine") or {}
    live = m.get("live") or {}
    reqs = m.get("requests") or []
    totals = m.get("totals") or {}
    avg = None
    if totals.get("output_tokens") and totals.get("decode_ms"):
        avg = round(totals["output_tokens"] / (totals["decode_ms"] / 1000.0), 1)
    last = reqs[0] if reqs else {}
    return {"state": live.get("state"), "tok_s": live.get("tok_s"), "last_tok_s": last.get("decode_tok_s"),
            "last_projection": last.get("projection"), "avg_tok_s": avg, "cvec": eng.get("cvec"),
            "spec": eng.get("spec"), "slots": eng.get("expert_slots"), "engine": eng.get("engine"),
            "vram_free_mib": eng.get("vram_free_mib")}


def engine_log_of_server(port: int, folders: list[Path]) -> str | None:
    """The engine log of a server this TUI did not start: /config names its run config file; the first of `folders` that has
    that file gives its "log". None when it cannot be told."""
    data = fetch_json(port, "/config") or {}
    name = data.get("file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    for folder in folders:
        try:
            cfg = json.loads((Path(folder) / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and cfg.get("log"):
            return str(cfg["log"])
    return None


def snapshot(port: int, engine_log: Path | str | None, profile: dict | None = None,
             strata_root: Path | None = None) -> dict:
    """Everything the status line needs in one dict (no exception escapes)."""
    try:
        start = parse_start(last_start(read_tail(engine_log)))
    except Exception:
        start = {"drafter": "unknown"}
    view = metrics_view(fetch_json(port, "/metrics"))
    diffs: list[str] = []
    compared = bool(profile is not None and strata_root is not None and start.get("argv"))
    if compared:
        try:
            want, got = P.engine_argv(profile, strata_root), start["argv"]
            if profile.get("drafting") == "auto":       # the drafter's options are Strata's choice there, not a difference
                want, got = P.without(want, P.DRAFTER_OWNED), P.without(got, P.DRAFTER_OWNED)
            diffs = compare_args(want, got)
        except Exception as e:  # noqa: BLE001
            diffs = [f"compare failed: {e}"]
    return {"start": start, "metrics": view, "diffs": diffs, "compared": compared}


def status_line(snap: dict) -> str:
    """One plain line: drafter, control vector, speed, slots, and whether the command line matches the profile."""
    s, m = snap.get("start") or {}, snap.get("metrics") or {}
    drafter = {"dflash2": "DFlash2", "mtp": "MTP", "off": "no drafter"}.get(s.get("drafter"), "drafter ?")
    cv = s.get("cvec")
    if cv:
        cvec = f"refusal-vector ON x{s.get('cvec_scale', '?')} L{cv['first']}-{cv['last']}"
    elif m.get("cvec") not in (None, "0", 0):
        cvec = f"refusal-vector ON ({m['cvec']})"
    elif s.get("version") or m:
        cvec = "refusal-vector off"
    else:
        cvec = ""
    speed = []
    if m.get("tok_s"):
        speed.append(f"{m['tok_s']} tok/s now")
    if m.get("last_tok_s"):
        speed.append(f"last {m['last_tok_s']}")
    if m.get("avg_tok_s"):
        speed.append(f"avg {m['avg_tok_s']}")
    slots = m.get("slots") or s.get("slots")
    parts = [f"engine {s.get('version') or m.get('engine') or '?'}", drafter, cvec,
             " · ".join(speed) or "no requests yet", f"{slots} slots" if slots else ""]
    diffs = snap.get("diffs") or []
    parts.append(f"{len(diffs)} arg difference(s) from the profile" if diffs else ("args = profile" if snap.get("compared") else ""))
    return "  ".join(p for p in parts if p)
