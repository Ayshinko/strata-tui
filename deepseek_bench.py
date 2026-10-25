"""DeepSeek 4.1 benchmark support: parse the engine's own log statistics, sample the
running engine's RAM, snapshot the active performance configuration and keep a
small result history for comparison.

The engine writes all numbers to its log (stderr): the per-request "maya ds4 serve"
line (decode / prefill tok/s), the "store:" expert-cache line (--ssd-cache) and the
"RAM store:" / "CUDA n layers" lines.  Nothing here runs the engine; every number is
read after the fact, so a benchmark snapshot never changes the model's behaviour.
"""
from __future__ import annotations

import csv
import ctypes
import json
import ntpath
import os
import re
import subprocess
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent

SERVE_RE = re.compile(
    r'maya ds4 serve: prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) ms '
    r'\(([\d.]+) tok/s\), (\d+) generated in ([\d.]+) ms \(([\d.]+) tok/s\); '
    r'CPU lane ([\d.]+)% of the experts')
STORE_RE = re.compile(
    r"store: ([\d.]+)% of the CPU lane's experts were in RAM \(([\d.]+) hits, "
    r"([\d.]+) misses a token\); read ([\d.]+) MB a token .*?slots (\d+) of (\d+), "
    r"(\d+) evictions, (\d+) failed reads")
RAMSTORE_RE = re.compile(r'RAM store: up to (\d+) slots of ([\d.]+) MB \(([\d.]+) GB\), policy (\d)')
VRAM_RE = re.compile(
    r'CUDA\d+ layers .*: weights ([\d.]+) GB, experts ([\d.]+) GB'
    r'(?: \((\d+) of (\d+) in VRAM\))?,? caches ([\d.]+) GB, (\d+) MiB free')
STOREFILL_RE = re.compile(r"RAM store: (\d+) of the profile's hottest experts read in \(([\d.]+) GB\)")


def parse_log(log_path):
    """The newest statistics of the most recent request run in the engine log.

    Returns a dict with optional keys (None when the log has no such line yet):
    decode_tok_s, prefill_tok_s, prompt_tokens, generated_tokens, cpu_lane_pct,
    store (dict), ram_store_gb, ram_store_slots, ram_store_policy,
    store_warm_experts, store_warm_gb,
    vram_weights_gb, vram_experts_gb, vram_caches_gb, vram_free_mib,
    vram_experts_in_vram, vram_experts_total.  Always a dict (possibly empty).
    """
    path = Path(log_path) if log_path else Path('')
    result: dict = {}
    if not path.is_file():
        return result
    try:
        size = path.stat().st_size
        with path.open('r', encoding='utf-8', errors='replace') as fh:
            if size > 200 * 1024:
                fh.seek(-200 * 1024, os.SEEK_END)
                fh.readline()
            text = fh.read()
    except OSError:
        return result
    # Never mix a previous engine start with the current one.
    starts = list(re.finditer(r'^\[strata\] .*engine started:', text, re.MULTILINE))
    if starts:
        text = text[starts[-1].start():]
    for key, pattern, convert in (
        ('prefill_chunk', r'prompts in chunks of (\d+)', int),
        ('cpu_copy_gb', r'expert copy final: data=([\d.]+) GB', float),
    ):
        matches = list(re.finditer(pattern, text))
        if matches:
            result[key] = convert(matches[-1][1])
            result[key + '_source'] = matches[-1].group(0)
    # Official builds report completed copy totals without the experimental final line.
    if 'cpu_copy_gb' not in result:
        copies = list(re.finditer(r"\d+ of the CPU lane's \d+ experts copied into RAM \(([\d.]+) GB", text))
        if copies:
            result['cpu_copy_gb'] = float(copies[-1][1])
            result['cpu_copy_gb_source'] = copies[-1].group(0)
    serve = SERVE_RE.findall(text)
    if serve:
        prompt, reused, read, p_ms, p_tps, generated, d_ms, d_tps, cpu = serve[-1]
        result.update(decode_tok_s=float(d_tps), prefill_tok_s=float(p_tps),
                      prompt_tokens=int(prompt), generated_tokens=int(generated),
                      cpu_lane_pct=float(cpu))
    store = STORE_RE.findall(text)
    if store:
        hits, hit_n, miss_n, mb, slots, slots_max, evict, failed = store[-1]
        result['store'] = {'hits_pct': float(hits), 'hits_per_token': float(hit_n),
                           'misses_per_token': float(miss_n), 'read_mb_per_token': float(mb),
                           'slots': int(slots), 'slots_max': int(slots_max),
                           'evictions': int(evict), 'failed_reads': int(failed)}
    ram = RAMSTORE_RE.findall(text)
    if ram:
        slots, mb, gb, policy = ram[-1]
        result.update(ram_store_gb=float(gb), ram_store_slots=int(slots), ram_store_policy=int(policy))
    warm = STOREFILL_RE.findall(text)
    if warm:
        experts, gb = warm[-1]
        result.update(store_warm_experts=int(experts), store_warm_gb=float(gb))
    vram = VRAM_RE.findall(text)
    if vram:
        weights, experts, in_vram, total, caches, free = vram[-1]
        result.update(vram_weights_gb=float(weights), vram_experts_gb=float(experts),
                      vram_caches_gb=float(caches), vram_free_mib=int(free))
        if in_vram and total:
            result['vram_experts_in_vram'] = int(in_vram)
            result['vram_experts_total'] = int(total)
    return result


