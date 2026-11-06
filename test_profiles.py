"""Profiles, the live status parser and the update check. No engine is started and no Strata file is written."""

from __future__ import annotations

import json
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
import update_check as U  # noqa: E402

DEFAULT = HERE / "profiles-default" / "qwen3.8-iq3s-dflash2.json"
VERIFIED = ["--max-context", "262144", "--kv", "int8", "--kv-resident", "20480", "--vram-reserve-mib", "360",
            "--expert-cache", "auto", "--prefill", "auto", "--vision", "--draft", "dflash2", "--draft-model",
            r"E:\strata\dflash2\qwen3.8-flash-next-dflash2.q4_0-selq4.pack", "--draft-fallback", "none",
            "--mtp-draft-vocab", r"E:\strata\mtp\rt\draft_vocab.bin", "--spec", "6", "--draft-conf", "0.8"]


def default_profile() -> dict:
    return P.load(DEFAULT)


class DefaultProfile(unittest.TestCase):
    def test_holds_the_verified_options_verbatim(self):
        prof = default_profile()
        self.assertEqual(prof["args"], VERIFIED)
        self.assertEqual(prof["env"], {"STRATA_ROUTE_PRIOR": "1.5"})
        self.assertEqual(prof["drafting"], "dflash2")
        self.assertEqual(prof["port"], 8080)
        self.assertFalse(prof["refusal"]["enabled"])

    def test_run_config_puts_every_option_in_args_and_user_args(self):
        prof = default_profile()
        with tempfile.TemporaryDirectory() as d:
            cfg = P.run_config(prof, strata_root=Path(d), components=Path(d), log_path=Path(d) / "e.log")
        self.assertEqual(cfg["args"], cfg["user_args"])
        self.assertEqual(cfg["args"][8:], VERIFIED)
        self.assertEqual(cfg["drafting"], "dflash2")
        self.assertEqual(cfg["env"], {"STRATA_ROUTE_PRIOR": "1.5"})
        self.assertFalse(cfg["vision"]["gpu"])
        self.assertNotIn("sampling", cfg)


class Refusal(unittest.TestCase):
    def test_flags_follow_the_documented_form(self):
        prof = default_profile()
        prof["refusal"].update(enabled=True, scale=0.5, layers=[6, 40])
        root = Path(r"C:\Strata")
        self.assertEqual(P.refusal_args(prof["refusal"], root), [
            "--control-vector-scaled", str(root / P.BUILTIN_VECTOR) + ":0.5", "--control-vector-layer-range", "6", "40",
            "--cvec-mode", "project", "--cvec-dir", "per-layer"])
        self.assertEqual(P.refusal_label(prof), "on x0.5 L6-40 builtin")

    def test_external_vector_and_per_request_default(self):
        prof = default_profile()
        prof["refusal"].update(enabled=True, vector=r"D:\v\mine.gguf", per_request_default=False)
        with tempfile.TemporaryDirectory() as d:
            cfg = P.run_config(prof, strata_root=Path(d), components=Path(d), log_path=Path(d) / "e.log")
        self.assertIn(r"D:\v\mine.gguf:1", cfg["args"])
        self.assertEqual(cfg["sampling"], {"experimental_speed_projection": False})

    def test_off_adds_nothing(self):
        prof = default_profile()
        self.assertEqual(P.refusal_args(prof["refusal"], Path(".")), [])

    def test_bad_values_are_refused(self):
        for bad in ({"scale": 0}, {"scale": True}, {"layers": [44, 4]}, {"layers": [4]}, {"mode": "x"}, {"dir": "x"}):
            prof = default_profile()
            prof["refusal"].update(bad)
            with self.assertRaises(P.ProfileError, msg=str(bad)):
                P.validate(prof)


class Validation(unittest.TestCase):
    def test_model_and_cvec_flags_belong_elsewhere(self):
        for extra in (["--pack", "x"], ["--cvec-mode", "project"]):
            prof = default_profile()
            prof["args"] = prof["args"] + extra
            with self.assertRaises(P.ProfileError):
                P.validate(prof)

    def test_vision_flag_and_mode_agree(self):
        prof = default_profile()
        prof["vision"]["mode"] = "off"
        with self.assertRaises(P.ProfileError):
            P.validate(prof)

    def test_save_keeps_a_backup_and_save_as_refuses_overwrite(self):
        prof = default_profile()
        with tempfile.TemporaryDirectory() as d:
            path = P.save_as(Path(d), "mine", prof)
            prof["name"] = "changed"
            P.save(path, prof)
            self.assertTrue(path.with_suffix(".json.bak").is_file())
            self.assertEqual(P.load(path)["name"], "changed")
            with self.assertRaises(P.ProfileError):
                P.save_as(Path(d), "mine", prof)
            with self.assertRaises(P.ProfileError):
                P.save_as(Path(d), "bad name", prof)


LOG = r"""[strata] 2026-11-05 22:00:00 engine started: C:\S\strata.exe --serve --pack E:\p --native E:\n.gguf --spec 6 --draft dflash2 --control-vector-scaled C:\v.gguf:0.5 --control-vector-layer-range 4 44
strata generate: expert cache auto: 5.38 GiB free, 360 MiB reserved -> 1777 slots
strata generate: control vector mode = project, dir = per-layer, layers 4..44 (41 steered)
strata generate: session is up (engine 0.1.57)
strata serve: DFlash2 drafter on (E:\x.pack, graphs captured in 3 ms); windows up to 8 tokens
"""


