"""Focused, no-engine tests for the merged LOAD MODEL workflow."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_tui():
    sys.modules.setdefault("setup", types.SimpleNamespace(MODELS={}, FAMILIES={}))
    sys.modules.setdefault("gguf_reader", types.SimpleNamespace(GGUFFile=object))
    path = Path(__file__).with_name("STRATA-TUI.py")
    spec = importlib.util.spec_from_file_location("strata_tui_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tui = load_tui()


def sample_item(path=None):
    path = path or Path("strata-test.json")
    cfg = {
        "model_name": "test", "log": "engine.log", "port": 8080,
        "args": ["--max-context", "65536", "--kv", "int8",
                 "--expert-cache", "auto"],
    }
    return tui.Item("config", "Qwen Test Q2_0", "READY", path, cfg=cfg,
                    family="qwen", model="Q2_0")


class MergedScreen(unittest.TestCase):
    def test_default_allocations_and_per_model_persistence(self):
        item_a = sample_item(Path("model-a.json"))
        item_a.cfg["args"] = ["--max-context", "262144", "--kv-resident", "32768"]
        item_b = sample_item(Path("model-b.json"))
        with tempfile.TemporaryDirectory() as td:
            with patch.object(tui, "SETTINGS_FILE", Path(td) / "settings.json"):
                self.assertEqual(tui.initial_kv_allocation(item_a),
                                 {"kv_vram": 32768, "kv_ram": 32768, "context": 65536})
                self.assertEqual(tui.initial_kv_allocation(item_b),
                                 {"kv_vram": 32768, "kv_ram": 32768, "context": 65536})
                tui.save_kv_allocation(item_a, 32768, 229376)
                self.assertEqual(tui.initial_kv_allocation(item_a),
                                 {"kv_vram": 32768, "kv_ram": 229376, "context": 262144})
                self.assertEqual(tui.initial_kv_allocation(item_b)["context"], 65536)
                tui.save_kv_allocation(item_a, 65536, 458752)
                self.assertEqual(tui.initial_kv_allocation(item_a),
                                 {"kv_vram": 65536, "kv_ram": 458752, "context": 524288})

    def test_262k_ram_editor_full_ui_input_and_reload(self):
        import vram
        item = sample_item()
        item.cfg["args"] = ["--max-context", "262144", "--kv-resident", "32768",
                            "--kv", "int8"]
        context = tui.config_context(item.cfg)
        resident = vram.model_kv_vram(item.cfg, context)
        self.assertEqual((context, resident, vram.kv_ram(context, resident)),
                         (262144, 32768, 229376))
        self.assertIn("229,376", tui.load_options_frame(
            item, context, "Off", 512, resident, None, 0))
        seen = {}
        def prompt(label, current, unit, validator):
            seen.update(label=label, current=current, validator=validator)
            self.assertTrue(validator(229376))
            return 229376
        with patch.object(tui, "paint"), patch.object(
                tui, "read_key", side_effect=["down"] * 4 + ["enter", "esc"]), patch.object(
                tui, "_vram_prompt_int", side_effect=prompt):
            result = tui.load_options_page(item, context, "Off", 512, resident, None)
        self.assertEqual(result, ("back", "Off", 512, 32768, 262144))
        self.assertEqual(seen["current"], 229376)
        self.assertTrue(seen["validator"](229376))
        cmd = tui.build_vram_settings_cmd(item, item.cfg, result[3], 512, context=result[4])
        self.assertEqual(cmd[cmd.index("--kv-resident") + 1], "32768")
        self.assertEqual(tui.config_context(item.cfg), 262144)
        self.assertEqual(vram.kv_ram(tui.config_context(item.cfg),
                                     vram.model_kv_vram(item.cfg, 262144)), 229376)

    def test_ram_and_vram_edits_recompute_context_without_old_total_cap(self):
        item = sample_item()
        item.cfg["args"] = ["--max-context", "65536", "--kv-resident", "15536"]
        frames = []
        def prompt(label, current, unit, validator):
            self.assertEqual(label, "KV IN RAM (tokens)")
            self.assertEqual(current, 50000)
            self.assertTrue(validator(229376))
            return 229376
        keys = ["down"] * 4 + ["enter", "down", "down", "enter"]
        with patch.object(tui, "paint", side_effect=frames.append), patch.object(
                tui, "read_key", side_effect=keys), patch.object(
                tui, "_vram_prompt_int", side_effect=prompt):
            action, vision, reserve, resident, total = tui.load_options_page(
                item, 65536, "Off", 512, 15536, None)
        self.assertEqual((action, resident, total), ("load", 15536, 244912))
        self.assertTrue(any("244,912" in frame and "229,376" in frame for frame in frames))

        # The edited RAM is retained while an independent VRAM edit changes only
        # its component; the displayed total is always their sum.
        prompt_calls = []
        def vram_prompt(label, current, unit, validator):
            prompt_calls.append((label, current, validator(131072)))
            return 131072
        keys = ["down"] * 3 + ["enter", "down", "down", "down", "enter"]
        frames.clear()
        with patch.object(tui, "paint", side_effect=frames.append), patch.object(
                tui, "read_key", side_effect=keys), patch.object(
                tui, "_vram_prompt_int", side_effect=vram_prompt):
            action, vision, reserve, resident, total = tui.load_options_page(
                item, 262144, "Off", 512, 32768, None)
        self.assertEqual((resident, total), (131072, 360448))
        self.assertEqual(prompt_calls[0][:2], ("KV IN VRAM (tokens)", 32768))
        self.assertTrue(any("360,448" in frame and "229,376" in frame for frame in frames))

    def test_unbounded_allocations_save_args_and_reload(self):
        import manager_identity
        import vram
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "strata-test.json"
            original = {"model_name": "test", "args": ["--max-context", "1048576",
                        "--kv", "int8", "--kv-resident", "131072"]}
            path.write_text(json.dumps(original), encoding="utf-8")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            context = vram.model_context(loaded)
            resident = vram.model_kv_vram(loaded, context)
            ram = vram.kv_ram(context, resident)
            self.assertEqual((context, resident, ram), (1048576, 131072, 917504))

            item = tui.Item("config", "test", "READY", path, cfg=loaded)
            command = tui.build_vram_settings_cmd(item, loaded, 131072, 512, context=1048576)
            self.assertEqual(command[command.index("--kv-resident") + 1], "131072")
            args = types.SimpleNamespace(root=td, config=path.name, context=1048576,
                                         vision="", kv_resident=131072,
                                         vram_reserve_mib=512)
            with patch.object(sys.modules["setup"], "derived_factor", lambda ctx: 4, create=True):
                self.assertEqual(manager_identity.apply_settings(args), 0)
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(vram.model_context(reloaded), 1048576)
            self.assertEqual(vram.model_kv_vram(reloaded, 1048576), 131072)
            self.assertEqual(vram.kv_ram(1048576, 131072), 917504)

            huge = 10**30
            self.assertTrue(vram.validate_kv_vram(huge))
            self.assertTrue(vram.validate_kv_ram(huge))
            self.assertEqual(vram.kv_total(huge, huge), huge * 2)

    def test_context_enter_is_read_only_and_escape_writes_nothing(self):
        item = sample_item()
        with patch.object(tui, "paint"), patch.object(
                tui, "read_key", side_effect=["enter", "esc"]), patch.object(
                tui, "_vram_backup") as backup:
            result = tui.load_options_page(item, 65536, "Off", 512, 32768, None)
        self.assertEqual(result, ("back", "Off", 512, 32768, 65536))
        backup.assert_not_called()

    def test_frame_contains_merged_rows_and_no_save(self):
        frame = tui.load_options_frame(sample_item(), 65536, "Off", 512, 32768,
                                       3752, 0)
        for text in ("TOTAL CONTEXT", "Vision", "VRAM reserve", "KV in VRAM",
                     "KV in RAM", "Estimated experts", "Load", "Back"):
            self.assertIn(text, frame)
        self.assertNotIn("\n  Save\n", frame)
        self.assertIn("32,768", frame)

    def test_vram_command_has_only_supported_explicit_args(self):
        cmd = tui.build_vram_settings_cmd(sample_item(), sample_item().cfg, 32768, 512)
        self.assertIn("--kv-resident", cmd)
        self.assertIn("--vram-reserve-mib", cmd)
        self.assertNotIn("--kv-ram", cmd)
        self.assertNotIn("--vision", cmd)
        self.assertNotIn("--context", cmd)


class FakeProc:
    def __init__(self, rc=0):
        self.returncode = rc
    def poll(self):
        return self.returncode


class OrderedRunner(unittest.TestCase):
    def test_commands_finish_in_order_before_serve(self):
        calls = []
        item = sample_item()
        with patch.object(tui, "_spawn_setup",
                          side_effect=lambda cmd, label: calls.append(cmd) or FakeProc()), \
             patch.object(tui, "start_item", side_effect=lambda unused: calls.append("serve") or False):
            runner = tui.LoadRunner(item, [["model-settings"], ["vram-settings"]])
            self.assertFalse(runner.alive()); self.assertIsNone(runner.finish())
            self.assertFalse(runner.alive()); self.assertIsNone(runner.finish())
            self.assertFalse(runner.alive())
        self.assertEqual(calls, [["model-settings"], ["vram-settings"], "serve"])

    def test_failed_command_prevents_serve(self):
        item = sample_item()
        with patch.object(tui, "_spawn_setup", return_value=FakeProc(7)), \
             patch.object(tui, "start_item") as start:
            runner = tui.LoadRunner(item, [["bad"]])
            self.assertFalse(runner.alive())
            self.assertEqual(runner.finish()[0], "ERROR")
        start.assert_not_called()


class LivePath(unittest.TestCase):
    def test_timestamped_backup_contains_original_config(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "strata-test.json"
            config.write_text('{"original": true}', encoding="utf-8")
            backup_name = tui._vram_backup(config)
            backup = config.with_name(backup_name)
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), '{"original": true}')

    def test_load_backs_up_orders_commands_and_enters_log_page(self):
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "strata-test.json"
            config.write_text("{}", encoding="utf-8")
            item = sample_item(config)
            captured = {}
            class Runner:
                def __init__(self, got_item, commands):
                    captured["commands"] = commands
                def alive(self): return False
                def finish(self): return ("ERROR", "done", None)
            with patch.object(tui, "load_options_page",
                              return_value=("load", "Off", 512, 32768, 65536)), \
                 patch.object(tui, "health", return_value=None), \
                 patch.object(tui, "build_load_plan", return_value=["model-settings"]), \
                 patch.object(tui, "LoadRunner", Runner), \
                 patch.object(tui, "_vram_backup", return_value="backup.bak") as backup, \
                 patch.object(tui, "append_log"), \
                 patch.object(tui, "log_page") as log:
                tui.start_via_options(item)
            self.assertEqual(captured["commands"][0], ["model-settings"])
            self.assertIn("--kv-resident", captured["commands"][1])
            backup.assert_called_once_with(config)
            self.assertEqual(log.call_args.args[0], "LOADING")
            self.assertEqual(log.call_args.kwargs["sources"],
                             [tui.LOG_FILE, Path("engine.log")])
            self.assertTrue(callable(log.call_args.kwargs["active"]))
            self.assertTrue(callable(log.call_args.kwargs["finished"]))


class SurgicalPersistence(unittest.TestCase):
    def test_vram_only_update_preserves_vision_kv_and_expert_cache(self):
        import manager_identity
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "strata-test.json"
            original = {
                "vision": {"gpu": True, "max_tokens": 1024},
                "args": ["--max-context", "65536", "--kv", "int8",
                         "--expert-cache", "auto", "--vision"],
            }
            config.write_text(json.dumps(original), encoding="utf-8")
            args = types.SimpleNamespace(
                root=td, config=config.name, context=0, vision="",
                kv_resident=32768, vram_reserve_mib=512)
            self.assertEqual(manager_identity.apply_settings(args), 0)
            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(saved["vision"], original["vision"])
            self.assertEqual(tui.arg_value(saved["args"], "--kv"), "int8")
            self.assertEqual(tui.arg_value(saved["args"], "--expert-cache"), "auto")
            self.assertEqual(tui.arg_value(saved["args"], "--kv-resident"), "32768")
            self.assertEqual(tui.arg_value(saved["args"], "--vram-reserve-mib"), "512")
            self.assertNotIn("--kv-ram", saved["args"])


if __name__ == "__main__":
    unittest.main()