def parse_tasklist_bytes(output, pid, exe_name):
    """tasklist CSV's K is KiB. Require exact PID/name; malformed/ambiguous = unknown."""
    matches = []
    for row in csv.reader(output.splitlines()):
        if len(row) != 5 or row[0].casefold() != exe_name.casefold():
            continue
        if row[1].strip() != str(pid):
            continue
        value = re.fullmatch(r'\s*(\d+|\d{1,3}(?:,\d{3})+)\s+K\s*', row[4])
        if value:
            matches.append(int(value[1].replace(',', '')) * 1024)
    return matches[0] if len(matches) == 1 else None


def tasklist_working_set_bytes(pid, exe_name):
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        return None
    try:
        output = subprocess.check_output(
            ['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
            text=True, errors='replace', timeout=10, creationflags=0x08000000)
        return parse_tasklist_bytes(output, pid, exe_name)
    except (OSError, subprocess.SubprocessError, csv.Error):
        return None


def process_memory_gb(exe_name, pid=None):
    """Deprecated compatibility API: returns GiB, NOT GB; never searches by name alone.

    The caller must already have verified the PID. New callers use memory_snapshot().
    """
    value = tasklist_working_set_bytes(pid, exe_name)
    return value / 2**30 if value is not None else None


def _path(value):
    return ntpath.normcase(ntpath.normpath(str(value)))


def _pack(args):
    values = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--pack']
    return values[-1] if values else None


def resolve_engine_identity(cfg, launch, processes):
    """Read-only identity selection: exact executable + --serve + pack + launch time.

    Fail closed without structured launch evidence or on multiple matching processes.
    """
    from datetime import datetime
    try:
        exe, args = launch['exe'], launch['args']
        started = datetime.strptime(launch['started_at'], '%Y-%m-%d %H:%M:%S').timestamp()
        pack = _pack(args)
        expected = args[1:] if args and args[0] == '--serve' else args
        if not pack or _path(exe) != _path(cfg.get('exe', '')):
            return None
        if _path(pack) != _path(_pack(cfg.get('args') or [])):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    matches = []
    for row in processes:
        command = row.get('cmdline') or []
        created = row.get('create_time')
        if (type(row.get('pid')) is int and row['pid'] > 0 and row.get('exe')
                and _path(row['exe']) == _path(exe) and '--serve' in command
                and _pack(command) and _path(_pack(command)) == _path(pack)
                and command[command.index('--serve') + 1:] == expected
                and isinstance(created, (int, float)) and abs(created - started) <= 10):
            matches.append(dict(pid=row['pid'], exe=row['exe'], cmdline=command,
                                create_time=created))
    return matches[0] if len(matches) == 1 else None


def windows_memory_bytes():
    """GetPerformanceInfo: physical available and system-wide commit in BYTES.

    Private Commit is a process metric, not this system Commit Total. Available RAM
    includes reclaimable pages; neither is an expert-residency measurement.
    """
    if os.name != 'nt':
        return {}
    from ctypes import wintypes

    class PerformanceInfo(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in (
            'CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal',
            'PhysicalAvailable', 'SystemCache', 'KernelTotal', 'KernelPaged',
            'KernelNonpaged', 'PageSize')] + [(name, wintypes.DWORD) for name in (
            'HandleCount', 'ProcessCount', 'ThreadCount')]
    try:
        info = PerformanceInfo()
        info.cb = ctypes.sizeof(info)
        api = ctypes.WinDLL('psapi', use_last_error=True).GetPerformanceInfo
        api.argtypes = [ctypes.POINTER(PerformanceInfo), wintypes.DWORD]
        api.restype = wintypes.BOOL
        if not api(ctypes.byref(info), info.cb):
            return {}
        return {key: int(getattr(info, field) * info.PageSize) for key, field in (
            ('windows_available_ram_bytes', 'PhysicalAvailable'),
            ('windows_commit_total_bytes', 'CommitTotal'),
            ('windows_commit_limit_bytes', 'CommitLimit'))}
    except (OSError, AttributeError):
        return {}


def memory_snapshot(cfg, launch=None):
    """Instantaneous sample, NOT request peak or guaranteed expert residency.

    psutil Windows rss = Working Set; private = Private Commit. Never substitute
    VMS, Working Set, or system RAM for missing Private Commit.
    """
    result = {'schema': 1, 'sampled_at': time.time(), 'units': 'bytes',
              'status': 'unknown', 'reason': 'no verified unique engine identity',
              'scope': 'instantaneous; not request peak or expert residency',
              'system_source': 'Windows GetPerformanceInfo',
              'process': None, 'system': windows_memory_bytes()}
    if os.name != 'nt':
        result['reason'] = 'Windows sampler unavailable on this platform'
        return result
    try:
        import psutil
    except ImportError:
        result['reason'] = 'psutil unavailable; name-only fallback disabled'
        return result
    try:
        rows = [p.info for p in psutil.process_iter(
            ['pid', 'exe', 'cmdline', 'create_time'], ad_value=None)]
        identity = resolve_engine_identity(
            cfg, launch if launch is not None else launch_evidence(cfg.get('log')), rows)
        if identity is None:
            return result
        p = psutil.Process(identity['pid'])

        def same_process():
            return (p.is_running() and p.create_time() == identity['create_time']
                    and _path(p.exe()) == _path(identity['exe'])
                    and p.cmdline() == identity['cmdline'])

        if not same_process():
            result['reason'] = 'process identity changed before sampling'
            return result
        source = 'psutil.memory_info.rss'
        try:
            memory = p.memory_info()
            working_set = int(memory.rss)
            private = getattr(memory, 'private', None)
            private = int(private) if private is not None else None
        except psutil.AccessDenied:
            working_set = tasklist_working_set_bytes(identity['pid'], ntpath.basename(identity['exe']))
            private = None
            source = 'tasklist CSV (KiB rounded)'
        if not same_process():
            result['reason'] = 'process exited or PID reused during sampling'
            return result
        result.update(status='ok' if working_set is not None and private is not None else 'partial',
                      reason=None if private is not None else 'Private Commit unavailable',
                      process={**identity, 'working_set_bytes': working_set,
                               'private_commit_bytes': private, 'working_set_source': source,
                               'private_commit_source': 'psutil.memory_info.private' if private is not None else None})
    except (psutil.Error, OSError, ValueError, TypeError) as error:
        result['reason'] = type(error).__name__ + ': process sample unavailable'
    return result


def memory_lines(memory):
    lines = ['  Memory: instantaneous; bytes / 1e9 = GB, bytes / 2^30 = GiB']
    process = memory.get('process') or {}
    system = memory.get('system') or {}
    for label, value in (
        ('Working Set', process.get('working_set_bytes')),
        ('Private Commit', process.get('private_commit_bytes')),
        ('Windows Available RAM', system.get('windows_available_ram_bytes')),
        ('Windows Commit Total', system.get('windows_commit_total_bytes')),
        ('Windows Commit Limit', system.get('windows_commit_limit_bytes'))):
        lines.append(f'  {label}: ' + (f'{value / 2**30:.2f} GiB ({value / 1e9:.2f} GB)'
                                      if value is not None else 'Unknown'))
    lines.append('  Sample time (Unix): ' + str(memory.get('sampled_at', 'Unknown')))
    lines.append('  Working Set source: ' + str(process.get('working_set_source') or 'Unknown'))
    lines.append('  Engine PID: ' + str(process.get('pid', 'Unknown')))
    if memory.get('reason'):
        lines.append('  Memory status: ' + memory['reason'])
    return lines


def engine_version(cfg):
    """Human version of the engine behind a config: BUILD.json version plus the
    component build id, e.g. '0.1.49.2 (d2d198a49+dsv41+cuda13.0)'."""
    exe = Path(cfg.get('exe') or '')
    version = ''
    try:
        build = exe.parent / 'BUILD.json'
        if build.is_file():
            version = (json.loads(build.read_text(encoding='utf-8', errors='replace'))
                       .get('version', '') or '')
        component = exe.parent / 'engine-component.json'
        if component.is_file():
            build_id = (json.loads(component.read_text(encoding='utf-8', errors='replace'))
                        .get('build_id', ''))
            if build_id:
                return f'{version} ({build_id})'
        return version or exe.name
    except Exception:                                  # noqa: BLE001
        return exe.name


def config_summary(cfg):
    """Every DeepSeek performance option as the TUI shows it (effective values, engine
    defaults for the flags that are omitted).  Keys are the settings screen labels."""
    import deepseek_settings as ds
    args = ds.effective(cfg)
    return {label: ds.display_value(flag, ds.current_value(args, flag))
            for label, flag in ds.ALL_FIELDS}


def effective_args(cfg):
    import deepseek_settings as ds
    return ds.effective(cfg)


def results_path():
    """JSON-lines history file.  STRATA_TUI_BENCH_FILE overrides it (isolated tests)."""
    override = os.environ.get('STRATA_TUI_BENCH_FILE')
    if override:
        return Path(override)
    return (_HERE / 'logs' / 'deepseek-bench.jsonl')


def save_result(cfg):
    """Take a manual snapshot: current config summary plus the newest engine numbers,
    append it to the history and return the record.  Never launches or touches the engine."""
    stats = parse_log(cfg.get('log'))
    context = None
    launch = launch_evidence(cfg.get('log'))
    args = launch.get('args') or []
    from strata_control import user_args
    ctx = [g[-1] for g in user_args.groups(args) if g and g[0] == '--max-context' and len(g) == 2]
    if ctx:
        try:
            context = int(ctx[-1])
        except ValueError:
            context = None
    record = {
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        'model': next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--pack'), 'Unknown'),
        'model_version': launch.get('model_version') or 'Unknown',
        'schema': 2,
        'memory_schema': 1,
        'memory': memory_snapshot(cfg, launch),
        'launch': launch,
        'engine': launch.get('engine') or 'Unknown',
        'context': context,
        'observed': stats,
        'vram_experts_in_vram': stats.get('vram_experts_in_vram'),
        'cpu_copy_gb': stats.get('cpu_copy_gb'),
        'config': explicit_summary(args) if launch else {},
        'args': args if launch else None,
        'decode_tok_s': stats.get('decode_tok_s'),
        'prefill_tok_s': stats.get('prefill_tok_s'),
        'prompt_tokens': stats.get('prompt_tokens'),
        'generated_tokens': stats.get('generated_tokens'),
        'cpu_lane_pct': stats.get('cpu_lane_pct'),
        'ram_gb': None,  # deprecated ambiguous unit/identity field; use memory.*_bytes
        'store': stats.get('store'),
        'ram_store_gb': stats.get('ram_store_gb'),
        'ram_store_slots': stats.get('ram_store_slots'),
        'ram_store_policy': stats.get('ram_store_policy'),
        'store_warm_experts': stats.get('store_warm_experts'),
        'store_warm_gb': stats.get('store_warm_gb'),
        'vram_weights_gb': stats.get('vram_weights_gb'),
        'vram_experts_gb': stats.get('vram_experts_gb'),
        'vram_caches_gb': stats.get('vram_caches_gb'),
        'vram_free_mib': stats.get('vram_free_mib'),
    }
    path = results_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            json.dump(record, fh, ensure_ascii=False)
            fh.write('\n')
    except OSError:
        return None
    return record


def load_results():
    """History records, newest first.  Damaged lines are skipped."""
    path = results_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                out.append(record)
        except ValueError:
            continue
    return out


def launch_evidence(log):
    """Only structured, unambiguous startup evidence; never today's settings."""
    try:
        text = Path(log).read_text(encoding='utf-8', errors='replace')
        start = text.rfind('engine started:')
        marker = text.rfind('[strata-benchmark-launch] ')
        if marker < 0 or marker < start:
            return {}
        return json.loads(text[marker:].split('\n', 1)[0].split('] ', 1)[1])
    except (OSError, ValueError, TypeError):
        return {}


def explicit_summary(args):
    import deepseek_settings as ds
    return {label: ds.display_value(flag, ds.current_value(args, flag))
            if ds.flag_present(args, flag) else 'Unknown (omitted; engine default)'
            for label, flag in ds.ALL_FIELDS}


def delete_result(record):
    """Append a tombstone: original lines (including damaged ones) stay untouched."""
    import hashlib
    key = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    with results_path().open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'_deleted': key}) + '\n')


