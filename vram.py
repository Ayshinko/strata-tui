"""VRAM Optimize for the Strata TUI (pure helpers, stdlib only).

Nothing here imports the official checkout (no setup.py, no gguf_reader), so the
module is unit-testable in isolation.  The Strata model config (a strata-*.json in
STRATA_ROOT) stays the source of truth: values are read from and written back to
its "args" array.

The KV-resident screen holds one hard invariant:

    KV in VRAM + KV in RAM = Context

Only KV in VRAM is passed to Strata (--kv-resident).  KV in RAM is a TUI-level
derivation and is never passed anywhere.  --kv int8 and --expert-cache auto are
left untouched by this screen.

The "estimated experts" figure is a *pre-load estimate* only.  It is calibrated
from the most recent load of the SAME model's Strata log (the engine's own
"expert cache auto: ... -> N slots" and "expert cache N slots, X GiB of VRAM"
lines) and then re-scaled for the current VRAM reserve and KV residency.  When no
calibration baseline exists it reports unavailable instead of inventing a number.
"""

from __future__ import annotations

import re
from pathlib import Path

MIB = 1024 * 1024

# The KV geometry this screen reasons about is the qwen4exp QSA shape Strata
# documents (kv_q8.hpp / kv_q4.hpp): 2 KV heads x 256 dim across 12 QSA layers.
# Bytes per token (one context position across all layers), per --kv format.
KV_INT8_BYTES_PER_TOKEN = 12_672     # 1056 B/cell x 12 layers (the code in use)
KV_Q4_BYTES_PER_TOKEN = 6_912        # 576 B/cell x 12
KV_K8V4_BYTES_PER_TOKEN = 9_792      # 816 B/cell x 12
KV_F16_BYTES_PER_TOKEN = 24_576      # 2048 B/cell x 12

KV_BYTES_BY_FORMAT = {
    "int8": KV_INT8_BYTES_PER_TOKEN,
    "q4_0": KV_Q4_BYTES_PER_TOKEN,
    "k8v4": KV_K8V4_BYTES_PER_TOKEN,
    "f16": KV_F16_BYTES_PER_TOKEN,
    "fp16": KV_F16_BYTES_PER_TOKEN,
}

# The official streaming default setup.py writes for a >=64K context.
KV_STREAMING_DEFAULT = 32768
KV_STREAMING_FROM = 65536

_ETA_CACHE_AUTO = re.compile(
    r"expert cache auto:\s+([\d.]+) GiB free,\s*(\d+) MiB reserved "
    r"\(\+(\d+) MiB for the draft head\) ->\s*(\d+) slots")
_ETA_CACHE_FINAL = re.compile(r"expert cache\s+(\d+) slots,\s*([\d.]+) GiB of VRAM")


def arg_value(args: list, key: str):
    try:
        i = args.index(key)
        return args[i + 1] if i + 1 < len(args) else None
    except ValueError:
        return None


