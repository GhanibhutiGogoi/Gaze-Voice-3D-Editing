"""Gaze from the camera as a long-lived worker: the face model loads once, then positions stream in.

A gaze part for the voice loop (`.start()`, `.stop()`, `.alive`, `.track`, `.radius`, `.view`), built like SpeechService.
The worker owns the camera, so the camera is open exactly as long as this tracker is started.
"""

from __future__ import annotations

import collections
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from .gaze import DEFAULT_RADIUS, GazeSample, GazeTrack, viewport_size

START_SECONDS = 60.0  # model load, and the camera's first picture, which can wait for the person to say yes
STOP_SECONDS = 4.0  # how long a worker gets to let go of the camera after its stdin closes
LOOKING_AWAY_MARGIN = 0.1  # a reading this far or less outside the viewport is noise; further out is looking away


class GazeError(Exception):
    """Gaze tracking failed; the message says why."""


def radius_of(calibration: dict | None) -> tuple[float, float]:
    """The patch half-size a calibration's check measured, or DEFAULT_RADIUS if it has none."""
    try:
        reach = calibration["check"]["radius"]  # type: ignore[index]
        if not isinstance(reach, (list, tuple)):
            return DEFAULT_RADIUS
        ru, rv = (float(x) for x in reach)
    except (TypeError, KeyError, ValueError):
        return DEFAULT_RADIUS
    return (ru, rv) if all(math.isfinite(x) and x > 0 for x in (ru, rv)) else DEFAULT_RADIUS


def view_of(calibration: dict | None) -> tuple[int, int] | None:
    """The viewport size (width, height) a calibration was made on, or None if it doesn't say."""
    try:
        return viewport_size(calibration["view"])  # type: ignore[index]
    except (TypeError, KeyError):
        return None