def visible_results():
    import hashlib
    rows = load_results()
    deleted = {r['_deleted'] for r in rows if '_deleted' in r}
    return [r for r in rows if '_deleted' not in r and
            hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest() not in deleted]


def restore_result(item, record):
    """Restore performance flags only; preserve model paths and server identity."""
    import deepseek_settings as ds
    if record.get('schema') != 2 or not record.get('launch') or not isinstance(record.get('args'), list):
        raise ValueError('Unknown: this legacy snapshot has no verified launch parameters')
    args = record['args']
    changes = {flag: ds.current_value(args, flag) for _, flag in ds.ALL_FIELDS}
    new, paths = ds.persist(item, changes, running=True)
    item.cfg = new
    return paths


def comparison(results):
    lines = ['  Comparison (all saved runs; workloads/models may differ)']
    for field, label in [('decode_tok_s', 'Best Decode'), ('prefill_tok_s', 'Best Prefill')]:
        rows = [r for r in results if isinstance(r.get(field), (int, float))]
        best = max((r[field] for r in rows), default=None)
        latest = results[0].get(field) if results else None
        delta = latest - best if isinstance(latest, (int, float)) and best is not None else None
        lines.append(f'  {label}: {best if best is not None else "Unknown"} · Latest: '
                     f'{latest if latest is not None else "Unknown"} · Δ: '
                     f'{delta if delta is not None else "Unknown"} tok/s')
    return lines


