"""Speech-to-text on this Mac: Parakeet or Whisper, on Apple's MLX, in their own venv."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..jobs import Job, JobError
from .base import ActionSpec, Component, ComponentState, Health

WORKER = Path(__file__).resolve().parents[1] / "speech_worker.py"
VENV_PYTHON = "3.12"
TEST_SENTENCE = "Make the selected cube twice as tall"


@dataclass(frozen=True)
class EngineSpec:
    key: str
    label: str
    package: str
    module: str
    model: str


ENGINES = {
    "parakeet": EngineSpec("parakeet", "Parakeet (NVIDIA)", "parakeet-mlx", "parakeet_mlx",
                           "mlx-community/parakeet-tdt-0.6b-v2"),
    "whisper": EngineSpec("whisper", "Whisper large-v3-turbo", "mlx-whisper", "mlx_whisper",
                          "mlx-community/whisper-large-v3-turbo"),
}


def hf_cached(model: str, hf_home: Path | None = None) -> bool:
    """True when the Hugging Face cache holds at least one snapshot of the model."""
    home = hf_home or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")).expanduser()
    org, name = model.split("/", 1)
    snapshots = home / "hub" / f"models--{org}--{name}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


class Speech(Component):
    id = "speech"
    name = "Speech-to-text"
    role = "Turns your voice into text on this Mac"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.hf_home: Path | None = None

    @property
    def engine(self) -> EngineSpec:
        return ENGINES[self.settings.stt_engine]

    @property
    def venv(self) -> Path:
        return self.paths.venvs / "speech"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    def _install_action(self) -> ActionSpec:
        return ActionSpec(
            "install",
            f"Install {self.engine.label.split(' ')[0]}",
            confirm=f"Install {self.engine.label}? This sets up its own Python environment and downloads the "
                    "model (a few GB).",
        )

    def check(self) -> ComponentState:
        engine = self.engine
        if not self.runner.which("uv"):
            return self.state(Health.ERROR, "uv is needed to set up speech-to-text. Install uv (astral.sh/uv), then refresh.")
        details = [("Engine", engine.label), ("Model", engine.model)]
        if not self.python.exists():
            return self.state(
                Health.MISSING,
                f"Not installed. {engine.label} runs locally on Apple silicon, so your voice never leaves this Mac.",
                details,
                [self._install_action()],
            )
        if not self.probe([str(self.python), "-c", f"import {engine.module}"], timeout=20, ttl=60).ok:
            return self.state(Health.MISSING, f"{engine.label} isn't installed yet.", details, [self._install_action()])
        if not hf_cached(engine.model, self.hf_home):
            return self.state(
                Health.INSTALLED,
                f"{engine.label} is installed, but its model isn't downloaded yet.",
                details,
                [self._install_action()],
            )
        return self.state(Health.READY, f"{engine.label} ready", details, [ActionSpec("test", "Test")])

    def action_install(self, job: Job) -> dict:
        engine = self.engine
        if not self.runner.which("uv"):
            raise JobError("uv isn't installed. Install it from astral.sh/uv, then try again.")
        steps = []
        if not self.python.exists():
            steps.append(("Creating a Python 3.12 environment…", ["uv", "venv", "--python", VENV_PYTHON, str(self.venv)]))
        steps.append((f"Installing {engine.package}…", ["uv", "pip", "install", "--python", str(self.python), engine.package]))
        steps.append((f"Downloading {engine.model}…",
                      [str(self.python), str(WORKER), "--engine", engine.key, "--model", engine.model, "--download-only"]))
        for message, argv in steps:
            job.log(message)
            code = self.runner.stream(argv, job.log, timeout=3600)
            if code != 0:
                raise JobError(f"{message.rstrip('…')} failed (exit {code}). See the log above.")
        return {"installed": True}

    def action_test(self, job: Job) -> dict:
        engine = self.engine
        if not self.runner.which("ffmpeg"):
            raise JobError("ffmpeg isn't installed, so the test clip can't be prepared. Install it from the Microphone unit.")
        work = self.paths.work
        work.mkdir(parents=True, exist_ok=True)
        aiff, wav = work / "speech-test.aiff", work / "speech-test.wav"
        job.log(f"Making a test clip with macOS's voice: “{TEST_SENTENCE}”")
        if not self.runner.run(["say", "-o", str(aiff), TEST_SENTENCE], timeout=30).ok:
            raise JobError("macOS couldn't generate the test clip with `say`.")
        convert = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", str(wav)]
        if not self.runner.run(convert, timeout=30).ok:
            raise JobError("ffmpeg couldn't convert the test clip.")
        job.log(f"Transcribing with {engine.label}…")
        result = self.runner.run(
            [str(self.python), str(WORKER), "--engine", engine.key, "--model", engine.model, "--audio", str(wav)],
            timeout=300,
        )
        try:
            reply = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            detail = " ".join((result.stderr or result.stdout).split())[-300:]
            raise JobError(f"Transcription failed: {detail or f'exit {result.returncode}'}") from None
        job.log(f"Heard: “{reply.get('text', '')}” in {reply.get('seconds')} s (model load {reply.get('load_seconds')} s)")
        return {"text": reply.get("text", ""), "seconds": reply.get("seconds"), "load_seconds": reply.get("load_seconds")}
