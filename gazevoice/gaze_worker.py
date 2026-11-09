"""Watch the face and say where it is looking. Runs inside GazeVoice's own gaze venv, never in Blender.

    python gaze_worker.py --serve --camera 1 --model-file face_landmarker.task
    → {"ready": true, "load_seconds": 2.9}       the model is loaded and the first picture has arrived
    → {"t": 1794000000.1, "uv": [0.4, 0.3], "confidence": 0.9}     one per frame, once a calibration is loaded
    ← {"cmd": "load", "model": {…}}              → {"loaded": true}, or {"error": "…", "cmd": "load"}
    ← {"cmd": "collect", "uv": [0.5, 0.5]}       keep a row of the face for every frame while the person looks at
                                                 that dot ("check": true holds the rows back to test the fit)
    ← {"cmd": "stop_collect"}
    ← {"cmd": "guide", "on": true}               while on, up to GUIDE_RATE a second:
    → {"guide": {"found": true, "x": share, "y": share, "distance": cm, "box": [left, top, right, bottom]}},
      or {"guide": {"found": false}}             x and y: the middle of the eyes as shares of the picture, not mirrored;
                                                 box: how far the face reaches each way, as shares of the picture
    ← {"cmd": "fit", "screen_width_cm": 29.0}    → {"fit": {model}, "check": {…}, "rows": n, "check_rows": n,
                                                    "held_out": true, "seen": "what the frames showed",
                                                    "model": "the set of features chosen",
                                                    "guess": what always saying the middle would miss on the check dots,
                                                    "moved": {"cm": n, "degrees": n}, how far the head went,
                                                    "screen_width_cm": the width the head's turn was allowed for with,
                                                    or null when there was none},
                                                    or {"error": "…", "cmd": "fit"}

The camera is read by ffmpeg, which scales every frame to CAMERA_SIZE whatever modes the camera offers. One
frame at a time is read, looked at and dropped. No picture is saved. ffmpeg is this worker's child and stops
when the worker's stdin closes, when the worker is told to stop, and, since it then has nobody to send
frames to, when the worker is killed. Stdin is listened to before the model loads, so a stop that arrives
during the load is heard first, and the camera is never opened.

Only what is needed is imported, only when serving for real, so `--help` and the tests work anywhere:
    --fake              no camera, no ffmpeg, no mediapipe: the same walk around the viewport every run
    --source-argv JSON  read frames from this command instead of ffmpeg (a stand-in camera for checks)

--readings-file PATH keeps the numbers of a run there, for study when a calibration fails or to see what to try next:
the readings, in the order they came, and for each of them when it came, MediaPipe's 14 scores for the eyes, the head's
4x4 transform, how wide the face was in pixels, its box in the picture (as the guide reads it) and where the eyes'
outlines and irises were in the picture; the size and rate the camera really sends; and for every frame looked at
while the guide or a dot was up, when it came, which dot it went with, what it showed and how bright it was. "seen" is
one of "face", "shut" (a face with the eyes shut), "no face", "half-read" (a face that couldn't be measured) or
"error". They are the words the result card uses for the kinds of frame ("no face in 3, half-read in 1, eyes counted
as shut in 2, errors in 0"), while the counts beside the frames are named no_face, half_read, shut and errors. Numbers
only, never a picture.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple

try:
    from . import gaze_model
except ImportError:  # started as a script by the gaze venv's Python
    import gaze_model

# 16:9, as gaze_model.CAMERA_ASPECT says. Not bigger, on purpose. MediaPipe follows a face it has already found, but
# to find one again after losing it (a hand, a turn of the head) it shrinks the whole frame to a small square and
# looks. From a bigger frame that shrink is rougher, the score it gives a real face falls under its 0.5 cut-off, and
# the face isn't found again. On noisy frames, like a camera's in dim light, its middle score was 0.76 from 960 by
# 540 and 0.23 from 1280 by 720. tests/test_face_refind.py checks this against the real thing.
CAMERA_SIZE = (960, 540)
FPS = 30
BLINK_LIMIT = 0.5  # MediaPipe's eyeBlink score at which an eye counts as shut
# Fewer usable readings than this and the dots weren't seen: about one dot's worth, of 24. A run with a few more
# still has to pass its check dots to be saved.
MIN_FIT_ROWS = 30
STOP_WAIT = 1.5  # seconds ffmpeg gets to let go of the camera before it is killed
GUIDE_RATE = 10  # at most this many readings of where the head is, a second, while the guide is on
EYE_CORNERS = (33, 263)  # the outer corners of the two eyes: their middle is the middle of the eyes
HEAD_DISTANCE = gaze_model.BASE_NAMES.index("head distance")  # where the row holds how far the head is, in centimetres
FRAME_NAMES = ["ms", "dot", "seen", "light"]  # what each frame in the readings file holds, in order
# What else the readings file keeps of each row, for the next study: MediaPipe's scores for the eyes, by its own names
# (the eyes looking up, down, in and out, blinking, squinting and wide open, each eye apart), and the landmarks of the
# eyes' outlines and irises. Those are MediaPipe 1.2.2's own lists, read from FaceLandmarksConnections in the gaze venv:
# FACE_LANDMARKS_RIGHT_EYE, then the right iris's middle and FACE_LANDMARKS_RIGHT_IRIS, then the same for the left.
EYE_SCORES = [
    "eyeLookUpLeft", "eyeLookUpRight", "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight", "eyeBlinkLeft", "eyeBlinkRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
]
EYE_POINTS = [
    33, 7, 163, 144, 145, 153, 154, 155, 133, 246, 161, 160, 159, 158, 157, 173, 468, 469, 470, 471, 472,
    263, 249, 390, 373, 374, 380, 381, 382, 362, 466, 388, 387, 386, 385, 384, 398, 473, 474, 475, 476, 477,
]
# The readings file keeps at most this many frames, the first ones, so that it stays small however long the guide
# is left on. That is over two minutes at 30 frames a second, more than the guide (up to a minute) and the dots
# (about half a minute) take.
FRAME_LIMIT = 4000

_detector = None  # kept for the life of the process: MediaPipe takes about 3 s to tear down, and nothing needs it to


class CameraError(Exception):
    """The camera, or ffmpeg, couldn't be started."""


