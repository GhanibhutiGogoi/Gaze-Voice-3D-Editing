"""The microphone as a rolling buffer.

While voice is on, ffmpeg streams 16 kHz mono PCM into memory. A key press
marks a time, a release marks another, and the command is cut from the buffer
with a little lead-in. Nothing is lost while ffmpeg starts up, and nothing is
written to disk unless you spoke a command.
"""

from __future__ import annotations

import collections
import os
import signal
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Sequence

RATE = 16000
BYTES_PER_SAMPLE = 2
PERMISSION_HELP = (
    "Check System Settings › Privacy & Security › Microphone and allow the app GazeVoice "
    "was started from (Terminal or your editor)."
)
SILENCE_HELP = (
    "The microphone is sending pure silence. That's what macOS sends an app that isn't allowed to use the "
    f"mic (or the mic is muted). {PERMISSION_HELP} Then stop and start voice."
)


def is_digital_silence(pcm: bytes) -> bool:
    """Exact zeros, which no working microphone produces, even in a quiet room."""
    return bool(pcm) and pcm.count(0) == len(pcm)


class MicError(Exception):
    """The microphone couldn't start; the message says why and what to do."""


def mic_argv(device_index: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", f":{device_index}",
            "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(BYTES_PER_SAMPLE)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    return path


class RingBuffer:
    """Timestamped PCM chunks covering the last `seconds` of audio."""

    def __init__(self, rate: int = RATE, seconds: float = 60.0):
        self.rate = rate
        self.seconds = seconds
        self._chunks: collections.deque[tuple[float, bytes]] = collections.deque()
        self._carry = b""  # an odd trailing byte waiting for its partner
        self._lock = threading.Lock()

    @property
    def latest_time(self) -> float:
        with self._lock:
            return self._chunks[-1][0] if self._chunks else 0.0

    def append(self, pcm: bytes, end_time: float) -> None:
        with self._lock:
            data = self._carry + pcm
            usable = len(data) - len(data) % BYTES_PER_SAMPLE
            self._carry = data[usable:]
            if usable:
                self._chunks.append((end_time, data[:usable]))
            horizon = end_time - self.seconds
            while self._chunks and self._chunks[0][0] < horizon:
                self._chunks.popleft()

    def capture(self, t0: float, t1: float) -> bytes:
        """The audio between two moments (seconds since the epoch), sample-aligned."""
        parts = []
        with self._lock:
            for end, data in self._chunks:
                samples = len(data) // BYTES_PER_SAMPLE
                start = end - samples / self.rate
                if end <= t0 or start >= t1:
                    continue
                first = max(0, round((t0 - start) * self.rate))
                last = min(samples, round((t1 - start) * self.rate))
                if last > first:
                    parts.append(data[first * BYTES_PER_SAMPLE:last * BYTES_PER_SAMPLE])
        return b"".join(parts)


class MicStream:
    """ffmpeg → RingBuffer, running in the background."""

    def __init__(self, argv: Sequence[str], buffer: RingBuffer | None = None, chunk_bytes: int = 3200):
        self.argv = [str(a) for a in argv]
        self.buffer = buffer or RingBuffer()
        self.chunk_bytes = chunk_bytes
        self.proc: subprocess.Popen | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self._first_audio = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def error(self) -> str:
        return " | ".join(line for line in self._stderr if line)

    def heard_only_silence(self, min_seconds: float = 1.0) -> bool:
        """True once at least `min_seconds` of audio arrived and every sample of it is exactly zero."""
        latest = self.buffer.latest_time
        pcm = self.buffer.capture(0.0, latest + 1.0) if latest else b""
        return len(pcm) >= min_seconds * RATE * BYTES_PER_SAMPLE and is_digital_silence(pcm)

    def start(self, wait_seconds: float = 5.0) -> None:
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise MicError(f"{self.argv[0]} isn't installed, so the microphone can't be heard.") from exc
        self._threads = [
            threading.Thread(target=self._read_audio, daemon=True, name="mic-audio"),
            threading.Thread(target=self._read_errors, daemon=True, name="mic-errors"),
        ]
        for thread in self._threads:
            thread.start()
        deadline = time.monotonic() + wait_seconds
        while not self._first_audio.wait(0.05):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                self._threads[1].join(timeout=0.5)
                detail = self.error or f"exit {self.proc.returncode}" if self.proc.poll() is not None else "no audio"
                self.stop()
                raise MicError(f"The microphone didn't start ({detail}). {PERMISSION_HELP}")

    def _read_audio(self) -> None:
        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, self.chunk_bytes)
            except OSError:
                break
            if not data:
                break
            self.buffer.append(data, time.time())
            self._first_audio.set()

    def _read_errors(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace").strip())

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for thread in self._threads:
            thread.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
