"""Watch the face and say where it is looking. Runs inside GazeVoice's own gaze venv, never in Blender.

    python gaze_worker.py --serve --camera 1 --model-file face_landmarker.task
    → {"ready": true, "load_seconds": 2.9}       the model is loaded and the first picture has arrived
    → {"t": 1794000000.1, "uv": [0.4, 0.3], "confidence": 0.9}     one per frame, once a calibration is loaded
    ← {"cmd": "load", "model": {…}}              → {"loaded": true}, or {"error": "…", "cmd": "load"}
    ← {"cmd": "collect", "uv": [0.5, 0.5]}       keep a row of the face for every frame while the person looks at
                                                 that dot ("check": true holds the rows back to test the fit)
    ← {"cmd": "stop_collect"}
    ← {"cmd": "fit"}                             → {"fit": {model}, "check": {…}, "rows": n, "check_rows": n,
                                                    "held_out": true}, or {"error": "…", "cmd": "fit"}

The camera is read by ffmpeg, which scales every frame to CAMERA_SIZE whatever modes the camera offers. One
frame at a time is read, looked at and dropped. Nothing is saved. ffmpeg is this worker's child and stops
when the worker's stdin closes, when the worker is told to stop, and, since it then has nobody to send
frames to, when the worker is killed. Stdin is listened to before the model loads, so a stop that arrives
during the load is heard first, and the camera is never opened.

Only what is needed is imported, only when serving for real, so `--help` and the tests work anywhere:
    --fake              no camera, no ffmpeg, no mediapipe: the same walk around the viewport every run
    --source-argv JSON  read frames from this command instead of ffmpeg (a stand-in camera for checks)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

try:
    from . import gaze_model
except ImportError:  # started as a script by the gaze venv's Python
    import gaze_model

CAMERA_SIZE = (960, 540)
FPS = 30
BLINK_LIMIT = 0.5  # MediaPipe's eyeBlink score at which an eye counts as shut
MIN_FIT_ROWS = 30  # fewer usable readings than this and the dots weren't seen
STOP_WAIT = 1.5  # seconds ffmpeg gets to let go of the camera before it is killed

_detector = None  # kept for the life of the process: MediaPipe takes about 3 s to tear down, and nothing needs it to


class CameraError(Exception):
    """The camera, or ffmpeg, couldn't be started."""


class ModelError(Exception):
    """The face model, or MediaPipe, couldn't be loaded."""


