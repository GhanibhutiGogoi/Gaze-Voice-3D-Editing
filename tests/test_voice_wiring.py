import dataclasses
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gazevoice.providers import KeyStore, preset
from gazevoice.runner import TIMED_OUT
from gazevoice.settings import update_settings
from gazevoice.voice.api_brain import ApiSession

from gazevoice.components.gaze import MODEL_SIZE, WORKER as GAZE_WORKER
from gazevoice.components.microphone import LIST_DEVICES
from gazevoice.events import EventBus
from gazevoice.voice.audio import MicError
from gazevoice.voice.gaze_tracker import GazeError, GazeTracker
from gazevoice.components.brains import CLAUDE_ISOLATION
from gazevoice.voice.brains import claude_session_argv
from gazevoice.voice.picture import PICTURE_MODEL, PICTURE_RULES, Describer
from gazevoice.voice.service import PointerTracker
from gazevoice.voice.wiring import build_voice, voice_readiness

from .helpers import TempCtx
from .test_gaze_tracker import a_calibration
from .test_voice_service import FakeBridge, FakeMic, FakeSession, FakeStt


def listing(*names, cameras=("FaceTime HD Camera",)):
    lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:"]
    lines += [f"[AVFoundation indev @ 0x1] [{i}] {camera}" for i, camera in enumerate(cameras)]
    lines += ["[AVFoundation indev @ 0x1] AVFoundation audio devices:"]
    lines += [f"[AVFoundation indev @ 0x1] [{i}] {name}" for i, name in enumerate(names)]
    return "\n".join(lines) + "\n"


class BuildVoiceTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={"ffmpeg": "/usr/bin/ffmpeg", "uv": "/usr/bin/uv"})
        self.t.runner.add(LIST_DEVICES, stderr=listing("Aggregate Device", "MacBook Air Microphone"))

    def tearDown(self):
        self.t.cleanup()

    @staticmethod
    def device(mic):
        return mic.argv[mic.argv.index("-i") + 1]

    def test_the_mic_is_picked_again_each_time_voice_starts(self):
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        self.assertEqual(self.device(voice.factories.mic()), ":1")
        # A USB interface plugged in since: the built-in mic's number moved.
        self.t.runner.add(LIST_DEVICES, stderr=listing("Aggregate Device", "USB Audio", "MacBook Air Microphone"))
        self.t.ctx.probes.clear()
        self.assertEqual(self.device(voice.factories.mic()), ":2")

    def test_a_missing_mic_is_reported_when_voice_starts(self):
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, mic_device="7")
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        with self.assertRaises(MicError):
            voice.factories.mic()

    def test_brain_sessions_use_the_chosen_models_and_thinking(self):
        self.t.ctx.settings = dataclasses.replace(
            self.t.ctx.settings, editor_model="claude-opus-5-5", editor_effort="max",
            refiner_model="claude-haiku-5-5", refiner_effort="default")
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        editor, refiner = voice.factories.editor(), voice.factories.refiner()
        self.assertEqual(editor.argv[editor.argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(editor.argv[editor.argv.index("--effort") + 1], "max")
        self.assertEqual(editor.effort, "max")
        self.assertEqual(refiner.argv[refiner.argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertNotIn("--effort", refiner.argv)

    def test_build_voice_uses_api_sessions_for_providers(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "providers": [preset("deepseek").to_dict()], "editor_source": "deepseek",
            "editor_api_model": "deepseek-v4-pro", "editor_api_thinking": "max"})
        self.t.runner.add(["security", "find-generic-password", "-s", "GazeVoice", "-a", "deepseek", "-w"],
                          stdout="sk-1234\n")
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "deepseek"}, KeyStore(self.t.runner))
        editor = voice.factories.editor()
        self.assertIsInstance(editor, ApiSession)
        self.assertEqual((editor.init["model"], editor.effort, editor.kind), ("deepseek-v4-pro", "max", "deepseek"))
        self.assertEqual(editor.init["provider"], "DeepSeek")
        self.assertIsNotNone(editor.mcp_factory)
        self.assertEqual(editor.max_steps, 150)  # detailed builds take many steps
        self.assertNotIsInstance(voice.factories.refiner(), ApiSession)

    def test_claude_edits_get_150_turns(self):
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        argv = voice.factories.editor().argv
        self.assertEqual(argv[argv.index("--max-turns") + 1], "150")

    def test_unity_uses_its_bridge_mcp_server_and_prompts(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "voice_host": "unity", "unity_mcp_command": "/opt/unity-mcp", "unity_mcp_args": ["--stdio"]
        })
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        editor = voice.factories.editor()
        bridge = voice.factories.bridge()
        self.assertEqual(voice.host, "unity")
        self.assertEqual(bridge.port, 9878)
        self.assertIn("mcp__unity", editor.argv)
        config_path = Path(editor.argv[editor.argv.index("--mcp-config") + 1])
        config = json.loads(config_path.read_text())
        self.assertEqual(config["mcpServers"]["unity"], {
            "command": "/opt/unity-mcp", "args": ["--stdio"]
        })
        prompt = Path(editor.argv[editor.argv.index("--system-prompt-file") + 1]).read_text()
        self.assertIn("Unity MCP tools", prompt)

    def test_unity_api_editor_gets_dynamic_mcp_tool_factory(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "voice_host": "unity", "unity_mcp_command": "/opt/unity-mcp", "unity_mcp_args": ["serve"],
            "providers": [preset("deepseek").to_dict()], "editor_source": "deepseek",
            "editor_api_model": "deepseek-v4-pro"})
        self.t.runner.add(["security", "find-generic-password", "-s", "GazeVoice", "-a", "deepseek", "-w"],
                          stdout="sk-1234\n")
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "deepseek"}, KeyStore(self.t.runner))
        editor = voice.factories.editor()
        self.assertEqual(editor.host, "Unity")
        self.assertIsNotNone(editor.mcp_factory)

    def test_unity_api_editor_waits_as_long_as_a_csharp_run_can_take(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "voice_host": "unity", "editor_api_model": "deepseek-v4-pro",
            "providers": [preset("deepseek").to_dict()], "editor_source": "deepseek"})
        keys = MagicMock()
        keys.get.return_value = "sk-test"
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "deepseek"}, keys=keys)
        with patch("gazevoice.voice.wiring.host_mcp") as host_mcp:
            voice.factories.editor()
        self.assertGreaterEqual(host_mcp.call_args.kwargs["timeout"], 300)

    # --- gaze ---------------------------------------------------------------------------------------
    def gaze_voice(self, host="blender", **gaze):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"voice_host": host, **gaze})
        return build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})

    def test_gaze_is_off_by_default_and_builds_no_gaze_part(self):
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        self.assertIsNone(voice.factories.gaze)
        self.assertEqual((voice.gaze_mode, voice.gaze_source), ("off", "camera"))

    def test_the_mouse_stands_in_for_gaze_in_both_watching_modes(self):
        for mode in ("shadow", "on"):
            voice = self.gaze_voice(gaze_mode=mode, gaze_source="mouse")
            part = voice.factories.gaze()
            self.assertIsInstance(part, PointerTracker)
            self.assertEqual(part.radius, (0.02, 0.03))  # a mouse is nearly exact
            self.assertEqual((voice.gaze_mode, voice.gaze_source), (mode, "mouse"))

    def test_the_mouse_with_gaze_off_builds_nothing(self):
        voice = self.gaze_voice(gaze_mode="off", gaze_source="mouse")
        self.assertIsNone(voice.factories.gaze)

    def test_camera_gaze_builds_no_gaze_part_until_gaze_is_installed(self):
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.assertIsNone(voice.factories.gaze)  # voice runs on the selection
        self.assertEqual((voice.gaze_mode, voice.gaze_source), ("on", "camera"))

    # --- pictures ---------------------------------------------------------------------------------
    def leftover_image(self):
        images = self.t.paths.gaze_images
        images.mkdir(parents=True, exist_ok=True)
        leftover = images / "gazevoice-gaze-123.png"
        leftover.write_bytes(b"\x89PNG left by a crash")
        return leftover

    def test_pictures_are_off_by_default_so_no_describer_is_built(self):
        voice = self.gaze_voice(gaze_mode="on", gaze_source="mouse")
        self.assertIsNotNone(voice.factories.pictures)  # starting voice always clears what a crash left
        self.assertIsNone(voice.factories.pictures())

    def test_starting_voice_clears_leftover_images_whether_or_not_pictures_are_on(self):
        for pictures in (False, True):
            leftover = self.leftover_image()
            voice = self.gaze_voice(gaze_mode="on", gaze_source="mouse", gaze_pictures=pictures)
            voice.factories.pictures()
            self.assertFalse(leftover.exists(), pictures)

    def test_turning_pictures_on_builds_a_describer_that_uses_this_apps_folder_and_claude(self):
        voice = self.gaze_voice(gaze_mode="on", gaze_source="mouse", gaze_pictures=True)
        describer = voice.factories.pictures()
        self.assertIsInstance(describer, Describer)
        self.assertEqual(describer.paths, self.t.paths)
        self.assertEqual(describer.command, ("claude",))
        self.assertFalse(describer.ready())  # nothing has been checked yet

    def test_unity_never_builds_a_picture_part(self):
        voice = self.gaze_voice(host="unity", gaze_pictures=True)
        self.assertIsNone(voice.factories.pictures)

    def test_the_refiner_and_the_describer_are_started_in_the_same_folder_with_the_same_isolation(self):
        voice = self.gaze_voice(gaze_mode="on", gaze_source="mouse", gaze_pictures=True)
        refiner = voice.factories.refiner()
        argv = claude_session_argv(PICTURE_RULES, PICTURE_MODEL, None, max_turns=1)
        self.assertEqual(str(refiner.cwd), str(self.t.paths.work))
        for flag in CLAUDE_ISOLATION:
            self.assertIn(flag, refiner.argv)
            self.assertIn(flag, argv)

    # --- camera gaze: only once it is installed, calibrated and has a camera ------------------------
    def set_up_gaze(self, calibration="default", model=True, venv=True):
        """What the Gaze unit leaves behind: its venv, the face model, and a saved calibration."""
        paths = self.t.paths
        if venv:
            python = paths.venvs / "gaze" / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("")
        paths.gaze_model_file.parent.mkdir(parents=True, exist_ok=True)
        if model:
            with open(paths.gaze_model_file, "wb") as handle:
                handle.truncate(MODEL_SIZE)
        if calibration == "default":
            calibration = a_calibration()
        if calibration is not None:
            paths.gaze_calibration.write_text(json.dumps(calibration))

    def new_camera_listing(self, *cameras):
        self.t.runner.add(LIST_DEVICES, stderr=listing("MacBook Air Microphone", cameras=cameras))
        self.t.ctx.probes.clear()

    def test_build_voice_only_adds_gaze_when_its_on_and_calibrated(self):
        self.set_up_gaze()
        for mode in ("shadow", "on"):
            voice = self.gaze_voice(gaze_mode=mode, gaze_source="camera")
            tracker = voice.factories.gaze()
            self.assertIsInstance(tracker, GazeTracker)
            self.assertEqual((voice.gaze_mode, voice.gaze_source), (mode, "camera"))
        gaze_python = self.t.paths.venvs / "gaze" / "bin" / "python"
        self.assertEqual(tracker.argv, [str(gaze_python), str(GAZE_WORKER), "--serve", "--camera", "0",
                                        "--model-file", str(self.t.paths.gaze_model_file)])
        self.assertEqual(tracker.calibration, a_calibration())
        self.assertEqual(tracker.radius, (0.07, 0.11))  # what the calibration's check measured
        self.assertFalse(tracker.alive)  # built, not started: the camera opens when voice starts

        self.assertIsNone(self.gaze_voice(gaze_mode="off", gaze_source="camera").factories.gaze)
        self.assertIsNone(self.gaze_voice(host="unity", gaze_mode="on", gaze_source="camera").factories.gaze)

    def test_without_each_piece_gaze_is_not_built_and_voice_uses_the_selection(self):
        for name, missing in {"no venv": dict(venv=False), "no face model": dict(model=False),
                              "no calibration": dict(calibration=None)}.items():
            with self.subTest(name):
                shutil.rmtree(self.t.paths.venvs, ignore_errors=True)
                shutil.rmtree(self.t.paths.gaze_model_file.parent, ignore_errors=True)
                self.set_up_gaze(**missing)
                self.assertIsNone(self.gaze_voice(gaze_mode="on", gaze_source="camera").factories.gaze)

    def test_without_a_camera_gaze_is_built_and_says_why_when_voice_starts(self):
        self.set_up_gaze()
        for name, cameras in {"no camera": (), "only a phone": ("Nokia Unbreakable V Camera", "Capture screen 0")}.items():
            with self.subTest(name):
                self.new_camera_listing(*cameras)
                voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
                with self.assertRaisesRegex(GazeError, "isn't connected"):
                    voice.factories.gaze()

    def plugged_in_later(self):
        """A voice built with no camera, then one plugged in, with no time for the cached listing to run out."""
        self.set_up_gaze()
        self.new_camera_listing()
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        with self.assertRaises(GazeError):
            voice.factories.gaze()
        self.t.runner.add(LIST_DEVICES, stderr=listing("MacBook Air Microphone", cameras=("MacBook Air Camera",)))
        return voice

    def test_a_camera_plugged_in_after_the_build_is_found_when_voice_starts_again(self):
        self.assertEqual(self.plugged_in_later().factories.gaze().argv[4], "0")

    def fake_parts(self, voice):
        bridge = FakeBridge()
        voice.factories = dataclasses.replace(
            voice.factories, bridge=lambda: bridge, mic=FakeMic, stt=lambda: FakeStt([]),
            refiner=FakeSession, editor=FakeSession)
        self.addCleanup(voice.stop)
        return bridge

    def test_voice_starts_without_gaze_when_the_camera_is_missing_and_says_why(self):
        self.set_up_gaze()
        self.new_camera_listing()
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        bridge = self.fake_parts(voice)
        voice.start()
        self.assertEqual(voice.state, "ready")
        self.assertFalse(voice.snapshot()["gaze"]["live"])
        self.assertIn("isn't connected", voice.snapshot()["gaze"]["problem"])
        self.assertIn("so voice uses your selection", voice.snapshot()["gaze"]["problem"])
        self.assertTrue(any("isn't connected" in text for text, _ in bridge.statuses))
        self.assertIsNone(self.t.ctx.camera.holder)  # free, so Calibrate isn't told voice is using it

    def test_a_slow_camera_listing_stops_gaze_not_voice(self):
        self.set_up_gaze()
        self.t.runner.add(LIST_DEVICES, returncode=TIMED_OUT, stderr="ffmpeg: timed out after 10s")
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.assertIsNotNone(voice.factories.gaze)
        self.fake_parts(voice)
        voice.start()
        self.assertEqual(voice.state, "ready")
        self.assertIn("cameras in time", voice.snapshot()["gaze"]["problem"])

    def test_the_viewport_a_camera_was_calibrated_on_goes_with_the_gaze_part(self):
        self.set_up_gaze({**a_calibration(), "view": {"width": 1840, "height": 1100}, "when": 1.0})
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.assertEqual(voice.factories.gaze().view, (1840, 1100))  # the voice loop compares it with Blender's, per command
        self.assertIsNone(self.gaze_voice(gaze_mode="on", gaze_source="mouse").factories.gaze().view)

    def test_camera_gaze_shares_one_claim_on_the_camera_with_calibration(self):
        self.set_up_gaze()
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.assertIs(voice.camera, self.t.ctx.camera)  # the same object the Gaze unit takes before it opens the camera

    def test_voice_that_never_opens_the_camera_makes_no_claim_on_it(self):
        self.set_up_gaze()
        for name, chosen in {"gaze off": dict(gaze_mode="off", gaze_source="camera"),
                             "the mouse": dict(gaze_mode="on", gaze_source="mouse"),
                             "watching the mouse": dict(gaze_mode="shadow", gaze_source="mouse"),
                             "unity": dict(host="unity", gaze_mode="on", gaze_source="camera")}.items():
            with self.subTest(name):
                self.assertIsNone(self.gaze_voice(**chosen).camera)

    def test_camera_gaze_that_isnt_set_up_makes_no_claim_either(self):
        self.assertIsNone(self.gaze_voice(gaze_mode="on", gaze_source="camera").camera)

    def test_voice_can_always_see_when_calibration_is_drawing_in_the_viewport(self):
        for chosen in (dict(gaze_mode="off"), dict(gaze_mode="on", gaze_source="mouse")):
            self.assertIs(self.gaze_voice(**chosen).overlay, self.t.ctx.camera)

    def test_the_mouse_needs_no_calibration_camera_or_install(self):
        voice = self.gaze_voice(gaze_mode="on", gaze_source="mouse")
        self.assertIsInstance(voice.factories.gaze(), PointerTracker)

    def test_the_camera_is_picked_again_each_time_voice_starts(self):
        self.set_up_gaze()
        self.new_camera_listing("MacBook Air Camera", "Capture screen 0")
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.assertEqual(voice.factories.gaze().argv[4], "0")
        # A phone plugged in since: the built-in camera's number moved.
        self.new_camera_listing("Nokia Unbreakable V Camera", "MacBook Air Camera", "Capture screen 0")
        self.assertEqual(voice.factories.gaze().argv[4], "1")

    def test_a_chosen_camera_is_found_by_name(self):
        self.set_up_gaze()
        self.new_camera_listing("MacBook Air Camera", "Logitech BRIO")
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera", camera_device="Logitech BRIO")
        self.assertEqual(voice.factories.gaze().argv[4], "1")
        self.new_camera_listing("Logitech BRIO", "MacBook Air Camera")
        self.assertEqual(voice.factories.gaze().argv[4], "0")  # it moved, and gaze followed it

    def test_a_camera_that_went_away_since_the_build_stops_gaze_not_voice(self):
        self.set_up_gaze()
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.new_camera_listing("Nokia Unbreakable V Camera")
        with self.assertRaisesRegex(GazeError, "Routing"):  # the service turns this into "voice uses your selection"
            voice.factories.gaze()

    def test_a_calibration_that_went_out_of_date_since_the_build_stops_gaze_not_voice(self):
        self.set_up_gaze()
        voice = self.gaze_voice(gaze_mode="on", gaze_source="camera")
        self.t.paths.gaze_calibration.write_text(json.dumps({"model": {"kind": "ridge", "features": 3}}))
        with self.assertRaisesRegex(GazeError, "[Cc]alibrate"):
            voice.factories.gaze()

    def test_unity_never_gets_gaze(self):
        voice = self.gaze_voice(host="unity", gaze_mode="on", gaze_source="mouse")
        self.assertIsNone(voice.factories.gaze)
        self.assertEqual(voice.gaze_mode, "off")

    def test_unity_readiness_uses_unity_requirements_not_blender_units(self):
        settings = update_settings(self.t.ctx.settings, {"voice_host": "unity"})
        with patch("gazevoice.blender_socket.port_open", return_value=False):
            missing = voice_readiness({}, {"refiner": None, "editor": None}, settings)
        self.assertTrue(any("Unity Editor bridge" in item for item in missing))
        self.assertFalse(any("Blender" in item for item in missing))

    def test_unity_builtin_mcp_works_without_external_command(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"voice_host": "unity"})
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        editor = voice.factories.editor()
        config_path = Path(editor.argv[editor.argv.index("--mcp-config") + 1])
        server = json.loads(config_path.read_text())["mcpServers"]["unity"]
        self.assertEqual(server["args"], ["-m", "gazevoice.unity_mcp"])
        self.assertIn("PYTHONPATH", server["env"])

    def test_a_provider_without_a_key_fails_with_a_plain_message(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "providers": [preset("deepseek").to_dict()], "refiner_source": "deepseek", "refiner_api_model": "m"})
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "deepseek", "editor": "claude"}, KeyStore(self.t.runner))
        with self.assertRaisesRegex(Exception, "DeepSeek has no API key"):
            voice.factories.refiner()

    def test_speech_never_reaches_for_the_network(self):
        voice = build_voice(self.t.ctx, EventBus(), {"refiner": "claude", "editor": "claude"})
        self.assertEqual(voice.factories.stt().env.get("HF_HUB_OFFLINE"), "1")


if __name__ == "__main__":
    unittest.main()
