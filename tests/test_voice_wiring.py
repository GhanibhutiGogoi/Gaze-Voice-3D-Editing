import dataclasses
import unittest

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
        voice = build_voice(self.t.ctx, EventBus(), "claude")
        self.assertEqual(self.device(voice.factories.mic()), ":1")
        # A USB interface plugged in since: the built-in mic's number moved.
        self.t.runner.add(LIST_DEVICES, stderr=listing("Aggregate Device", "USB Audio", "MacBook Air Microphone"))
        self.t.ctx.probes.clear()
        self.assertEqual(self.device(voice.factories.mic()), ":2")

    def test_a_missing_mic_is_reported_when_voice_starts(self):
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, mic_device="7")
        voice = build_voice(self.t.ctx, EventBus(), "claude")
        with self.assertRaises(MicError):
            voice.factories.mic()

    def test_brain_sessions_use_the_chosen_models_and_thinking(self):
        self.t.ctx.settings = dataclasses.replace(
            self.t.ctx.settings, editor_model="claude-opus-5-5", editor_effort="max",
            refiner_model="claude-haiku-5-5", refiner_effort="default")
        voice = build_voice(self.t.ctx, EventBus(), "claude")
        editor, refiner = voice.factories.editor(), voice.factories.refiner()
        self.assertEqual(editor.argv[editor.argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(editor.argv[editor.argv.index("--effort") + 1], "max")
        self.assertEqual(editor.effort, "max")
        self.assertEqual(refiner.argv[refiner.argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertNotIn("--effort", refiner.argv)

    def test_speech_never_reaches_for_the_network(self):
        voice = build_voice(self.t.ctx, EventBus(), "claude")
        self.assertEqual(voice.factories.stt().env.get("HF_HUB_OFFLINE"), "1")


if __name__ == "__main__":
    unittest.main()
