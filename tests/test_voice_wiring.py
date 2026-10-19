import dataclasses
import unittest

from gazevoice.providers import KeyStore, preset
from gazevoice.settings import update_settings
from gazevoice.voice.api_brain import ApiSession

from gazevoice.components.microphone import LIST_DEVICES
from gazevoice.events import EventBus
from gazevoice.voice.audio import MicError
from gazevoice.voice.wiring import build_voice

from .helpers import TempCtx


def listing(*names):
    lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:",
             "[AVFoundation indev @ 0x1] [0] FaceTime HD Camera",
             "[AVFoundation indev @ 0x1] AVFoundation audio devices:"]
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
