"""Speech-to-text as a long-lived worker: the model loads once, then each command is quick."""

from __future__ import annotations

import collections
import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping


class SpeechError(Exception):
    """Speech-to-text failed; the message says why."""


class SpeechService:
    def __init__(self, python: Path, worker: Path, engine: str, model: str, env: Mapping[str, str] | None = None):
        self.argv = [str(python), str(worker), "--serve", "--engine", engine, "--model", model]
        self.env = dict(os.environ, **(env or {}))
        self.proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _stderr_tail(self) -> str:
        if self._threads:
            self._threads[1].join(timeout=0.5)
        return " | ".join(line for line in list(self._stderr)[-6:] if line) or "no output"

    def start(self, timeout: float = 180.0) -> float:
        """Start the worker and wait for the model to load. Returns the load time in seconds."""
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1, env=self.env, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SpeechError("Speech-to-text isn't installed. Install it on the Speech-to-text unit.") from exc
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True, name="stt-out"),
            threading.Thread(target=self._read_stderr, daemon=True, name="stt-err"),
        ]
        for thread in self._threads:
            thread.start()
        reply = self._next_reply(timeout, waiting_for="the speech model to load")
        if not reply.get("ready"):
            self.stop()
            raise SpeechError(f"Speech-to-text didn't start: {reply}")
        return float(reply.get("load_seconds") or 0)

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._lines.put(line)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _next_reply(self, timeout: float, waiting_for: str) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=0.1)
            except queue.Empty:
                if self.proc is None or self.proc.poll() is not None:
                    code = self.proc.returncode if self.proc else "?"
                    raise SpeechError(f"Speech-to-text stopped (exit {code}): {self._stderr_tail()}") from None
                if time.monotonic() > deadline:
                    raise SpeechError(f"Timed out waiting for {waiting_for}.") from None
                continue
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue

    def transcribe(self, audio: Path, prompt: str | None = None, timeout: float = 60.0) -> dict:
        with self._lock:
            if not self.alive:
                raise SpeechError(f"Speech-to-text isn't running: {self._stderr_tail()}")
            rid = next(self._ids)
            request = {"id": rid, "audio": str(audio)}
            if prompt:
                request["prompt"] = prompt
            try:
                assert self.proc and self.proc.stdin
                self.proc.stdin.write(json.dumps(request) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise SpeechError(f"Speech-to-text stopped: {self._stderr_tail()}") from exc
            while True:
                reply = self._next_reply(timeout, waiting_for="the transcript")
                if reply.get("id") == rid:
                    break
        if "error" in reply:
            raise SpeechError(f"Transcription failed: {reply['error']}")
        return reply

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=2)
        for thread in self._threads:
            thread.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