def camera_argv(index: str, width: int, height: int, fps: int = 30) -> list[str]:
    """ffmpeg's command for camera `index` (from the AVFoundation list): raw RGB frames of one fixed size on stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-framerate", str(fps), "-i", f"{index}:none",
        "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]


def blink_confidence(left: float, right: float) -> float:
    """1 for open eyes, falling as they close, and 0 once either is shut (from MediaPipe's eyeBlink scores)."""
    blink = max(left, right)
    return 0.0 if blink >= BLINK_LIMIT else 1.0 - blink


class Observation(NamedTuple):
    """What one frame showed. `row` is the face's features, or None when there was no face to measure."""

    row: list[float] | None
    confidence: float
    uv: tuple[float, float] | None = None  # only the fake knows the answer without a model


def observation_from(found, aspect: float) -> Observation:
    """What MediaPipe's result for one frame says: the face's features and how open its eyes are.

    No face, a face it couldn't measure, or a result missing its blendshapes or matrix all read as no face.
    """
    if not (found.face_landmarks and found.face_blendshapes and found.facial_transformation_matrixes):
        return Observation(None, 0.0)
    landmarks = [(p.x, p.y, p.z) for p in found.face_landmarks[0]]
    try:
        row = gaze_model.features(landmarks, found.facial_transformation_matrixes[0].tolist(), aspect)
    except ValueError:
        return Observation(None, 0.0)
    scores = {c.category_name: c.score for c in found.face_blendshapes[0]}
    return Observation(row, blink_confidence(scores.get("eyeBlinkLeft", 0.0), scores.get("eyeBlinkRight", 0.0)))


class FrameSource:
    """A command that writes raw RGB frames to stdout, read one whole frame at a time. It dies with us."""

    def __init__(self, argv: list[str], width: int, height: int):
        self.frame_bytes = width * height * 3
        self._closing = threading.Lock()
        self._closed = False
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, bufsize=self.frame_bytes
            )  # same process group as ours, so killing the group ends it; stderr is ours too
        except OSError as exc:
            raise CameraError(f"Couldn't start {Path(argv[0]).name}: {exc}") from exc

    def read(self) -> bytes | None:
        """The next whole frame, or None once the command has stopped or the source is closed."""
        if self._closed:
            return None
        try:
            frame = self.proc.stdout.read(self.frame_bytes)
        except (OSError, ValueError):  # closed under us
            return None
        return frame if len(frame) == self.frame_bytes else None

    def close(self) -> None:
        """Stop the command and wait for it, so the camera is free when this returns. Safe from any thread, twice."""
        with self._closing:  # a second caller waits for the first to finish
            if self._closed:
                return
            self._closed = True
            if self.proc.poll() is None:
                self.proc.terminate()
                # A child stuck writing to a full pipe can't act on the signal, so keep the pipe empty meanwhile.
                threading.Thread(target=self._drain, daemon=True, name="frames-drain").start()
                try:
                    self.proc.wait(timeout=STOP_WAIT)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            self.proc.stdout.close()  # waits for the drain to see the end of the pipe

    def _drain(self) -> None:
        try:
            while self.proc.stdout.read(65536):
                pass
        except (OSError, ValueError):
            pass

    @property
    def exit_code(self) -> int | None:
        return self.proc.poll()


class FakeFrameSource:
    """No camera: a tick every frame time, with nothing in it."""

    def __init__(self, stop: threading.Event):
        self.stop = stop

    def read(self) -> bytes | None:
        return None if self.stop.wait(1 / FPS) else b""

    def close(self) -> None:
        pass


def fake_row(u: float, v: float) -> list[float]:
    return [u, v] + [0.0] * (gaze_model.FEATURE_COUNT - 2)


class FakeDetector:
    """No MediaPipe: a walk around the viewport that is the same every run, with a blink every 25th frame."""

    def __init__(self) -> None:
        self.frames = 0

    def look(self, frame: bytes) -> Observation:
        k, self.frames = self.frames, self.frames + 1
        u, v = 0.5 + 0.35 * math.sin(k / 12), 0.5 + 0.35 * math.sin(k / 9 + 1)
        return Observation(fake_row(u, v), 0.0 if k % 25 == 24 else 0.9, (u, v))


class FaceDetector:
    """MediaPipe's Face Landmarker on one RGB frame at a time."""

    def __init__(self, model_file: Path, size: tuple[int, int] = CAMERA_SIZE):
        if not model_file.is_file():
            raise ModelError(f"The face model file {model_file.name} wasn't found at {model_file}.")
        try:
            import mediapipe as mp
            import numpy
            from mediapipe.tasks import python as mp_tasks
            from mediapipe.tasks.python import vision
        except ImportError as exc:
            raise ModelError(f"MediaPipe isn't installed in this Python ({exc}).") from exc
        self.mp, self.numpy = mp, numpy
        self.width, self.height = size
        options = vision.FaceLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=str(model_file)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,  # the library's own spelling
        )
        try:
            self.landmarker = vision.FaceLandmarker.create_from_options(options)
        except Exception as exc:  # noqa: BLE001 - a bad model file comes in several types
            raise ModelError(f"MediaPipe couldn't load {model_file.name}: {exc}") from exc
        self._began = time.monotonic()
        self._last_ms = -1

    def look(self, frame: bytes) -> Observation:
        picture = self.numpy.frombuffer(frame, dtype=self.numpy.uint8).reshape(self.height, self.width, 3)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=picture)  # RGB, which is what ffmpeg sends
        self._last_ms = max(self._last_ms + 1, int((time.monotonic() - self._began) * 1000))  # must only go up
        return observation_from(self.landmarker.detect_for_video(image, self._last_ms), self.width / self.height)


