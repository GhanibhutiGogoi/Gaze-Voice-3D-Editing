import contextlib
import io
import json
import unittest

from gazevoice import speech_worker
from gazevoice.components.base import Health
from gazevoice.components.speech import ENGINES, WORKER, Speech, hf_cached
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut
from gazevoice.settings import Settings

from .helpers import TempCtx, make_job

UV = {"uv": "/Users/me/.local/bin/uv"}


class SpeechTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={**UV, "ffmpeg": "/opt/homebrew/bin/ffmpeg"})
        self.speech = Speech(self.t.ctx)
        self.speech.hf_home = self.t.root / "hf"
        self.venv = self.t.paths.venvs / "speech"
        self.py = self.venv / "bin" / "python"

    def tearDown(self):
        self.t.cleanup()

    def make_venv(self, importable=True):
        self.py.parent.mkdir(parents=True)
        self.py.write_text("")
        self.t.runner.add([str(self.py), "-c", "import parakeet_mlx"], returncode=0 if importable else 1)

    def cache_model(self, model="mlx-community/parakeet-tdt-0.6b-v2"):
        org, name = model.split("/")
        (self.speech.hf_home / "hub" / f"models--{org}--{name}" / "snapshots" / "abc123").mkdir(parents=True)

    def test_engines(self):
        self.assertEqual(ENGINES["parakeet"].model, "mlx-community/parakeet-tdt-0.6b-v2")
        self.assertEqual(ENGINES["whisper"].package, "mlx-whisper")

    def test_needs_uv(self):
        self.t.runner.set_which("uv", None)
        self.assertEqual(self.speech.check().health, Health.ERROR)

    def test_no_venv_offers_install_with_consent(self):
        state = self.speech.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIsNotNone(next(a for a in state.actions if a.id == "install").confirm)

    def test_package_missing(self):
        self.make_venv(importable=False)
        self.assertEqual(self.speech.check().health, Health.MISSING)

    def test_timed_out_import_check_is_not_reported_as_missing(self):
        self.py.parent.mkdir(parents=True)
        self.py.write_text("")
        self.t.runner.add([str(self.py), "-c", "import parakeet_mlx"], returncode=124, stderr="timed out")
        with self.assertRaises(ProbeTimedOut):
            self.speech.check()

    def test_test_without_ffmpeg_says_so(self):
        self.make_venv()
        self.cache_model()
        self.t.runner.set_which("ffmpeg", None)
        self.t.runner.add(["say", "-o", str(self.t.paths.work / "speech-test.aiff"), "Make the selected cube twice as tall"])
        with self.assertRaises(JobError) as caught:
            self.speech.run_action("test", make_job("speech", "test"))
        self.assertIn("ffmpeg isn't installed", str(caught.exception))

    def test_model_not_downloaded(self):
        self.make_venv()
        state = self.speech.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertTrue(state.offers("install"))

    def test_ready(self):
        self.make_venv()
        self.cache_model()
        state = self.speech.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("Parakeet", state.summary)
        self.assertTrue(state.offers("test"))

    def test_whisper_follows_settings(self):
        self.t.ctx.settings = Settings(stt_engine="whisper")
        self.py.parent.mkdir(parents=True)
        self.py.write_text("")
        self.t.runner.add([str(self.py), "-c", "import mlx_whisper"])
        self.cache_model("mlx-community/whisper-large-v3-turbo")
        self.assertIn("Whisper", self.speech.check().summary)

    def test_install_sequence(self):
        model = ENGINES["parakeet"].model
        steps = [
            ["uv", "venv", "--python", "3.12", str(self.venv)],
            ["uv", "pip", "install", "--python", str(self.py), "parakeet-mlx"],
            [str(self.py), str(WORKER), "--engine", "parakeet", "--model", model, "--download-only"],
        ]
        for argv in steps:
            self.t.runner.add(argv, stdout="ok\n")
        self.speech.run_action("install", make_job("speech", "install"))
        streamed = [c for c in self.t.runner.calls if c in {tuple(s) for s in steps}]
        self.assertEqual(streamed, [tuple(s) for s in steps])

    def test_test_action_transcribes_generated_speech(self):
        self.make_venv()
        self.cache_model()
        work = self.t.paths.work
        aiff, wav = work / "speech-test.aiff", work / "speech-test.wav"
        self.t.runner.add(["say", "-o", str(aiff), "Make the selected cube twice as tall"])
        self.t.runner.add(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff),
                           "-ac", "1", "-ar", "16000", str(wav)])
        reply = {"text": "Make the selected cube twice as tall.", "seconds": 0.41, "load_seconds": 1.2}
        self.t.runner.add([str(self.py), str(WORKER), "--engine", "parakeet", "--model",
                           ENGINES["parakeet"].model, "--audio", str(wav)], stdout=json.dumps(reply) + "\n")
        result = self.speech.run_action("test", make_job("speech", "test"))
        self.assertEqual(result["text"], "Make the selected cube twice as tall.")
        self.assertEqual(result["seconds"], 0.41)


class HfCacheTest(unittest.TestCase):
    def test_hf_cached(self):
        t = TempCtx()
        try:
            self.assertFalse(hf_cached("mlx-community/x", t.root))
            (t.root / "hub" / "models--mlx-community--x" / "snapshots").mkdir(parents=True)
            self.assertFalse(hf_cached("mlx-community/x", t.root))  # empty snapshots
            (t.root / "hub" / "models--mlx-community--x" / "snapshots" / "rev").mkdir()
            self.assertTrue(hf_cached("mlx-community/x", t.root))
        finally:
            t.cleanup()


class WorkerArgsTest(unittest.TestCase):
    def test_download_only_needs_no_audio(self):
        args = speech_worker.parse_args(["--engine", "parakeet", "--model", "m", "--download-only"])
        self.assertTrue(args.download_only)

    def test_audio_required_otherwise(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            speech_worker.parse_args(["--engine", "whisper", "--model", "m"])


if __name__ == "__main__":
    unittest.main()