def _int(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- reads

def model_context(cfg: dict) -> int | None:
    """The model's max context (read-only here), from --max-context."""
    return _int(arg_value(cfg.get("args") or [], "--max-context"))


def model_kv_format(cfg: dict) -> str:
    """The --kv format (e.g. int8), defaulting to int8 when absent."""
    return (arg_value(cfg.get("args") or [], "--kv") or "int8").lower()


def default_kv_vram(context) -> int:
    """KV in VRAM when the config has no --kv-resident: Strata keeps it all in
    VRAM below 64K; from 64K up setup's streaming default is 32768."""
    ctx = _int(context, 0) or 0
    if ctx >= KV_STREAMING_FROM:
        return KV_STREAMING_DEFAULT
    return ctx


def model_reserve_mib(cfg: dict, default: int = 512) -> int:
    """The config's --vram-reserve-mib, or the UI default 512 when absent."""
    v = _int(arg_value(cfg.get("args") or [], "--vram-reserve-mib"), default)
    return v if (v is not None and v >= 0) else default


def model_kv_vram(cfg: dict, context=None) -> int:
    """KV in VRAM tokens (from --kv-resident; a fallback default when absent)."""
    v = _int(arg_value(cfg.get("args") or [], "--kv-resident"))
    if v is not None:
        return v
    ctx = context if context is not None else model_context(cfg)
    return default_kv_vram(ctx)


# ------------------------------------------------------------------- the invariant

def kv_ram(context, kv_vram: int) -> int:
    """KV in RAM = Context - KV in VRAM.  TUI-level only; never passed to Strata."""
    return (context or 0) - (kv_vram or 0)


def reconcile_kv_vram(context, kv_ram: int) -> int:
    """Keep the invariant when the user edits KV in RAM instead of KV in VRAM."""
    return (context or 0) - (kv_ram or 0)


def kv_total(context) -> int:
    return context or 0


def _whole(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_kv_vram(context, value) -> bool:
    ctx = context or 0
    return _whole(value) and 0 <= value <= ctx


def validate_kv_ram(context, value) -> bool:
    ctx = context or 0
    return _whole(value) and 0 <= value <= ctx


def validate_reserve_mib(value) -> bool:
    return _whole(value) and value >= 0


def effective_args(cfg: dict, kv_vram: int, reserve_mib: int) -> tuple:
    """What to persist.  Returns (kv_resident_arg, reserve_arg): kv_resident_arg is
    the --kv-resident value, or 0 when the KV is fully resident (drop it); None
    when the screen did not change it.  --kv and --expert-cache are never touched."""
    ctx = model_context(cfg) or 0
    if kv_vram is None:
        kv_arg = None
    elif kv_vram < ctx:
        kv_arg = kv_vram
    else:
        kv_arg = 0                      # fully resident in VRAM: drop --kv-resident
    reserve_arg = reserve_mib if reserve_mib is not None else None
    return kv_arg, reserve_arg


# ------------------------------------------------------------------- the estimate

def _kv_bytes_per_token(kv_format: str) -> int:
    return KV_BYTES_BY_FORMAT.get((kv_format or "int8").lower(), KV_INT8_BYTES_PER_TOKEN)


def parse_calibration(log_text: str) -> dict | None:
    """Pull the engine's own expert-cache numbers from a load log (the most recent
    run wins).  Returns dict(slots, cache_gib, free_gib, reserve_at, draft_mib) or
    None when the log has no usable cache line."""
    slots = cache_gib = free_gib = draft_mib = reserve_at = None
    for line in reversed(log_text.splitlines()):
        m = _ETA_CACHE_FINAL.search(line)
        if m and slots is None:
            slots = int(m.group(1))
            cache_gib = float(m.group(2))
        m2 = _ETA_CACHE_AUTO.search(line)
        if m2 and free_gib is None:
            free_gib = float(m2.group(1))
            reserve_at = _int(m2.group(2), 0)
            draft_mib = _int(m2.group(3), 0)
            if slots is None:
                slots = _int(m2.group(4))
        if slots is not None and free_gib is not None:
            break
    if slots is None or free_gib is None:
        return None
    return {"slots": slots, "cache_gib": cache_gib, "free_gib": free_gib,
            "reserve_at": reserve_at, "draft_mib": draft_mib}


def calibration_from_log(log_path) -> dict | None:
    if not log_path:
        return None
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_calibration(text)


def estimate_experts(context, kv_vram, reserve_mib, kv_format, calibration) -> int | None:
    """Rough pre-load resident-expert count (integer slots), or None when there is
    not enough real data for a defensible estimate.

    Model: the available VRAM for the expert cache = the free VRAM the last load
    reported, minus the current VRAM reserve and the draft head, plus any VRAM the
    current KV residency frees (a config with no --kv-resident keeps the whole
    context in VRAM, so reducing KV-in-VRAM adds that difference back)."""
    if not calibration:
        return None
    slots = calibration.get("slots")
    free_gib = calibration.get("free_gib")
    reserve_at = calibration.get("reserve_at")
    draft = calibration.get("draft_mib") or 0
    if slots is None or free_gib is None:
        return None
    # The engine's own per-slot cost, from its "auto" budget line:
    #   <free> GiB - <reserve_at> - <draft head> -> <slots>
    if slots <= 0:
        return None
    budget_free = (free_gib * 1024.0) - (reserve_at or 0) - draft
    if budget_free <= 0:
        return None
    per_slot = budget_free / float(slots)
    ctx = _int(context, 0) or 0
    kv = kv_vram if kv_vram is not None else ctx
    k_mib = _kv_bytes_per_token(kv_format) / MIB
    freed = max(0, ctx - kv) * k_mib
    budget = ((free_gib * 1024.0) - reserve_mib - draft + freed)
    est = int(budget / per_slot)
    return max(0, est)


def format_estimate(est) -> str:
    """'≈ 3,750' when an integer estimate is available, else '≈ unavailable'."""
    if est is None:
        return "≈ unavailable"
    return f"≈ {est:,}"