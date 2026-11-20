"""Cross-platform adapter contracts and real, isolated Windows smoke tests."""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request
import uuid

from gazevoice import platforms
from gazevoice.components import blender, microphone, speech, unity
from gazevoice.runner import CommandRunner, FakeRunner
from gazevoice.speech_worker import FasterWhisperEngine
from gazevoice.voice import leftovers
from gazevoice.windows_desktop import viewport_area
from .helpers import TempCtx, make_job, process_alive


class PlatformContractTest(unittest.TestCase):
    def test_windows_speech_repairs_conda_and_uses_compatible_packages(self):
        t = TempCtx(which={"uv": "uv"})
        self.addCleanup(t.cleanup)
        unit = speech.Speech(t.ctx)
        unit.python.parent.mkdir(parents=True)
        unit.python.touch()
        (unit.venv / "pyvenv.cfg").write_text("home = D:/miniconda\n", encoding="utf-8")
        with mock.patch.object(speech, "is_windows", return_value=True), mock.patch.object(
                speech, "is_apple_silicon", return_value=False):
            state = unit.check()
            self.assertEqual(state.actions[0].label, "Repair speech")
            self.assertEqual(t.runner.calls, [])
            with mock.patch.object(t.runner, "stream", return_value=0) as stream:
                unit.action_install(make_job())
            create, install, download = [call.args[0] for call in stream.call_args_list]
        self.assertNotIn("--managed-python", create)
        self.assertEqual(stream.call_args_list[0].kwargs["env"]["UV_PYTHON_PREFERENCE"], "only-managed")
        self.assertIn("faster-whisper==1.2.1", install)
        self.assertIn("av>=11,<19", install)
        self.assertIn("--download-only", download)

    def test_speech_ready_checks_native_runtime_and_decoder_without_downloading(self):
        t = TempCtx(which={"uv": "uv"})
        self.addCleanup(t.cleanup)
        unit = speech.Speech(t.ctx)
        unit.python.parent.mkdir(parents=True)
        unit.python.touch()
        from gazevoice.runner import CommandResult
        for code, ready in ((3221225477, False), (1, False), (0, True)):
            with self.subTest(code=code), mock.patch.object(speech, "is_apple_silicon", return_value=False), mock.patch.object(
                    speech, "hf_cached", return_value=True), mock.patch.object(unit, "probe", side_effect=[
                        CommandResult((), 0, "", ""), CommandResult((), code, "", "")]) as probe:
                state = unit.check()
            self.assertEqual(state.health.value, "ready" if ready else "error")
            self.assertEqual(state.actions[0].label, "Test" if ready else "Repair speech")
            runtime_code = probe.call_args_list[1].args[0][-1]
            self.assertIn("local_files_only=True", runtime_code)
            self.assertIn("av.__version__", runtime_code)

    def test_access_violation_has_repair_guidance_for_both_exit_code_forms(self):
        from gazevoice.speech_worker import failure_message
        for code in (3221225477, -1073741819):
            message = failure_message(code)
            self.assertIn("0xC0000005", message)
            self.assertIn("Repair speech", message)
        self.assertEqual(failure_message(1, "bad audio"), "Transcription failed: bad audio")

    def test_windows_paths_and_identity(self):
        with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(Path("local")), "APPDATA": str(Path("roaming"))}):
            self.assertEqual(platforms.app_data("GazeVoice"), Path("local/GazeVoice"))
            self.assertEqual(platforms.app_data("Blender", roaming=True), Path("roaming/Blender"))
            self.assertEqual(platforms.venv_executable(Path("env")), Path("env/Scripts/python.exe"))
            self.assertEqual(platforms.system_info()["talk_key"], "Ctrl+Alt+Space")
            self.assertIn("BlenderFoundation.Blender", platforms.install_command("blender"))

    def test_macos_paths_and_package_manager(self):
        with mock.patch.object(platforms, "is_windows", return_value=False):
            self.assertEqual(platforms.app_data("GazeVoice"), Path.home() / "Library/Application Support/GazeVoice")
            self.assertEqual(platforms.venv_executable(Path("env")), Path("env/bin/python"))
            self.assertEqual(platforms.install_command("blender", cask=True), ["brew", "install", "--cask", "blender"])

    def test_apple_silicon_and_portable_speech_selection(self):
        with mock.patch.object(speech, "is_apple_silicon", return_value=True):
            self.assertEqual(speech.engine_for("parakeet").module, "parakeet_mlx")
        with mock.patch.object(speech, "is_apple_silicon", return_value=False):
            for preference in ("parakeet", "whisper"):
                self.assertEqual(speech.engine_for(preference).key, "faster-whisper")

    def test_audio_inputs_preserve_literal_device_ids(self):
        device = '@device_cm_{abc}\\wave_{123}&中文'
        self.assertEqual(platforms.audio_input(device, windows=True), ["-f", "dshow", "-i", "audio=" + device])
        self.assertEqual(platforms.audio_input("2", windows=False), ["-f", "avfoundation", "-i", ":2"])

    def test_duplicate_directshow_names_use_unique_ids(self):
        listing = '\n'.join(['[dshow] "Microphone" (audio)', '[dshow] Alternative name "@one"',
                             '[dshow] "Microphone" (audio)', '[dshow] Alternative name "@two"',
                             '[dshow] "Camera" (video)', '[dshow] Alternative name "@video"'])
        self.assertEqual(microphone.parse_dshow_devices(listing, "audio"), [("@one", "Microphone"), ("@two", "Microphone")])
        self.assertEqual(microphone.parse_dshow_devices(listing, "video"), [("@video", "Camera")])

    def test_windows_camera_auto_avoids_phone_and_virtual(self):
        with mock.patch.object(microphone, "is_windows", return_value=True):
            devices = [("0", "DroidCam phone"), ("1", "OBS Virtual Camera"), ("2", "Integrated Camera")]
            self.assertEqual(microphone.pick_camera(devices, ""), devices[2])
            self.assertIsNone(microphone.pick_camera(devices[:2], ""))
            self.assertEqual(microphone.pick_camera(devices, "DroidCam phone"), devices[0])

    def test_tasklist_ignores_successful_no_match_response(self):
        runner = FakeRunner()
        args = ["tasklist", "/FI", "IMAGENAME eq Blender.exe", "/FO", "CSV", "/NH"]
        runner.add(args, stdout='INFO: No tasks are running which match the specified criteria.')
        self.assertFalse(platforms.process_running(runner, "Blender", windows=True))
        runner.add(args, stdout='"blender.exe","123","Console","1","123 K"')
        self.assertTrue(platforms.process_running(runner, "Blender", windows=True))

    def test_npm_shim_is_launched_through_node_without_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "node_modules/@openai/codex/bin/codex.js"
            entry.parent.mkdir(parents=True)
            entry.touch()
            shim = root / "codex.cmd"
            with mock.patch.object(platforms, "is_windows", return_value=True), mock.patch.object(
                platforms.shutil, "which", side_effect=lambda name: str(shim) if name == "codex" else "node.exe"):
                self.assertEqual(platforms.command_argv(["codex", '中文 & "quoted"', ""]),
                                 ["node.exe", str(entry), '中文 & "quoted"', ""])

    def test_unknown_shim_returns_actionable_launch_error(self):
        with mock.patch("gazevoice.runner.command_argv", side_effect=OSError("unsupported shim")):
            runner = CommandRunner()
            self.assertEqual(runner.run(["custom.cmd"]).returncode, 126)
            lines = []
            self.assertEqual(runner.stream(["custom.cmd"], lines.append), 126)
            self.assertIn("unsupported shim", lines[0])

    def test_faster_whisper_cpu_timestamps_and_offline_mode(self):
        word = SimpleNamespace(word=" this", start=0.12345, end=0.56789)
        segment = SimpleNamespace(text=" this", words=[word])
        model = mock.Mock()
        model.transcribe.return_value = (iter([segment]), None)
        factory = mock.Mock(return_value=model)
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory)}), mock.patch.dict(
            os.environ, {"HF_HUB_OFFLINE": "1"}):
            engine = FasterWhisperEngine("local-model")
            self.assertEqual(engine.transcribe("test.wav", "cube"),
                             {"text": "this", "words": [{"text": "this", "start": 0.123, "end": 0.568}]})
        factory.assert_called_once_with("local-model", device="cpu", compute_type="int8", local_files_only=True)
        model.transcribe.assert_called_once_with("test.wav", initial_prompt="cube", word_timestamps=True)

    def test_windows_blender_and_unity_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "Blender 5.2/blender.exe"
            binary.parent.mkdir()
            binary.touch()
            editor = root / "6000.3.7f1/Editor/Unity.exe"
            editor.parent.mkdir(parents=True)
            editor.touch()
            with mock.patch.object(blender, "is_windows", return_value=True):
                self.assertEqual(blender.find_blender(FakeRunner(), [binary.parent]), binary)
            with mock.patch.object(unity, "is_windows", return_value=True), mock.patch.dict(os.environ, {"UNITY_EDITOR_PATH": ""}):
                found = unity.find_editors([root])
                self.assertEqual([(e.version, e.binary) for e in found], [("6000.3.7f1", editor)])

    def test_viewport_scaling_and_bottom_left_origin(self):
        reply = dict(window_width=1600, window_height=1000, x=200, y=100, width=800, height=600, scale=1)
        self.assertEqual(viewport_area(reply, (100, 50, 800, 500)), (200, 200, 400, 300))
        reply.update(window_width=800, window_height=500, scale=2)
        self.assertEqual(viewport_area(reply, (100, 50, 800, 500)), (200, 200, 400, 300))

    def test_unity_compiler_uses_windows_runtime_name(self):
        source = (Path(__file__).resolve().parents[1] / "unity_package/Editor/GazeVoiceBridge.cs").read_text(encoding="utf-8")
        self.assertIn('RuntimePlatform.WindowsEditor ? "dotnet.exe" : "dotnet"', source)
        self.assertIn('Path.Combine(root, "NetCoreRuntime", runtimeName)', source)