def detail_lines(record):
    import deepseek_settings as ds
    lines = ['Snapshot JSON: ' + str(results_path().resolve())]
    for key in ('ts', 'model', 'model_version', 'engine', 'decode_tok_s', 'prefill_tok_s', 'ram_gb',
                'cpu_lane_pct', 'vram_weights_gb', 'vram_experts_gb', 'vram_caches_gb',
                'vram_free_mib', 'ram_store_gb', 'store'):
        lines.append(key + ': ' + str(record.get(key) if record.get(key) is not None else 'Unknown'))
    if record.get('memory_schema') == 1:
        lines.extend(memory_lines(record.get('memory') or {}))
    else:
        lines.append('WARNING: legacy ram_gb is UNTRUSTED (CSV/PID/unit ambiguity); not a residency measurement.')
    verified = record.get('schema') == 2 and record.get('launch')
    lines.append('Launch argv: ' + (json.dumps(record.get('args'), ensure_ascii=False) if verified else 'Unknown (legacy/unverified)'))
    lines.extend(configuration_lines(record.get('args') or [], record.get('observed') or {}) if verified else ['Configuration: Unknown'])
    lines.append('Saved raw record:')
    lines.extend(json.dumps(record, ensure_ascii=False, indent=2).splitlines())
    return lines


