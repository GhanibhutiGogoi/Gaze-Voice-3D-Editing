import math
import struct
import unittest
import wave

from gazevoice.components.base import Health
from gazevoice.components.microphone import (
    LIST_DEVICES,
    Microphone,
    parse_avfoundation_devices,
    pick_device,
    wav_level_dbfs,
)
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut

from .helpers import TempCtx, make_job

LISTING = """\
[AVFoundation indev @ 0x14be04b80] AVFoundation video devices:
[AVFoundation indev @ 0x14be04b80] [0] FaceTime HD Camera
[AVFoundation indev @ 0x14be04b80] [1] Capture screen 0
[AVFoundation indev @ 0x14be04b80] AVFoundation audio devices:
[AVFoundation indev @ 0x14be04b80] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x14be04b80] [1] Studio Display Microphone
: Input/output error
"""


def write_wav(path, amplitude, seconds=0.5, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)


MEETING_FIRST = """\
[AVFoundation indev @ 0x7b] AVFoundation audio devices:
[AVFoundation indev @ 0x7b] [0] Aggregate Device
[AVFoundation indev @ 0x7b] [1] WeMeet Audio Device
[AVFoundation indev @ 0x7b] [2] MacBook Air Microphone
[AVFoundation indev @ 0x7b] [3] PICO Virtual Microphone
"""


class AutoPickTest(unittest.TestCase):
    def test_prefers_built_in_mic_over_virtual_devices(self):
        devices = parse_avfoundation_devices(MEETING_FIRST)
        self.assertEqual(pick_device(devices, ""), ("2", "MacBook Air Microphone", True))

    def test_falls_back_to_first_real_input(self):
        devices = [("0", "BlackHole 2ch"), ("1", "Shure MV7"), ("2", "Zoom Audio")]
        self.assertEqual(pick_device(devices, ""), ("1", "Shure MV7", True))

    def test_explicit_choice_wins(self):
        devices = parse_avfoundation_devices(MEETING_FIRST)
        self.assertEqual(pick_device(devices, "3"), ("3", "PICO Virtual Microphone", False))
        self.assertIsNone(pick_device(devices, "9"))


class HelpersTest(unittest.TestCase):
    def test_parse_audio_devices_only(self):
        self.assertEqual(
            parse_avfoundation_devices(LISTING),
            [("0", "MacBook Pro Microphone"), ("1", "Studio Display Microphone")],
        )
        self.assertEqual(parse_avfoundation_devices("no devices here"), [])

    def test_wav_levels(self):
        t = TempCtx()
        try:
            loud, silent = t.root / "loud.wav", t.root / "silent.wav"
            write_wav(loud, 1.0)
            write_wav(silent, 0.0)
            self.assertAlmostEqual(wav_level_dbfs(loud), -3.0, delta=1.0)
            self.assertEqual(wav_level_dbfs(silent), -120.0)
        finally:
            t.cleanup()


class MicrophoneTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={"ffmpeg": "/opt/homebrew/bin/ffmpeg", "brew": "/opt/homebrew/bin/brew"})
        self.mic = Microphone(self.t.ctx)

    def tearDown(self):
        self.t.cleanup()

    def test_missing_ffmpeg_offers_brew_install(self):
        self.t.runner.set_which("ffmpeg", None)
        state = self.mic.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_timed_out_listing_is_not_reported_as_no_microphones(self):
        self.t.runner.add(LIST_DEVICES, returncode=124, stderr="ffmpeg: timed out after 10s")
        with self.assertRaises(ProbeTimedOut):
            self.mic.check()

    def test_corrupt_verified_file_is_ignored(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        marker = self.t.paths.root / "state" / "microphone.json"
        marker.parent.mkdir(parents=True)
        marker.write_text('{"device": "0"}')
        self.assertEqual(self.mic.check().health, Health.INSTALLED)

    def test_no_devices_is_a_problem(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr="nothing")
        self.assertEqual(self.mic.check().health, Health.ERROR)

    def test_found_but_not_verified_yet(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        state = self.mic.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Test", state.summary)
        self.assertIn(("Devices", "0=MacBook Pro Microphone | 1=Studio Display Microphone"), state.details)
        test = next(a for a in state.actions if a.id == "test")
        self.assertIn("MacBook Pro Microphone", test.confirm)

    def test_auto_uses_built_in_mic_and_says_so(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=MEETING_FIRST)
        state = self.mic.check()
        self.assertIn(("Using", "MacBook Air Microphone (auto)"), state.details)

    def test_device_list_is_cached(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        self.mic.check()
        self.mic.check()
        self.assertEqual(self.t.runner.calls.count(tuple(LIST_DEVICES)), 1)

    def test_selected_device_missing(self):
        self.t.ctx.settings = self.t.ctx.settings.__class__(mic_device="7")
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        state = self.mic.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("Routing", state.summary)

    def test_test_records_measures_and_verifies(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        self.mic.record = lambda device, out, seconds: write_wav(out, 0.3)
        result = self.mic.run_action("test", make_job("microphone"))
        self.assertLess(result["level_dbfs"], -5)
        self.assertEqual(result["device"], "MacBook Pro Microphone")
        state = self.mic.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("dBFS", state.summary)

    def test_silence_explains_permission(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        self.mic.record = lambda device, out, seconds: write_wav(out, 0.0)
        with self.assertRaises(JobError) as caught:
            self.mic.run_action("test", make_job("microphone"))
        self.assertIn("Privacy & Security", str(caught.exception))
        self.assertEqual(self.mic.check().health, Health.INSTALLED)

    def test_default_recorder_runs_ffmpeg(self):
        self.t.runner.add(LIST_DEVICES, returncode=1, stderr=LISTING)
        out = self.t.paths.work / "mic-test.wav"
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":0",
                "-t", "3", "-ac", "1", "-ar", "16000", "-y", str(out)]
        self.t.runner.add(argv, returncode=1, stderr="Error opening input: Operation not permitted")
        with self.assertRaises(JobError) as caught:
            self.mic.run_action("test", make_job("microphone"))
        self.assertIn(tuple(argv), self.t.runner.calls)
        self.assertIn("Privacy & Security", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