class Protection(unittest.TestCase):
    def test_verified_profile_cannot_be_saved_or_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "qwen3.8-iq3s-dflash2.json"
            path.write_bytes(DEFAULT.read_bytes())
            with self.assertRaises(P.ProfileError):
                P.save(path, default_profile())
            with self.assertRaises(P.ProfileError):
                P.soft_delete(path)
            with self.assertRaises(P.ProfileError):
                P.save_as(Path(d) / "x", "qwen3.8-iq3s-dflash2", default_profile())
            self.assertEqual(path.read_bytes(), DEFAULT.read_bytes())

    def test_soft_delete_moves_the_file_and_its_backup(self):
        with tempfile.TemporaryDirectory() as d:
            path = P.save_as(Path(d), "long-chat", default_profile())
            P.save(path, default_profile())                       # makes long-chat.json.bak
            dest = P.soft_delete(path)
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".json.bak").exists())
            self.assertEqual(dest.parent, Path(d) / P.TRASH)
            self.assertTrue(dest.is_file() and dest.with_suffix(".json.bak").is_file())
            self.assertEqual(P.load(dest)["name"], default_profile()["name"])
            again = P.save_as(Path(d), "long-chat", default_profile())
            self.assertNotEqual(P.soft_delete(again), dest)       # a second delete never overwrites the first


class LiveStatus(unittest.TestCase):
    def test_parses_the_last_start(self):
        s = L.parse_start(L.last_start("old\n[strata] x engine started: C:\\a --serve --mtp E:\\m\nstrata mtp: draft layer loaded\n" + LOG))
        self.assertEqual((s["version"], s["drafter"], s["slots"], s["cvec_scale"]), ("0.1.57", "dflash2", 1777, "0.5"))
        self.assertEqual(s["cvec"]["steered"], 41)
        self.assertEqual(s["argv"][:2], ["--pack", r"E:\p"])

    def test_mtp_and_no_drafter(self):
        mtp = L.parse_start("[strata] engine started: C:\\a --serve --mtp E:\\m\nstrata mtp: draft layer loaded, 837 MiB\n"
                            "strata generate: session is up (engine 0.1.57)\n")
        self.assertEqual(mtp["drafter"], "mtp")
        off = L.parse_start("[strata] engine started: C:\\a --serve --spec 4\nstrata generate: session is up (engine 0.1.57)\n")
        self.assertEqual(off["drafter"], "off")

    def test_compare_args(self):
        self.assertEqual(L.compare_args(["--a", "1", "--b"], ["--b", "--a", "1"]), [])
        self.assertEqual(L.compare_args(["--a", "1", "--b"], ["--a", "2", "--c", "x"]),
                         ["changed --a: 1 -> 2", "missing --b", "added --c x"])

    def test_status_line(self):
        snap = {"start": L.parse_start(LOG), "metrics": {"avg_tok_s": 121.9, "last_tok_s": 74.7, "slots": 2292},
                "diffs": [], "compared": True}
        line = L.status_line(snap)
        for part in ("engine 0.1.57", "DFlash2", "refusal-vector ON x0.5 L4-44", "avg 121.9", "2292 slots", "args = profile"):
            self.assertIn(part, line)
        snap["compared"] = False
        self.assertNotIn("args = profile", L.status_line(snap))


class UpdateCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.root, self.comp, self.state = t / "strata", t / "components", t / "state"
        self.launch, self.presets = t / "launch", t / "presets"
        for d in (self.root, self.launch, self.presets, self.comp / "engine" / "0.1.57-win", self.comp / ".updates"):
            d.mkdir(parents=True)
        (self.comp / ".updates" / "state.json").write_text(json.dumps({"engine": {"active": "0.1.57-win"}}))
        self.prof = default_profile()

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self):
        return U.check(self.prof, strata_root=self.root, components=self.comp, state=self.state,
                       launch_dir=self.launch, preset_dir=self.presets)

    def test_first_check_only_remembers(self):
        r = self.run_check()
        self.assertEqual((r["version"], r["previous"], r["changed"]), ("0.1.57", None, False))
        self.assertEqual(U.last_seen(self.state), "0.1.57")
        self.assertFalse(self.run_check()["changed"])

    def test_changed_engine_lists_differences_and_writes_nothing_of_strata(self):
        U.mark_seen(self.state, "0.1.56")
        (self.launch / "iq3.json").write_text(json.dumps({"model_name": self.prof["model_name"], "drafting": "dflash2",
                                                          "env": {"STRATA_ROUTE_PRIOR": "1.5"},
                                                          "args": P.engine_argv(self.prof, self.root)}))
        (self.presets / "iq3.json").write_text(json.dumps({"drafting": "dflash2",
                                                           "extra": {"--vram-reserve-mib": "444", "STRATA_ROUTE_PRIOR": "1.5"}}))
        before = {p: p.read_bytes() for p in (self.launch / "iq3.json", self.presets / "iq3.json")}
        r = self.run_check()
        self.assertTrue(r["changed"])
        found = dict(r["sources"])
        self.assertEqual(found[str(self.launch / "iq3.json")], [])
        self.assertEqual(found[str(self.presets / "iq3.json") + " (preset extra)"], ["changed --vram-reserve-mib: 360 -> 444"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})


class StrataKeepsTheProfile(unittest.TestCase):
    """The run config through Strata's own start logic (serve/server.py engine_args): the engine options must be the profile's."""

    def test_engine_args_equal_the_profile(self):
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
        prof = default_profile()
        for enabled in (False, True):
            prof["refusal"]["enabled"] = enabled
            with tempfile.TemporaryDirectory() as d:
                cfg = P.run_config(prof, strata_root=root, components=components, log_path=Path(d) / "e.log")
                final = server.engine_args(cfg, str(folder / ("strata.exe" if os.name == "nt" else "strata")), announce=False)
            self.assertEqual(L.compare_args(P.engine_argv(prof, root), final), [], f"refusal {enabled}")


if __name__ == "__main__":
    unittest.main()
