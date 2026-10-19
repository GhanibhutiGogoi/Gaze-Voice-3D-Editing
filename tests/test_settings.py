import json
import tempfile
import unittest
from pathlib import Path

from gazevoice.providers import ProviderConfig, preset
from gazevoice.settings import Settings, load_settings, provider_ids, save_settings, update_settings

DEEPSEEK = preset("deepseek").to_dict()
BOX = ProviderConfig("custom_box", "custom", "Box", base_url="https://box.test/v1").to_dict()


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        s = Settings()
        self.assertEqual((s.editor_source, s.refiner_source, s.stt_engine, s.mic_device),
                         ("auto", "auto", "parakeet", ""))
        self.assertEqual((s.editor_api_model, s.editor_api_thinking, s.providers), ("", "default", ()))
        self.assertEqual(s.refiner_api_thinking, "off")  # cleanup on an API provider should be quick

    def test_mic_device_accepts_auto_or_a_number(self):
        self.assertEqual(update_settings(Settings(), {"mic_device": "2"}).mic_device, "2")
        self.assertEqual(update_settings(Settings(mic_device="2"), {"mic_device": ""}).mic_device, "")

    def test_round_trip(self):
        s = update_settings(Settings(), {"editor_source": "codex", "stt_engine": "whisper", "mic_device": "2"})
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
        self.path.write_text(json.dumps({"editor_source": "gpt", "stt_engine": "whisper", "extra": 1}))
        s = load_settings(self.path)
        self.assertEqual((s.editor_source, s.stt_engine), ("auto", "whisper"))

    def test_model_names_saved_before_full_names_carry_over(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"editor_model": "opus", "refiner_model": "haiku"}))
        s = load_settings(self.path)
        self.assertEqual((s.editor_model, s.refiner_model), ("claude-opus-5-5", "claude-haiku-5-5"))

    def test_update_rejects_a_bad_source(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"editor_source": "gpt"})

    def test_step_sources(self):
        self.assertEqual(update_settings(Settings(), {"editor_source": "codex"}).editor_source, "codex")
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"editor_source": "deepseek"})
        s = update_settings(Settings(), {"providers": [DEEPSEEK], "editor_source": "deepseek"})
        self.assertEqual((s.editor_source, provider_ids(s)), ("deepseek", {"deepseek"}))
        self.assertEqual(update_settings(s, {"refiner_source": "deepseek"}).refiner_source, "deepseek")

    def test_saved_brain_becomes_both_sources(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"brain": "codex"}))
        s = load_settings(self.path)
        self.assertEqual((s.editor_source, s.refiner_source), ("codex", "codex"))
        self.path.write_text(json.dumps({"brain": "codex", "editor_source": "claude"}))
        self.assertEqual(load_settings(self.path).editor_source, "claude")  # newer settings win

    def test_unknown_brain_key_is_ignored_on_load_and_refused_on_update(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"brain": "auto"})

    def test_api_thinking_values(self):
        for value in ("default", "off", "on", "low", "high", "max"):
            self.assertEqual(update_settings(Settings(), {"editor_api_thinking": value}).editor_api_thinking, value)
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"refiner_api_thinking": "ludicrous"})
        self.assertEqual(update_settings(Settings(), {"editor_api_model": "deepseek-v4-pro"}).editor_api_model,
                         "deepseek-v4-pro")
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"editor_api_model": "x" * 300})

    def test_providers_round_trip(self):
        s = update_settings(Settings(), {"providers": [DEEPSEEK, BOX], "refiner_source": "custom_box"})
        save_settings(self.path, s)
        self.assertEqual(load_settings(self.path), s)
        raw = json.loads(self.path.read_text())
        raw["providers"].append({"id": "x", "kind": "gpt", "name": "X"})
        self.path.write_text(json.dumps(raw))
        self.assertEqual(provider_ids(load_settings(self.path)), {"deepseek", "custom_box"})

    def test_a_source_whose_provider_is_gone_falls_back_to_auto_on_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"editor_source": "custom_gone"}))
        self.assertEqual(load_settings(self.path).editor_source, "auto")

    def test_update_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"volume": 11})

    def test_update_rejects_non_digit_mic(self):
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"mic_device": "built-in"})


if __name__ == "__main__":
    unittest.main()
