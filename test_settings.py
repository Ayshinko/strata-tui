"""The settings menu's rules (settings_spec.py). Nothing is saved and no engine is started."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = tempfile.mkdtemp(prefix="strata-tui-test-")          # never the real profiles / verified folders
os.environ["STRATA_TUI_PROFILES"] = os.path.join(_TMP, "profiles")
os.environ["STRATA_TUI_STATE"] = os.path.join(_TMP, "state")

import live_status as L  # noqa: E402
import profiles as P  # noqa: E402
import settings_spec as S  # noqa: E402


def item(key):
    return next(i for i in S.ITEMS if i.key == key)


class Defaults(unittest.TestCase):
    def test_verified_profile_has_no_changed_item(self):
        v = S.verified()
        self.assertEqual(S.changed_items(v, v), [])
        shown = {i.key: i.get(v) for i in S.visible(v)}
        self.assertEqual(shown["max_context"], "262144")
        self.assertEqual(shown["kv"], "int8")
        self.assertEqual(shown["kv_resident"], "20480")
        self.assertEqual(shown["kv_grow"], "off")
        self.assertEqual(shown["prefill"], "auto")
        self.assertEqual((shown["vision"], shown["vision_device"]), ("on", "cpu"))
        self.assertEqual(shown["drafting"], "dflash2")
        self.assertEqual((shown["spec"], shown["draft_conf"], shown["draft_fallback"]), ("6", "0.8", "none"))
        self.assertEqual((shown["vram_reserve"], shown["expert_cache"], shown["route_prior"]), ("360", "auto", "1.5"))
        self.assertEqual((shown["refusal"], shown["refusal_layers"], shown["port"]), ("off", "4 44", "8080"))


class Validation(unittest.TestCase):
    refused = [("max_context", "300000"), ("max_context", "2048"), ("max_context", "abc"), ("kv", "q8"),
               ("kv_resident", "10000"), ("kv_resident", "300000"), ("kv_resident", "-1"), ("prefill", "512"),
               ("prefill", "auto:9"), ("spec", "9"), ("spec", "1"), ("draft_conf", "1.5"), ("vram_reserve", "5000"),
               ("expert_cache", "-3"), ("route_prior", "2.5"), ("vision_tokens", "5000"), ("refusal_scale", "1.2"),
               ("refusal_scale", "-0.5"), ("refusal_layers", "0 44"), ("refusal_layers", "4 48"), ("refusal_layers", "40 4"),
               ("port", "80"), ("mtp_draft_vocab", r"Z:\no\such.bin"), ("refusal_vector", r"Z:\no\such.gguf"),
               ("vision_device", "npu"), ("kv_grow", "maybe")]

    def test_bad_values_are_refused_with_a_reason(self):
        v = S.verified()
        for key, text in self.refused:
            with self.assertRaises(S.Invalid, msg=f"{key}={text}") as cm:
                S.apply(item(key), v, text)
            self.assertTrue(str(cm.exception), key)

    def test_good_values(self):
        v = S.verified()
        for key, text, shown in [("max_context", "65536", "65536"), ("kv", "k8v4", "k8v4"), ("kv_resident", "0", "0"),
                                 ("prefill", "auto:32768", "auto:32768"), ("spec", "8", "8"),
                                 ("draft_conf", "0.35", "0.35"), ("vram_reserve", "444", "444"), ("expert_cache", "1800", "1800"),
                                 ("route_prior", "0", "0"), ("vision_tokens", "1024", "1024"), ("vision_device", "gpu", "gpu"),
                                 ("refusal_scale", "0.75", "0.75"), ("refusal_layers", "6-40", "6 40"), ("port", "8081", "8081")]:
            new = S.apply(item(key), v, text)
            self.assertEqual(item(key).get(new), shown, key)
            self.assertIn(item(key).label, S.changed_items(new, v))
            self.assertEqual(S.verified(), v)            # the input profile is never changed in place

    def test_context_below_kv_resident_is_refused(self):
        v = S.verified()
        with self.assertRaises(S.Invalid):
            S.apply(item("max_context"), v, "16384")       # below 4096? no - below --kv-resident 20480
        small = S.apply(item("kv_resident"), v, "0")
        self.assertEqual(item("max_context").get(S.apply(item("max_context"), small, "16384")), "16384")

    def test_strength_zero_turns_the_vector_off(self):
        v = S.verified()
        on = S.apply(item("refusal"), v, "on")
        self.assertEqual(item("refusal").get(S.apply(item("refusal_scale"), on, "0")), "off")

    def test_locked_items(self):
        v = S.verified()
        for key in ("draft_model", "draft_fallback"):
            with self.assertRaises(S.Invalid):
                S.apply(item(key), v, "x")


class KvGrow(unittest.TestCase):
    def test_needs_kv_resident_zero(self):
        v = S.verified()
        with self.assertRaises(S.Invalid):
            S.apply(item("kv_grow"), v, "on")                    # --kv-resident 20480: the engine would turn it off
        grow = S.apply(item("kv_grow"), S.apply(item("kv_resident"), v, "0"), "on")
        self.assertEqual(item("kv_grow").get(grow), "on")
        with self.assertRaises(S.Invalid):
            S.apply(item("kv_resident"), grow, "20480")

    def test_verified_file_is_read_only(self):
        path = S.verified_file()
        self.assertNotEqual(path, S.TEMPLATE_FILE)
        self.assertFalse(os.access(path, os.W_OK))
        self.assertEqual(S.verified()["args"], P.load(S.TEMPLATE_FILE)["args"])


class Drafting(unittest.TestCase):
    def test_switches_keep_the_owners_values(self):
        v = S.verified()
        mtp = S.apply(item("drafting"), v, "mtp")
        self.assertEqual(mtp["drafting"], "mtp")
        self.assertEqual(S.get_flag(mtp, "--mtp"), r"E:\strata\mtp\rt")
        self.assertEqual((S.get_flag(mtp, "--spec"), S.get_flag(mtp, "--spec-min-p")), ("4", "0.5"))
        for flag in S.DRAFT_FLAGS + ("--mtp-draft-vocab",):
            self.assertFalse(S.has_flag(mtp, flag), flag)
        back = S.apply(item("drafting"), mtp, "dflash2")
        self.assertEqual(L.compare_args(v["args"], back["args"]), [])
        off = S.apply(item("drafting"), v, "off")
        self.assertEqual(off["drafting"], "off")
        self.assertEqual(S.get_flag(off, "--spec"), "6")
        for flag in S.DRAFT_FLAGS + S.MTP_FLAGS:
            self.assertFalse(S.has_flag(off, flag), flag)

    def test_restore(self):
        v = S.verified()
        changed = S.apply(item("spec"), S.apply(item("vram_reserve"), v, "444"), "8")
        self.assertEqual(item("spec").get(S.restore(item("spec"), changed, v)), "6")
        self.assertEqual(S.changed_items(S.restore_all(changed, v), v), [])
        self.assertEqual(S.restore_all(changed, v)["args"], v["args"])
        off = S.apply(item("vision"), v, "off")
        self.assertFalse(S.has_flag(off, "--vision"))
        self.assertEqual(S.changed_items(S.restore(item("vision"), off, v), v), [])
        mtp = S.apply(item("drafting"), v, "mtp")
        self.assertEqual(L.compare_args(v["args"], S.restore(item("drafting"), mtp, v)["args"]), [])   # same options (order may differ)


class StrataKeepsTheSettings(unittest.TestCase):
    """Edited profiles through Strata's own start logic: the engine options must be the profile's (except what Strata owns)."""

    def test_engine_args(self):
        import manager_config
        try:
            root = manager_config.strata_root()
        except RuntimeError as e:
            self.skipTest(str(e))
        sys.path.insert(0, str(root))
        os.environ["STRATA_NO_PRESET"] = "1"
        try:
            from serve import server
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"Strata's server cannot be imported here: {e}")
        components = manager_config.components_dir()
        folder = P.active_engine_folder(components)
        if folder is None:
            self.skipTest("no active engine package")
        exe = str(folder / ("strata.exe" if os.name == "nt" else "strata"))
        v = S.verified()
        cases = {"mtp": S.apply(item("drafting"), v, "mtp"), "off": S.apply(item("drafting"), v, "off"),
                 "edited": S.apply(item("kv_grow"), S.apply(item("kv_resident"), S.apply(item("draft_conf"),
                                                     S.apply(item("max_context"), v, "131072"), "0.5"), "0"), "on")}
        for name, prof in cases.items():
            with tempfile.TemporaryDirectory() as d:
                cfg = P.run_config(prof, strata_root=root, components=components, log_path=Path(d) / "e.log")
                final = server.engine_args(cfg, exe, announce=False)
            self.assertEqual(L.compare_args(P.engine_argv(prof, root), final), [], name)


if __name__ == "__main__":
    unittest.main()
