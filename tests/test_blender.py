import threading
import unittest
from pathlib import Path

from gazevoice.components.base import Health
from gazevoice.components.blender import (
    Blender,
    blender_version,
    extensions_dir,
    find_blender,
    install_extension,
    parse_blender_version,
)
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut

from .helpers import TempCtx, make_job

BREW = {"brew": "/opt/homebrew/bin/brew"}
MDFIND = ["mdfind", "kMDItemCFBundleIdentifier == 'org.blenderfoundation.blender'"]


def fake_app(root: Path, name="Blender.app") -> Path:
    binary = root / name / "Contents" / "MacOS" / "Blender"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    return binary


class ParseVersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_blender_version("Blender 5.2.2 LTS\n\tbuild date: 2026-09-15\n"), (5, 2, 2))
        self.assertEqual(parse_blender_version("Blender 4.5.14 LTS"), (4, 5, 14))
        self.assertEqual(parse_blender_version("Blender 5.3.0 Alpha"), (5, 3, 0))
        self.assertIsNone(parse_blender_version("garbage"))


class FindBlenderTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()

    def tearDown(self):
        self.t.cleanup()

    def test_candidate_wins(self):
        binary = fake_app(self.t.root)
        self.assertEqual(find_blender(self.t.runner, [self.t.root / "Blender.app"]), binary)

    def test_falls_back_to_spotlight(self):
        binary = fake_app(self.t.root / "elsewhere")
        self.t.runner.add(MDFIND, stdout=f"{binary.parents[2]}\n")
        self.assertEqual(find_blender(self.t.runner, [self.t.root / "nope.app"]), binary)

    def test_nothing_found(self):
        self.assertIsNone(find_blender(self.t.runner, [self.t.root / "nope.app"]))


class BlenderComponentTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which=BREW)
        self.component = Blender(self.t.ctx)
        self.component.candidates = [self.t.root / "Blender.app"]

    def tearDown(self):
        self.t.cleanup()

    def _install_fake(self, version_line: str, running: bool) -> Path:
        binary = fake_app(self.t.root)
        self.t.runner.add([str(binary), "--version"], stdout=f"{version_line}\n")
        self.t.runner.add(["pgrep", "-x", "Blender"], returncode=0 if running else 1)
        return binary

    def test_missing_with_brew_offers_install(self):
        state = self.component.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))
        self.assertIsNotNone(next(a for a in state.actions if a.id == "install").confirm)

    def test_missing_without_brew(self):
        self.t.runner.set_which("brew", None)
        state = self.component.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Homebrew", state.summary)
        self.assertEqual(state.actions, ())

    def test_too_old(self):
        self._install_fake("Blender 4.5.14 LTS", running=False)
        state = self.component.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("5.1", state.summary)
        self.assertFalse(state.offers("install"))

    def test_ready_not_running_offers_launch(self):
        self._install_fake("Blender 5.2.2 LTS", running=False)
        state = self.component.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Blender 5.2.2 · not running")
        self.assertTrue(state.offers("launch"))
        self.assertIn(("Version", "5.2.2"), state.details)

    def test_ready_running(self):
        self._install_fake("Blender 5.2.2 LTS", running=True)
        state = self.component.check()
        self.assertEqual(state.summary, "Blender 5.2.2 · running")
        self.assertFalse(state.offers("launch"))

    def test_launch_uses_online_mode(self):
        binary = self._install_fake("Blender 5.2.2 LTS", running=False)
        app = str(binary.parents[2])
        self.t.runner.add(["open", "-a", app, "--args", "--online-mode"])
        self.component.run_action("launch", make_job("blender", "launch"))
        self.assertIn(("open", "-a", app, "--args", "--online-mode"), self.t.runner.calls)

    def test_install_streams_brew(self):
        self.t.runner.add(["brew", "install", "--cask", "blender"], stdout="==> Installing Cask blender\n")
        job = make_job("blender", "install")
        self.component.run_action("install", job)
        self.assertIn("==> Installing Cask blender", job.lines)

    def test_install_failure_raises(self):
        self.t.runner.add(["brew", "install", "--cask", "blender"], returncode=1, stdout="Error: oops\n")
        with self.assertRaises(JobError):
            self.component.run_action("install", make_job("blender", "install"))


class ProbeSharingTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()

    def tearDown(self):
        self.t.cleanup()

    def test_version_runs_once_for_concurrent_callers(self):
        binary = fake_app(self.t.root)
        self.t.runner.add([str(binary), "--version"], stdout="Blender 5.2.2 LTS\n", delay=0.3)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(blender_version(self.t.runner, binary, self.t.ctx.probes)))
            for _ in range(3)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(results, [(5, 2, 2)] * 3)
        self.assertEqual(self.t.runner.calls.count((str(binary), "--version")), 1)

    def test_spotlight_timeout_is_still_checking_not_missing(self):
        self.t.runner.add(MDFIND, returncode=124, stderr="mdfind: timed out after 5s")
        with self.assertRaises(ProbeTimedOut):
            find_blender(self.t.runner, [self.t.root / "nope.app"], self.t.ctx.probes)


class ExtensionHelpersTest(unittest.TestCase):
    def test_extensions_dir(self):
        home = Path("/Users/me")
        self.assertEqual(
            extensions_dir((5, 2, 2), home=home),
            home / "Library/Application Support/Blender/5.2/extensions/user_default",
        )

    def test_install_extension(self):
        t = TempCtx()
        try:
            binary, zip_path = Path("/Applications/Blender.app/Contents/MacOS/Blender"), t.root / "mcp-1.0.3.zip"
            argv = [str(binary), "--command", "extension", "install-file", "-r", "user_default", "-e", str(zip_path)]
            t.runner.add(argv, stdout="Installed \"mcp\"\n")
            job = make_job()
            install_extension(t.runner, binary, zip_path, job)
            self.assertIn(tuple(argv), t.runner.calls)
            t.runner.add(argv, returncode=1, stdout="Error\n")
            with self.assertRaises(JobError):
                install_extension(t.runner, binary, zip_path, make_job())
        finally:
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
