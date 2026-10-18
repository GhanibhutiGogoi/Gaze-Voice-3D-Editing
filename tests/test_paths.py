import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gazevoice.paths import AppPaths, default_paths, tilde


class PathsTest(unittest.TestCase):
    def test_env_override_and_ensure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"GAZEVOICE_HOME": tmp}):
                paths = default_paths()
            self.assertEqual(paths.root, Path(tmp))
            paths.ensure()
            for d in (paths.downloads, paths.venvs, paths.logs, paths.work):
                self.assertTrue(d.is_dir(), d)

    def test_default_root_is_application_support(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GAZEVOICE_HOME", None)
            paths = default_paths()
        self.assertEqual(paths.root, Path.home() / "Library" / "Application Support" / "GazeVoice")

    def test_tilde(self):
        self.assertEqual(tilde(Path.home() / "Library" / "x"), "~/Library/x")
        self.assertEqual(tilde("/opt/homebrew/bin"), "/opt/homebrew/bin")
        self.assertEqual(tilde(str(Path.home()) + "-other/x"), str(Path.home()) + "-other/x")

    def test_named_locations(self):
        paths = AppPaths(Path("/x"))
        self.assertEqual(paths.settings_file, Path("/x/settings.json"))
        self.assertEqual(paths.mcp_server_dir, Path("/x/blender-mcp"))


if __name__ == "__main__":
    unittest.main()
