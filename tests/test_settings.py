import json
import tempfile
import unittest
from pathlib import Path

from gazevoice.settings import Settings, load_settings, save_settings, update_settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        s = Settings()
        self.assertEqual((s.brain, s.stt_engine, s.mic_device), ("auto", "parakeet", ""))

    def test_mic_device_accepts_auto_or_a_number(self):
        self.assertEqual(update_settings(Settings(), {"mic_device": "2"}).mic_device, "2")
        self.assertEqual(update_settings(Settings(mic_device="2"), {"mic_device": ""}).mic_device, "")

    def test_round_trip(self):
        s = update_settings(Settings(), {"brain": "codex", "stt_engine": "whisper", "mic_device": "2"})
        save_settings(self.path, s)
        self.assertEqual(load_settings(self.path), s)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(load_settings(self.path), Settings())

    def test_corrupt_file_gives_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertEqual(load_settings(self.path), Settings())

    def test_invalid_values_in_file_are_ignored_individually(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"brain": "gpt", "stt_engine": "whisper", "extra": 1}))
        s = load_settings(self.path)
        self.assertEqual((s.brain, s.stt_engine), ("auto", "whisper"))

    def test_update_rejects_bad_brain(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"brain": "gpt"})

    def test_update_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"volume": 11})

    def test_update_rejects_non_digit_mic(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"mic_device": "built-in"})


if __name__ == "__main__":
    unittest.main()