class ModelError(Exception):
    """The face model, or MediaPipe, couldn't be loaded."""


def camera_argv(index: str, width: int, height: int, fps: int = 30) -> list[str]:
    """ffmpeg's command for camera `index` (from the AVFoundation list): raw RGB frames of one fixed size on stdout.

    It says what it is doing at the info level, each line tagged with its level, so that the camera's real size and rate
    can be read from it and only trouble passed on (see CameraLog). With no running count of frames, it says little after
    the camera opens.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "level+info", "-nostats",
        "-f", "avfoundation", "-framerate", str(fps), "-i", f"{index}:none",
        # Passthrough, because raw video otherwise holds to a steady frame rate by sending some frames twice.
        "-vf", f"scale={width}:{height}", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]


# A line as ffmpeg tags it with "level": "[info] …", or "[avfoundation @ 0x1] [error] …" when it names where, and
# "[in#0/avfoundation @ 0x1] [avfoundation @ 0x2] [error] …" when it names that part and the one above it.
_LEVEL = re.compile(r"^((?:\[[^\]]* @ [^\]]*\] ){0,2})\[(panic|fatal|error|warning|info|verbose|debug|trace)\] ")
# Left out, as they were when ffmpeg was asked for errors only. Warnings too: one can come with every frame, and they
# would push the reason for an error out of the few last lines its message shows.
_CHATTER = {"warning", "info", "verbose", "debug", "trace"}
_SIZE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")
_RATE = re.compile(r"\b(\d+(?:\.\d+)?)(k?) (fps|tbr)\b")
MAX_FPS = 240  # a rate above this is ffmpeg's timing, not a camera's


def camera_of(stream: str) -> dict | None:
    """The frame size and rate in the line ffmpeg prints for a video stream it opens: {"width", "height", "fps"}, with
    fps None when the line gives no rate a camera could have. None if the line holds no size."""
    video = stream.split("Video:", 1)[-1]
    size = _SIZE.search(video)
    if size is None:
        return None
    rates = {unit: float(number) * (1000 if thousands else 1) for number, thousands, unit in _RATE.findall(video)}
    fps = rates.get("fps", rates.get("tbr"))
    return {"width": int(size.group(1)), "height": int(size.group(2)), "fps": fps if fps and fps <= MAX_FPS else None}


class CameraLog:
    """What ffmpeg says on stderr, a line at a time. The camera's real size and rate are read once, from the line it
    prints for the camera it opens, and given to `on_camera`. Its other info lines and its warnings are dropped, so
    that they never crowd out the reason for an error, which is passed on to `out` (stderr) as ffmpeg said it before it
    was asked for more: without the level tag. A line with no tag isn't ffmpeg's, as from a stand-in camera, and is passed on whole."""

    def __init__(self, on_camera=None, out=None) -> None:
        self.on_camera = on_camera
        self.out = out
        self._read = False  # the camera's line has been read, or ffmpeg has gone on to what it sends out

    def line(self, text: str) -> None:
        text = text.rstrip("\r\n")
        tagged = _LEVEL.match(text)
        if tagged is None:
            self._say(text)
            return
        where, level, message = tagged.group(1) or "", tagged.group(2), text[tagged.end():]
        if level not in _CHATTER:
            self._say(where + message)
        elif not self._read and message.lstrip().startswith("Output #"):
            self._read = True  # from here its streams are the frames it sends, not the camera's
        elif not self._read and "Stream #" in message and "Video:" in message:
            self._read = True
            camera = camera_of(message)
            if camera is not None and self.on_camera is not None:
                self.on_camera(camera)

    def _say(self, text: str) -> None:
        if text:
            print(text, file=sys.stderr if self.out is None else self.out, flush=True)


def blink_confidence(left: float, right: float) -> float:
    """1 for open eyes, falling as they close, and 0 once either is shut (from MediaPipe's eyeBlink scores)."""
    blink = max(left, right)
    return 0.0 if blink >= BLINK_LIMIT else 1.0 - blink


def brightness(frame: bytes) -> float | None:
    """How bright a frame is, 0 to 255, from a sample of its bytes. One number: nothing of the picture is kept."""
    sample = frame[::997]
    return sum(sample) / len(sample) if sample else None


def _finite(value: object) -> float | None:
    """`value` as a number, or None when it isn't a finite one. JSON has no not-a-number, and the readings file is
    written whole or not at all, so one of them would lose the run."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class Detail(NamedTuple):
    """More of one frame's face than the fit reads, kept only for the readings file. Numbers, never a picture, and None
    for any that wasn't a finite number."""

    eye_scores: list[float | None]  # MediaPipe's, in the order of EYE_SCORES
    transform: list[float | None]  # its 4x4 facial transformation matrix, row by row
    face_px: float | None  # how wide the face's landmarks reach, in pixels of the frame
    eye_points: list[float | None]  # x then y of each landmark in EYE_POINTS, as shares of the picture


class Observation(NamedTuple):
    """What one frame showed. `row` is the face's features, or None when there was no face to measure."""

    row: list[float] | None
    confidence: float
    uv: tuple[float, float] | None = None  # only the fake knows the answer without a model
    why: str = ""  # with no row: "no face", "half-read" (found, but it couldn't be measured) or "error: …"
    blink: float = 0.0  # the higher of MediaPipe's two eyeBlink scores
    place: tuple[float, float, float] | None = None  # the middle of the eyes as shares of the picture, and cm from the camera
    # How far the face's landmarks reach: left, top, right and bottom, as shares of the picture, not mirrored. A face
    # partly out of the picture reaches past 0 or 1, because MediaPipe places what it can't see where it would be.
    box: tuple[float, float, float, float] | None = None
    detail: Detail | None = None  # only from MediaPipe: the fake face has none


def observation_from(found, aspect: float, width: int = CAMERA_SIZE[0]) -> Observation:
    """What MediaPipe's result for one frame says: the face's features and how open its eyes are.

    No face, a face it couldn't measure, or a result missing its blendshapes or matrix all read as no face. `width` is
    the frame's width in pixels, which the face's width is measured in.
    """
    if not found.face_landmarks:
        return Observation(None, 0.0, why="no face")
    if not (found.face_blendshapes and found.facial_transformation_matrixes):
        return Observation(None, 0.0, why="half-read")
    landmarks = [(p.x, p.y, p.z) for p in found.face_landmarks[0]]
    transform = found.facial_transformation_matrixes[0].tolist()
    try:
        row = gaze_model.features(landmarks, transform, aspect)
    except ValueError:
        return Observation(None, 0.0, why="half-read")
    scores = {c.category_name: c.score for c in found.face_blendshapes[0]}
    left, right = scores.get("eyeBlinkLeft", 0.0), scores.get("eyeBlinkRight", 0.0)
    x, y = (sum(landmarks[corner][axis] for corner in EYE_CORNERS) / len(EYE_CORNERS) for axis in (0, 1))
    across, down = [p[0] for p in landmarks], [p[1] for p in landmarks]
    # A face with a point nowhere has no box and no width: min and max would pass over a not-a-number, or keep it.
    box = (min(across), min(down), max(across), max(down)) if all(map(math.isfinite, across + down)) else None
    detail = Detail([_finite(scores.get(name)) for name in EYE_SCORES], [_finite(v) for line in transform for v in line],
                    None if box is None else (box[2] - box[0]) * width,
                    [_finite(landmarks[at][axis]) for at in EYE_POINTS for axis in (0, 1)])
    return Observation(row, blink_confidence(left, right), blink=max(left, right), place=(x, y, row[HEAD_DISTANCE]),
                       box=box, detail=detail)


class DotsSeen:
    """What the frames showed while the dots were up, as counts, so a calibration can say what went wrong."""

    def __init__(self) -> None:
        self.frames = self.no_face = self.half_read = self.shut = self.errors = 0
        self.error = ""
        self.blinks: list[float] = []
        self.light: list[float] = []

    def counts(self) -> dict[str, int]:
        return {"frames": self.frames, "no_face": self.no_face, "half_read": self.half_read, "shut": self.shut,
                "errors": self.errors}

    def add(self, seen: Observation, light: float | None) -> None:
        self.frames += 1
        if light is not None:
            self.light.append(light)
        if seen.row is not None:
            self.blinks.append(seen.blink)
            self.shut += seen.confidence <= 0
        elif seen.why.startswith("error"):
            self.errors += 1
            self.error = seen.why.removeprefix("error: ")
        elif seen.why == "half-read":
            self.half_read += 1
        else:
            self.no_face += 1

    def line(self) -> str:
        blink = "no blink scores"
        if self.blinks:
            blink = f"typical blink score {sorted(self.blinks)[len(self.blinks) // 2]:.2f}, limit {BLINK_LIMIT}"
        light = f"{round(sum(self.light) / len(self.light))} of 255" if self.light else "unknown"
        error = f" ({self.error})" if self.error else ""
        return (f"The camera sent {self.frames} frames while the dots were up: no face in {self.no_face}, "
                f"half-read in {self.half_read}, eyes counted as shut in {self.shut} ({blink}), "
                f"errors in {self.errors}{error}. Average brightness {light}.")


def kind_of_frame(seen: Observation) -> str:
    """What a frame showed, in the words DotsSeen counts by: "face", "shut" (a face with the eyes closed), "no face",
    "half-read" or "error"."""
    if seen.row is not None:
        return "shut" if seen.confidence <= 0 else "face"
    if seen.why.startswith("error"):
        return "error"
    return "half-read" if seen.why == "half-read" else "no face"


class FrameLog:
    """One short entry for each frame looked at while the guide or a dot was up, for the readings file: [ms, dot,
    seen, light]. ms is milliseconds since the first frame, dot is 0 for the guide and otherwise which dot it was
    (see FRAME_NAMES). Only the first FRAME_LIMIT are kept. The only thing taken from the picture is its brightness."""

    def __init__(self) -> None:
        self.entries: list[list] = []
        self._first: float | None = None

    def add(self, t: float, dot: int, seen: Observation, light: float | None) -> None:
        if len(self.entries) >= FRAME_LIMIT:
            return
        if self._first is None:
            self._first = t
        self.entries.append([self.ms(t), dot, kind_of_frame(seen), None if light is None else round(float(light), 1)])

    def ms(self, t: float) -> int:
        """Milliseconds from the run's first frame to `t`, the clock every frame and row of the readings file is on."""
        return 0 if self._first is None else round((t - self._first) * 1000)


class FrameSource:
    """A command that writes raw RGB frames to stdout, read one whole frame at a time. It dies with us.

    What it says on stderr goes through a CameraLog: `on_camera` is given the camera's real size and rate, and trouble
    is passed on to our stderr.
    """

    def __init__(self, argv: list[str], width: int, height: int, on_camera=None):
        self.frame_bytes = width * height * 3
        self._closing = threading.Lock()
        self._closed = False
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=self.frame_bytes
            )  # same process group as ours, so killing the group ends it
        except OSError as exc:
            raise CameraError(f"Couldn't start {Path(argv[0]).name}: {exc}") from exc
        self._log = CameraLog(on_camera)
        self._listening = threading.Thread(target=self._read_stderr, daemon=True, name="frames-stderr")
        self._listening.start()

    def _read_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                self._log.line(line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            self.proc.stderr.close()

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
        # What it said last is often why it stopped, and must be passed on before we say anything after it.
        self._listening.join(timeout=STOP_WAIT)

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


FAKE_PLACE = (0.5, 0.45, 30.0)  # where the app's outline is, at the distance it stands for, so a fake run passes the guide
FAKE_BOX = (0.36, 0.12, 0.64, 0.86)  # and with the whole face in view: about the box of a face 30 cm away


class FakeDetector:
    """No MediaPipe: a walk around the viewport that is the same every run, with a blink every 25th frame."""

    def __init__(self) -> None:
        self.frames = 0

    def look(self, frame: bytes) -> Observation:
        k, self.frames = self.frames, self.frames + 1
        u, v = 0.5 + 0.35 * math.sin(k / 12), 0.5 + 0.35 * math.sin(k / 9 + 1)
        return Observation(fake_row(u, v), 0.0 if k % 25 == 24 else 0.9, (u, v), place=FAKE_PLACE, box=FAKE_BOX)


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
        return observation_from(self.landmarker.detect_for_video(image, self._last_ms), self.width / self.height,
                                self.width)


def _write_whole(path: Path, data: dict) -> None:
    """Write `data` as JSON whole or not at all: a temp file beside `path`, then moved over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Kept(NamedTuple):
    """A row kept while a dot was up, with what the readings file keeps beside it."""

    row: list[float]
    target: tuple[float, float]  # the dot
    check: bool  # whether it was a check dot, held back from the fit
    ms: int  # when it came, on the frames' clock
    detail: Detail | None
    box: tuple[float, float, float, float] | None  # the face's box, as the guide reads it


class Session:
    """The calibration being collected or used. Commands arrive on one thread and frames on another."""

    def __init__(self, emit, readings_file: Path | None = None) -> None:
        self.emit = emit
        self._readings_file = readings_file
        self._lock = threading.Lock()
        self._model: dict | None = None
        self._collecting: tuple[float, float, bool] | None = None  # the dot being looked at, and whether it's a check dot
        # Every row kept while a dot was up, in the order they came. The check dots are mixed in among the others.
        self._kept: list[Kept] = []
        self._camera: dict | None = None  # what the camera really sends, once ffmpeg has said (see FrameSource)
        self._dots_seen = DotsSeen()
        self._frames = FrameLog()
        self._dot = 0  # how many dots have been asked for since the run began
        self._last_uv = (0.5, 0.5)
        self._guide = False  # whether the head's place is being reported, for the guide before the dots
        self._guide_at = -math.inf  # when the last report was made

    def saw_camera(self, camera: dict) -> None:
        """The camera's real size and rate, from what ffmpeg said when it opened it. Called from the thread reading that."""
        with self._lock:
            self._camera = camera

    @property
    def wants_frames(self) -> bool:
        """Whether a frame is worth looking at. With no model, no dots to collect and no guide, it would only be heat."""
        with self._lock:
            return self._model is not None or self._collecting is not None or self._guide

    def see(self, t: float, seen: Observation, light: float | None = None) -> None:
        """Keep the frame's row if a dot is being collected, report where the head is if the guide is on, and report
        where the face is looking."""
        with self._lock:
            model, collecting = self._model, self._collecting
            guiding = self._guide and t - self._guide_at >= 1 / GUIDE_RATE
            if guiding:
                self._guide_at = t
            if collecting:
                self._dots_seen.add(seen, light)
            if collecting or self._guide:
                self._frames.add(t, self._dot if collecting else 0, seen, light)
            if collecting and seen.row is not None and seen.confidence > 0:
                self._kept.append(Kept(seen.row, collecting[:2], collecting[2], self._frames.ms(t), seen.detail, seen.box))
        if guiding:
            self.emit({"guide": self._where(seen)})
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

    @staticmethod
    def _where(seen: Observation) -> dict:
        if seen.place is None:
            return {"found": False}
        x, y, distance = seen.place
        where = {"found": True, "x": round(float(x), 4), "y": round(float(y), 4), "distance": round(float(distance), 1)}
        if seen.box is not None:  # always, from a real face: without it the app can't tell a face cut off at the edge
            where["box"] = [round(float(edge), 4) for edge in seen.box]
        return where

    def handle(self, request: dict) -> None:
        command = request.get("cmd")
        if command == "guide":
            with self._lock:
                self._guide, self._guide_at = bool(request.get("on")), -math.inf
        elif command == "load":
            self._load(request.get("model"))
        elif command == "collect":
            self._collect(request.get("uv"), bool(request.get("check")))
        elif command == "stop_collect":
            with self._lock:
                self._collecting = None
        elif command == "fit":
            self._fit(self._width(request.get("screen_width_cm")))
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
            self._dot += 1

    @staticmethod
    def _width(width: object) -> float | None:
        """The screen's width in centimetres, or None when it wasn't given or makes no sense: the fit then does without
        allowing for the head's turn, as it did before."""
        if width is None:
            return None
        if type(width) in (int, float) and math.isfinite(width) and width > 0:
            return float(width)
        print(f"A screen width of {width!r} cm makes no sense, so the head's turn isn't allowed for.", file=sys.stderr,
              flush=True)
        return None

    def _fit(self, screen_width_cm: float | None = None) -> None:
        with self._lock:
            self._collecting = None
            kept, self._kept = self._kept, []
            dots, self._dots_seen = self._dots_seen, DotsSeen()
            frames, self._frames, self._dot = self._frames, FrameLog(), 0
            camera = self._camera
        self._keep_readings(kept, dots, frames, camera)
        rows = [k.row for k in kept if not k.check]
        targets = [k.target for k in kept if not k.check]
        check_rows = [k.row for k in kept if k.check]
        check_targets = [k.target for k in kept if k.check]
        seen = dots.line()
        if len(rows) < MIN_FIT_ROWS:
            self.emit({"error": f"Not enough face readings to calibrate: {len(rows)}, and {MIN_FIT_ROWS} are needed. "
                                f"Check that your face is lit and in view of the camera. {seen}", "cmd": "fit"})
            return
        every_row, every_target = [k.row for k in kept], [k.target for k in kept]  # in the order they came
        try:
            choice = gaze_model.choose(rows, targets, screen_width_cm)
            model = gaze_model.fit_choice(rows, targets, choice, screen_width_cm)
            held_out = bool(check_rows)
            # With no check dots, the fit is marked against its own dots, which flatters it; "held_out" says so.
            marks = gaze_model.check(model, check_rows, check_targets) if held_out else gaze_model.check(model, rows, targets)
            guess = gaze_model.middle_miss(targets, check_targets) if held_out else None
            if held_out:  # 24 places teach more than 16. The mark stays the one from before, the only honest one.
                model = gaze_model.fit_choice(every_row, every_target, choice, screen_width_cm)
            moved = gaze_model.head_moved(every_row, every_target)
        except ValueError as exc:
            self.emit({"error": f"{exc} {seen}", "cmd": "fit"})
            return
        self.emit({"fit": model, "check": marks, "rows": len(rows), "check_rows": len(check_rows), "held_out": held_out,
                   "seen": seen, "model": choice.name, "guess": guess, "moved": moved, "screen_width_cm": screen_width_cm})

    def _keep_readings(self, kept: list[Kept], dots: DotsSeen, frames: FrameLog, camera: dict | None) -> None:
        """Keep the run's numbers, if asked to and there are any, so a failed run can be studied. Never stops the fit.
        A run that never found a face has no rows but does have its frames, which say why, so they count as numbers.
        The rows are in the order they came, and `held_back` says which were check rows. Each list from `row_ms` to
        `boxes` has one entry for each row too, None where the frame had none (the fake face has no Detail), and None for
        any number that wasn't a finite one."""
        if self._readings_file is None or not (kept or frames.entries):
            return
        size = len(gaze_model.BASE_NAMES)

        def rounded(values, places: int) -> list[float | None]:
            return [None if v is None else round(v, places) for v in map(_finite, values)]

        def each(make):
            return [None if k.detail is None else make(k.detail) for k in kept]

        readings = {
            "names": gaze_model.BASE_NAMES,
            "rows": [[round(value, 5) for value in k.row[:size]] for k in kept],
            "targets": [list(k.target) for k in kept],
            "held_back": [k.check for k in kept],
            "row_ms": [k.ms for k in kept],  # on the same clock as the frames'
            "eye_score_names": EYE_SCORES,
            "eye_scores": each(lambda d: rounded(d.eye_scores, 4)),
            "transforms": each(lambda d: rounded(d.transform, 5)),
            "face_px": each(lambda d: rounded([d.face_px], 1)[0]),
            "eye_point_landmarks": EYE_POINTS,
            "eye_points": each(lambda d: rounded(d.eye_points, 5)),
            # left, top, right and bottom, as shares of the picture, as the guide reads them
            "boxes": [None if k.box is None else rounded(k.box, 4) for k in kept],
            "camera": camera,  # the size and rate the camera really sends, before ffmpeg scales it to "size"
            "counts": dots.counts(),
            "size": list(CAMERA_SIZE),  # the frames' width and height
            "frame_names": FRAME_NAMES,
            "frames": frames.entries,
            "when": time.time(),  # so a file left by an older run, which no new one has replaced, can be told apart
        }
        try:
            _write_whole(self._readings_file, readings)
        except (OSError, ValueError) as exc:
            print(f"Couldn't keep this run's readings: {exc}", file=sys.stderr, flush=True)


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
    session = Session(emit, Path(args.readings_file) if args.readings_file else None)
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
            source = (FakeFrameSource(stop) if args.fake and not args.source_argv
                      else FrameSource(argv, *CAMERA_SIZE, session.saw_camera))
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
                seen = Observation(None, 0.0, why=f"error: {exc}")
            session.see(t, seen, brightness(frame))
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
    parser.add_argument("--readings-file", help="keep the numbers of each fit here: no picture, and each replaces the last")
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
