import tempfile
import unittest
from pathlib import Path

from gazevoice.unity_install import (
    PACKAGE_DIR,
    PACKAGE_NAME,
    add_bridge,
    bridge_current,
    bridge_installed,
    remove_bridge,
)

from .helpers import fake_unity_project


class InstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = fake_unity_project(Path(self._tmp.name) / "Game")
        self.folder = self.project / "Packages" / PACKAGE_NAME

    def test_a_new_project_has_no_bridge(self):
        self.assertFalse(bridge_installed(self.project))
        self.assertFalse(bridge_current(self.project))

    def test_the_package_lands_in_the_projects_own_packages_folder(self):
        add_bridge(self.project)
        self.assertTrue((self.folder / "package.json").is_file())
        self.assertTrue((self.folder / "Editor" / "GazeVoice.Bridge.Editor.asmdef").is_file())
        self.assertTrue(bridge_current(self.project))

    def test_the_manifest_isnt_touched(self):
        before = (self.project / "Packages" / "manifest.json").read_text()
        add_bridge(self.project)
        self.assertEqual((self.project / "Packages" / "manifest.json").read_text(), before)

    def test_every_file_comes_with_its_meta(self):
        add_bridge(self.project)
        copied = {p.relative_to(self.folder) for p in self.folder.rglob("*")}
        original = {p.relative_to(PACKAGE_DIR) for p in PACKAGE_DIR.rglob("*") if p.name != ".DS_Store"}
        self.assertEqual(copied, original)

    def test_a_changed_copy_isnt_current_and_add_brings_it_up_to_date(self):
        add_bridge(self.project)
        script = self.folder / "Editor" / "GazeVoiceBridge.cs"
        script.write_text("// an older version")
        self.assertTrue(bridge_installed(self.project))
        self.assertFalse(bridge_current(self.project))
        add_bridge(self.project)
        self.assertTrue(bridge_current(self.project))

    def test_a_file_left_over_from_an_older_copy_is_cleared_out(self):
        add_bridge(self.project)
        (self.folder / "Editor" / "Old.cs").write_text("// gone in the new version")
        add_bridge(self.project)
        self.assertFalse((self.folder / "Editor" / "Old.cs").exists())

    def test_no_staging_folder_is_left_behind(self):
        add_bridge(self.project)
        self.assertEqual({p.name for p in (self.project / "Packages").iterdir()}, {"manifest.json", PACKAGE_NAME})

    def test_remove_takes_out_only_our_folder(self):
        add_bridge(self.project)
        remove_bridge(self.project)
        self.assertFalse(self.folder.exists())
        self.assertTrue((self.project / "Packages" / "manifest.json").is_file())

    def test_removing_when_its_not_there_is_fine(self):
        remove_bridge(self.project)


if __name__ == "__main__":
    unittest.main()
