"""Focused tests for the VRAM Optimize screen logic (STRATA-TUI / vram.py).

Pure stdlib + unittest, mirroring gui/test_manager.py's style: no GPU, no network,
no engine.  Only temp configs / in-memory args are touched.

    python -m unittest test_vram
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import vram
import manager_config


def cfg(args, **over):
    return {"exe": "C:/fake/strata.exe", "args": list(args),
            "cwd": "C:/fake", "model_name": "test", **over}


class ContextReadOnly(unittest.TestCase):
    def test_max_context_read(self):
        c = cfg(["--max-context", "65536", "--kv", "int8"])
        self.assertEqual(vram.model_context(c), 65536)

    def test_no_context_gives_none(self):
        self.assertIsNone(vram.model_context(cfg([])))


class Reserve(unittest.TestCase):
    def test_default_512_when_absent(self):
        self.assertEqual(vram.model_reserve_mib(cfg([])), 512)

    def test_present_value_used(self):
        c = cfg(["--vram-reserve-mib", "700"])
        self.assertEqual(vram.model_reserve_mib(c), 700)

    def test_invalid_falls_back(self):
        c = cfg(["--vram-reserve-mib", "abc"])
        self.assertEqual(vram.model_reserve_mib(c), 512)

    def test_validation_rejects_negative(self):
        self.assertFalse(vram.validate_reserve_mib(-1))
        self.assertTrue(vram.validate_reserve_mib(0))
        self.assertTrue(vram.validate_reserve_mib(512))


class KvResidency(unittest.TestCase):
    def test_present_kv_resident_wins(self):
        c = cfg(["--max-context", "65536", "--kv-resident", "16384"])
        self.assertEqual(vram.model_kv_vram(c, 65536), 16384)

    def test_default_32768_at_64k(self):
        c = cfg(["--max-context", "65536"])
        self.assertEqual(vram.model_kv_vram(c, 65536), 32768)

    def test_all_resident_below_64k(self):
        c = cfg(["--max-context", "32768"])
        self.assertEqual(vram.model_kv_vram(c, 32768), 32768)


class Invariant(unittest.TestCase):
    def test_kv_ram(self):
        self.assertEqual(vram.kv_ram(65536, 32768), 32768)
        self.assertEqual(vram.kv_ram(65536, 0), 65536)

    def test_reconcile_from_ram(self):
        self.assertEqual(vram.reconcile_kv_vram(65536, 32768), 32768)
        self.assertEqual(vram.reconcile_kv_vram(65536, 0), 65536)

    def test_regression_pair_table(self):
        for kv_vram, kv_ram in ((65536, 0), (49152, 16384), (32768, 32768), (16384, 49152)):
            self.assertEqual(kv_vram + kv_ram, 65536)
            self.assertEqual(vram.kv_ram(65536, kv_vram), kv_ram)

    def test_validation(self):
        ctx = 65536
        self.assertFalse(vram.validate_kv_vram(ctx, -1))
        self.assertFalse(vram.validate_kv_vram(ctx, ctx + 1))
        self.assertFalse(vram.validate_kv_vram(ctx, 1.5))
        self.assertTrue(vram.validate_kv_vram(ctx, 0))
        self.assertTrue(vram.validate_kv_vram(ctx, ctx))


class EffectiveArgs(unittest.TestCase):
    def test_partial_streaming_sets_kv_resident(self):
        c = cfg(["--max-context", "65536", "--kv", "int8", "--expert-cache", "auto"])
        kv_arg, reserve_arg = vram.effective_args(c, 32768, 512)
        self.assertEqual(kv_arg, 32768)
        self.assertEqual(reserve_arg, 512)

    def test_fully_resident_drops_kv_resident(self):
        c = cfg(["--max-context", "65536"])
        kv_arg, _ = vram.effective_args(c, 65536, 512)
        self.assertEqual(kv_arg, 0)

    def test_reference_command_line(self):
        # The effective relevant args must equal --kv-resident 32768 / --vram-reserve-mib 512.
        c = cfg(["--max-context", "65536", "--kv", "int8", "--expert-cache", "auto"])
        kv_arg, reserve_arg = vram.effective_args(c, 32768, 512)
        self.assertEqual(["--kv-resident", str(kv_arg), "--vram-reserve-mib", str(reserve_arg)],
                         ["--kv-resident", "32768", "--vram-reserve-mib", "512"])


LOG = """L1: engine started: ... --max-context 65536 --kv int8
L2: expert cache auto: 5.12 GiB free, 700 MiB reserved (+184 MiB for the draft head) -> 3309 slots
L3: expert cache 3309 slots, 4.26 GiB of VRAM; policy is
L4: expert cache auto: 5.12 GiB free, 700 MiB reserved (+184 MiB for the draft head) -> 3000 slots
L5: expert cache 3000 slots, 3.50 GiB of VRAM
"""


class Estimate(unittest.TestCase):
    def setUp(self):
        self.cal = vram.parse_calibration(LOG)
        self.assertIsNotNone(self.cal)

    def test_parse_most_recent_run_wins(self):
        self.assertEqual(self.cal["slots"], 3000)
        self.assertAlmostEqual(self.cal["cache_gib"], 3.50)

    def test_calibration_baseline_reproduces_count(self):
        # At the calibration's own reserve (700) and full-resident KV, the estimate
        # returns ~the same slots the log reported.
        est = vram.estimate_experts(65536, 65536, 700, "int8", self.cal)
        self.assertIsNotNone(est)
        self.assertLessEqual(abs(est - 3000), 5)

    def test_reducing_kv_vram_increases_estimate(self):
        full = vram.estimate_experts(65536, 65536, 512, "int8", self.cal)
        half = vram.estimate_experts(65536, 32768, 512, "int8", self.cal)
        self.assertIsNotNone(full)
        self.assertIsNotNone(half)
        self.assertGreater(half, full)

    def test_increasing_reserve_decreases_estimate(self):
        low = vram.estimate_experts(65536, 32768, 512, "int8", self.cal)
        high = vram.estimate_experts(65536, 32768, 1200, "int8", self.cal)
        self.assertLess(high, low)

    def test_reference_shapes(self):
        # With the default screen values (reserve 512, KV 32768/32768) the estimate
        # lands in the "≈ 3,xxx" band the screen promises.
        est = vram.estimate_experts(65536, 32768, 512, "int8", self.cal)
        self.assertTrue(3000 <= est < 4000)
        self.assertRegex(vram.format_estimate(est), r"^≈ 3,\d{3}$")

    def test_no_log_means_unavailable(self):
        self.assertIsNone(vram.estimate_experts(65536, 32768, 512, "int8", None))
        self.assertIsNone(vram.calibration_from_log(""))
        self.assertEqual(vram.format_estimate(None), "≈ unavailable")


class Persistence(unittest.TestCase):
    """Write back through manager_identity.apply_settings - the TUI's own config
    mechanism - and read it back as a freshly-launched config would, proving the
    saved --kv-resident / --vram-reserve-mib survive a restart and feed the
    engine's args without touching --kv / --expert-cache."""

    SAMPLE = {"exe": "C:/fake/strata.exe", "cwd": "C:/fake",
              "model_name": "qwen3.8-flash-next-q2_0",
              "args": ["--pack", "C:/fake/p", "--expert-cache", "auto",
                        "--max-context", "65536", "--kv", "int8"]}

    @staticmethod
    def _mid():
        import manager_config as _mc
        root = str(_mc.strata_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        if "setup" not in sys.modules:          # apply_settings imports the official setup.py
            import setup                         # noqa: F401
        import manager_identity as mid
        return mid

    def _apply(self, cfg_dict, **kw):
        d = Path(tempfile.mkdtemp())
        name = "strata-vtest.json"
        p = d / name
        p.write_text(json.dumps(cfg_dict, indent=1), encoding="utf-8")
        ns = types.SimpleNamespace(root=str(d), config=name, context=65536,
                                   vision="no", save_backup=True,
                                   kv_resident=32768, vram_reserve_mib=512)
        for k, v in kw.items():
            setattr(ns, k, v)
        rc = self._mid().apply_settings(ns)
        return rc, p

    def test_persists_and_survives_restart(self):
        rc, p = self._apply(dict(self.SAMPLE))
        self.assertEqual(rc, 0)
        reread = json.loads(p.read_text(encoding="utf-8-sig"))
        a = reread["args"]
        self.assertEqual(vram.arg_value(a, "--kv-resident"), "32768")
        self.assertEqual(vram.arg_value(a, "--vram-reserve-mib"), "512")
        # nothing unrelated changed
        self.assertEqual(vram.arg_value(a, "--max-context"), "65536")
        self.assertEqual(vram.arg_value(a, "--kv"), "int8")
        self.assertEqual(vram.arg_value(a, "--expert-cache"), "auto")
        # the engine sees these when it reads the config args fresh (a "restart")
        fresh = vram.model_kv_vram(reread, 65536), vram.model_reserve_mib(reread)
        self.assertEqual(fresh, (32768, 512))

    def test_fully_resident_drops_kv_resident(self):
        rc, p = self._apply(dict(self.SAMPLE), kv_resident=0)
        self.assertEqual(rc, 0)
        a = json.loads(p.read_text(encoding="utf-8-sig"))["args"]
        self.assertNotIn("--kv-resident", a)
        self.assertEqual(vram.arg_value(a, "--vram-reserve-mib"), "512")

    def test_rejects_invalid_reserve_cli(self):
        # a negative reserve is a no-op (the flag is not written)
        rc, p = self._apply(dict(self.SAMPLE), kv_resident=0, vram_reserve_mib=-5)
        self.assertEqual(rc, 0)
        a = json.loads(p.read_text(encoding="utf-8-sig"))["args"]
        self.assertNotIn("--kv-resident", a)
        self.assertNotIn("--vram-reserve-mib", a)


if __name__ == "__main__":
    unittest.main()