@unittest.skipUnless(sys.platform == "win32", "Windows native boundary")
class WindowsNativeTest(unittest.TestCase):
    def test_camera_mapping_requires_uniform_monitor_scaling(self):
        from gazevoice import windows_desktop as desktop
        for scales, expected in (([96], True), ([144, 144], True),
                                 ([96, 144], False), ([96, None], False), ([], False)):
            with self.subTest(scales=scales):
                api, shcore = mock.Mock(), mock.Mock()

                def dpi(handle, _kind, x, y):
                    value = scales[handle - 1]
                    if value is None:
                        return -1
                    x._obj.value = y._obj.value = value
                    return 0

                def enumerate_monitors(_dc, _clip, visit, _data):
                    for handle in range(1, len(scales) + 1):
                        visit(handle, None, None, 0)
                    return True

                shcore.GetDpiForMonitor.side_effect = dpi
                api.EnumDisplayMonitors.side_effect = enumerate_monitors
                with mock.patch.object(desktop.ctypes, "WinDLL", return_value=shcore):
                    self.assertEqual(desktop._uniform_scaling(api), expected)

    def test_mixed_dpi_skips_window_mapping_and_restores_thread_awareness(self):
        from gazevoice import windows_desktop as desktop
        api = mock.Mock()
        api.SetThreadDpiAwarenessContext.return_value = 123
        with mock.patch.object(desktop, "_user32", return_value=api), mock.patch.object(
                desktop, "_uniform_scaling", return_value=False):
            self.assertIsNone(desktop.frame(1, 800, 600))
        api.EnumWindows.assert_not_called()
        self.assertEqual(api.SetThreadDpiAwarenessContext.call_args_list[-1], mock.call(123))

    def test_unicode_argument_roundtrip(self):
        arguments = ["", "中文 文件夹", '& | < > ^ % !', 'a"b', "trailing\\"]
        result = CommandRunner().run([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments])
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)

    def test_credentials_roundtrip_in_isolated_random_account(self):
        from gazevoice import windows_credentials as credentials
        account = "test-" + uuid.uuid4().hex
        try:
            self.assertIsNone(credentials.read(account))
            credentials.write(account, "test-only-中文")
            self.assertEqual(credentials.read(account), "test-only-中文")
            credentials.delete(account)
            self.assertIsNone(credentials.read(account))
        finally:
            credentials.delete(account)

    def test_oversize_credential_is_rejected_before_write(self):
        from gazevoice import windows_credentials as credentials
        with mock.patch.object(credentials, "_api") as api:
            with self.assertRaises(ValueError):
                credentials.write("test", "x" * 1281)
            api.assert_not_called()

    def test_native_regular_file_and_junction_scan(self):
        from gazevoice.windows_files import open_regular
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside, outside = root / "inside", root / "outside"
            inside.mkdir()
            outside.mkdir()
            file = inside / "中文.txt"
            file.write_bytes(b"safe")
            with os.fdopen(open_regular(str(file)), "rb") as handle:
                self.assertEqual(handle.read(), b"safe")
            (outside / "secret").write_bytes(b"external-marker")
            link = inside / "junction"
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
            try:
                self.assertEqual(leftovers.search([b"external-marker"], 0, [inside]).found, [])
            finally:
                link.rmdir()

    def test_privacy_permission_error_fails_a_strict_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "changed").write_bytes(b"marker")
            with mock.patch.object(leftovers, "_holds", side_effect=PermissionError(errno.EACCES, "denied")):
                result = leftovers.search([b"marker"], 0, [leftovers.Place(root, strict=True)])
            self.assertEqual(len(result.failed), 1)

    def test_job_reclaims_child_after_root_exits(self):
        child = None
        script = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],stdout=subprocess.DEVNULL); print(p.pid,flush=True)'
        proc = platforms.spawn([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, **platforms.process_options())
        try:
            child = int(proc.stdout.readline())
            proc.wait(timeout=10)
            deadline = time.monotonic() + 5
            while process_alive(child) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(process_alive(child))
        finally:
            proc.close_tree()
            proc.communicate(timeout=5)

    def test_main_serves_platform_state_and_stops_on_ctrl_break(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = platforms.spawn([sys.executable, "-u", "-m", "gazevoice", "--port", "0", "--no-browser"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env={**os.environ, "GAZEVOICE_HOME": tmp},
                **platforms.process_options())
            try:
                line = proc.stdout.readline()
                url = line.split("http://", 1)[1].split()[0]
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open("http://" + url + "/api/state", timeout=10) as response:
                    state = json.load(response)
                self.assertEqual(state["platform"]["id"], "windows")
                proc.send_signal(signal.CTRL_BREAK_EVENT)
                stdout, stderr = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertIn("Stopping", stdout)
            finally:
                if proc.poll() is None:
                    proc.close_tree()
                    proc.communicate(timeout=5)
