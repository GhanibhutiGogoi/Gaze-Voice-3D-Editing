"""Regressions for the macOS/Windows support review. No installs or model downloads."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock

from gazevoice import platforms, python_tools, settings, speech_worker
from gazevoice.components import blender, microphone, speech, unity
from gazevoice.jobs import JobError
from gazevoice.runner import CommandRunner, FakeRunner
from gazevoice.voice import leftovers
from .helpers import TempCtx, make_job


class ReviewRegressionsTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={"uv": "uv"})
        self.addCleanup(self.t.cleanup)

    def test_trainer_script_imports_installed_package_instead_of_local_module(self):
        package = self.t.root / "site-packages" / "mlagents"
        (package / "trainers").mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "trainers" / "__init__.py").touch()
        (package / "trainers" / "learn.py").write_text(
            "def main():\n    import sys\n    print('installed trainer ' + sys.argv[1])\n", encoding="utf-8")
        worker = Path(platforms.__file__).with_name("trainer_worker.py")
        result = subprocess.run([sys.executable, str(worker), "--help"],
                                env={**os.environ, "PYTHONPATH": str(package.parent)},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "installed trainer --help")

    def test_rosetta_detects_apple_hardware_and_intel_does_not(self):
        with mock.patch.object(platforms.sys, "platform", "darwin"), mock.patch.object(
                platforms.platform, "machine", return_value="x86_64"), mock.patch.object(
                platforms.subprocess, "run") as run:
            for answer, expected in (("1\n", True), ("0\n", False)):
                run.return_value = subprocess.CompletedProcess([], 0, answer, "")
                self.assertEqual(platforms.is_apple_silicon(), expected)
            run.side_effect = subprocess.TimeoutExpired("sysctl", 3)
            self.assertFalse(platforms.is_apple_silicon())
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_native_apple_and_nonmac_do_not_probe_sysctl(self):
        with mock.patch.object(platforms.subprocess, "run") as run, mock.patch.object(
                platforms.platform, "machine", return_value="arm64"):
            with mock.patch.object(platforms.sys, "platform", "darwin"):
                self.assertTrue(platforms.is_apple_silicon())
            with mock.patch.object(platforms.sys, "platform", "win32"):
                self.assertFalse(platforms.is_apple_silicon())
            run.assert_not_called()

    def test_windows_gui_detaches_without_joining_owned_worker_job(self):
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
                platforms, "command_argv", side_effect=lambda argv: argv), mock.patch.object(
                platforms.subprocess, "Popen") as popen, mock.patch.object(
                platforms.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), mock.patch.object(
                platforms.subprocess, "DETACHED_PROCESS", 8, create=True):
            platforms.launch_app(FakeRunner(), Path("blender.exe"))
        self.assertEqual(popen.call_args.kwargs["creationflags"], 520)
        popen.return_value.poll.assert_not_called()

    def test_failed_ctrl_break_closes_job_and_exited_root_still_reclaims_descendants(self):
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
                platforms.signal, "CTRL_BREAK_EVENT", 1, create=True):
            for code in (6, 87):
                proc = mock.Mock()
                proc.poll.return_value = None
                proc.send_signal.side_effect = OSError(code, "no console")
                platforms.signal_process_tree(proc, signal.SIGINT)
                proc.close_tree.assert_called_once()
            proc = mock.Mock()
            proc.poll.return_value = 0
            platforms.signal_process_tree(proc, signal.SIGINT)
            proc.send_signal.assert_not_called()
            proc.close_tree.assert_called_once()

    def test_successful_ctrl_break_preserves_save_on_interrupt(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
                platforms.signal, "CTRL_BREAK_EVENT", 1, create=True):
            platforms.signal_process_tree(proc, signal.SIGINT)
        proc.send_signal.assert_called_once_with(1)
        proc.close_tree.assert_not_called()

    def test_winget_resolution_is_shared_by_status_and_command_launch(self):
        links = self.t.root / "Microsoft" / "WinGet" / "Links"
        links.mkdir(parents=True)
        binary = links / "ffmpeg.exe"
        binary.touch()
        def which(name, path=None):
            return str(binary) if name == "ffmpeg" and path == str(links) else None
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
                platforms, "app_data", return_value=links), mock.patch.object(
                platforms.shutil, "which", side_effect=which):
            self.assertEqual(CommandRunner().which("ffmpeg"), str(binary))
            self.assertEqual(platforms.command_argv(["ffmpeg", "-version"]), [str(binary), "-version"])

    def test_camera_child_uses_the_shared_executable_resolver(self):
        from gazevoice import gaze_worker
        process = mock.Mock()
        with mock.patch.object(gaze_worker, "command_argv", return_value=["resolved-ffmpeg.exe"]), mock.patch.object(
                gaze_worker.subprocess, "Popen", return_value=process) as popen, mock.patch.object(
                gaze_worker.threading.Thread, "start"):
            gaze_worker.FrameSource(["ffmpeg"], 4, 3)
        self.assertEqual(popen.call_args.args[0], ["resolved-ffmpeg.exe"])

    def test_directshow_combined_and_none_entries_do_not_steal_unique_ids(self):
        listing = '\n'.join(['[dshow] "Hybrid" (audio, video)', '[dshow] Alternative name "@hybrid"',
                             '[dshow] "Unknown" (none)', '[dshow] Alternative name "@none"',
                             '[dshow] "Mic" (audio)', '[dshow] Alternative name "@mic"'])
        self.assertEqual(microphone.parse_dshow_devices(listing, "audio"),
                         [("@hybrid", "Hybrid"), ("@mic", "Mic")])
        self.assertEqual(microphone.parse_dshow_devices(listing, "video"), [("@hybrid", "Hybrid")])

    def test_speech_repair_rebuilds_existing_environment_and_refetches_model(self):
        unit = speech.Speech(self.t.ctx)
        unit.python.parent.mkdir(parents=True)
        unit.python.touch()
        (unit.venv / "pyvenv.cfg").write_text("home = C:/Python312\n", encoding="utf-8")
        with mock.patch.object(speech, "is_windows", return_value=True), mock.patch.object(
                speech, "is_apple_silicon", return_value=False), mock.patch.object(
                self.t.runner, "stream", return_value=0) as stream:
            self.assertEqual(unit._repair_action().id, "repair")
            unit.run_action("repair", make_job())
        self.assertEqual(len(stream.call_args_list), 3)
        self.assertIn("--clear", stream.call_args_list[0].args[0])
        self.assertIn("--force-download", stream.call_args_list[2].args[0])
        self.assertEqual(stream.call_args_list[0].kwargs["env"], {"UV_PYTHON_PREFERENCE": "only-managed"})

    def test_speech_repair_preserves_unrelated_folder(self):
        unit = speech.Speech(self.t.ctx)
        unit.venv.mkdir(parents=True)
        saved = unit.venv / "notes.txt"
        saved.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(JobError, "left alone"), mock.patch.object(
                self.t.runner, "stream") as stream:
            unit.action_repair(make_job())
        stream.assert_not_called()
        self.assertEqual(saved.read_text(encoding="utf-8"), "keep")

    def test_repair_download_bypasses_cache(self):
        fetch = mock.Mock(return_value="snapshot")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(speech_worker.download("org/model", fetch=fetch, force=True), "snapshot")
        fetch.assert_called_once_with("org/model", force_download=True)

    def test_conda_detection_uses_components_not_username_substrings(self):
        unit = speech.Speech(self.t.ctx)
        unit.venv.mkdir(parents=True)
        with mock.patch.object(speech, "is_windows", return_value=True):
            for home, expected in (("C:/Users/Gioconda/Python312", False), ("D:/miniconda3", True)):
                (unit.venv / "pyvenv.cfg").write_text("home = " + home, encoding="utf-8")
                self.assertEqual(unit._uses_conda(), expected)

    def test_app_owned_uv_wins_over_stale_path_uv(self):
        binary = platforms.venv_executable(self.t.paths.root / "tools" / "uv", "uv")
        binary.parent.mkdir(parents=True)
        binary.touch()
        (self.t.paths.root / "tools" / "uv" / ".gazevoice-ready").touch()
        self.assertEqual(python_tools.find_uv(self.t.runner, self.t.paths), str(binary))

    def test_unity_project_accepts_only_native_absolute_paths(self):
        native = str(self.t.root / "Game")
        self.assertEqual(settings.update_settings(settings.Settings(), {"unity_project": native}).unity_project, native)
        foreign = "/Users/me/Game" if os.name == "nt" else r"C:\Users\me\Game"
        with self.assertRaises(ValueError):
            settings.update_settings(settings.Settings(), {"unity_project": foreign})

    def test_unity_hub_accepts_location_lists_strings_and_editor_folders(self):
        editor = self.t.root / "custom" / "6000.3.7f1" / "Editor" / "Unity.exe"
        editor.parent.mkdir(parents=True)
        editor.touch()
        config = self.t.root / "hub"
        config.mkdir()
        with mock.patch.object(unity, "is_windows", return_value=True), mock.patch.dict(
                os.environ, {"UNITY_EDITOR_PATH": ""}):
            for location in (str(editor), [str(editor)], str(editor.parent), [str(editor.parent.parent)]):
                (config / "editors-v2.json").write_text(json.dumps({"data": [{"location": location}]}), encoding="utf-8")
                self.assertEqual([item.binary for item in unity.find_editors([], config)], [editor])

    def test_blender_chooses_numeric_newest_across_install_roots(self):
        binaries = []
        for root, version in (("system", "5.9.0"), ("user", "5.10.0"), ("legacy", "4.2.0")):
            binary = self.t.root / root / ("Blender " + version) / "blender.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            binaries.append(binary)
            self.t.runner.add([str(binary), "--version"], stdout="Blender " + version)
        with mock.patch.object(blender, "is_windows", return_value=True):
            self.assertEqual(blender.find_blender(self.t.runner, binaries), binaries[1])

    def test_windows_launch_errors_are_user_visible_job_errors(self):
        unit = blender.Blender(self.t.ctx)
        with mock.patch.object(blender, "is_windows", return_value=True), mock.patch.object(
                blender, "find_blender", return_value=Path("blender.exe")), mock.patch.object(
                blender, "launch_app", side_effect=OSError("access denied")):
            with self.assertRaisesRegex(JobError, "Couldn't launch Blender.*access denied"):
                unit.action_launch(make_job())
        with mock.patch.object(unity, "is_windows", return_value=True), mock.patch.object(
                unity, "launch_app", side_effect=OSError("access denied")):
            with self.assertRaisesRegex(JobError, "Couldn't open Unity Hub"):
                unity.Unity(self.t.ctx).open_hub()
            with mock.patch.object(unity, "is_running", return_value=False):
                with self.assertRaisesRegex(JobError, "Couldn't open Unity:.*access denied"):
                    unity.Unity(self.t.ctx)._open_project(
                        make_job(), self.t.root / "Game", unity.UnityEditor("6000.3.7f1", Path("Unity.exe")))

    def test_windows_sharing_lock_is_retried_but_persistent_error_still_fails(self):
        changed = self.t.root / "changed"
        changed.write_bytes(b"marker")
        error = OSError(errno.EACCES, "sharing violation")
        error.winerror = 32
        real_open = leftovers._open_regular
        for failures, expected_found, expected_failed in ((1, True, False), (3, False, True)):
            def opener(path, pending=[failures]):
                if pending[0]:
                    pending[0] -= 1
                    raise error
                return real_open(path)
            with mock.patch.object(leftovers, "is_windows", return_value=True), mock.patch.object(
                    leftovers, "_open_regular", side_effect=opener), mock.patch.object(leftovers.time, "sleep"):
                result = leftovers.search([b"marker"], 0, [leftovers.Place(changed, strict=True)])
            self.assertEqual(bool(result.found), expected_found)
            self.assertEqual(bool(result.failed), expected_failed)
