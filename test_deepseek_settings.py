"""DeepSeek controls and persistence, temp fixtures only; never starts a GPU."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_official_families import tui

# The DeepSeek Performance screen layout (see deepseek_load_options_page):
#   rows 0..11  = ds.PERFORMANCE (12 options, user-facing order)
#   row   12    = ds.ADVANCED (VRAM reserve)
#   rows 13..17 = actions: Save, Load & Start, Reset to Default, Reload Configuration, Back
CONTEXT_ROW = 8            # --max-context
VRAM_RESERVE_ROW = 12      # --reserve-mib (official engine; no experimental budget)
SAVE_ROW = 13
LOAD_START_ROW = 14
RESET_ROW = 15
RELOAD_ROW = 16


class DeepSeekSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'arch': 'deepseek41', 'exe': 'strata-dsv41.exe',
                    'model_name': 'deepseek-v4.1-flash', 'port': 8080,
                    'args': ['--pack', str(self.root / 'pack'), '--max-context', '8192',
                             '--dspark', 'auto', '--dspark-conf', '0.65', '--eos', '1',
                             '--future-option', 'kept']}
        self.cfg['args_written'] = list(self.cfg['args'])
        self.path = self.root / 'strata-deepseek.json'
        self.path.write_text(json.dumps(self.cfg))
        self.item = tui.Item('config', 'DeepSeek 中文', 'READY', self.path,
                             cfg=copy.deepcopy(self.cfg), family='deepseek41', model='IST-DAS-1.75bpw')
        p = patch.object(tui.sys.stdin, 'isatty', return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.frames = []
        for name, value in [('paint', self.frames.append), ('LOG_FILE', self.root / 'test.log')]:
            p = patch.object(tui, name, value)
            p.start()
            self.addCleanup(p.stop)

    def down_to(self, row, start=0):
        return ['down'] * max(0, row - start)

    def test_context_key_input_load_and_cancel_are_temporary(self):
        original = self.path.read_bytes()
        # Context Length is row 8. Edit it, then Load & Start (row 14).
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(LOAD_START_ROW, CONTEXT_ROW) + ['enter']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'):
            changes = tui.deepseek_load_options_page(self.item)
        self.assertEqual(changes, {'--max-context': '16384'})
        text = self.frames[0]
        for label in ('RAM Expert Store (GB)', 'SSD Cache Policy', 'DSpark Mode',
                      'DSpark Confidence', 'DSpark Block', 'Expert mmap', 'VRAM Experts',
                      'Context Length', 'Prefill Chunk', 'Expert Profile', 'Expert Cache',
                      'CPU Threads', 'VRAM reserve', 'Save', 'Load & Start',
                      'Reset to Default', 'Reload Configuration'):
            self.assertIn(label, text)
        self.assertEqual(self.path.read_bytes(), original)
        # Esc inside the editor discards everything (nothing written) - the real console path.
        keys = self.down_to(CONTEXT_ROW) + ['enter', 'esc']
        with patch.object(tui.sys.stdin, 'isatty', return_value=True), \
             patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', side_effect=AssertionError('console editor must not call input')):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        self.assertEqual(self.path.read_bytes(), original)

    def test_all_controls_validate_and_roundtrip_draft(self):
        # Edit every visible option in order (13 rows), finish with Load & Start.
        keys = ['enter'] + ['down', 'enter'] * 12 + ['down', 'down', 'enter']
        values = ['12.5', '8', 'lfulru', 'off', '0.8', '4', 'on', '0.75',
                  '16384', '512', 'none', 'disabled', '768']
        with patch.object(tui, 'read_key', side_effect=keys), patch('builtins.input', side_effect=values):
            changes = tui.deepseek_load_options_page(self.item)
        self.assertEqual(changes, {'--ssd-cache': '12.5', '--cpu-threads': '8',
                         '--ssd-policy': 'lfulru', '--dspark': 'off',
                         '--dspark-conf': '0.8', '--dspark-block': '4',
                         '--mmap-experts': 'on', '--vram-experts': '0.75',
                         '--max-context': '16384', '--prefill-chunk': '512',
                         '--expert-profile': 'none', '--no-expert-cache': 'disabled',
                         '--reserve-mib': '768'})
        text = '\n'.join(self.frames)
        for label in ('RAM Expert Store (GB)', 'CPU Threads', 'SSD Cache Policy',
                      'DSpark Mode', 'DSpark Confidence', 'DSpark Block', 'Expert mmap',
                      'VRAM Experts', 'Context Length', 'Prefill Chunk', 'Expert Profile',
                      'Expert Cache', '75% of expert budget',
                      'Disabled (--no-expert-cache)', 'none (no profile)'):
            self.assertIn(label, text)
        from deepseek_settings import parse_value
        # Auto = omit the flag, except where the engine takes the literal auto.
        for flag, raw, expected in [('--ssd-cache', 'auto', None), ('--ssd-cache', 'off', '0'),
                                    ('--cpu-threads', 'auto', None), ('--reserve-mib', 'auto', None),
                                    ('--prefill-chunk', 'auto', None), ('--dspark-block', 'auto', None),
                                    ('--vram-experts', 'auto', None), ('--expert-profile', 'auto', None),
                                    ('--expert-profile', 'none', 'none'),
                                    ('--no-expert-cache', 'off', 'disabled'),
                                    ('--no-expert-cache', 'disable', 'disabled'),
                                    ('--no-expert-cache', 'on', 'enabled'),
                                    ('--no-expert-cache', 'auto', 'enabled'),
                                    ('--dspark', 'auto', 'auto'), ('--mmap-experts', 'auto', 'auto')]:
            self.assertEqual(parse_value(flag, raw), expected, (flag, raw))
        invalid = {'--max-context': ['0', '-1', '1.5', 'nan', '2147483648'],
                   '--reserve-mib': ['-1', 'inf', '1.5'], '--cpu-threads': ['-1', '1.2'],
                   '--ssd-cache': ['-2', '-0.5', 'nan', 'inf', '1e20'],
                   '--vram-experts': ['-0.1', '1.1', 'nan'], '--dspark-conf': ['-1', '1.1', 'inf'],
                   '--dspark-block': ['-1'], '--prefill-chunk': ['-1'],
                   '--ssd-policy': ['random'], '--mmap-experts': ['yes'], '--dspark': ['yes'],
                   '--expert-profile': ['--flag', 'bad\npath'], '--no-expert-cache': ['maybe']}
        for flag, values in invalid.items():
            for value in values:
                with self.subTest(flag=flag, value=value), self.assertRaises(ValueError):
                    parse_value(flag, value)

    def test_rejected_input_and_blank_leave_existing_values_unchanged(self):
        # RAM Expert Store row 0: 'nan' is rejected, the second blank keeps the value.
        with patch.object(tui, 'read_key', side_effect=['enter', 'esc']), \
             patch('builtins.input', side_effect=['nan', '']):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        self.assertIn('Invalid', '\n'.join(self.frames))
        self.assertEqual(self.path.read_bytes(), json.dumps(self.cfg).encode())

    def test_left_right_cycles_enum_choices_and_writes_pending(self):
        # SSD Cache Policy row 2: right cycles lru -> lfu (pending, never written);
        # Esc offers to discard and 'enter' (Yes on the default choice) leaves.
        keys = ['down', 'down', 'right', 'esc', 'esc', 'enter']
        with patch.object(tui, 'read_key', side_effect=keys):
            changes = tui.deepseek_load_options_page(self.item)
        self.assertIsNone(changes)
        self.assertIn('lfu', '\n'.join(self.frames))
        self.assertEqual(self.path.read_bytes(), json.dumps(self.cfg).encode())

    def test_deliberate_load_persists_and_official_launch_cannot_overwrite_choices(self):
        from strata_control import user_args
        from strata_cli.launch.runner import derive_config
        from strata_cli.launch.settings import LaunchSettings
        managed, profile, preset = self.official_fixture()
        profile_before = profile.read_bytes()
        unrelated = preset.with_name('unrelated.json')
        unrelated.write_text(json.dumps({'schema': 'strata.launch/1', 'family': 'qwen',
                                         'model': 'Q2_0', 'context': 8192}))
        unrelated_before = unrelated.read_bytes()
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(LOAD_START_ROW, CONTEXT_ROW) + ['enter']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'), patch.object(tui, 'health', return_value=None), \
             patch.object(tui, 'log_page') as page, patch.object(tui, 'start_item') as start:
            tui.start_via_options(self.item)
        saved = json.loads(self.path.read_text())
        self.assertIn('--max-context', saved.get('user_args', []))
        self.assertEqual(tui.arg_value(saved['args'], '--max-context'), '16384')
        self.assertEqual(saved['args_written'], saved['args'])
        self.assertEqual(tui.arg_value(saved['args'], '--future-option'), 'kept')
        self.assertEqual(saved['model_name'], self.cfg['model_name'])
        self.assertEqual(profile.read_bytes(), profile_before)
        self.assertEqual(unrelated.read_bytes(), unrelated_before)
        self.assertEqual(json.loads(preset.read_text())['context'], 16384)
        self.assertEqual(json.loads(preset.read_text())['extra']['--max-context'], '16384')
        self.assertEqual(json.loads(preset.read_text())['created'], 'preserve-date')
        self.assertEqual(json.loads(managed.read_text())['args'], saved['args'])
        # Exercise official derivation after setup regenerates its old default args.
        regenerated = copy.deepcopy(saved)
        regenerated['args'] = list(self.cfg['args'])
        user_args.apply(regenerated)
        derived = derive_config(regenerated, LaunchSettings.from_dict(json.loads(preset.read_text())), bool_flags=frozenset())
        self.assertEqual(tui.arg_value(derived['args'], '--max-context'), '16384')
        self.assertEqual(page.call_args.args[0], 'LOADING')
        start.assert_not_called()  # log_page mock does not exercise runner; no GPU starts.
        self.assertTrue(list(self.root.glob('.deepseek-backups/*/manifest.json')))
        self.item.cfg = saved
        self.frames.clear()
        with patch.object(tui, 'read_key', side_effect=['esc']):
            tui.deepseek_load_options_page(self.item)
        self.assertIn('16384', self.frames[0])

    def test_external_running_model_saves_pending_never_stops_or_claims_applied(self):
        self.official_fixture()
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(LOAD_START_ROW, CONTEXT_ROW) + ['enter']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'), \
             patch.object(tui, 'health', return_value={'model': self.cfg['model_name'], 'max_context': 8192}), \
             patch.object(tui, 'log_page') as page, patch.object(tui, 'start_item') as start, \
             patch.object(tui, 'stop_owned') as stop:
            tui.start_via_options(self.item)
        self.assertEqual(page.call_args.args[0], 'RELOAD REQUIRED')
        self.assertIn('not applied', page.call_args.args[1])
        self.assertIn('owner', page.call_args.args[1])
        self.assertEqual(tui.arg_value(json.loads(self.path.read_text())['args'], '--max-context'), '16384')
        stop.assert_not_called()
        start.assert_not_called()

    def test_save_action_writes_marks_restart_required_and_never_starts(self):
        self.official_fixture()
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(SAVE_ROW, CONTEXT_ROW) + \
               ['enter', 'esc']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'), patch.object(tui, 'health', return_value=None), \
             patch.object(tui, 'start_item') as start, patch.object(tui, 'log_page') as page:
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        saved = json.loads(self.path.read_text())
        self.assertEqual(tui.arg_value(saved['args'], '--max-context'), '16384')
        self.assertEqual(tui.arg_value(saved['user_args'], '--max-context'), '16384')
        self.assertTrue(saved.get('tui_deepseek_reload_required'))
        self.assertEqual(saved['args_written'], saved['args'])
        self.assertTrue(list(self.root.glob('.deepseek-backups/*/manifest.json')))
        start.assert_not_called()
        page.assert_not_called()
        self.assertIn('Saved.', '\n'.join(self.frames))

    def test_save_refuses_to_overwrite_a_foreign_running_model(self):
        self.official_fixture()
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(SAVE_ROW, CONTEXT_ROW) + \
               ['enter', 'esc', 'enter']
        before = self.path.read_bytes()
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'), \
             patch.object(tui, 'health', return_value={'model': 'different-model'}), \
             patch.object(tui, 'log_page') as page:
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn('ERROR', '\n'.join(self.frames))
        page.assert_not_called()

    def test_reset_to_default_then_save_omits_flags_and_writes_engine_defaults(self):
        self.official_fixture()
        keys = self.down_to(RESET_ROW) + ['enter'] + ['up', 'up', 'enter', 'esc']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch.object(tui, 'health', return_value=None):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        saved = json.loads(self.path.read_text())
        for flag in ('--ssd-cache', '--cpu-threads', '--vram-experts', '--dspark-block',
                     '--prefill-chunk', '--reserve-mib', '--expert-profile'):
            self.assertNotIn(flag, saved['args'], flag)
            self.assertNotIn(flag, saved.get('user_args', []), flag)
        self.assertEqual(tui.arg_value(saved['args'], '--dspark'), 'auto')
        self.assertEqual(tui.arg_value(saved['args'], '--mmap-experts'), 'auto')
        self.assertEqual(tui.arg_value(saved['args'], '--ssd-policy'), 'lru')
        self.assertEqual(tui.arg_value(saved['args'], '--dspark-conf'), '0.5')
        self.assertEqual(tui.arg_value(saved['args'], '--max-context'), '32768')
        self.assertNotIn('--no-expert-cache', saved['args'])
        self.assertEqual(tui.arg_value(saved.get('user_args', []), '--ssd-policy'), 'lru')
        self.assertEqual(tui.arg_value(saved.get('user_args', []), '--mmap-experts'), 'auto')
        self.assertEqual(tui.arg_value(saved.get('user_args', []), '--dspark-conf'), '0.5')
        self.assertEqual(tui.arg_value(saved.get('user_args', []), '--max-context'), '32768')
        self.assertIn('engine defaults', '\n'.join(self.frames))

    def test_reload_configuration_discards_unsaved_edits_and_restores_the_file(self):
        self.official_fixture()
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(RELOAD_ROW, CONTEXT_ROW) + \
               ['enter', 'enter', 'esc']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        self.assertEqual(self.path.read_text(), json.dumps(self.cfg))
        self.assertIn('reloaded', '\n'.join(self.frames).lower())

    def test_unsaved_edits_are_never_silently_dropped(self):
        keys = self.down_to(CONTEXT_ROW) + ['enter'] + self.down_to(RELOAD_ROW, CONTEXT_ROW) + \
               ['enter', 'esc', 'esc', 'enter']
        with patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', return_value='16384'):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        # The first Esc (No on the discard prompt) kept the pending edit and it stays visible
        # as unsaved; the second Esc confirmed discarding. Nothing was written.
        self.assertIn('Unsaved changes', '\n'.join(self.frames))
        self.assertEqual(self.path.read_bytes(), json.dumps(self.cfg).encode())

    def test_pending_reload_survives_reopen_without_new_edits(self):
        self.official_fixture()
        live = {'model': self.cfg['model_name'], 'max_context': 8192}
        with patch.object(tui, 'deepseek_load_options_page', return_value={'--reserve-mib': '900'}), \
             patch.object(tui, 'health', return_value=live), patch.object(tui, 'log_page'):
            tui.start_via_options(self.item)
        self.item.cfg = json.loads(self.path.read_text())
        with patch.object(tui, 'deepseek_load_options_page', return_value={}), \
             patch.object(tui, 'health', return_value=live), patch.object(tui, 'log_page') as page:
            tui.start_via_options(self.item)
        self.assertEqual(page.call_args.args[0], 'RELOAD REQUIRED')
        # Only an explicit Load when not running clears the pending marker.
        with patch.object(tui, 'deepseek_load_options_page', return_value={}), \
             patch.object(tui, 'health', return_value=None), patch.object(tui, 'log_page'):
            tui.start_via_options(self.item)
        self.assertFalse(json.loads(self.path.read_text()).get('tui_deepseek_reload_required'))

    def test_ui_widths_navigation_and_cancel_preserve_everything(self):
        import re
        import unicodedata
        for columns in (32, 48, 80, 120, 180):
            self.frames.clear()
            with patch.object(tui.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, 40))), \
                 patch.object(tui, 'read_key', side_effect=['esc']):
                self.assertIsNone(tui.deepseek_load_options_page(self.item))
            for frame in self.frames:
                for line in frame.splitlines():
                    plain = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', line)
                    cells = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in plain)
                    self.assertLessEqual(cells, columns - 2)
            self.assertIn('DeepSeek 中文', self.frames[0])
            self.assertTrue(any('Save' in f for f in self.frames))
            self.assertTrue(any('Load & Start' in f for f in self.frames))
            self.assertEqual(self.path.read_text(), json.dumps(self.cfg))
        # The RAM store help keeps its warning on a narrow terminal.
        self.frames.clear()
        with patch.object(tui.shutil, 'get_terminal_size', return_value=os.terminal_size((48, 40))), \
             patch.object(tui, 'read_key', side_effect=['enter', 'esc']), \
             patch('builtins.input', return_value=''):
            tui.deepseek_load_options_page(self.item)
        self.assertIn('NOT SSD space', '\n'.join(self.frames))

    def test_noop_and_escape_do_not_write_any_settings_or_backup(self):
        self.official_fixture()
        from deepseek_settings import persist
        before = {p: p.read_bytes() for p in self.root.rglob('*.json')}
        self.assertEqual(persist(self.item, {})[1], [])
        with patch.object(tui, 'read_key', side_effect=['esc']), patch.object(tui, 'log_page') as page:
            tui.start_via_options(self.item)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*.json')})
        page.assert_not_called()
        self.assertFalse((self.root / '.deepseek-backups').exists())

    def test_atomic_failure_rolls_back_all_targets_and_verifies_backups(self):
        import hashlib
        from strata_cli.launch import store
        from deepseek_settings import persist
        managed, profile, preset = self.official_fixture()
        before = {p: p.read_bytes() for p in (self.path, managed, profile, preset)}
        writer = store._private_write
        def fail_on_preset(path, payload):
            if Path(path) == preset:
                raise OSError('injected preset write failure')
            writer(path, payload)
        with patch.object(store, '_private_write', side_effect=fail_on_preset), self.assertRaisesRegex(OSError, 'rolled back'):
            persist(self.item, {'--reserve-mib': '900'})
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        for manifest in self.root.glob('.deepseek-backups/*/manifest.json'):
            for record in json.loads(manifest.read_text()):
                self.assertEqual(hashlib.sha256(Path(record['backup']).read_bytes()).hexdigest(), record['sha256'])

    def test_save_errors_never_launch_and_stale_config_is_not_overwritten(self):
        from deepseek_settings import persist
        self.official_fixture()
        self.path.write_text(json.dumps({**self.cfg, 'port': 8888}))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed since'):
            persist(self.item, {'--reserve-mib': '900'})
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(tui, 'deepseek_load_options_page', return_value={'--reserve-mib': '900'}), \
             patch.object(tui, 'health', return_value=None), patch.object(tui, 'log_page') as page, \
             patch.object(tui, 'start_item') as start:
            tui.start_via_options(self.item)
        self.assertEqual(page.call_args.args[0], 'ERROR')
        start.assert_not_called()

    def test_foreign_live_model_blocks_save_and_all_process_actions(self):
        before = self.path.read_bytes()
        with patch.object(tui, 'deepseek_load_options_page', return_value={'--reserve-mib': '900'}), \
             patch.object(tui, 'health', return_value={'model': 'different-model'}), \
             patch.object(tui, 'log_page') as page, patch.object(tui, 'start_item') as start, \
             patch.object(tui, 'stop_owned') as stop:
            tui.start_via_options(self.item)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(page.call_args.args[0], 'ERROR')
        start.assert_not_called()
        stop.assert_not_called()

    def test_duplicate_saved_flags_show_engine_last_value_and_preserve_legacy_off(self):
        self.item.cfg['args'] += ['--max-context', '16384', '--dspark', 'off']
        self.item.cfg['args_written'] = list(self.item.cfg['args'])
        with patch.object(tui, 'read_key', side_effect=['esc']):
            tui.deepseek_load_options_page(self.item)
        self.assertIn('Context Length: 16384', self.frames[0])
        self.assertIn('off (drafter still loaded)', self.frames[0])

    def test_preset_with_different_explicit_override_is_not_the_same_selection(self):
        from deepseek_settings import persist
        managed, profile, preset = self.official_fixture(extra={'--reserve-mib': '2000'})
        before = {p: p.read_bytes() for p in (managed, profile, preset)}
        persist(self.item, {'--reserve-mib': '900'})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_legacy_unknown_manual_flags_survive_later_official_setup(self):
        from deepseek_settings import persist
        from test_official_families import official
        self.official_fixture()
        self.cfg.pop('args_written')
        self.item.cfg = copy.deepcopy(self.cfg)
        self.path.write_text(json.dumps(self.cfg))
        saved, _ = persist(self.item, {'--reserve-mib': '900'})
        regenerated = copy.deepcopy(self.cfg)
        regenerated['args'] = ['--pack', str(self.root / 'pack'), '--max-context', '8192']
        official.carry_user_args(saved, regenerated)
        self.assertEqual(tui.arg_value(regenerated['args'], '--future-option'), 'kept')

    def test_real_key_editor_handles_backspace_enter_and_escape_discards_all(self):
        # Exercise the console path, not builtins.input's line buffering.
        import io
        stdout_patch = patch.object(tui.sys, 'stdout', io.StringIO())
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        keys = self.down_to(CONTEXT_ROW) + ['enter', *list('16385'), '\b', '4', 'esc']
        with patch.object(tui.sys.stdin, 'isatty', return_value=True), \
             patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', side_effect=AssertionError('Console editor must use raw keys')):
            self.assertIsNone(tui.deepseek_load_options_page(self.item))
        self.assertEqual(self.path.read_text(), json.dumps(self.cfg))
        self.frames.clear()
        keys = self.down_to(CONTEXT_ROW) + ['enter', *list('16385'), '\x7f', '4', 'enter'] + \
               self.down_to(LOAD_START_ROW, CONTEXT_ROW) + ['enter']
        with patch.object(tui.sys.stdin, 'isatty', return_value=True), \
             patch.object(tui, 'read_key', side_effect=keys), \
             patch('builtins.input', side_effect=AssertionError('Console editor must use raw keys')):
            self.assertEqual(tui.deepseek_load_options_page(self.item), {'--max-context': '16384'})

    def test_corrupt_unrelated_preset_does_not_block_matching_model(self):
        from deepseek_settings import persist
        managed, profile, preset = self.official_fixture()
        corrupt = preset.with_name('unrelated-corrupt.json')
        corrupt.write_bytes(b'{broken')
        saved, _ = persist(self.item, {'--reserve-mib': '900'})
        self.assertEqual(tui.arg_value(saved['args'], '--reserve-mib'), '900')
        self.assertEqual(corrupt.read_bytes(), b'{broken')
        self.assertEqual(json.loads(preset.read_text())['extra']['--reserve-mib'], '900')

    def test_backup_failure_keeps_all_originals_and_does_not_launch(self):
        from strata_cli.launch import store
        self.official_fixture()
        before = {p: p.read_bytes() for p in self.root.rglob('*.json')}
        with patch.object(store, '_private_write', side_effect=OSError('injected backup failure')), \
             patch.object(tui, 'deepseek_load_options_page', return_value={'--reserve-mib': '900'}), \
             patch.object(tui, 'health', return_value=None), patch.object(tui, 'log_page') as page, \
             patch.object(tui, 'start_item') as start:
            tui.start_via_options(self.item)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(page.call_args.args[0], 'ERROR')
        start.assert_not_called()

    def test_other_deepseek_pack_and_different_context_preset_remain_untouched(self):
        from deepseek_settings import persist
        managed, profile, preset = self.official_fixture('other-pack')
        other = copy.deepcopy(self.cfg)
        other['args'][1] = str(self.root / 'other-pack')
        managed.write_text(json.dumps(other))
        managed2, profile2, preset2 = self.official_fixture('different-context', context=32768)
        before = {p: p.read_bytes() for p in (managed, profile, preset, managed2, profile2, preset2)}
        persist(self.item, {'--reserve-mib': '900'})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_explicit_user_overrides_unknown_args_aliases_and_reset_roundtrip(self):
        from deepseek_settings import persist
        from strata_control import user_args
        self.official_fixture()
        self.cfg['aliases'] = ['user-alias', '中文模型']
        self.cfg['user_args'] = ['--reserve-mib', '800', '--future-user', 'keep']
        self.item.cfg = copy.deepcopy(self.cfg)
        self.path.write_text(json.dumps(self.cfg))
        with patch.object(tui, 'read_key', side_effect=['esc']):
            tui.deepseek_load_options_page(self.item)
        self.assertIn('800 MiB', self.frames[0])
        saved, _ = persist(self.item, {'--reserve-mib': None, '--ssd-cache': None,
                                       '--cpu-threads': None, '--expert-profile': 'none'})
        self.assertEqual(saved['aliases'], self.cfg['aliases'])
        self.assertNotIn('--reserve-mib', saved['args'])
        self.assertNotIn('--reserve-mib', saved['user_args'])
        self.assertEqual(tui.arg_value(saved['args'], '--future-option'), 'kept')
        self.assertEqual(tui.arg_value(saved['user_args'], '--future-user'), 'keep')
        self.assertEqual(tui.arg_value(saved['args'], '--expert-profile'), 'none')
        self.assertNotIn('--ssd-cache', saved['args'])
        self.assertNotIn('--cpu-threads', saved['args'])
        regenerated = copy.deepcopy(saved)
        regenerated['args'] = list(self.cfg['args'])
        user_args.apply(regenerated)
        self.assertIsNone(tui.arg_value(regenerated['args'], '--ssd-cache'))
        self.assertIsNone(tui.arg_value(regenerated['args'], '--cpu-threads'))
        self.assertEqual(tui.arg_value(regenerated['args'], '--expert-profile'), 'none')

    def test_no_expert_cache_disabled_is_a_bare_flag_in_cfg_and_derived_config(self):
        from deepseek_settings import persist
        from strata_cli.launch.runner import derive_config
        from strata_cli.launch.settings import LaunchSettings
        managed, profile, preset = self.official_fixture()
        saved, _ = persist(self.item, {'--no-expert-cache': 'disabled'})
        self.assertIn('--no-expert-cache', saved['args'])
        self.assertEqual(saved['args'][-1], '--no-expert-cache')
        self.assertEqual(saved.get('user_args'), ['--no-expert-cache'])
        self.assertEqual(json.loads(preset.read_text())['extra']['--no-expert-cache'], '')
        derived = derive_config(saved, LaunchSettings.from_dict(json.loads(preset.read_text())), bool_flags=frozenset())
        self.assertIn('--no-expert-cache', derived['args'])
        self.item.cfg = saved
        saved_again, _ = persist(self.item, {'--no-expert-cache': 'enabled'})
        self.assertNotIn('--no-expert-cache', saved_again['args'])
        self.assertNotIn('--no-expert-cache', saved_again.get('user_args', []))
        self.assertNotIn('--no-expert-cache', json.loads(preset.read_text()).get('extra', {}))

    def test_advanced_key_input_load_reaches_runner_and_matching_preset(self):
        from strata_cli.launch.runner import derive_config
        from strata_cli.launch.settings import LaunchSettings
        managed, profile, preset = self.official_fixture()
        # VRAM Experts (row 7) -> editor '0.75' -> Load & Start (row 14).
        keys = self.down_to(7) + ['enter'] + self.down_to(LOAD_START_ROW, 7) + ['enter']
        with patch.object(tui, 'read_key', side_effect=keys), patch('builtins.input', return_value='0.75'), \
             patch.object(tui, 'health', return_value=None), patch.object(tui, 'log_page') as page:
            tui.start_via_options(self.item)
        runner = page.call_args.kwargs['active'].__self__
        self.assertEqual(tui.arg_value(runner.item.cfg['args'], '--vram-experts'), '0.75')
        saved = json.loads(self.path.read_text())
        derived = derive_config(saved, LaunchSettings.from_dict(json.loads(preset.read_text())), bool_flags=frozenset())
        self.assertEqual(tui.arg_value(derived['args'], '--vram-experts'), '0.75')
        self.assertEqual(tui.arg_value(saved['args'], '--max-context'), '8192')
        self.assertEqual(tui.arg_value(json.loads(managed.read_text())['args'], '--vram-experts'), '0.75')
        # Execute the real runner state machine with a safe fake process only.
        process = unittest.mock.Mock()
        process.poll.return_value = None
        with patch.object(tui, 'start_item', return_value=(process, 8080)) as start, \
             patch.object(tui, 'health', side_effect=[None, {'model': self.cfg['model_name']}, {'model': self.cfg['model_name']}]):
            self.assertTrue(runner.alive())
            self.assertEqual(runner.finish()[0], 'RUNNING')
        start.assert_called_once_with(runner.item)

    def test_narrow_editor_keeps_dspark_unload_semantics_readable(self):
        # DSpark Mode is row 3; its help keeps the none-vs-off meaning on a narrow screen.
        with patch.object(tui.shutil, 'get_terminal_size', return_value=os.terminal_size((32, 40))), \
             patch.object(tui, 'read_key', side_effect=[*(['down'] * 3), 'enter', 'esc']), \
             patch('builtins.input', return_value=''):
            tui.deepseek_load_options_page(self.item)
        self.assertIn('none = drafter not even loaded',
                      ' '.join('\n'.join(self.frames).split()))
        self.assertIn('off = drafting disabled but the drafter is STILL loaded',
                      ' '.join('\n'.join(self.frames).split()))

    def official_fixture(self, slug='deepseek-iq1_m-8k', **overrides):
        from strata_control import paths, profiles
        from strata_cli.launch import store
        state, config = self.root / 'state', self.root / 'config'
        for folder in (state / 'launch', config / 'profiles', config / 'launch-presets'):
            folder.mkdir(parents=True, exist_ok=True)
        for obj, name, value in [(paths, 'state_base', lambda: state),
                                 (profiles, 'profile_dir', lambda: config / 'profiles'),
                                 (store, 'preset_dir', lambda: config / 'launch-presets')]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        managed = state / 'launch' / (slug + '.json')
        managed.write_text(json.dumps(self.cfg))
        profile = config / 'profiles' / ('launch-' + slug + '.json')
        profile.write_text(json.dumps({'schema_version': 1, 'id': 'launch-' + slug,
                                      'source_config': str(managed), 'overrides': {'port': 8080}}))
        preset = config / 'launch-presets' / (slug + '.json')
        preset.write_text(json.dumps({'schema': 'strata.launch/1', 'name': slug,
                    'family': 'deepseek41', 'model': self.item.model, 'context': 8192,
                    'extra': {'--unrelated': 'keep'}, 'created': 'preserve-date', **overrides}))
        return managed, profile, preset


if __name__ == '__main__':
    unittest.main()