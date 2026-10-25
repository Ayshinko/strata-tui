"""Copy-budget safety/argv tests. Temp files and mocked UI only; no model or GPU."""
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_official_families import tui
import deepseek_settings as ds


class CopyBudgetTests(unittest.TestCase):
    def test_units_values_and_auto(self):
        self.assertIsNone(ds.parse_value('--expert-copy-gb', 'Auto'))
        for value in ('0', '114', '116', '118', '121', '114.5'):
            self.assertEqual(ds.parse_value('--expert-copy-gb', value), value)
        for value in ('nan', 'inf', '-1', '121GiB', '1e99', 'no'):
            with self.assertRaises(ValueError):
                ds.parse_value('--expert-copy-gb', value)
        self.assertIn('HIGH MEMORY RISK', ds.display_value('--expert-copy-gb', '121'))

    def test_argv_and_auto_remove(self):
        cfg = {'args': ['--mmap-experts', 'off', '--ssd-cache', '0'], 'user_args': []}
        for value in ('114', '116', '118', '121'):
            new = ds.edited_config(cfg, {'--expert-copy-gb': value})
            self.assertEqual(ds.flag_value(ds.effective(new), '--expert-copy-gb'), value)
            cleared = ds.edited_config(new, {'--expert-copy-gb': None})
            self.assertIsNone(ds.flag_value(ds.effective(cleared), '--expert-copy-gb'))

    def test_safety_and_engine_selection_temp_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = {'arch': 'deepseek41', 'cwd': str(root), 'args': [
                '--mmap-experts', 'off', '--ssd-cache', '0'], 'user_args': []}
            path = root / 'test.json'
            path.write_text(json.dumps(cfg))
            item = tui.Item('config', 'test', 'READY', path, cfg=cfg, family='deepseek41')
            with self.assertRaises(ValueError):
                ds.select_copy_engine(item, True)
            folder = root / 'engine-dsv41' / 'copy-budget-experimental'
            folder.mkdir(parents=True)
            (folder / 'strata-dsv41.exe').write_bytes(b'test fixture; not executable')
            (folder / 'COPY-BUDGET-EXPERIMENTAL.json').write_text('{}')
            ds.select_copy_engine(item, True)
            self.assertEqual(Path(item.cfg['exe']).parent, folder)
            new = ds.edited_config(item.cfg, {'--expert-copy-gb': '118'})
            ds.validate_copy_config(new)
            for changes in ({'--ssd-cache': '110'}, {'--mmap-experts': 'on'}):
                with self.assertRaises(ValueError):
                    ds.validate_copy_config(ds.edited_config(new, changes))
            stable = root / 'engine-dsv41' / '0.1.49.2-windows-x86_64-cuda-cuda13'
            stable.mkdir()
            (stable / 'strata-dsv41.exe').write_bytes(b'stable fixture')
            item.cfg = new
            path.write_text(json.dumps(new))
            ds.select_copy_engine(item, False)
            self.assertIsNone(ds.flag_value(ds.effective(item.cfg), '--expert-copy-gb'))
            self.assertTrue(list((root / '.deepseek-backups').glob('*.json')))

    def test_tui_manual_transmission_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'strata-dsv41.exe').write_bytes(b'fixture')
            (folder / 'COPY-BUDGET-EXPERIMENTAL.json').write_text('{}')
            cfg = {'arch': 'deepseek41', 'exe': str(folder / 'strata-dsv41.exe'),
                   'args': ['--mmap-experts', 'off', '--ssd-cache', '0'], 'user_args': []}
            item = tui.Item('config', 'test', 'READY', folder / 'cfg.json', cfg=cfg, family='deepseek41')
            self.assertNotIn('--expert-copy-gb', dict((flag, label) for label, flag in ds.fields_for_config(cfg)))
            with patch.object(tui, 'paint'), patch.object(tui, 'read_key', return_value='esc'):
                self.assertIsNone(tui.deepseek_load_options_page(item))

    def test_installed_engine_tui_selection_no_model(self):
        root = Path(tui.ROOT)
        exe = root / 'engine-dsv41/copy-budget-experimental/strata-dsv41.exe'
        if not exe.is_file():
            self.skipTest('experimental engine not built on this host')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.json'
            cfg = {'arch': 'deepseek41', 'cwd': str(root),
                   'args': ['--mmap-experts', 'off', '--ssd-cache', '0'], 'user_args': []}
            path.write_text(json.dumps(cfg))
            item = tui.Item('config', 'test', 'READY', path, cfg=cfg, family='deepseek41')
            # Real TUI selection action, but writes only this temporary fixture.
            select_row = len(ds.ALL_FIELDS) + 4
            with patch.object(tui, 'paint'), \
                 patch.object(tui, 'read_key', side_effect=['down'] * select_row + ['enter', 'esc']), \
                 patch.object(tui, 'deepseek_confirm', return_value=True):
                self.assertIsNone(tui.deepseek_load_options_page(item))
            self.assertEqual(Path(item.cfg['exe']), exe)
            pending = ds.edited_config(item.cfg, {'--expert-copy-gb': '118'})
            ds.validate_copy_config(pending)
            # Only the allocation-free option-check mode; no --pack/--serve, no arming.
            result = subprocess.run([str(exe), *ds.effective(pending), '--check-copy-options'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('expert_copy_gb=118 mmap=0 ssd_cache_gb=0', result.stdout)
            ds.select_copy_engine(item, False)
            self.assertIn('0.1.49.2-windows-x86_64-cuda-cuda13', item.cfg['exe'])
            self.assertIsNone(ds.flag_value(ds.effective(item.cfg), '--expert-copy-gb'))

    def test_saved_manual_budget_cannot_start_without_session_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {'arch': 'deepseek41', 'args': ['--expert-copy-gb', '121'], 'port': 8080}
            item = tui.Item('config', 'test', 'READY', Path(tmp) / 'cfg.json',
                            cfg=cfg, family='deepseek41')
            with patch.object(tui, 'health', return_value=None), \
                 patch.object(tui, 'port_open', return_value=False), \
                 patch.object(tui, 'LOG_FILE', Path(tmp) / 'log'), \
                 patch.object(tui, 'append_log'), \
                 patch.object(tui.subprocess, 'Popen') as spawn:
                self.assertIsNone(tui.start_item(item))
                spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
