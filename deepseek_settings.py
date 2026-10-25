"""DeepSeek-only performance settings; official argv semantics, no Qwen aliases or GPU work.

Every option mirrors the strata-dsv41 engine's own CLI (src/dsv41/ds4_main.cpp and the
Options struct in src/dsv41/ds4_model.hpp) - the flag, its accepted values and the engine's
default when the flag is simply omitted.  "Auto" in the TUI means "omit the flag" for every
option that does not take the literal value ``auto`` (only ``--dspark`` and ``--mmap-experts``
do); the engine then uses its own default, which is exactly what omitting gives.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import uuid

#: (TUI label, engine flag) - the DeepSeek performance settings shown on the main page.
#: Exactly the twelve options the user asked for; --ssd-policy appears only once.
PERFORMANCE = [('RAM Expert Store (GB)', '--ssd-cache'),
               ('CPU Threads', '--cpu-threads'),
               ('SSD Cache Policy', '--ssd-policy'),
               ('DSpark Mode', '--dspark'),
               ('DSpark Confidence', '--dspark-conf'),
               ('DSpark Block', '--dspark-block'),
               ('Expert mmap', '--mmap-experts'),
               ('VRAM Experts', '--vram-experts'),
               ('Context Length', '--max-context'),
               ('Prefill Chunk', '--prefill-chunk'),
               ('Expert Profile', '--expert-profile'),
               ('Expert Cache', '--no-expert-cache')]
BASIC = PERFORMANCE                     # compatibility alias
ADVANCED = [('VRAM reserve', '--reserve-mib')]
ALL_FIELDS = PERFORMANCE + ADVANCED

#: flags that take no value: present alone means its engine default is switched off.
VALUELESS = frozenset({'--no-expert-cache'})

ENUMS = {'--dspark': ('auto', 'on', 'none', 'off'), '--ssd-policy': ('lru', 'lfu', 'lfulru'),
         '--mmap-experts': ('auto', 'on', 'off'), '--no-expert-cache': ('enabled', 'disabled')}

HELP = {
    '--expert-copy-gb': 'EXPERIMENTAL: Auto omits the flag (original behavior, including DS4_COPY_GB). '
                        'Manual decimal GB, e.g. 114 / 116 / 118 / 121. HIGH MEMORY RISK: Windows may '
                        'page or become unresponsive. Requires experimental engine, mmap off and RAM Store 0. '
                        'Retains 2 GiB physical floor and Windows Commit margin; may copy less than requested. '
                        'Not locked residency. Never starts automatically.',
    '--max-context': 'Positive tokens (up to 2147483647). Larger contexts use more memory.',
    '--reserve-mib': 'MiB left free per GPU; 0+ or auto (engine default: 600).',
    '--ssd-cache': 'GB of RAM for the expert store, NOT SSD space. Official 0.1.51 auto may '
                   'copy all CPU experts when RAM and Windows commit allow it; otherwise sizes '
                   'a demand store. Explicit mmap/store choices override auto. 0 = no store. '
                   'Copied data is not guaranteed physical residency.',
    '--cpu-threads': 'CPU threads computing the experts the GPU lane does not hold. '
                     'auto = omit (the physical cores); 0 also means physical cores.',
    '--dspark': 'on / off / none / auto. auto = engine loads the DSpark drafter only where it '
                'helps. on = always draft. off = drafting disabled but the drafter is STILL '
                'loaded. none = drafter not even loaded.',
    '--vram-experts': 'The share (0..1) of the expert budget kept in VRAM - NOT a GB amount, not '
                      'an expert count. 1 = the whole budget. auto = omit (engine default 1).',
    '--ssd-policy': 'Which experts the RAM store drops first: lru / lfu / lfulru.',
    '--dspark-block': 'At most this many drafts a round. auto = omit (the pack\'s block size); '
                      '0 also means the pack\'s block size.',
    '--dspark-conf': 'A draft is verified while its confidence (0..1) is at least this floor.',
    '--prefill-chunk': 'Prompt tokens read at a time on the fast path. auto = omit (engine '
                       'default); 0 = short windows of 16; positive = chunk size.',
    '--mmap-experts': 'Official 0.1.51: auto copies all CPU experts only when they fit with '
                      '8 GiB RAM spare and Windows commit room (store must also be Auto). '
                      'on reads in place; off copies what safely fits. No --expert-copy-gb support.',
    '--expert-profile': 'FILE or none. The expert hit-count table deciding what is kept in '
                        'memory first. none = no profile at all; auto = omit (the pack\'s own '
                        'expert-profile.bin).',
    '--no-expert-cache': 'Expert cache: enabled = experts move between VRAM and the CPU lane '
                         '(engine default). disabled = keep them in place, adds --no-expert-cache.',
}

#: the engine's default for each option when its flag is omitted (what the TUI shows as auto);
#: None means "no fixed number, the engine picks it".
DEFAULTS = {'--expert-copy-gb': None, '--max-context': '32768', '--reserve-mib': '600', '--ssd-cache': None,
            '--cpu-threads': None, '--dspark': 'auto', '--vram-experts': '1',
            '--ssd-policy': 'lru', '--dspark-block': None, '--dspark-conf': '0.5',
            '--prefill-chunk': None, '--mmap-experts': 'auto', '--expert-profile': None,
            '--no-expert-cache': 'enabled'}

_INT_MAX = 2147483647


def parse_value(flag, raw):
    """The argv value for ``raw``, or None when the choice means "omit the flag" (the engine's
    own default).  Rejects the values the engine would misread; never guesses a range."""
    if flag not in DEFAULTS:
        raise ValueError('Unsupported DeepSeek option')
    text = str(raw).strip()
    low = text.lower()
    if flag == '--no-expert-cache':
        if low in ('auto', 'on', 'yes', 'enable', 'enabled'):
            return 'enabled'
        if low in ('off', 'no', 'disable', 'disabled'):
            return 'disabled'
        raise ValueError('Choose enabled or disabled')
    if flag in ENUMS:
        if low not in ENUMS[flag]:
            raise ValueError('Choose ' + ' / '.join(ENUMS[flag]))
        return low
    if low == 'auto':
        # The engine accepts the literal "auto" only for --dspark / --mmap-experts (handled as
        # ENUMS above).  For every other option Auto means omit: the flag is not written.
        return None
    if flag == '--expert-profile':
        if low in ('none', 'no'):
            return 'none'
        if text.startswith('--') or not text or any(c in text for c in '\r\n\x00'):
            raise ValueError('Use a file path or none')
        return text
    if flag == '--ssd-cache' and low == 'off':
        return '0'
    if flag in ('--expert-copy-gb', '--ssd-cache', '--vram-experts', '--dspark-conf'):
        try:
            value = float(text)
        except (ValueError, OverflowError):
            raise ValueError('Use a finite number') from None
        if not math.isfinite(value):
            raise ValueError('Use a finite number')
        if flag in ('--ssd-cache', '--expert-copy-gb'):
            if value < 0 or value > 10 ** 7:
                raise ValueError('Use a nonnegative number of GB (0 / off / auto)')
            return format(value, '.15g')
        if value < 0 or value > 1:
            raise ValueError('Use 0..1')
        return format(value, '.15g')
    if not re.fullmatch(r'\d+', low):
        raise ValueError('Use a whole nonnegative number')
    number = int(low)
    if number > _INT_MAX or number < (1 if flag == '--max-context' else 0):
        raise ValueError('Outside the safe integer range')
    return str(number)


def default_option(flag):
    """What "Reset to Default" writes for this option; None = omit the flag, the engine default.
    Only the options the engine would not get by omission need an explicit value here."""
    explicit = {'--dspark': 'auto', '--mmap-experts': 'auto', '--ssd-policy': 'lru',
                '--dspark-conf': '0.5', '--max-context': '32768',
                '--no-expert-cache': 'enabled'}
    return explicit.get(flag)


def flag_present(args, flag):
    """True when the valueless flag occurs in the argument list (its presence is the choice)."""
    from strata_control import user_args
    return any(g and g[0] == flag for g in user_args.groups(args))


def flag_value(args, flag):
    from strata_control import user_args
    groups = [g for g in user_args.groups(args) if g and g[0] == flag]
    return groups[-1][1] if groups and len(groups[-1]) == 2 else None


def effective(cfg):
    from strata_control import user_args
    return user_args.merge(cfg.get('args') or [], user_args.capture(cfg))


def current_value(args, flag):
    """The value the engine would use right now: the last explicit flag, or the engineered
    default when the flag is missing.  Valueless flags read presence instead."""
    if flag in VALUELESS:
        return 'disabled' if flag_present(args, flag) else 'enabled'
    return flag_value(args, flag)


def display_value(flag, value):
    """Human-readable value for the TUI.  ``None`` means the flag is not written: the engine's
    own default, shown with "[engine default]" and never as the literal string "auto"."""
    default = value is None
    shown = DEFAULTS[flag] if default else value
    if flag == '--no-expert-cache':
        out = ('Enabled (experts move between VRAM and the CPU lane)'
               if shown == 'enabled' else 'Disabled (--no-expert-cache)')
    elif flag == '--expert-profile':
        if shown is None or shown == '':
            out = 'auto (pack/expert-profile.bin)'
        elif shown == 'none':
            out = 'none (no profile)'
        else:
            try:
                out = Path(shown).name
            except (TypeError, ValueError):
                out = str(shown)
    elif flag == '--vram-experts':
        try:
            out = f'{float(shown) * 100:g}% of expert budget'
        except (TypeError, ValueError):
            out = str(shown)
    elif flag == '--ssd-cache':
        if shown is None:
            out = 'auto'
        elif float(shown) == 0:
            out = 'off (no store)'
        else:
            out = f'{show_float(shown)} GB RAM'
    elif flag == '--expert-copy-gb':
        out = 'Auto (original behavior)' if shown is None else f'{show_float(shown)} GB — HIGH MEMORY RISK'
    elif flag == '--reserve-mib':
        out = str(shown) + ' MiB'
    elif flag == '--cpu-threads':
        out = 'auto (physical cores)' if str(shown) in ('0', 'None', '') \
            else str(shown) + ' threads'
    elif flag == '--dspark':
        out = {'auto': 'auto (engine decides)', 'on': 'on (draft always)',
               'off': 'off (drafter still loaded)',
               'none': 'none (not even loaded)'}.get(str(shown), str(shown))
    elif flag == '--dspark-block':
        out = 'auto (pack block size)' if str(shown) in ('0', 'None', '') \
            else str(shown) + ' drafts/round'
    elif flag == '--prefill-chunk':
        out = 'auto (engine default)' if shown is None else str(shown) + ' tokens'
    elif flag == '--mmap-experts':
        out = str(shown) + (' [engine decides]' if str(shown) == 'auto' else '')
    elif flag == '--ssd-policy':
        out = str(shown).upper()          # the engine reads lowercase; the TUI shows the choice
    elif flag == '--dspark-conf':
        out = str(shown)
    elif flag == '--max-context':
        out = str(shown)
    else:
        out = str(shown)
    return out + (' [engine default]' if default else '')


def show_float(value) -> str:
    """'100' -> '100', '12.5' -> '12.5' (no trailing .0 noise)."""
    value = float(value)
    return str(int(value)) if value == int(value) else format(value, '.15g')


def edited_config(cfg, changes):
    """A copy of ``cfg`` with ``changes`` applied to both its ``args`` and the person's
    ``user_args`` (so a later official setup re-applies them).  None means the flag is removed;
    engine defaults are never written as explicit options.  Valueless flags are handled as bare
    argv entries, exactly as the engine wants them."""
    from strata_control import user_args
    result = copy.deepcopy(cfg)
    mine = user_args.capture(result)
    if user_args.WRITTEN_KEY not in result:
        import setup
        # Follow setup's own legacy migration instead of reclassifying unknown
        # options as generated when stamping this companion's first edit.
        mine = user_args.merge(user_args.dropped_by_setup(
            result.get('args'), [], setup.setup_flags(), setup.SETUP_CHOICE_FLAGS), mine)
    args = effective(result)
    for flag, value in changes.items():
        args = user_args.set_flag(args, flag, None)
        mine = user_args.set_flag(mine, flag, None)
        if flag in VALUELESS:
            if value == 'disabled':
                args.append(flag)
                mine.append(flag)
        elif value is not None:
            args = user_args.set_flag(args, flag, value)
            mine = user_args.set_flag(mine, flag, value)
    result['args'] = args
    user_args.stamp(result, mine)
    return result


def fields_for_config(cfg):
    """Official engine UI never offers the archived experimental-only flag."""
    return [(label, flag) for label, flag in ALL_FIELDS if flag != '--expert-copy-gb']


def validate_copy_config(cfg):
    """Reject the dangerous combined store, and never pass an unknown flag to stable."""
    args = effective(cfg)
    value = flag_value(args, '--expert-copy-gb')
    if value is None or value == 'auto':
        return
    parse_value('--expert-copy-gb', value)
    if flag_value(args, '--mmap-experts') != 'off' or flag_value(args, '--ssd-cache') != '0':
        raise ValueError('Manual Expert Copy Budget requires Expert mmap off and RAM Expert Store 0')
    exe = Path(cfg.get('exe') or '')
    if not exe.is_file() or not (exe.parent / 'COPY-BUDGET-EXPERIMENTAL.json').is_file():
        raise ValueError('Select the built Copy Budget experimental engine first; stable is unchanged')


def select_copy_engine(item, experimental):
    """Explicit save-only engine selection. Never launches anything or alters a preset."""
    from strata_cli.launch.store import _private_write
    path = Path(item.path)
    before = path.read_bytes()
    cfg = json.loads(before.decode('utf-8-sig'))
    if cfg != item.cfg or cfg.get('arch') != 'deepseek41':
        raise ValueError('Configuration changed or is not DeepSeek; reload first')
    root = Path(cfg.get('cwd') or path.parent)
    folder = root / 'engine-dsv41' / ('copy-budget-experimental' if experimental
                                      else '0.1.49.2-windows-x86_64-cuda-cuda13')
    exe = folder / 'strata-dsv41.exe'
    if not exe.is_file():
        raise ValueError('Engine is not built/installed: ' + str(exe))
    if experimental and not (folder / 'COPY-BUDGET-EXPERIMENTAL.json').is_file():
        raise ValueError('Experimental build has not passed its smoke tests')
    # Switching either way clears the experimental budget; never silently arms a high-RAM test.
    new = edited_config(cfg, {'--expert-copy-gb': None})
    new['exe'] = str(exe)
    new['lib_dirs'] = [str(folder)]
    new['tui_deepseek_reload_required'] = True
    backup = path.parent / '.deepseek-backups' / ('engine-' + uuid.uuid4().hex + '.json')
    _private_write(backup, before)
    if backup.read_bytes() != before or path.read_bytes() != before:
        raise ValueError('Backup verification/conflict failure')
    _private_write(path, (json.dumps(new, indent=2) + '\n').encode('utf-8'))
    item.cfg = new
    return 'Engine saved; budget reset to Auto. Nothing started. Backup: ' + str(backup)


def canonical(path):
    return os.path.normcase(os.path.abspath(str(path))).replace('\\', '/').casefold()


def _preset_extra_value(flag, value):
    """The preset "extra" form of a change.  Valueless flags use the official empty-string
    convention (present); enabled means the key is removed."""
    if flag in VALUELESS:
        return '' if value == 'disabled' else None
    return value


def plan_changes(item, changes):
    """Exact model + profile-linked managed config; never broad name matching."""
    from strata_control import user_args, paths, profiles
    from strata_cli.launch import store
    path = Path(item.path)
    original = path.read_bytes()
    cfg = json.loads(original.decode('utf-8-sig'))
    if cfg != item.cfg:
        raise ValueError('Config changed since this screen opened; refresh before loading')
    if cfg.get('arch') != 'deepseek41' or item.family != 'deepseek41':
        raise ValueError('These settings support only DeepSeek 4.1')
    checked = {flag: parse_value(flag, value) if value is not None else None
               for flag, value in changes.items()}
    if any(flag not in DEFAULTS for flag in checked):
        raise ValueError('Unsupported DeepSeek option')
    new = edited_config(cfg, checked)
    validate_copy_config(new)
    updates = {path: (original, new)}
    args = effective(cfg)
    pack = flag_value(args, '--pack')
    if not pack:
        raise ValueError('Missing DeepSeek pack identity')
    for preset_path in sorted(store.preset_dir().glob('*.json')):
        raw = preset_path.read_bytes()
        try:
            preset = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            continue  # Like official list_presets: damaged entries are not launchable matches.
        if not isinstance(preset, dict):
            continue
        if preset.get('schema') != 'strata.launch/1' or preset.get('family') != item.family or preset.get('model') != item.model:
            continue
        slug = preset_path.stem
        profile_path = profiles.profile_dir() / ('launch-' + slug + '.json')
        if not profile_path.is_file():
            continue
        profile = json.loads(profile_path.read_text(encoding='utf-8-sig'))
        managed_path = paths.state_base() / 'launch' / (slug + '.json')
        if profile.get('id') != 'launch-' + slug or profile.get('schema_version') != 1 or canonical(profile.get('source_config', '')) != canonical(managed_path):
            continue
        managed_raw = managed_path.read_bytes()
        managed = json.loads(managed_raw.decode('utf-8-sig'))
        managed_args = effective(managed)
        if managed.get('arch') != 'deepseek41' or canonical(flag_value(managed_args, '--pack') or '') != canonical(pack):
            continue
        # Another preset for the same model with different tuning/context is not this selection.
        if any(flag not in VALUELESS and flag_value(managed_args, flag) != flag_value(args, flag)
               for flag in DEFAULTS):
            continue
        context = flag_value(args, '--max-context') or DEFAULTS['--max-context']
        if preset.get('context') not in (None, int(context)):
            continue
        extra_before = preset.get('extra') or {}
        if any(flag in extra_before and flag not in VALUELESS
               and str(extra_before[flag]) != str(flag_value(args, flag) or DEFAULTS[flag])
               for flag in DEFAULTS):
            continue
        changed_preset = copy.deepcopy(preset)
        extra = dict(changed_preset.get('extra') or {})
        for flag, value in checked.items():
            extra_value = _preset_extra_value(flag, value)
            if extra_value is None:
                extra.pop(flag, None)
            else:
                extra[flag] = extra_value
            if flag == '--max-context':
                changed_preset['context'] = int(value) if value is not None else None
        changed_preset['extra'] = extra
        updates[preset_path] = (raw, changed_preset)
        updates[managed_path] = (managed_raw, edited_config(managed, checked))
    return new, updates


def persist(item, changes, *, running=False):
    """Verified backups, optimistic conflict checks, atomic per-file official writes.

    Multi-file failures roll back the already-written files. Backups remain for
    recovery after process/power loss (not a cross-file filesystem transaction).
    ``running`` marks the config as needing a reload (Restart Required); a fresh
    start clears the marker instead.
    """
    from strata_cli.launch.store import _private_write
    if not changes and (running or not item.cfg.get('tui_deepseek_reload_required')):
        return item.cfg, []
    new, updates = plan_changes(item, changes)
    for _, data in updates.values():
        if data.get('arch') == 'deepseek41':
            if running:
                data['tui_deepseek_reload_required'] = True
            else:
                data.pop('tui_deepseek_reload_required', None)
    backup_dir = Path(item.path).parent / '.deepseek-backups' / uuid.uuid4().hex
    backup_dir.mkdir(parents=True)
    records = []
    for index, (path, (before, data)) in enumerate(updates.items()):
        backup = backup_dir / (str(index) + '-' + path.name)
        _private_write(backup, before)
        digest = hashlib.sha256(before).hexdigest()
        if hashlib.sha256(backup.read_bytes()).hexdigest() != digest:
            raise OSError('Backup verification failed: ' + str(backup))
        records.append({'path': str(path), 'backup': str(backup), 'sha256': digest})
    _private_write(backup_dir / 'manifest.json', json.dumps(records, indent=2).encode('utf-8'))
    for path, (before, _) in updates.items():
        if path.read_bytes() != before:
            raise ValueError('Config/preset changed while saving: ' + str(path))
    written = []
    try:
        for path, (before, data) in updates.items():
            if path.read_bytes() != before:
                raise ValueError('Config/preset changed while saving: ' + str(path))
            payload = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
            _private_write(path, payload)
            written.append((path, payload))
            if path.read_bytes() != payload:
                raise OSError('Write verification failed: ' + str(path))
    except Exception as error:
        failures = []
        for path, payload in reversed(written):
            try:
                if path.read_bytes() != payload:
                    raise OSError('File changed after write; refusing to overwrite another editor')
                _private_write(path, updates[path][0])
                if path.read_bytes() != updates[path][0]:
                    raise OSError('Rollback verification failed')
            except Exception as rollback_error:
                failures.append(str(path) + ': ' + str(rollback_error))
        raise OSError(f'Save failed: {error}; backups: {backup_dir}' +
                      ('; rollback errors: ' + '; '.join(failures) if failures else '; rolled back')) from error
    return new, [str(p) for p in updates]