class GazeTracker:
    """The gaze worker: samples go into `.track`, and calibration goes through `.collect`, `.stop_collect` and `.fit`.

    `calibration` is a saved one, `{"model", "check", "view", …}`, or None before the first. Its model is loaded
    when the worker starts, its check sets `.radius`, and its view is `.view`: the viewport size it was made
    for, which the voice loop compares with Blender's now. `model_file` is MediaPipe's face model, which the
    worker needs unless `extra_args` has "--fake".
    """

    def __init__(
        self,
        python: Path,
        worker: Path,
        camera: str,
        calibration: dict | None = None,
        env: Mapping[str, str] | None = None,
        *,
        model_file: Path | None = None,
        extra_args: Sequence[str] = (),
    ):
        self.argv = [str(python), str(worker), "--serve", "--camera", str(camera)]
        if model_file is not None:
            self.argv += ["--model-file", str(model_file)]
        self.argv += list(extra_args)
        self.env = dict(os.environ, **(env or {}))
        self.calibration = calibration
        self.radius = radius_of(calibration)
        self.view = view_of(calibration)
        self.track = GazeTrack()
        self.proc: subprocess.Popen | None = None
        self._replies: queue.Queue[dict] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self._asking = threading.Lock()
        self._writing = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _stderr_tail(self) -> str:
        if self._threads:
            self._threads[1].join(timeout=0.5)
        return " | ".join(line for line in list(self._stderr)[-6:] if line) or "no output"

    def start(self, timeout: float = START_SECONDS) -> float:
        """Start the worker and wait for its first picture from the camera. Returns the start-up time in seconds."""
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1, env=self.env, start_new_session=True,
            )
        except OSError as exc:
            raise GazeError("Gaze tracking isn't installed. Install it on the Gaze unit.") from exc
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True, name="gaze-out"),
            threading.Thread(target=self._read_stderr, daemon=True, name="gaze-err"),
        ]
        for thread in self._threads:
            thread.start()
        try:
            reply = self._next_reply(time.monotonic() + timeout, "the camera to start")
            if not reply.get("ready"):
                raise GazeError(f"Gaze tracking didn't start: {reply}")
            if self.calibration and self.calibration.get("model"):
                self._load_model(self.calibration["model"], time.monotonic() + timeout)
        except GazeError:
            self.stop()
            raise
        return float(reply.get("load_seconds") or 0)

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "uv" in message and "t" in message:
                self._keep(message)
            else:
                self._replies.put(message)

    def _keep(self, message: dict) -> None:
        try:
            t, confidence = float(message["t"]), float(message.get("confidence", 0))
            u, v = (float(x) for x in message["uv"])
        except (KeyError, TypeError, ValueError):
            return
        if all(math.isfinite(x) for x in (t, u, v, confidence)):  # one NaN would spoil every median after it
            if not all(-LOOKING_AWAY_MARGIN <= x <= 1 + LOOKING_AWAY_MARGIN for x in (u, v)):
                confidence = 0.0  # looking away from the screen: ignored, rather than clamped to the nearest edge
            self.track.add(GazeSample(t, u, v, confidence))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _next_reply(self, deadline: float, waiting_for: str) -> dict:
        while True:
            try:
                return self._replies.get(timeout=0.1)
            except queue.Empty:
                if self.proc is None or self.proc.poll() is not None:
                    code = self.proc.returncode if self.proc else "?"
                    raise GazeError(f"Gaze tracking stopped (exit {code}): {self._stderr_tail()}") from None
                if time.monotonic() > deadline:
                    raise GazeError(f"Timed out waiting for {waiting_for}.") from None

    def _send(self, request: dict) -> None:
        if not self.alive:
            raise GazeError(f"Gaze tracking isn't running: {self._stderr_tail()}")
        try:
            assert self.proc and self.proc.stdin
            with self._writing:
                self.proc.stdin.write(json.dumps(request) + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise GazeError(f"Gaze tracking stopped: {self._stderr_tail()}") from exc

    def _ask(self, request: dict, key: str, deadline: float, waiting_for: str) -> dict:
        """Send a command and wait for its reply, the one holding `key` or an error for that command."""
        with self._asking:
            self._send(request)
            while True:
                reply = self._next_reply(deadline, waiting_for)
                if "error" in reply and reply.get("cmd") == request["cmd"]:
                    raise GazeError(str(reply["error"]))
                if key in reply:
                    return reply

    def _load_model(self, model: dict, deadline: float) -> None:
        try:
            self._ask({"cmd": "load", "model": model}, "loaded", deadline, "the calibration to load")
        except GazeError as exc:
            if self.alive:  # the worker refused it, rather than dying
                raise GazeError(f"The saved gaze calibration is out of date ({exc}). Calibrate gaze again.") from exc
            raise

    def load(self, calibration: dict, timeout: float = 10.0) -> None:
        """Start (or change) what the stream is calibrated with, and take `.radius` and `.view` from it."""
        self._load_model(calibration["model"], time.monotonic() + timeout)
        self.calibration = calibration
        self.radius = radius_of(calibration)
        self.view = view_of(calibration)

    def collect(self, uv: tuple[float, float], check: bool = False) -> None:
        """From now on keep a row of the face for each frame, as looking at `uv`.

        A `check` dot is held back from the fit and used to mark it, so it should be somewhere the fit
        has not seen.
        """
        request = {"cmd": "collect", "uv": [float(uv[0]), float(uv[1])]}
        if check:
            request["check"] = True
        self._send(request)

    def stop_collect(self) -> None:
        self._send({"cmd": "stop_collect"})

    def fit(self, timeout: float = 30.0) -> dict:
        """Fit the collected rows. The reply holds `fit` (the model), `check` (median, p90 and radius), `rows`,
        `check_rows` and `held_out`: whether `check` came from dots the fit had not seen. The collected rows
        are used up. The stream does not change until the model is passed to `load`."""
        return self._ask({"cmd": "fit"}, "fit", time.monotonic() + timeout, "the fit")

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the worker leads its own group, and ffmpeg is in it
        except (ProcessLookupError, PermissionError):
            proc.kill()

    def stop(self) -> None:
        """Stop the worker, and make sure its ffmpeg is gone too, so the camera is free when this returns."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the worker takes this as the signal to let go of the camera and finish
        except OSError:
            pass
        try:
            proc.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            proc.wait(timeout=2)
        else:
            if proc.returncode != 0:  # it didn't get to clean up after itself
                self._kill_group(proc)
        for thread in self._threads:
            thread.join(timeout=1)
        if not any(thread.is_alive() for thread in self._threads):  # closing a pipe someone is reading waits for them
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
