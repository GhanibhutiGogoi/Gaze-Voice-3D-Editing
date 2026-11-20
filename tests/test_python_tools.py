"""Installing a component bootstraps its shared uv dependency without PATH changes."""
import concurrent.futures
from pathlib import Path
import sys
import unittest
from unittest import mock
import zipfile

from gazevoice import platforms, python_tools
from gazevoice.components.blender_mcp import McpServer, server_command
from gazevoice.components.gaze import Gaze
from gazevoice.components.mlagents import MLAgents
from gazevoice.components.speech import Speech
from gazevoice.jobs import JobError
from gazevoice.platforms import venv_executable

from .helpers import TempCtx, make_job


class PythonToolsTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.addCleanup(self.t.cleanup)
        self.folder = self.t.paths.root / "tools" / "uv"
        self.python = venv_executable(self.folder)
        self.binary = venv_executable(self.folder, "uv")
        self.create = [sys.executable, "-m", "venv", "--clear", str(self.folder)]
        self.pip = [str(self.python), "-m", "pip", "install", "--disable-pip-version-check",
                    "--no-input", "--only-binary=:all:", "uv"]
        self.job = make_job()

    def simulate_bootstrap(self, *, pip_code=0, version="uv 0.test\n"):
        self.t.runner.add(self.create)
        self.t.runner.add(self.pip, returncode=pip_code)
        self.t.runner.add([str(self.binary), "--version"], stdout=version)
        original = self.t.runner.stream

        def stream(argv, on_line, **kwargs):
            code = original(argv, on_line, **kwargs)
            if code == 0 and list(argv) == self.create:
                self.python.parent.mkdir(parents=True, exist_ok=True)
                self.python.touch()
                (self.folder / "pyvenv.cfg").write_text("home = test", encoding="utf-8")
            elif code == 0 and list(argv) == self.pip:
                self.binary.touch()
            return code

        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.t.runner, "stream", side_effect=stream).start()

    def test_check_does_not_install_or_create_folders(self):
        self.assertIsNone(python_tools.find_uv(self.t.runner, self.t.paths))
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.t.runner.calls, [])

    def test_existing_path_uv_is_reused_without_install(self):
        self.t.runner.set_which("uv", "existing/uv")
        self.assertEqual(python_tools.ensure_uv(self.t.runner, self.t.paths, self.job), "uv")
        self.assertEqual(self.t.runner.calls, [])
        self.assertFalse(self.folder.exists())

    def test_install_is_immediately_discoverable_and_reused(self):
        self.simulate_bootstrap()
        command = python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        self.assertEqual(command, str(self.binary))
        self.assertEqual(python_tools.find_uv(self.t.runner, self.t.paths), command)
        self.assertEqual(python_tools.ensure_uv(self.t.runner, self.t.paths, self.job), command)
        self.assertEqual(self.t.runner.calls, [tuple(self.create), tuple(self.pip), (command, "--version")])
        self.assertIsNone(self.t.runner.which("uv"))

    def test_failed_download_can_be_retried_without_recreating_environment(self):
        self.simulate_bootstrap(pip_code=1)
        with self.assertRaisesRegex(JobError, "Could not install uv"):
            python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        self.assertIsNone(python_tools.find_uv(self.t.runner, self.t.paths))
        self.t.runner.add(self.pip)
        self.assertEqual(python_tools.ensure_uv(self.t.runner, self.t.paths, self.job), str(self.binary))
        self.assertEqual(self.t.runner.calls.count(tuple(self.create)), 1)

    def test_unverified_binary_is_not_used(self):
        self.simulate_bootstrap(version="unexpected program")
        with self.assertRaisesRegex(JobError, "could not run"):
            python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        self.assertIsNone(python_tools.find_uv(self.t.runner, self.t.paths))

    def test_creation_failure_stops_before_pip(self):
        with self.assertRaisesRegex(JobError, "Could not create uv's Python environment"):
            python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        self.assertEqual(self.t.runner.calls, [tuple(self.create)])

    def test_unrelated_folder_is_left_alone(self):
        self.folder.mkdir(parents=True)
        notes = self.folder / "notes.txt"
        notes.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(JobError, "left alone"):
            python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        self.assertEqual(notes.read_text(encoding="utf-8"), "keep me")
        self.assertEqual(self.t.runner.calls, [])

    def test_concurrent_components_install_shared_tool_once(self):
        self.simulate_bootstrap()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            commands = list(pool.map(lambda _: python_tools.ensure_uv(
                self.t.runner, self.t.paths, make_job()), range(3)))
        self.assertEqual(commands, [str(self.binary)] * 3)
        self.assertEqual(self.t.runner.calls.count(tuple(self.pip)), 1)

    def test_windows_and_macos_managed_executable_layouts(self):
        for windows in (True, False):
            with self.subTest(windows=windows), mock.patch.object(platforms, "is_windows", return_value=windows):
                binary = venv_executable(self.folder, "uv")
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.touch()
                (self.folder / ".gazevoice-ready").touch()
                self.assertEqual(python_tools.find_uv(self.t.runner, self.t.paths), str(binary))

    def test_all_component_install_buttons_continue_after_bootstrap(self):
        for cls in (Speech, Gaze, MLAgents, McpServer):
            with self.subTest(component=cls.__name__):
                # Use a fresh root for each component: none can rely on a prior installation.
                t = TempCtx()
                try:
                    unit = cls(t.ctx)
                    state = unit.check()
                    self.assertTrue(state.offers("install"))
                    self.assertIn("uv", next(a.confirm for a in state.actions if a.id == "install"))
                    managed = str(venv_executable(t.paths.root / "tools" / "uv", "uv"))
                    if isinstance(unit, McpServer):
                        bundle = t.paths.downloads / "bundle.zip"
                        with zipfile.ZipFile(bundle, "w") as archive:
                            archive.writestr("pyproject.toml", "[project]")
                        unit.download = mock.Mock(return_value=bundle)
                    with mock.patch(f"{cls.__module__}.ensure_uv", return_value=managed) as setup, mock.patch.object(
                            t.runner, "stream", return_value=0) as stream, mock.patch.object(
                            Gaze, "_model_checks_out", return_value=True):
                        self.assertEqual(unit.run_action("install", make_job()), {"installed": True})
                    setup.assert_called_once()
                    self.assertEqual(stream.call_args_list[0].args[0][0], managed)
                    self.assertTrue(all(call.args[0][0] != "uv" for call in stream.call_args_list))
                finally:
                    t.cleanup()

    def test_mcp_launch_uses_managed_uv_with_unchanged_path(self):
        self.simulate_bootstrap()
        python_tools.ensure_uv(self.t.runner, self.t.paths, self.job)
        with mock.patch("gazevoice.runner.shutil.which", return_value=None):
            self.assertEqual(server_command(self.t.paths, None)["command"], str(self.binary))
