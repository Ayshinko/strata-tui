"""No GPU, downloads or user-setting writes: installed official-family regression tests."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import manager_config
ROOT = manager_config.strata_root()
sys.path.insert(0, str(ROOT))
from tools.gguf_reader import GGUFFile as OfficialGGUFFile
spec = importlib.util.spec_from_file_location('manager_official_setup_test', ROOT / 'setup.py')
assert spec is not None and spec.loader is not None
official = importlib.util.module_from_spec(spec)
spec.loader.exec_module(official)
sys.modules.setdefault('setup', official)
spec = importlib.util.spec_from_file_location('manager_family_tui_test', HERE / 'STRATA-TUI.py')
assert spec is not None and spec.loader is not None
tui = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tui
spec.loader.exec_module(tui)


class OfficialFamilies(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        for name, value in [('strata_setup', official), ('GGUFFile', OfficialGGUFFile),
                            ('SETTINGS_FILE', self.tmp / 'settings.json'),
                            ('RUNTIME_FILE', self.tmp / 'runtime.json'), ('LOG_FILE', self.tmp / 'tui.log')]:
            p = patch.object(tui, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_gguf(self, path, arch, index=0, count=1, expert_type=29, integer_metadata=False):
        import struct
        def string(s):
            b = s.encode()
            return struct.pack('<Q', len(b)) + b
        md = {'general.architecture': arch, 'split.no': index, 'split.count': count,
              arch + '.expert_count': 384, arch + '.expert_used_count': 6}
        if arch == 'qwen4exp':
            md.update(tui.ARCH_REQUIRED)
        header = b'GGUF' + struct.pack('<IQQ', 3, 2 if integer_metadata else 1, len(md))
        for key, value in md.items():
            header += string(key)
            header += struct.pack('<I', 8) + string(value) if isinstance(value, str) else struct.pack('<II', 4, value)
        header += string('blk.0.ffn_gate_exps.weight') + struct.pack('<IQIQ', 1, 256, expert_type, 0)
        from tools.gguf_reader import BLOCK_GEOMETRY, GGML_TYPES
        payload = 256 // BLOCK_GEOMETRY[GGML_TYPES[expert_type]][0] * BLOCK_GEOMETRY[GGML_TYPES[expert_type]][1]
        if integer_metadata:
            offset = (payload + 31) // 32 * 32
            header += string('engram_token_map.weight') + struct.pack('<IQIQ', 1, 8, 26, offset)
            payload = offset + 32
        path.write_bytes(header + b'\0' * ((-len(header)) % 32 + payload))

    def test_local_deepseek_and_own_path_discovery_is_validated_not_qwen(self):
        import copy
        families = copy.deepcopy(official.MULTI_FILE_FAMILIES)
        ds = families['deepseek41']
        folder = self.tmp / 'DeepSeek-V4.1-Flash-1017-IQ1_M'
        folder.mkdir()
        model = 'IST-DAS-1.75bpw'
        for i, name in enumerate(ds['models'][model]['shards']):
            self.write_gguf(folder / name, 'deepseek41', i, 4)
            ds['sha256'][name] = ((folder / name).stat().st_size, 'test-header-only')
        # Auxiliaries must not become independently selectable models.
        self.write_gguf(folder / official.DSV41_ISTA_ENGRAM, 'deepseek41')
        self.write_gguf(folder / official.DSV41_DSPARK, 'deepseek41')
        with patch.object(official, 'MULTI_FILE_FAMILIES', families):
            good, bad = tui.gguf_items(self.tmp, set())
            self.assertEqual([(i.family, i.model, i.variant, i.status) for i in good],
                             [('deepseek41', model, None, 'PREPARE')])
            self.assertTrue(all(i.status in ('UNSUPPORTED', 'AUXILIARY') for i in bad))
            self.assertTrue(all(i.first_gguf.name not in ds['models'][model]['shards'] for i in bad))
            self.assertIn('deepseek41', good[0].reason)
            self.assertIn('SHA-256', good[0].reason)
            self.assertIn('DSpark', good[0].reason)
            (folder / ds['models'][model]['shards'][2]).unlink()
            good, bad = tui.gguf_items(self.tmp, set())
            self.assertFalse(good)
            self.assertTrue(any('missing' in i.reason.lower() for i in bad))

    def test_deepseek_integer_metadata_uses_its_own_official_reader(self):
        import copy
        families = copy.deepcopy(official.MULTI_FILE_FAMILIES)
        ds = families['deepseek41']
        model = 'IST-DAS-1.75bpw'
        for i, name in enumerate(ds['models'][model]['shards']):
            self.write_gguf(self.tmp / name, 'deepseek41', i, 4, integer_metadata=True)
            ds['sha256'][name] = ((self.tmp / name).stat().st_size, 'fixture')
        with patch.object(official, 'MULTI_FILE_FAMILIES', families):
            check = tui.validate_gguf_headers(self.tmp, 'deepseek41', model)
        self.assertTrue(check.ok, check.reason)

    def test_deepseek_prepare_preserves_backend_defaults_and_existing_context(self):
        item = tui.Item('gguf', 'DeepSeek', 'PREPARE', self.tmp,
                        family='deepseek41', model='IST-DAS-1.75bpw')
        cmd = tui.prepare_command(item)
        self.assertEqual(cmd[1], str(tui.ROOT / 'setup.py'))
        self.assertEqual(cmd[cmd.index('--family') + 1], 'deepseek41')
        self.assertNotIn('--context', cmd)
        for forbidden in ('--vision', '--kv', '--kv-resident', '--vram-reserve-mib', '--variant'):
            self.assertNotIn(forbidden, cmd)
        item.context = 16384
        cmd = tui.prepare_command(item)
        self.assertEqual(cmd[cmd.index('--context') + 1], '16384')
        item.family = 'qwen36'
        item.model = 'Q8_0'
        self.assertEqual(tui.prepare_command(item)[cmd.index('--context') + 1], '16384')

    def test_qwen_component_reference_uses_official_resolution_without_changing_config(self):
        import copy
        from strata_control import engine_component as ec
        pack = self.tmp / 'component-pack'
        tokenizer = pack / 'tokenizer'
        tokenizer.mkdir(parents=True)
        (pack / 'native_experts.txt').write_text('fixture')
        for name in ('vocab.json', 'merges.txt', 'token_type.json'):
            (tokenizer / name).write_text('{}')
        exe = self.tmp / 'strata.exe'
        exe.write_bytes(b'fixture - never executed')
        cfg = {'exe': 'component:strata-engine', 'args': ['--pack', str(pack)],
               'tokenizer': str(tokenizer)}
        original = copy.deepcopy(cfg)
        with patch.object(ec, 'expand', return_value=str(exe)) as resolve:
            ready, problems = tui.config_ready(cfg)
        self.assertTrue(ready, problems)
        resolve.assert_called_once_with('component:strata-engine', cfg, full=False)
        self.assertEqual(cfg, original)
        with patch.object(ec, 'expand', side_effect=ec.EngineMissing('missing component', self.tmp)):
            ready, problems = tui.config_ready(cfg)
        self.assertFalse(ready)
        self.assertTrue(any('missing component' in p for p in problems), problems)

    def test_own_engine_config_readiness_uses_backend_artifacts(self):
        import json
        exe = self.tmp / 'strata-dsv41.exe'
        exe.write_bytes(b'fixture engine - never executed')
        pack = self.tmp / 'pack'
        tok = pack / 'tokenizer'
        tok.mkdir(parents=True)
        for name in ('vocab.json', 'merges.txt', 'token_type.json'):
            (tok / name).write_text('{}')
        (pack / 'tensors.txt').write_text('fixture')
        (pack / 'ds4.txt').write_text('fixture')
        cfg = {'exe': str(exe), 'args': ['--pack', str(pack), '--max-context', '8192'],
               'arch': 'deepseek41', 'tokenizer': str(tok), 'model_name': 'My DeepSeek'}
        path = self.tmp / 'strata-deepseek-v4.1-flash-ist-das-1.75bpw.json'
        path.write_text(json.dumps(cfg))
        with patch.object(official, 'dsv41_pack_info', return_value=(True, 'all present')) as check:
            items = tui.installed_configs(self.tmp)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, 'READY', items[0].reason)
        check.assert_called_once()
        with patch.object(official, 'dsv41_pack_info', return_value=(False, 'MISSING Engram')):
            items = tui.installed_configs(self.tmp)
        self.assertNotEqual(items[0].status, 'READY')
        self.assertIn('Engram', items[0].reason)
        # Own-path Qwen3.6 has no --pack; --mtp 2 is a draft count, not a path.
        cfg['arch'] = 'qwen35moe'
        cfg['exe'] = str(self.tmp / 'strata.exe')
        Path(cfg['exe']).write_bytes(b'fixture')
        gguf = self.tmp / 'Qwen3.6-35B-A3B-Q8_0.gguf'
        gguf.write_bytes(b'fixture')
        cfg['args'] = ['--native', str(gguf), '--mtp', '2', '--max-context', '32768']
        path.unlink()
        path = self.tmp / 'strata-qwen36-q8_0.json'
        path.write_text(json.dumps(cfg))
        items = tui.installed_configs(self.tmp)
        self.assertEqual(items[0].status, 'READY', items[0].reason)

    def test_own_engine_load_keeps_config_and_uses_native_start_routing(self):
        import json
        cfg = {'arch': 'deepseek41', 'exe': 'strata-dsv41.exe',
               'args': ['--pack', 'pack', '--max-context', '8192', '--dspark', 'auto'],
               'model_name': 'My custom DeepSeek', 'aliases': ['ds-local'], 'port': 8989}
        item = tui.Item('config', 'DeepSeek', 'READY', self.tmp / 'strata-ds.json',
                        cfg=cfg, family='deepseek41', model='IST-DAS-1.75bpw')
        before = json.dumps(cfg, sort_keys=True)
        frames = []
        with patch.object(tui, 'paint', side_effect=frames.append), \
             patch.object(tui, 'read_key', side_effect=[*(['down'] * 14), 'enter']), \
             patch.object(tui, 'health', return_value=None), \
             patch.object(tui, 'initial_kv_allocation', side_effect=AssertionError('Qwen allocation used')), \
             patch.object(tui, 'log_page') as logs:
            tui.start_via_options(item)
        self.assertIn('DEEPSEEK PERFORMANCE', frames[0])
        self.assertIn('8192', frames[0])
        self.assertNotIn('KV in VRAM', frames[0])
        self.assertEqual(json.dumps(cfg, sort_keys=True), before)
        self.assertFalse(self.tmp.joinpath('settings.json').exists())
        runner = logs.call_args.kwargs['active'].__self__
        self.assertEqual(runner.pre_commands, [])
        with patch.object(tui, 'health', return_value=None), \
             patch.object(tui, 'port_open', return_value=False), \
             patch.object(tui, 'config_ready', return_value=(True, [])), \
             patch.object(tui.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 54321
            result = tui.start_item(item)
        command = spawn.call_args.args[0]
        self.assertEqual(command[1], str(tui.ROOT / 'serve' / 'server.py'))
        self.assertEqual(command[command.index('--config') + 1], str(item.path))
        self.assertEqual(command[command.index('--port') + 1], '8989')
        self.assertNotIn('--kv', command)
        self.assertIsNotNone(result)

    def test_ready_pack_family_is_discovered_without_a_base_gguf(self):
        model = next(m for m, d in official.MODELS.items() if 'nvfp4' in d.get('families', ()))
        files = official.ready_needed(model, self.tmp)
        first = official.ready_where(self.tmp, files[0])
        first.parent.mkdir(parents=True)
        first.write_bytes(b'partial local pack')
        items, _ = tui.all_items(self.tmp, False, self.tmp)
        packs = [i for i in items if i.family == 'nvfp4']
        self.assertEqual(len(packs), 1)
        self.assertEqual((packs[0].model, packs[0].status), (model, 'PREPARE'))
        self.assertIn('missing', packs[0].reason.lower())
        cmd = tui.prepare_command(packs[0])
        self.assertNotIn('--gguf-dir', cmd)
        self.assertEqual(cmd[cmd.index('--models-dir') + 1], str(self.tmp))
        self.assertEqual(cmd[cmd.index('--family') + 1], 'nvfp4')

    def test_deepseek_repair_recovers_source_from_native_pack_manifest(self):
        pack = self.tmp / 'pack'
        pack.mkdir()
        source = self.tmp / 'DeepSeek files'
        source.mkdir()
        (pack / 'ds4.txt').write_text('format gguf\nmodel_dir ' + str(source) + '\n')
        cfg = {'arch': 'deepseek41', 'args': ['--pack', str(pack)]}
        self.assertEqual(tui.config_source_gguf(cfg), source)

    def test_published_unsloth_shard_overrides_do_not_create_a_fake_variant(self):
        model = 'UD-IQ4_XS'
        family = 'unsloth'
        name = official.model_file(official.FAMILIES[family], model, 1)
        self.assertIsNone(tui.variant_hint(self.tmp, self.tmp / name, family, model))

    def test_ready_pack_config_repair_reuses_models_root_not_dense_gguf_folder(self):
        import json
        family, model = 'nvfp4', 'NVFP4'
        repo = self.tmp / official.MODELS[model]['repo'].replace('/', '--')
        pack = repo / 'pack'
        pack.mkdir(parents=True)
        cfg = {'exe': str(self.tmp / 'missing-strata.exe'), 'tokenizer': str(pack / 'tokenizer'),
               'args': ['--pack', str(pack), '--native', str(repo / 'dense.gguf')], 'model_name': 'mine'}
        path = self.tmp / 'strata-nvfp4-nvfp4.json'
        path.write_text(json.dumps(cfg))
        item = tui.installed_configs(self.tmp)[0]
        self.assertEqual(item.status, 'PREPARE', item.reason)
        cmd = tui.prepare_command(item)
        self.assertEqual(cmd[cmd.index('--models-dir') + 1], str(self.tmp))
        self.assertNotIn('--gguf-dir', cmd)

    def test_shared_engram_is_labelled_auxiliary_not_an_unsupported_base(self):
        name = official.DSV41_ISTA_ENGRAM
        self.write_gguf(self.tmp / name, 'deepseek41')
        good, hidden = tui.gguf_items(self.tmp, set())
        self.assertFalse(good)
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0].status, 'AUXILIARY')
        self.assertIn('not a base model', hidden[0].reason)
        self.assertIsNone(tui.prepare(hidden[0]))

    def test_menu_distinguishes_packs_auxiliaries_and_shows_backend_titles(self):
        items = [tui.Item('pack', 'NVFP4', 'PREPARE', self.tmp),
                 tui.Item('auxiliary', 'Engram', 'AUXILIARY', self.tmp)]
        with patch.object(tui, 'running_status', return_value=None), patch.object(tui, 'paint') as draw:
            tui.render_menu(items, 0, self.tmp, 1, False, '')
        self.assertIn('PACK', draw.call_args.args[0])
        self.assertIn('AUX', draw.call_args.args[0])
        self.assertIn('non-launchable', draw.call_args.args[0])

    def test_stop_never_signals_a_managed_server_from_a_stale_pid_record(self):
        record = {'pid': 15000, 'port': 8080, 'model_name': 'deepseek-v4.1-flash',
                  'config': str(self.tmp / 'strata-ds.json')}
        with patch.object(tui, 'runtime_load', return_value=record), \
             patch.object(tui, 'health', return_value={'model': 'deepseek-v4.1-flash'}), \
             patch.object(tui, 'append_log') as log, \
             patch.object(tui.time, 'sleep'), patch.object(tui.subprocess, 'run') as kill:
            tui.stop_owned()
        kill.assert_not_called()
        self.assertTrue(any('identity' in str(c).lower() for c in log.call_args_list))

    def test_start_records_official_process_identity_for_safe_later_stop(self):
        from strata_control import procs
        item = tui.Item('config', 'Test', 'READY', self.tmp / 'strata-test.json',
                        cfg={'model_name': 'test'}, family='qwen', model='IQ3_S')
        ident = procs.Ident(54321, 'w:fixture', ('python', 'serve/server.py', '--config', str(item.path)))
        with patch.object(tui, 'health', return_value=None), patch.object(tui, 'port_open', return_value=False), \
             patch.object(procs, 'identify', return_value=ident), patch.object(tui.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 54321
            tui.start_item(item)
        rt = tui.runtime_load()
        self.assertEqual(rt.get('process_token'), ident.token)
        self.assertEqual(rt.get('process_cmd'), list(ident.cmd))
        with patch.object(procs, 'is_same', return_value=False), patch.object(tui.subprocess, 'run') as signal:
            tui.stop_owned()
        signal.assert_not_called()

    def test_requested_model_is_not_reported_running_when_port_has_another_model(self):
        item = tui.Item('config', 'Qwen', 'READY', self.tmp / 'strata-test.json',
                        cfg={'model_name': 'qwen-test'}, family='qwen', model='IQ3_S')
        runner = tui.LoadRunner(item)
        runner.serve = False
        with patch.object(tui, 'health', return_value={'model': 'deepseek-v4.1-flash'}):
            result = runner.finish()
        self.assertEqual(result[0], 'ERROR')
        self.assertIn('deepseek-v4.1-flash', result[1])
        self.assertIn('not started', result[1])

    def test_corrupt_published_deepseek_header_never_exits_the_tui(self):
        import copy
        families = copy.deepcopy(official.MULTI_FILE_FAMILIES)
        ds = families['deepseek41']
        model = 'IST-DAS-1.75bpw'
        for i, name in enumerate(ds['models'][model]['shards']):
            self.write_gguf(self.tmp / name, 'deepseek41', i, 4)
            ds['sha256'][name] = ((self.tmp / name).stat().st_size, 'fixture')
        first = self.tmp / ds['models'][model]['shards'][0]
        first.write_bytes(b'BAD!' + first.read_bytes()[4:])
        with patch.object(official, 'MULTI_FILE_FAMILIES', families):
            try:
                check = tui.validate_gguf_headers(self.tmp, 'deepseek41', model)
            except SystemExit:
                self.fail('Backend header reader exited the whole TUI')
        self.assertFalse(check.ok)
        self.assertIn('GGUF', check.reason)

    def test_own_backend_screen_uses_terminal_cells_and_escape_does_not_write(self):
        import os
        import unicodedata
        item = tui.Item('config', 'DeepSeek ' + '模型' * 20, 'READY', self.tmp / 'strata-ds.json',
                        cfg={'arch': 'deepseek41', 'exe': 'strata-dsv41.exe',
                             'args': ['--max-context', '8192']}, family='deepseek41')
        for columns in (40, 80, 120):
            frames = []
            with patch.object(tui.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, 30))), \
                 patch.object(tui, 'paint', side_effect=frames.append), \
                 patch.object(tui, 'read_key', side_effect=['down', 'up', 'esc']):
                self.assertFalse(tui.own_load_options_page(item))
            for frame in frames:
                self.assertIn('DeepSeek', frame)
                self.assertIn('Load', frame)
                self.assertIn('Back', frame)
                for line in frame.splitlines():
                    text = tui.ANSI_RE.sub('', line)
                    cells = sum(0 if unicodedata.combining(c) else
                                2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)
                    self.assertLessEqual(cells, columns - 2, repr(text))
        self.assertFalse(self.tmp.joinpath('settings.json').exists())

    def test_catalog_combinations_use_the_official_identity_and_prepare_route(self):
        import json
        checked = []
        for family, definition in tui.official_families().items():
            for model in tui.official_models(family):
                with self.subTest(family=family, model=model):
                    path = self.tmp / ('strata-' + definition.get('tag', '') + model.lower() + '.json')
                    path.write_text(json.dumps({'args': ['--max-context', '8192']}))
                    got_family, got_model, variant, context = tui.config_identity(path)
                    self.assertEqual((got_family, got_model, variant, context), (family, model, None, 8192))
                    item = tui.Item('pack' if definition.get('ready') else 'gguf', model, 'PREPARE', self.tmp,
                                    family=family, model=model)
                    cmd = tui.prepare_command(item)
                    self.assertEqual(cmd[cmd.index('--family') + 1], family)
                    self.assertEqual(cmd[cmd.index('--model') + 1], model)
                    self.assertIn('--no-start', cmd)
                    self.assertEqual('--gguf-dir' in cmd, not definition.get('ready', False))
                    sentinel = object()
                    with patch.object(tui, '_spawn_setup', return_value=sentinel) as spawn:
                        self.assertIs(tui.prepare(item), sentinel)
                    self.assertEqual(spawn.call_args.args[0], cmd)
                    checked.append((family, model))
        self.assertEqual(len(checked), sum(len(tui.official_models(f)) for f in tui.official_families()))

    def test_own_path_and_gyro_are_header_discovered_and_wrong_architecture_is_refused(self):
        import copy
        own = copy.deepcopy(official.OWN_PATH_FAMILIES)
        definition = own['qwen36']
        model = 'Q8_0'
        name = definition['file'].format(q=model)
        self.write_gguf(self.tmp / name, definition['arch'], expert_type=8)
        definition['sha256'][name] = ((self.tmp / name).stat().st_size, 'fixture')
        with patch.object(official, 'OWN_PATH_FAMILIES', own):
            good, _ = tui.gguf_items(self.tmp, set())
            self.assertEqual([(i.family, i.model) for i in good], [('qwen36', 'Q8_0')])
            self.write_gguf(self.tmp / name, 'llama', expert_type=8)
            definition['sha256'][name] = ((self.tmp / name).stat().st_size, 'fixture')
            good, bad = tui.gguf_items(self.tmp, set())
            self.assertFalse(good)
            self.assertTrue(any('Architecture' in i.reason for i in bad))
        # Gyro is named by the official table, not GGUF_QUANT's IQ/Q regex.
        gyro = self.tmp / official.model_file(official.FAMILIES['gyro'], 'Gyro-S', 1)
        self.write_gguf(gyro, 'qwen4exp', expert_type=144)
        self.assertEqual(tui.official_file_identity(gyro), ('gyro', 'Gyro-S'))
        good, _ = tui.gguf_items(self.tmp, set())
        self.assertEqual([(i.family, i.model, i.variant) for i in good], [('gyro', 'Gyro-S', None)])

    def test_prepare_banner_does_not_invent_a_qwen_context_for_deepseek(self):
        item = tui.Item('gguf', 'DeepSeek', 'PREPARE', self.tmp,
                        family='deepseek41', model='IST-DAS-1.75bpw')
        from types import SimpleNamespace
        with patch.object(tui, 'default_model_root', return_value=self.tmp), \
             patch.object(tui, 'all_items', return_value=([item], 0)), \
             patch.object(tui, 'render_menu'), patch.object(tui, 'full_clear'), \
             patch.object(tui, 'read_key', side_effect=['enter', 'q']), \
             patch.object(tui, 'prepare', return_value=SimpleNamespace(returncode=0, poll=lambda: 0)), \
             patch.object(tui, 'log_page') as page:
            tui.main()
        self.assertEqual(page.call_args.kwargs['extra'], 'Backend default')

    def test_load_page_reports_the_actual_port_owner_without_starting_a_second_model(self):
        item = tui.Item('config', 'Qwen', 'READY', self.tmp / 'strata-qwen.json',
                        cfg={'model_name': 'qwen-test', 'args': ['--max-context', '65536']}, family='qwen', model='IQ3_S')
        with patch.object(tui, 'load_options_page', return_value=('load', 'Off', 512, 32768, 65536)), \
             patch.object(tui, 'health', return_value={'model': 'deepseek-v4.1-flash'}), \
             patch.object(tui, 'log_page') as page, patch.object(tui, 'start_item') as start:
            tui.start_via_options(item)
        start.assert_not_called()
        self.assertEqual(page.call_args.args[0], 'ERROR')
        self.assertIn('deepseek-v4.1-flash', page.call_args.args[1])

    def test_repair_preparation_preserves_native_data_destination(self):
        pack = self.tmp / 'DeepSeek data' / 'packs' / 'deepseek-v4.1-flash-ist-das-1.75bpw'
        item = tui.Item('config', 'DS repair', 'PREPARE', self.tmp / 'strata-ds.json',
                        family='deepseek41', model='IST-DAS-1.75bpw', context=8192,
                        cfg={'args': ['--pack', str(pack)]}, prepare_dir=self.tmp / 'original shards')
        cmd = tui.prepare_command(item)
        self.assertIn('--data-dir', cmd)
        self.assertEqual(cmd[cmd.index('--data-dir') + 1], str(pack.parent.parent))
        self.assertEqual(cmd[cmd.index('--gguf-dir') + 1], str(item.prepare_dir))
        self.assertEqual(cmd[cmd.index('--context') + 1], '8192')

    def test_new_official_family_entries_are_discovered_without_a_tui_whitelist_edit(self):
        import copy
        own = copy.deepcopy(official.OWN_PATH_FAMILIES)
        definition = copy.deepcopy(own['qwen36'])
        definition.update(title='Future official model', tag='future-', file='Future-official-{q}.gguf')
        definition['models'] = {'Q8_0': {}}
        name = definition['file'].format(q='Q8_0')
        self.write_gguf(self.tmp / name, definition['arch'], expert_type=8)
        definition['sha256'] = {name: ((self.tmp / name).stat().st_size, 'fixture')}
        own['future'] = definition
        with patch.object(official, 'OWN_PATH_FAMILIES', own):
            good, _ = tui.gguf_items(self.tmp, set())
            self.assertEqual([(i.family, i.model) for i in good], [('future', 'Q8_0')])
            command = tui.prepare_command(good[0])
            self.assertEqual(command[command.index('--family') + 1], 'future')
            self.assertNotIn('--vision', command)
            self.assertNotIn('--context', command)

    def test_owned_stop_rechecks_identity_before_a_force_signal(self):
        from strata_control import procs
        record = {'pid': 54321, 'port': 8080, 'model_name': 'test',
                  'process_token': 'w:fixture', 'process_cmd': ['python', 'test-server.py']}
        with patch.object(tui, 'runtime_load', return_value=record), \
             patch.object(procs, 'is_same', side_effect=[True, False]), \
             patch.object(tui, 'health', return_value={'model': 'test'}), \
             patch.object(tui, 'append_log'), patch.object(tui.time, 'sleep'), \
             patch.object(tui.os, 'killpg', create=True) as group_signal, \
             patch.object(tui.subprocess, 'run') as signal:
            tui.stop_owned()
        self.assertEqual(signal.call_count + group_signal.call_count, 1,
                         'PID identity changed after the graceful signal')

    def test_every_official_family_title_and_own_model_identity(self):
        families = {**official.FAMILIES, **official.OWN_PATH_FAMILIES, **official.MULTI_FILE_FAMILIES}
        for family, definition in families.items():
            with self.subTest(family=family):
                self.assertEqual(tui.family_title(family), definition['title'])
        family = 'deepseek41'
        model = 'IST-DAS-1.75bpw'
        first = self.tmp / official.MULTI_FILE_FAMILIES[family]['models'][model]['shards'][0]
        self.assertEqual(tui.infer_family(self.tmp, first, 'IQ1_M'), family)
        first = self.tmp / official.OWN_PATH_FAMILIES['qwen36']['file'].format(q='Q8_0')
        self.assertEqual(tui.infer_family(self.tmp, first, 'Q8_0'), 'qwen36')


if __name__ == '__main__':
    unittest.main()
