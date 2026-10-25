"""DeepSeek benchmark parsing and result history; temp fixtures only, no engine runs."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import re
from datetime import datetime
from types import SimpleNamespace

import deepseek_bench as bench
from test_official_families import tui  # noqa: F401  (imports the TUI: puts the Strata root on sys.path)


SERVE_LINE = ("maya ds4 serve: prompt 2926 tokens = 0 reused + 2926 read in 40451 ms "
              "(72.3 tok/s), 13 generated in 3317 ms (3.92 tok/s); CPU lane 0.4% of the experts")
STORE_LINE = ("store: 96.4% of the CPU lane's experts were in RAM (48.2 hits, 1.8 misses a "
              "token); read 34.1 MB a token (demand and guesses), the CPU lane waited 2.4 ms "
              "a token for them; 500 prefetched, 300 used, 200 given up unused; slots 5514 of "
              "11488, 12 evictions, 0 failed reads; the GPU computed 0.0 experts a token from "
              "the store (0 slots visible)")
VRAM_LINE = ("ds4: CUDA0 layers [0, 39] + head: weights 5.86 GB, experts 1.24 GB "
             "(126 of 15360 in VRAM), caches 0.03 GB, 944 MiB free")
NO_WRITE_LINE = "ds4: CUDA0 layers [0, 39] + head: weights 5.86 GB, experts 1.24 GB, caches 0.03 GB, 1024 MiB free"
RAMSTORE_LINE = "ds4: RAM store: up to 11488 slots of 8.7 MB (100.0 GB), policy 0, 8 reader threads"
STOREFILL_LINE = "ds4: RAM store: 11232 of the profile's hottest experts read in (77.9 GB) in 46.1 s"


class DeepSeekBench(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'engine.log'
        self.results = self.root / 'bench.jsonl'
        self.env = patch.dict(os.environ, {'STRATA_TUI_BENCH_FILE': str(self.results)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = {'arch': 'deepseek41', 'exe': str(self.root / 'engine' / 'strata-dsv41.exe'),
                    'model_name': 'deepseek-v4.1-flash', 'log': str(self.log),
                    'args': ['--pack', str(self.root / 'pack'), '--max-context', '32768',
                             '--ssd-cache', '100', '--cpu-threads', '12',
                             '--dspark', 'auto', '--dspark-conf', '0.65',
                             '--ssd-policy', 'lfulru', '--mmap-experts', 'auto']}

    def test_parse_log_reads_the_last_run_and_store_statistics(self):
        self.log.write_text('\n'.join([SERVE_LINE, STORE_LINE, VRAM_LINE, RAMSTORE_LINE, STOREFILL_LINE, '']),
                            encoding='utf-8')
        stats = bench.parse_log(self.log)
        self.assertEqual(stats['decode_tok_s'], 3.92)
        self.assertEqual(stats['prefill_tok_s'], 72.3)
        self.assertEqual(stats['prompt_tokens'], 2926)
        self.assertEqual(stats['generated_tokens'], 13)
        self.assertEqual(stats['cpu_lane_pct'], 0.4)
        self.assertEqual(stats['store']['hits_pct'], 96.4)
        self.assertEqual(stats['store']['slots'], 5514)
        self.assertEqual(stats['store']['slots_max'], 11488)
        self.assertEqual(stats['store']['evictions'], 12)
        self.assertEqual(stats['store']['failed_reads'], 0)
        self.assertEqual(stats['ram_store_gb'], 100.0)
        self.assertEqual(stats['ram_store_policy'], 0)
        self.assertEqual(stats['store_warm_experts'], 11232)
        self.assertEqual(stats['store_warm_gb'], 77.9)
        self.assertEqual(stats['vram_weights_gb'], 5.86)
        self.assertEqual(stats['vram_experts_gb'], 1.24)
        self.assertEqual(stats['vram_experts_in_vram'], 126)
        self.assertEqual(stats['vram_experts_total'], 15360)
        self.assertEqual(stats['vram_free_mib'], 944)

    def test_parse_log_reads_the_newest_of_several_runs(self):
        older = ("maya ds4 serve: prompt 100 tokens = 0 reused + 100 read in 5000 ms "
                 "(20.0 tok/s), 5 generated in 2000 ms (2.50 tok/s); CPU lane 0.9% of the experts")
        self.log.write_text('\n'.join([older, SERVE_LINE, '']), encoding='utf-8')
        stats = bench.parse_log(self.log)
        self.assertEqual(stats['decode_tok_s'], 3.92)
        self.assertEqual(stats['prompt_tokens'], 2926)

    def test_parse_log_without_an_in_vram_block_still_reads_the_rest(self):
        self.log.write_text('\n'.join([SERVE_LINE, NO_WRITE_LINE, '']), encoding='utf-8')
        stats = bench.parse_log(self.log)
        self.assertEqual(stats['decode_tok_s'], 3.92)
        self.assertEqual(stats['vram_weights_gb'], 5.86)
        self.assertEqual(stats['vram_experts_gb'], 1.24)
        self.assertEqual(stats['vram_free_mib'], 1024)
        self.assertNotIn('vram_experts_in_vram', stats)

    def test_parse_log_missing_or_empty_log_is_an_empty_dict(self):
        self.assertEqual(bench.parse_log(self.root / 'missing.log'), {})
        self.assertEqual(bench.parse_log(''), {})

    def test_config_summary_uses_effective_args_and_keeps_the_saved_ssd_cache(self):
        summary = bench.config_summary(self.cfg)
        self.assertEqual(summary['RAM Expert Store (GB)'], '100 GB RAM')
        self.assertEqual(summary['CPU Threads'], '12 threads')
        self.assertEqual(summary['DSpark Mode'], 'auto (engine decides)')
        self.assertEqual(summary['Context Length'], '32768')
        self.assertEqual(summary['Expert Cache'], 'Enabled (experts move between VRAM and the CPU lane)')
        self.assertEqual(summary['SSD Cache Policy'], 'LFULRU')

    def test_save_and_load_results_roundtrip_with_versions_and_speeds(self):
        engine_dir = self.root / 'engine'
        engine_dir.mkdir(parents=True)
        (engine_dir / 'BUILD.json').write_text(json.dumps({'version': '0.1.49.2'}))
        (engine_dir / 'engine-component.json').write_text(
            json.dumps({'build_id': 'd2d198a49+dsv41+cuda13.0'}))
        self.log.write_text('[strata-benchmark-launch] ' + json.dumps({
            'args': self.cfg['args'], 'engine': '0.1.49.2 (d2d198a49+dsv41+cuda13.0)'}) + '\n' + SERVE_LINE + '\n', encoding='utf-8')
        sample = {'schema': 1, 'status': 'ok', 'process': {'pid': 123,
                  'working_set_bytes': 96 * 2**30, 'private_commit_bytes': 100 * 2**30},
                  'system': {'windows_commit_limit_bytes': 152 * 2**30}}
        with patch.object(bench, 'memory_snapshot', return_value=sample):
            record = bench.save_result(self.cfg)
        self.assertEqual(record['model'], str(self.root / 'pack'))
        self.assertEqual(record['model_version'], 'Unknown')
        self.assertEqual(record['engine'], '0.1.49.2 (d2d198a49+dsv41+cuda13.0)')
        self.assertEqual(record['context'], 32768)
        self.assertEqual(record['decode_tok_s'], 3.92)
        self.assertEqual(record['prefill_tok_s'], 72.3)
        self.assertIsNone(record['ram_gb'])
        self.assertEqual(record['memory_schema'], 1)
        self.assertEqual(record['memory'], sample)
        self.assertEqual(record['args'], self.cfg['args'])
        history = bench.load_results()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['ts'], record['ts'])
        self.assertEqual(history[0]['config']['RAM Expert Store (GB)'], '100 GB RAM')

    def test_unknown_without_launch_and_preserve_history(self):
        self.log.write_text(SERVE_LINE + '\n', encoding='utf-8')
        record = bench.save_result(self.cfg)
        self.assertIsNone(record['args'])
        self.assertEqual(record['config'], {})
        before = self.results.read_bytes()
        bench.delete_result(record)
        self.assertTrue(self.results.read_bytes().startswith(before))
        self.assertEqual(bench.visible_results(), [])
        self.assertIn('Unknown', '\n'.join(bench.detail_lines(record)))

    def test_launch_not_later_settings(self):
        args = list(self.cfg['args'])
        self.log.write_text('[strata-benchmark-launch] ' + json.dumps({'args': args}) + '\n' + SERVE_LINE)
        self.cfg['args'] = ['--ssd-cache', '1']
        record = bench.save_result(self.cfg)
        self.assertEqual(record['args'], args)
        self.assertEqual(record['config']['RAM Expert Store (GB)'], '100 GB RAM')

    def test_history_navigation_and_detail(self):
        rows = [{'ts': str(i), 'decode_tok_s': i} for i in range(9)]
        frames = []
        item = type('Item', (), {'cfg': self.cfg, 'title': 'DeepSeek'})()
        with patch.object(bench, 'visible_results', return_value=rows), \
             patch.object(bench, 'memory_snapshot', return_value={'status': 'unknown'}), \
             patch.object(tui, 'paint', side_effect=frames.append), \
             patch.object(tui, 'read_key', side_effect=['up', 'enter', 'b']), \
             patch.object(tui, 'benchmark_detail') as detail:
            tui.benchmark_page(item)
        detail.assert_called_once_with(item, rows[-1])
        self.assertIn('Best Prefill', frames[0])
        self.assertIn(str(bench.results_path().resolve()), frames[0])

    def test_restore_uses_verified_args_and_backup_writer(self):
        import deepseek_settings as ds
        item = type('Item', (), {'cfg': self.cfg})()
        record = {'schema': 2, 'launch': {'args': self.cfg['args']}, 'args': self.cfg['args']}
        with patch.object(ds, 'persist', return_value=({'restored': True}, ['config.json'])) as persist:
            bench.restore_result(item, record)
        self.assertEqual(persist.call_args.args[1]['--ssd-cache'], '100')
        self.assertTrue(persist.call_args.kwargs['running'])
        self.assertEqual(item.cfg, {'restored': True})

    def test_detail_cancel_never_restores_or_deletes(self):
        item = type('Item', (), {'cfg': self.cfg})()
        with patch.object(tui, 'paint'), \
             patch.object(tui, 'read_key', side_effect=['r', 'd', 'b']), \
             patch.object(tui, 'deepseek_confirm', return_value=False), \
             patch.object(bench, 'restore_result') as restore, \
             patch.object(bench, 'delete_result') as delete:
            tui.benchmark_detail(item, {})
        restore.assert_not_called()
        delete.assert_not_called()

    def test_restore_rejects_unverified_history_without_writes(self):
        with self.assertRaises(ValueError):
            bench.restore_result(None, {'args': ['--ssd-cache', '100']})

    def test_best_decode_and_history_order(self):
        self.results.write_text('\n'.join([
            json.dumps({'ts': '2026-10-24 17:00:00', 'decode_tok_s': 2.17, 'prefill_tok_s': 40.6}),
            json.dumps({'ts': '2026-10-24 17:40:00', 'decode_tok_s': 4.22, 'prefill_tok_s': 73.4}),
            'not json', '',
        ]), encoding='utf-8')
        history = bench.load_results()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['ts'], '2026-10-24 17:40:00')       # newest first
        self.assertEqual(bench.best_decode(history), 4.22)
        self.assertIsNone(bench.best_decode([]))


class MemorySampling(unittest.TestCase):
    def setUp(self):
        self.exe = r'C:\AI\engine\strata-dsv41.exe'
        self.pack = r'E:\Model\pack'
        self.args = ['--serve', '--pack', self.pack]
        self.started = datetime(2026, 10, 25, 11, 22, 52).timestamp()
        self.launch = {'exe': self.exe, 'args': self.args,
                       'started_at': '2026-10-25 11:22:52'}
        self.cfg = {'exe': self.exe, 'args': self.args}
        self.row = {'pid': 123, 'exe': self.exe, 'cmdline': self.args,
                    'create_time': self.started + 1}

    def test_old_csv_split_bug_is_reproducible(self):
        # Quoted thousands separators were split into extra CSV columns.
        small = '"strata-dsv41.exe","123","Console","1","932,000 K"'
        old_column = [p.strip().strip('"') for p in small.split(',')][4]
        self.assertIsNone(re.search(r'([\d,]+)\s*K', old_column))
        self.assertEqual(bench.parse_tasklist_bytes(small, 123, 'strata-dsv41.exe'), 932000 * 1024)
        large = '"strata-dsv41.exe","123","Console","1","91,941,448 K"'
        self.assertEqual(bench.parse_tasklist_bytes(large, 123, 'strata-dsv41.exe'), 91941448 * 1024)

    def test_csv_exact_pid_name_and_unknown_inputs(self):
        good = '"strata-dsv41.exe","123","Console","1","1024 K"'
        other = '"strata-dsv41.exe","999","Console","1","99,999 K"'
        self.assertEqual(bench.parse_tasklist_bytes(other + '\n' + good, 123, 'strata-dsv41.exe'), 2**20)
        for output in ('', 'INFO: No tasks', good, good + '\n' + good,
                       '"strata-dsv41.exe","123","Console","1","1,23 K"'):
            pid = 999 if output == good else 123
            self.assertIsNone(bench.parse_tasklist_bytes(output, pid, 'strata-dsv41.exe'))
        self.assertIsNone(bench.parse_tasklist_bytes(good, 123, 'other.exe'))

    def test_tasklist_is_filtered_by_pid_and_units_are_gib(self):
        with patch.object(bench.os, 'name', 'nt'), patch.object(bench.subprocess, 'check_output',
                return_value='"strata-dsv41.exe","123","Console","1","1,048,576 K"') as call:
            self.assertIsNone(bench.process_memory_gb('strata-dsv41.exe'))
            call.assert_not_called()
            self.assertEqual(bench.process_memory_gb('strata-dsv41.exe', 123), 1.0)
            self.assertIn('PID eq 123', call.call_args.args[0])

    def test_identity_requires_exe_pack_time_serve_and_unique_match(self):
        resolve = lambda rows: bench.resolve_engine_identity(self.cfg, self.launch, rows)
        self.assertEqual(resolve([self.row])['pid'], 123)
        for change in ({'exe': r'C:\other\strata-dsv41.exe'},
                       {'cmdline': ['--serve', '--pack', r'E:\other']},
                       {'cmdline': ['--pack', self.pack]},
                       {'cmdline': self.args + ['--cpu-threads', '99']},
                       {'create_time': self.started + 100}, {'exe': None}):
            self.assertIsNone(resolve([{**self.row, **change}]))
        self.assertIsNone(resolve([self.row, {**self.row, 'pid': 456}]))
        self.assertIsNone(bench.resolve_engine_identity(self.cfg, {}, [self.row]))
        self.assertIsNone(bench.resolve_engine_identity(
            {**self.cfg, 'args': ['--pack', 'different']}, self.launch, [self.row]))

    def _sample(self, process):
        import psutil
        with patch.object(bench.os, 'name', 'nt'), \
             patch.object(bench, 'windows_memory_bytes', return_value={
                 'windows_available_ram_bytes': 8 * 2**30,
                 'windows_commit_total_bytes': 12 * 2**30,
                 'windows_commit_limit_bytes': 24 * 2**30}), \
             patch.object(psutil, 'process_iter', return_value=[SimpleNamespace(info=self.row)]), \
             patch.object(psutil, 'Process', return_value=process):
            return bench.memory_snapshot(self.cfg, self.launch)

    def _process(self):
        p = Mock()
        p.is_running.return_value = True
        p.create_time.return_value = self.row['create_time']
        p.exe.return_value = self.exe
        p.cmdline.return_value = self.args
        p.memory_info.return_value = SimpleNamespace(rss=2**30, private=3 * 2**30, vms=999)
        return p

    def test_working_set_private_commit_and_system_values_stay_separate(self):
        sample = self._sample(self._process())
        self.assertEqual(sample['status'], 'ok')
        self.assertEqual(sample['process']['working_set_bytes'], 2**30)
        self.assertEqual(sample['process']['private_commit_bytes'], 3 * 2**30)
        self.assertEqual(sample['system']['windows_available_ram_bytes'], 8 * 2**30)
        self.assertEqual(sample['system']['windows_commit_limit_bytes'], 24 * 2**30)
        lines = '\n'.join(bench.memory_lines(sample))
        self.assertIn('1.00 GiB (1.07 GB)', lines)
        self.assertIn('Private Commit', lines)
        self.assertIn('Windows Available RAM', lines)

    def test_pid_reuse_during_sample_discards_process_values(self):
        p = self._process()
        p.create_time.side_effect = [self.row['create_time'], self.row['create_time'] + 100]
        sample = self._sample(p)
        self.assertIsNone(sample['process'])
        self.assertEqual(sample['status'], 'unknown')

    def test_missing_private_never_substitutes_vms(self):
        p = self._process()
        p.memory_info.return_value = SimpleNamespace(rss=1000, vms=999999)
        sample = self._sample(p)
        self.assertIsNone(sample['process']['private_commit_bytes'])
        self.assertEqual(sample['status'], 'partial')

    def test_access_denied_uses_csv_only_after_identity_validation(self):
        import psutil
        p = self._process()
        p.memory_info.side_effect = psutil.AccessDenied(123)
        with patch.object(bench, 'tasklist_working_set_bytes', return_value=4096) as fallback:
            sample = self._sample(p)
        fallback.assert_called_once_with(123, 'strata-dsv41.exe')
        self.assertEqual(sample['process']['working_set_bytes'], 4096)
        self.assertIsNone(sample['process']['private_commit_bytes'])

    def test_unknown_identity_never_samples_an_arbitrary_process(self):
        import psutil
        with patch.object(bench.os, 'name', 'nt'), \
             patch.object(bench, 'windows_memory_bytes', return_value={}), \
             patch.object(psutil, 'process_iter', return_value=[SimpleNamespace(info=self.row)]), \
             patch.object(psutil, 'Process') as process, \
             patch.object(bench, 'tasklist_working_set_bytes') as fallback, \
             patch.object(bench, 'launch_evidence') as reread:
            sample = bench.memory_snapshot(self.cfg, {})
        process.assert_not_called()
        fallback.assert_not_called()
        reread.assert_not_called()
        self.assertIsNone(sample['process'])
        self.assertEqual(sample['system'], {})

    def test_process_exit_is_unknown(self):
        import psutil
        p = self._process()
        p.memory_info.side_effect = psutil.NoSuchProcess(123)
        self.assertIsNone(self._sample(p)['process'])

    def test_legacy_warning_does_not_change_record_or_history(self):
        record = {'schema': 2, 'ram_gb': 0.0002}
        before = json.dumps(record, sort_keys=True)
        self.assertIn('UNTRUSTED', '\n'.join(bench.detail_lines(record)))
        self.assertNotIn('0.0', bench.result_row(record))
        self.assertIn('UNTRUSTED', '\n'.join(bench.detail_lines(record)))
        self.assertEqual(json.dumps(record, sort_keys=True), before)


if __name__ == '__main__':
    unittest.main()