class Session:
    """The calibration being collected or used. Commands arrive on one thread and frames on another."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self._lock = threading.Lock()
        self._model: dict | None = None
        self._collecting: tuple[float, float, bool] | None = None  # the dot being looked at, and whether it's a check dot
        self._fit_rows: list[list[float]] = []
        self._fit_targets: list[tuple[float, float]] = []
        self._check_rows: list[list[float]] = []
        self._check_targets: list[tuple[float, float]] = []
        self._last_uv = (0.5, 0.5)

    @property
    def wants_frames(self) -> bool:
        """Whether a frame is worth looking at. With no model and no dots to collect, it would only be heat."""
        with self._lock:
            return self._model is not None or self._collecting is not None

    def see(self, t: float, seen: Observation) -> None:
        """Keep the frame's row if a dot is being collected, and report where the face is looking."""
        with self._lock:
            model, collecting = self._model, self._collecting
            if collecting and seen.row is not None and seen.confidence > 0:
                rows, targets = (
                    (self._check_rows, self._check_targets) if collecting[2] else (self._fit_rows, self._fit_targets)
                )
                rows.append(seen.row)
                targets.append(collecting[:2])
        if model is None:
            return
        if seen.uv is not None:
            uv = seen.uv
        elif seen.row is not None:
            uv = gaze_model.predict(model, seen.row)
        else:
            uv = self._last_uv  # no face: nothing to say where, and confidence 0 says so
        self._last_uv = uv
        self.emit({"t": round(t, 3), "uv": [round(uv[0], 4), round(uv[1], 4)], "confidence": round(seen.confidence, 2)})

    def handle(self, request: dict) -> None:
        command = request.get("cmd")
        if command == "load":
            self._load(request.get("model"))
        elif command == "collect":
            self._collect(request.get("uv"), bool(request.get("check")))
        elif command == "stop_collect":
            with self._lock:
                self._collecting = None
        elif command == "fit":
            self._fit()
        else:
            print(f"Unknown command: {command!r}", file=sys.stderr, flush=True)

    def _load(self, model: object) -> None:
        try:
            gaze_model.validate(model)
        except ValueError as exc:
            self.emit({"error": str(exc), "cmd": "load"})
            return
        with self._lock:
            self._model = model
        self.emit({"loaded": True})

    def _collect(self, uv: object, check: bool) -> None:
        try:
            u, v = (float(x) for x in uv)  # type: ignore[union-attr]
            if not (math.isfinite(u) and math.isfinite(v)):
                raise ValueError("not finite")
        except (TypeError, ValueError):
            print(f"collect needs uv as two numbers, got {uv!r}", file=sys.stderr, flush=True)
            return
        with self._lock:
            self._collecting = (u, v, check)

    def _fit(self) -> None:
        with self._lock:
            self._collecting = None
            rows, targets = self._fit_rows, self._fit_targets
            check_rows, check_targets = self._check_rows, self._check_targets
            self._fit_rows, self._fit_targets, self._check_rows, self._check_targets = [], [], [], []
        if len(rows) < MIN_FIT_ROWS:
            self.emit({"error": f"Not enough face readings to calibrate: {len(rows)}, and {MIN_FIT_ROWS} are needed. "
                                "Check that your face is lit and in view of the camera.", "cmd": "fit"})
            return
        try:
            model = gaze_model.fit(rows, targets)
            held_out = bool(check_rows)
            # With no check dots, the fit is marked against its own dots, which flatters it; "held_out" says so.
            marks = gaze_model.check(model, check_rows, check_targets) if held_out else gaze_model.check(model, rows, targets)
        except ValueError as exc:
            self.emit({"error": str(exc), "cmd": "fit"})
            return
        self.emit({"fit": model, "check": marks, "rows": len(rows), "check_rows": len(check_rows), "held_out": held_out})


