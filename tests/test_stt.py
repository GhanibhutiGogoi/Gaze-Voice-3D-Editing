import sys
import tempfile
import unittest
from pathlib import Path

from gazevoice import speech_worker
from gazevoice.voice.audio import write_wav
from gazevoice.voice.stt import SpeechError, SpeechService

WORKER = Path(speech_worker.__file__)
FAKE = {"GAZEVOICE_FAKE_ENGINE": "1"}


class TokensToWordsTest(unittest.TestCase):
    def test_merges_subword_tokens_into_words(self):
        tokens = [(" make", 0.10, 0.30), (" th", 0.32, 0.40), ("is", 0.40, 0.48), (" taller", 0.50, 0.90)]
        self.assertEqual(
            speech_worker.tokens_to_words(tokens),
            [{"text": "make", "start": 0.1, "end": 0.3}, {"text": "this", "start": 0.32, "end": 0.48},
             {"text": "taller", "start": 0.5, "end": 0.9}],
        )

    def test_sentencepiece_marker_starts_words(self):
        words = speech_worker.tokens_to_words([("▁make", 0.0, 0.2), ("▁it", 0.25, 0.3), ("▁big", 0.4, 0.6), ("ger", 0.6, 0.7)])
        self.assertEqual([w["text"] for w in words], ["make", "it", "bigger"])


class SpeechServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.audio = write_wav(Path(self.tmp.name) / "cmd.wav", b"\x00\x00" * 8000)
        self.service = SpeechService(Path(sys.executable), WORKER, "parakeet", "fake/model", env=FAKE)

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def test_loads_once_and_transcribes_many(self):
        load_seconds = self.service.start(timeout=20)
        self.assertGreaterEqual(load_seconds, 0)
        first = self.service.transcribe(self.audio)
        second = self.service.transcribe(self.audio, prompt="bevel, extrude")
        self.assertEqual(first["text"], "make this twice as tall")
        self.assertEqual(first["words"][1], {"text": "this", "start": 0.4, "end": 0.6})
        self.assertIn("seconds", second)
        self.assertTrue(self.service.alive)

    def test_bad_audio_raises_readable_error(self):
        self.service.start(timeout=20)
        with self.assertRaises(SpeechError) as caught:
            self.service.transcribe(Path(self.tmp.name) / "missing.wav")
        self.assertIn("missing.wav", str(caught.exception))
        self.assertTrue(self.service.alive)  # one bad request doesn't kill the worker

    def test_dead_worker_raises(self):
        self.service.start(timeout=20)
        self.service.proc.kill()
        self.service.proc.wait()
        with self.assertRaises(SpeechError):
            self.service.transcribe(self.audio, timeout=5)

    def test_worker_that_cannot_load_reports_stderr(self):
        broken = SpeechService(Path(sys.executable), WORKER, "parakeet", "fake/model",
                               env={"GAZEVOICE_FAKE_ENGINE": "fail"})
        with self.assertRaises(SpeechError) as caught:
            broken.start(timeout=20)
        self.assertIn("could not load", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