def best_decode(results):
    """The fastest decode tok/s across snapshots, or None when nothing has one."""
    values = [r['decode_tok_s'] for r in results
              if isinstance(r.get('decode_tok_s'), (int, float))]
    return max(values) if values else None


def legacy_result_row(record):
    """One comparison line for the history: date, decode, prefill, context and the
    three options that changed the speed the most (ssd-cache, dspark, vram-experts)."""
    decode = record.get('decode_tok_s')
    prefill = record.get('prefill_tok_s')
    ctx = record.get('context')
    config = record.get('config') or {}
    ts = str(record.get('ts') or '')[-16:]
    decode_s = ('%.2f' % decode) if isinstance(decode, (int, float)) else '--'
    prefill_s = ('%.1f' % prefill) if isinstance(prefill, (int, float)) else '--'
    ctx_s = ('%dK' % (int(ctx) // 1024)) if ctx else '-K'

    def val(label):
        return abridge(config.get(label, ''), 11)

    return (f'{ts:>16}  {decode_s:>6} dec  {prefill_s:>6} pf  {ctx_s:>4}  '
            f'ssd={val("RAM Expert Store (GB)")}  dspark={val("DSpark Mode")}  '
            f'vram={val("VRAM Experts")}' +
            ('  [RAM untrusted]' if record.get('memory_schema') != 1 else ''))


def configuration_lines(args, stats):
    """Configured argv is not proof of runtime values or declared defaults."""
    import deepseek_settings as ds
    lines = []
    for label, flag in ds.fields_for_config({}):
        configured = ds.display_value(flag, ds.current_value(args, flag)) if ds.flag_present(args, flag) else 'Auto'
        observed = stats.get('prefill_chunk') if flag == '--prefill-chunk' else None
        source = stats.get('prefill_chunk_source')
        lines.append(f'{label}: Configured {configured}' +
                     (f' → Effective {observed} [log: {source}]' if observed is not None else ' · Effective Unknown') +
                     ' · Default Unknown')
    experts = stats.get('vram_experts_in_vram')
    lines.append('GPU Experts (loaded count, NOT --vram-experts ratio): ' +
                 (('Auto → ' if not ds.flag_present(args, '--vram-experts') else '') +
                  f'Effective {experts} experts [CUDA startup log]' if experts is not None else 'Unknown'))
    lines.append('CPU Copy: ' + (f"{stats['cpu_copy_gb']:.3f} GB [log: {stats.get('cpu_copy_gb_source')}]" if stats.get('cpu_copy_gb') is not None else 'Unknown') + '; copied data, not physical residency')
    return lines


def result_row(record, width=120):
    def number(key, decimals=0):
        value = record.get(key)
        return f'{value:.{decimals}f}' if isinstance(value, (int, float)) else '—'
    args = record.get('args') or []
    modes = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--dspark']
    mode = modes[-1] if record.get('launch') and modes else '—'
    stamp = str(record.get('ts') or '—')
    stamp = stamp[5:16] if width < 95 else stamp
    cells = [stamp, number('decode_tok_s', 2), number('prefill_tok_s', 1),
             number('context'), mode, number('vram_experts_in_vram'), number('cpu_copy_gb', 1)]
    return ' '.join(cells)


def abridge(text, limit=16):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + '…'