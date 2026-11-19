"""Speech-to-text on this computer: Parakeet or Whisper, on Apple's MLX, in their own venv."""

from __future__ import annotations

from ..platforms import is_apple_silicon, is_windows, venv_executable

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..jobs import Job, JobError
from ..python_tools import ensure_uv, find_uv
from .base import ActionSpec, Component, ComponentState, Health

WORKER = Path(__file__).resolve().parents[1] / "speech_worker.py"
VENV_PYTHON = "3.12"
PORTABLE_PACKAGES = ("faster-whisper==1.2.1", "av>=11,<19")
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


def engine_for(key: str) -> EngineSpec:
    """Keep MLX on Apple silicon; use portable CPU inference elsewhere."""
    if is_apple_silicon():
        return ENGINES[key]
    return EngineSpec("faster-whisper", "Whisper small (CPU int8)", "faster-whisper", "faster_whisper",
                      "Systran/faster-whisper-small")


def hf_cached(model: str, hf_home: Path | None = None) -> bool:
    """True when the Hugging Face cache holds at least one snapshot of the model."""
    home = hf_home or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")).expanduser()
    org, name = model.split("/", 1)
    snapshots = home / "hub" / f"models--{org}--{name}" / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


class Speech(Component):
    id = "speech"
    name = "Speech-to-text"
    role = "Turns your voice into text on this computer"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.hf_home: Path | None = None

    @property
    def engine(self) -> EngineSpec:
        return engine_for(self.settings.stt_engine)

    @property
    def venv(self) -> Path:
        return self.paths.venvs / "speech"

    @property
    def python(self) -> Path:
        return venv_executable(self.venv)

    def _install_action(self) -> ActionSpec:
        prerequisite = " If needed, uv is installed in GazeVoice's own tools folder first."
        return ActionSpec(
            "install",
            f"Install {self.engine.label.split(' ')[0]}",
            confirm=f"Install {self.engine.label}? This sets up its own Python environment and downloads the "
                    "model (a few GB)." + prerequisite,
        )

    def _repair_action(self) -> ActionSpec:
        return ActionSpec("install", "Repair speech", confirm=
                          "Repair speech-to-text's private Python environment and packages? "
                          "Windows uses an independent Python runtime. Cached models are reused.")

    def _uses_conda(self) -> bool:
        if not is_windows():
            return False
        try:
            config = (self.venv / "pyvenv.cfg").read_text(encoding="utf-8")
        except OSError:
            return False
        for line in config.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                home = Path(value.strip())
                return "conda" in str(home).lower() or (home / "conda-meta").is_dir()
        return False

    def check(self) -> ComponentState:
        engine = self.engine
        if not find_uv(self.runner, self.paths):
            return self.state(Health.MISSING, "Press Install to set up speech-to-text, including uv and its Python environment.",
                              actions=[self._install_action()])
        details = [("Engine", engine.label), ("Model", engine.model)]
        if self.python.exists() and self._uses_conda():
            return self.state(Health.MISSING, "Speech uses a Conda Python runtime whose Windows DLLs may conflict. "
                              "Press Repair speech to switch to an independent Python environment.",
                              details, [self._repair_action()])
        if not self.python.exists() and self.venv.exists():
            return self.state(
                Health.MISSING,
                f"{engine.label}'s Python environment doesn't work any more, usually because the Python it was made "
                "from was removed. Press Install to set it up again.",
                details,
                [self._install_action()],
            )
        if not self.python.exists():
            return self.state(
                Health.MISSING,
                f"Not installed. {engine.label} runs locally on this computer, so speech stays on this device.",
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
        if engine.key == "faster-whisper":
            # Importing the package alone does not exercise the native inference
            # runtime or decoder. Verify both before calling this unit Ready.
            code = ("import av; assert 11 <= int(av.__version__.split('.')[0]) < 19, "
                    "'Incompatible PyAV decoder; press Repair speech'; "
                    "from faster_whisper import WhisperModel; "
                    f"WhisperModel({engine.model!r}, device='cpu', compute_type='int8', local_files_only=True)")
            result = self.probe([str(self.python), "-c", code], timeout=60, ttl=600)
            if not result.ok:
                return self.state(Health.ERROR, "Speech's decoder or native model runtime could not load. "
                                  "Press Repair speech, then run Test.", details, [self._repair_action()])
        return self.state(Health.READY, f"{engine.label} ready", details, [ActionSpec("test", "Test")])

    def action_install(self, job: Job) -> dict:
        engine = self.engine
        uv = ensure_uv(self.runner, self.paths, job)
        steps = []
        repair_conda = self._uses_conda()
        if not self.python.exists() or repair_conda:
            if self.venv.exists():  # a folder whose Python is gone (an uninstalled Anaconda, say); it holds only packages
                # uv's --clear deletes whatever folder it is given, so only an environment or an empty folder is cleared.
                if not ((self.venv / "pyvenv.cfg").is_file() or (self.venv.is_dir() and not any(self.venv.iterdir()))):
                    raise JobError(f"{self.venv} isn't a Python environment, so it's left alone. "
                                   "Move or delete that folder, then press Install again.")
                job.log("Replacing the Conda-based speech environment with an independent Python runtime." if repair_conda
                        else "The Python environment from before has no working Python, so it's being made again.")
            steps.append(("Creating a Python 3.12 environment…",
                          [uv, "venv", "--clear", *(["--managed-python"] if is_windows() else []),
                           "--python", VENV_PYTHON, str(self.venv)], 3600))
        packages = PORTABLE_PACKAGES if engine.key == "faster-whisper" else (engine.package,)
        steps.append((f"Installing {engine.package}…",
                      [uv, "pip", "install", "--python", str(self.python), *packages], 3600))
        # The worker resumes a stalled download itself and prints progress; on a slow line a few GB takes hours.
        steps.append((f"Downloading {engine.model}…",
                      [str(self.python), str(WORKER), "--engine", engine.key, "--model", engine.model, "--download-only"],
                      6 * 3600))
        for message, argv, timeout in steps:
            job.log(message)
            code = self.runner.stream(argv, job.log, timeout=timeout)
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
        if is_windows():
            import base64
            aiff = work / "speech-test-source.wav"
            script = ("$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Speech; "
                      "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      + "$voice.SetOutputToWaveFile('" + str(aiff).replace("'", "''") + "'); $voice.Speak('"
                      + TEST_SENTENCE.replace("'", "''") + "'); $voice.Dispose()")
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode("ascii")]
        else:
            argv = ["say", "-o", str(aiff), TEST_SENTENCE]
        job.log(f"Making a local test clip: {TEST_SENTENCE}")
        if not self.runner.run(argv, timeout=30).ok:
            if is_windows():
                raise JobError("Windows could not generate the test clip. Install a Windows speech voice in Settings.")
            raise JobError("macOS couldn't generate the test clip with `say`.")
        convert = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", str(wav)]
        if not self.runner.run(convert, timeout=30).ok:
            raise JobError("ffmpeg couldn't convert the test clip.")
        job.log(f"Transcribing with {engine.label}…")
        result = self.runner.run(
            [str(self.python), str(WORKER), "--engine", engine.key, "--model", engine.model, "--audio", str(wav)],
            timeout=300,
        )
        if not result.ok:
            from ..speech_worker import failure_message
            raise JobError(failure_message(result.returncode, result.stderr or result.stdout))
        try:
            reply = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            detail = " ".join((result.stderr or result.stdout).split())[-300:]
            raise JobError(f"Transcription failed: {detail or f'exit {result.returncode}'}") from None
        job.log(f"Heard: “{reply.get('text', '')}” in {reply.get('seconds')} s (model load {reply.get('load_seconds')} s)")
        return {"text": reply.get("text", ""), "seconds": reply.get("seconds"), "load_seconds": reply.get("load_seconds")}
