"""setup.py runs in Strata's installer mode (as strata launch runs it): no packages, no engine download, component engine."""

import unittest

from test_merged_load import tui


class SetupEnv(unittest.TestCase):
    def test_setup_py_gets_the_installer_mode(self):
        env = tui.setup_env([tui.normal_python(), str(tui.ROOT / "setup.py"), "--family", "qwen", "--yes"])
        self.assertIsNotNone(env)
        self.assertEqual(env["STRATA_CONTROL_INSTALL"], "1")
        self.assertTrue(env["STRATA_INSTALL_DIR"])
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        self.assertNotIn("STRATA_NO_ACQUIRE", env)
        self.assertNotIn("STRATA_NEED_ENGINE", env)

    def test_other_commands_inherit_the_environment(self):
        self.assertIsNone(tui.setup_env([tui.normal_python(), "manager_identity.py"]))
        self.assertIsNone(tui.setup_env([tui.normal_python()]))


if __name__ == "__main__":
    unittest.main()
