"""The microphone: listed through ffmpeg's AVFoundation input, checked with a real recording.

Whether macOS lets us hear the mic can't be known without recording, so the
unit says Ready only after a test recording actually heard something.
"""

from __future__ import annotations

import array
import json
import math
import re
import time
import wave
from pathlib import Path

from ..jobs import Job, JobError
from ..runner import TIMED_OUT
from .base import ActionSpec, Component, ComponentState, Health

LIST_DEVICES = ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
TEST_SECONDS = 3
SILENCE_DBFS = -60.0
DEVICE_CACHE_SECONDS = 30.0  # listing devices takes ffmpeg ~2.5 s
_BUILT_IN = re.compile(r"\b(MacBook|iMac|Built-in)\b.*\bMicrophone\b", re.IGNORECASE)
_NOT_A_MIC = re.compile(
    r"aggregate|virtual|blackhole|loopback|soundflower|teams|zoom|wemeet|webex|discord|obs|screen", re.IGNORECASE
)
PERMISSION_HELP = (
    "Check System Settings › Privacy & Security › Microphone and allow the app GazeVoice was started from "
    "(Terminal or your editor), then try again."
)
_DEVICE = re.compile(r"\]\s*\[(\d+)\]\s+(.+?)\s*$")


def parse_avfoundation_devices(listing: str) -> list[tuple[str, str]]:
    """Audio inputs as (index, name), from `ffmpeg -f avfoundation -list_devices true` output."""
    devices: list[tuple[str, str]] = []
    in_audio = False
    for line in listing.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if in_audio:
            match = _DEVICE.search(line)
            if match:
                devices.append((match.group(1), match.group(2)))
    return devices


def pick_device(devices: list[tuple[str, str]], setting: str) -> tuple[str, str, bool] | None:
    """(index, name, chosen_automatically) for the setting; "" means auto. None if the index is gone.

    Auto prefers the Mac's built-in microphone, then the first input that isn't a
    virtual or meeting-app device (those often come first in AVFoundation's list).
    """
    names = dict(devices)
    if setting:
        return (setting, names[setting], False) if setting in names else None
    for index, name in devices:
        if _BUILT_IN.search(name):
            return index, name, True
    for index, name in devices:
        if not _NOT_A_MIC.search(name):
            return index, name, True
    return (devices[0][0], devices[0][1], True) if devices else None


def wav_level_dbfs(path: Path) -> float:
    """RMS level of a 16-bit PCM WAV in dBFS; silence is -120."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("expected 16-bit audio")
        samples = array.array("h", wav.readframes(wav.getnframes()))
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return round(20 * math.log10(rms / 32768), 1) if rms > 0 else -120.0


class Microphone(Component):
    id = "microphone"
    name = "Microphone"
    role = "Hears your commands"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.record = self._record_with_ffmpeg  # tests swap in a fake recorder

    @property
    def _verified_file(self) -> Path:
        return self.paths.root / "state" / "microphone.json"

    def _verified(self, index: str) -> dict | None:
        try:
            data = json.loads(self._verified_file.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("device") != index:
            return None
        return data if isinstance(data.get("level_dbfs"), (int, float)) else None

    def devices(self, fresh: bool = False) -> list[tuple[str, str]]:
        if not fresh:
            listing = self.probe(LIST_DEVICES, timeout=10, ttl=DEVICE_CACHE_SECONDS)
        else:
            listing = self.runner.run(LIST_DEVICES, timeout=10)
            if listing.returncode == TIMED_OUT:
                raise JobError("macOS didn't list the microphones in time. Try again in a moment.")
        return parse_avfoundation_devices(listing.stderr + listing.stdout)

    def check(self) -> ComponentState:
        if not self.runner.which("ffmpeg"):
            install = [ActionSpec("install", "Install ffmpeg")] if self.runner.which("brew") else []
            return self.state(Health.MISSING, "ffmpeg is needed to hear the microphone.", actions=install)
        devices = self.devices()
        if not devices:
            return self.state(Health.ERROR, "No microphones found. Plug one in, then refresh.")
        listing = " | ".join(f"{index}={name}" for index, name in devices)
        picked = pick_device(devices, self.settings.mic_device)
        if picked is None:
            return self.state(
                Health.ERROR,
                f"Input {self.settings.mic_device} isn't connected. Pick another microphone in Routing.",
                [("Devices", listing)],
            )
        index, name, auto = picked
        details = [("Using", f"{name} (auto)" if auto else name), ("Devices", listing)]
        test = ActionSpec("test", "Test", confirm=f"Record {TEST_SECONDS} seconds from “{name}” to check its level?")
        verified = self._verified(index)
        if verified:
            return self.state(
                Health.READY,
                f"{name} · last test {verified['level_dbfs']:.1f} dBFS",
                details,
                [test],
            )
        return self.state(
            Health.INSTALLED,
            f"Found {len(devices)} input{'s' if len(devices) != 1 else ''}. Run Test to check the level and permission.",
            details,
            [test],
        )

    def _record_with_ffmpeg(self, device: str, out: Path, seconds: int) -> None:
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", f":{device}",
                "-t", str(seconds), "-ac", "1", "-ar", "16000", "-y", str(out)]
        result = self.runner.run(argv, timeout=seconds + 15)
        if not result.ok:
            detail = " ".join((result.stderr or result.stdout).split())[-200:]
            raise JobError(f"Couldn't record from the microphone ({detail or result.returncode}). {PERMISSION_HELP}")

    def action_install(self, job: Job) -> dict:
        if not self.runner.which("brew"):
            raise JobError("Homebrew isn't installed. Get it from brew.sh, then install ffmpeg.")
        code = self.runner.stream(["brew", "install", "ffmpeg"], job.log, timeout=1800)
        if code != 0:
            raise JobError(f"Homebrew couldn't install ffmpeg (exit {code}).")
        return {"installed": True}

    def action_test(self, job: Job) -> dict:
        picked = pick_device(self.devices(fresh=True), self.settings.mic_device)
        if picked is None:
            raise JobError("That microphone isn't connected any more. Pick another in Routing.")
        device, name, _ = picked
        out = self.paths.work / "mic-test.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        job.log(f"Recording {TEST_SECONDS} s from {name}… say something.")
        self.record(device, out, TEST_SECONDS)
        try:
            level = wav_level_dbfs(out)
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            raise JobError(f"The recording couldn't be read ({exc}). {PERMISSION_HELP}") from exc
        job.log(f"Level: {level:.1f} dBFS")
        if level < SILENCE_DBFS:
            self._verified_file.unlink(missing_ok=True)
            raise JobError(f"Heard only silence ({level:.1f} dBFS). {PERMISSION_HELP}")
        self._verified_file.parent.mkdir(parents=True, exist_ok=True)
        self._verified_file.write_text(json.dumps({"device": device, "level_dbfs": level, "at": time.time()}))
        return {"level_dbfs": level, "device": name}