def _read_lines(fd: int):
    """Lines from a file descriptor. Read straight from the descriptor, so that a thread still waiting on it
    when the program ends can't trip over Python's buffered stdin."""
    pending = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def serve(args: argparse.Namespace) -> int:
    global _detector
    # Our replies own the real stdout; anything a library prints goes to stderr instead.
    replies = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    sys.stdout = sys.stderr
    started = time.monotonic()
    stop = threading.Event()
    writing = threading.Lock()
    source = None

    def emit(message: dict) -> None:
        with writing:
            try:
                replies.write(json.dumps(message) + "\n")
            except (OSError, ValueError):  # the app has gone away
                stop.set()
                if source is not None:
                    source.close()

    def say(message: str) -> int:
        print(message, file=sys.stderr, flush=True)
        return 1

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the finally below runs and ffmpeg is let go
    session = Session(emit)
    opening = threading.Lock()  # a stop and the camera opening never overlap: whichever comes second sees the first

    def read_commands() -> None:
        try:
            for line in _read_lines(sys.stdin.fileno()):
                line = line.strip()
                if not line:
                    continue
                try:
                    session.handle(json.loads(line))
                except Exception as exc:  # noqa: BLE001 - one bad command mustn't stop the stream
                    print(f"Couldn't handle {line[:80]!r}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        finally:  # the app closed our stdin, or went away: let go of the camera, if it is open yet, and finish
            with opening:
                stop.set()
                opened = source
            if opened is not None:
                opened.close()

    # Listening comes first, so a stop that arrives while the model is still loading is heard before the camera opens.
    threading.Thread(target=read_commands, daemon=True, name="gaze-commands").start()
    try:
        # The model next: a missing file or library is found before the camera is ever opened.
        detector = _detector = FakeDetector() if args.fake else FaceDetector(Path(args.model_file or ""))
        argv = json.loads(args.source_argv) if args.source_argv else camera_argv(args.camera, *CAMERA_SIZE)
        with opening:
            if stop.is_set():
                return 0  # stopped while loading
            source = FakeFrameSource(stop) if args.fake and not args.source_argv else FrameSource(argv, *CAMERA_SIZE)
    except (ModelError, CameraError, ValueError) as exc:
        return say(str(exc))
    ready = False
    complained = ""
    try:
        while not stop.is_set():
            frame = source.read()
            if frame is None:
                break
            t = time.time()
            if not ready:
                ready = True
                emit({"ready": True, "load_seconds": round(time.monotonic() - started, 2)})
            if not session.wants_frames:
                continue
            try:
                seen = detector.look(frame)
            except Exception as exc:  # noqa: BLE001 - say why once, then carry on as if no face were there
                if str(exc) != complained:
                    complained = str(exc)
                    print(f"Couldn't look at a frame: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                seen = Observation(None, 0.0)
            session.see(t, seen)
        wanted = stop.is_set()
    finally:
        stop.set()
        source.close()
    if wanted:
        return 0
    code = getattr(source, "exit_code", None)
    how = f" (exit code {code})" if code is not None else ""
    return say(f"The camera stopped sending pictures{how}." if ready else f"The camera never sent a picture{how}.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--camera", default="0", help="the camera's number in ffmpeg's AVFoundation list")
    parser.add_argument("--model-file", help="MediaPipe's face_landmarker.task")
    parser.add_argument("--fake", action="store_true", help="no camera, no ffmpeg, no mediapipe (tests)")
    parser.add_argument("--source-argv", help="a JSON list: read frames from this command, not from ffmpeg (checks)")
    args = parser.parse_args(argv)
    if not args.fake and not args.model_file:
        parser.error("--model-file is required unless --fake")
    return args


def main(argv: list[str] | None = None) -> int:
    return serve(parse_args(argv))


if __name__ == "__main__":
    code = main()
    sys.stderr.flush()
    os._exit(code)  # no clean-up: there is nothing left to save, and MediaPipe's would take seconds
