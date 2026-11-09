"""Gaze on this Mac: the camera, MediaPipe's face model and a calibration, in their own Python environment.
Its card also holds the check that pictures of the viewport get cleaned up (see voice/picture.py).

The unit never opens the camera to check on it. It lists the cameras through the Microphone unit's cached
listing, which only reads the names. The camera is opened by the gaze worker, and only while voice runs with
camera gaze, or while the person starts a calibration on its page or confirms a Test. Those two take the camera for
the length of the job and give it back whatever happens, and they can't run while voice is using it (see CameraClaim).
Voice that isn't using the camera can run beside them, and leaves the test's drawing alone (see VoiceService.overlay).

Calibrating happens on the dashboard's own /calibrate page, full screen, so it needs nothing from Blender. What it
learns is where the eyes point on the screen, and the voice loop works out where that is in Blender's viewport at each
command. So Blender can be moved or resized freely. The browser only shows an outline to line the head up with, then
dots: the camera stays with the worker, and the page is sent numbers, never a picture.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import statistics
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .. import gaze_model
from ..blender_socket import BridgeClient
from ..download import download
from ..gaze_worker import GUIDE_RATE
from ..jobs import Job, JobError
from ..mac_windows import page_width_cm, window_frame
from ..paths import AppPaths
from ..probes import ProbeTimedOut
from ..voice.gaze import MIN_CONFIDENCE, Area, area_from, on_viewport, reach_on_viewport, viewport_area
from ..voice.gaze_tracker import GazeError, GazeTracker, screen_of
from ..voice.leftovers import CHECK_CONFIRM as PICTURE_CHECK_CONFIRM, run_leftover_check
from ..voice.picture import read_picture_check
from .base import ActionSpec, Component, ComponentState, Health
from .microphone import Microphone, face_cameras, pick_camera

WORKER = Path(__file__).resolve().parents[1] / "gaze_worker.py"
VENV_PYTHON = "3.12"
MEDIAPIPE = "mediapipe==1.2.2"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
             "face_landmarker.task")
MODEL_SIZE = 3_758_596
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
INSTALL_CONFIRM = (
    "Install gaze tracking? This sets up its own Python environment and downloads about 340 MB "
    "(MediaPipe and its face model). No picture from your camera ever leaves this Mac."
)
TEST_CONFIRM = "Opens the camera for about 15 seconds. A ring in Blender follows where you look."

# Calibration: dots on the calibration page, as shares of its width and height from the top left.
DOT_SECONDS = 1.0  # how long each dot is looked at and its readings kept: on the run of 2026-11-09, as good as 2 s
SETTLE_SECONDS = 0.5  # before that, the eyes get this long to find a dot that has just appeared
# The dots in the order they are shown, and whether each is a check dot, held back from the fit to mark it. Sixteen
# learn, on a four by four grid that reaches from edge to edge. Eight check, on the middles of the grid's squares but
# the screen's own middle, where no learning dot is. Every third dot is a check dot, so the check dots see the same
# head as the fit does: all at the end, they marked the head's drift as well as the fit. The order is deliberate too.
# Time follows neither axis (for all the dots, the learning ones and the check ones), or steady head drift reads as
# eye movement along it, and walking a grid row by row fooled the fit on half of the made-up runs. And each dot is at
# least 0.4 of the screen from the one before. So the dots jump about, and SETTLE_SECONDS gives the eyes time to get
# there. 24 dots of 1.5 s take about as long as the 14 of 2.5 s before.
DOT_ORDER = (
    ((0.35, 0.05), False), ((0.05, 0.35), False), ((0.5, 0.8), True),
    ((0.95, 0.35), False), ((0.65, 0.95), False), ((0.8, 0.5), True),
    ((0.35, 0.35), False), ((0.65, 0.65), False), ((0.2, 0.2), True),
    ((0.65, 0.35), False), ((0.35, 0.95), False), ((0.5, 0.2), True),
    ((0.05, 0.95), False), ((0.95, 0.95), False), ((0.2, 0.8), True),
    ((0.95, 0.05), False), ((0.35, 0.65), False), ((0.8, 0.2), True),
    ((0.05, 0.65), False), ((0.95, 0.65), False), ((0.2, 0.5), True),
    ((0.65, 0.05), False), ((0.05, 0.05), False), ((0.8, 0.8), True),
)
CALIBRATION_DOTS = tuple(dot for dot, check in DOT_ORDER if not check)  # the learning dots, in the order shown
CHECK_DOTS = tuple(dot for dot, check in DOT_ORDER if check)
MAX_MEDIAN_MISS = 0.25  # a calibration typically further off than this share of the screen isn't saved
PAGE_SECONDS = 3.0  # how long a dot has to appear on the page before the page counts as gone
# What a run that got through the dots but wasn't kept leaves as its result, so the page can tell it from one that
# stopped part way (see resultText in calibrate.js).
REFUSED = {"calibrated": False, "refused": True}
PAGE_GONE = ("The calibration was stopped (you pressed Esc, or the page was closed or hidden), so the camera was turned "
             "off. Nothing was saved.")

# The head guide, before the dots: an outline on the page and a ring where the head is. These limits have never been
# measured on a real face, so the guide can always be skipped (see DotPage.skip) and it gives up after GUIDE_SECONDS.
# GUIDE_RATE (updates a second, here and in the worker) comes from the worker, which sends the readings.
GUIDE_SECONDS = 60.0  # no head in place by now: stop, and the camera goes off
HOLD_SECONDS = 1.0  # the head has to stay in place this long
GUIDE_X = (0.40, 0.60)  # where the middle of the eyes belongs, as shares of the mirrored picture
GUIDE_Y = (0.30, 0.60)
# Centimetres from the camera. Close, so that the face is big in the picture and the eyes big and clear: at 30 cm it is
# about 230 to 280 pixels wide in the 960 by 540 picture, around the 256 the face model works at.
GUIDE_DISTANCE = (25.0, 45.0)
# Where the outline sits (the middle of GUIDE_X and GUIDE_Y), and the distance the size of its head stands for.
GUIDE_TARGET = (0.5, 0.45, 30.0)
GUIDE_STALE = 0.5  # a reading older than this says nothing about where the head is now, and counts as no face
GUIDE_EDGE = 0.02  # a face whose box comes this near an edge of the picture, as a share of it, is partly out of view
GUIDE_OUT_OF_VIEW = "Move back a little, part of your face is out of view"
GUIDE_NO_FACE = "I can't see your face. Check the light and look at the screen."
GUIDE_FITS = "Hold still"
GUIDE_GAVE_UP = (
    f"Your head wasn't lined up with the outline in {GUIDE_SECONDS:g} seconds, so the camera was turned off. Check that "
    "the light is on your face and that you are in front of the camera, about 30 cm from the screen with your whole "
    "face in view, then try again. Or, when you try again, press Start anyway to skip the outline."
)

# The test ring: shown for a while, redrawn often, at the middle of the most recent readings.
TEST_SECONDS = 10.0
TEST_RATE = 10  # redraws a second
TEST_WINDOW = 0.3  # seconds of readings the ring's middle is taken from
TEST_MIN_SAMPLES = 3  # fewer good readings than this in the window and the ring is taken down


def load_calibration(path: Path) -> dict | None:
    """The saved calibration, `{"model": …, "check": {"median", "p90", "radius"}, "screen": {…}}`, or None.

    None means there is no file, it can't be read, its model was made for a different feature list, or it doesn't
    say which part of the screen it was made on (so it would give wrong positions). The file holds numbers only.
    """
    try:
        data = json.loads(Path(path).read_text())
        gaze_model.validate(data["model"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data if screen_of(data) is not None else None


def save_calibration(path: Path, calibration: dict) -> None:
    """Write the calibration whole or not at all: a temp file beside it, then moved over the old one.

    Raises OSError if it can't be written, ValueError if it holds something JSON can't (like NaN), and in both
    cases the file that was there is untouched. `state/` isn't made by AppPaths.ensure, so it is made here.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gaze-calibration-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(calibration, handle, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def camera_gaze_installed(paths: AppPaths) -> bool:
    """Whether everything camera gaze needs from this Mac is in place: its environment, the model and a calibration."""
    return ((paths.venvs / "gaze" / "bin" / "python").exists()
            and paths.gaze_model_file.is_file() and paths.gaze_calibration.is_file())


def _percent(share: object) -> str | None:
    return f"{round(share * 100)}%" if isinstance(share, (int, float)) and math.isfinite(share) else None


def out_of_view(box: tuple[float, float, float, float] | None) -> bool:
    """Whether a face's box (left, top, right and bottom, as shares of the picture) reaches within GUIDE_EDGE of an edge.
    With no box, nothing is known, and it isn't."""
    if box is None:
        return False
    left, top, right, bottom = box
    return min(left, top, 1 - right, 1 - bottom) < GUIDE_EDGE


def guide_advice(x: float, y: float, distance: float, box: tuple[float, float, float, float] | None = None
                 ) -> tuple[str, bool]:
    """What to tell a head at the middle of the eyes (x, y) and `distance` cm away, and whether that already fits.

    `x` is mirrored, as the page is: too large means the head shows too far right, so the advice is to move left. One
    piece of advice at a time, for the largest miss. Misses are compared as a share of how much room their limit allows,
    so that centimetres and shares of a picture can be set side by side. A tie goes to the first in this order.

    `box` is the face's box in the picture. A face that reaches the edge of the picture doesn't fit either, but that is
    only said once the rest fits: a head too far to one side can touch the edge, and moving over is what it needs.
    """
    room = [(high - low) / 2 for low, high in (GUIDE_X, GUIDE_Y, GUIDE_DISTANCE)]
    misses = [
        ("Move left", (x - GUIDE_X[1]) / room[0]),
        ("Move right", (GUIDE_X[0] - x) / room[0]),
        ("Move up", (y - GUIDE_Y[1]) / room[1]),  # a head low in the picture
        ("Move down", (GUIDE_Y[0] - y) / room[1]),
        ("Come closer", (distance - GUIDE_DISTANCE[1]) / room[2]),
        ("Move back", (GUIDE_DISTANCE[0] - distance) / room[2]),
    ]
    advice, miss = max(misses, key=lambda option: option[1])
    if miss > 0:
        return advice, False
    return (GUIDE_OUT_OF_VIEW, False) if out_of_view(box) else (GUIDE_FITS, True)


class DotPage:
    """The calibration page, as the job sees it: it is sent each dot and says when the dot is up.

    The page is a browser tab the person can close at any moment, and the job has the camera open. So the job waits
    for each dot and ends as soon as the page has gone, or stops answering. During the head guide there is no dot to
    wait for, so the page says it is still there a few times a second instead.
    """

    def __init__(self, timeout: float = PAGE_SECONDS, clock=time.monotonic):
        self.timeout = timeout
        self.clock = clock
        self._changed = threading.Condition()
        self._job: str | None = None
        self._shown = 0
        self._gone = False
        self._skipped = False  # the person pressed Start anyway
        self._heard: float | None = None  # when the page last said it was there, during the guide

    def open(self, job_id: str) -> None:
        with self._changed:
            self._job, self._shown, self._gone = job_id, 0, False
            self._skipped, self._heard = False, None

    def close(self) -> None:
        with self._changed:
            self._job = None

    def shown(self, job_id: object, number: object) -> None:
        """The page has dot `number` up."""
        if not isinstance(number, int) or isinstance(number, bool):
            return
        with self._changed:
            if job_id == self._job:
                self._shown = max(self._shown, number)
                self._changed.notify_all()

    def left(self, job_id: object) -> None:
        """The page has been closed or hidden."""
        with self._changed:
            if job_id == self._job:
                self._gone = True
                self._changed.notify_all()

    def alive(self, job_id: object) -> None:
        """The page is still there, during the guide. It says so a few times a second."""
        with self._changed:
            if job_id == self._job:
                self._heard = self.clock()

    def skip(self, job_id: object) -> None:
        """The person pressed Start anyway."""
        with self._changed:
            if job_id == self._job:
                self._skipped = True
                self._changed.notify_all()

    def guide(self, job: Job, data: dict) -> bool:
        """Show the guide a reading. True if the person has pressed Start anyway.

        Raises if the page has gone, or has not said it is there for `timeout`: a closed or hidden tab sends nothing, and
        the camera must not stay on for it. The first reading gives the page the whole timeout to answer it.
        """
        now = self.clock()
        job.publish("gaze.guide", data)
        with self._changed:
            if self._heard is None:
                self._heard = now
            if self._gone or now - self._heard > self.timeout:
                raise JobError(PAGE_GONE)
            return self._skipped

    def show(self, job: Job, dot: tuple[float, float], number: int, total: int) -> None:
        """Put a dot on the page and wait until it is up, or raise if the page has gone."""
        job.publish("gaze.dot", {"uv": list(dot), "number": number, "total": total})
        with self._changed:
            up = self._changed.wait_for(lambda: self._gone or self._shown >= number, self.timeout)
        if not up:
            raise JobError(PAGE_GONE)
        self.check()

    def check(self) -> None:
        """Raise if the page has gone."""
        with self._changed:
            if self._gone:
                raise JobError(PAGE_GONE)


class Gaze(Component):
    id = "gaze"
    name = "Gaze"
    role = "Sees where you're looking, on this Mac"

    def __init__(self, ctx):
        super().__init__(ctx)
        # Tests swap these in: a fake download, the bridge to Blender, the gaze worker, what macOS says about windows and
        # screens, and the passing of time.
        self.opener = urllib.request.urlopen
        self.bridge_factory = BridgeClient
        self.tracker_factory = self._make_tracker
        self.window_frame = window_frame
        self.page_width_cm = page_width_cm
        self.sleep = time.sleep
        self.clock = time.time
        self.page = DotPage(clock=lambda: self.clock())  # looked up each time, so a swapped clock is the page's too

    @property
    def venv(self) -> Path:
        return self.paths.venvs / "gaze"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def model_file(self) -> Path:
        return self.paths.gaze_model_file

    def _model_there(self) -> bool:
        """Cheap enough to do on every refresh: the right size. Installing checks the contents too."""
        try:
            return self.model_file.stat().st_size == MODEL_SIZE
        except OSError:
            return False

    def _model_checks_out(self, path: Path) -> bool:
        try:
            data = path.read_bytes()
        except OSError:
            return False
        return len(data) == MODEL_SIZE and hashlib.sha256(data).hexdigest() == MODEL_SHA256

    def _install_action(self) -> ActionSpec:
        return ActionSpec("install", "Install gaze", confirm=INSTALL_CONFIRM)

    @staticmethod
    def _calibrate_action(again: bool = False) -> ActionSpec:
        return ActionSpec("calibrate", "Calibrate again" if again else "Calibrate", href="/calibrate")

    def check(self) -> ComponentState:
        return self._with_pictures(self._check_camera())

    def _with_pictures(self, state: ComponentState) -> ComponentState:
        """Add what pictures need to whatever state the camera side is in: they go through Claude Code, not the camera."""
        if not self.runner.which("claude"):
            return state
        try:
            parts = self.probe(["claude", "--version"], ttl=600).stdout.split()
        except ProbeTimedOut:  # a Claude Code that is slow to answer says nothing about the camera side of this card
            return state
        installed = parts[0] if parts else None
        check = read_picture_check(self.paths.gaze_picture_check)
        if check is None:
            where = "Not checked yet"
        elif check["claude"] == installed:
            where = f"Cleaned up, checked with Claude Code {installed}"
        else:
            now = f"Claude Code is {installed} now" if installed else "Claude Code didn't say which version it is"
            where = f"{now}, and the check was for {check['claude']}. Check again"
        action = ActionSpec("check_pictures", "Check pictures get cleaned up", confirm=PICTURE_CHECK_CONFIRM, uses_plan=True)
        return dataclasses.replace(state, details=(*state.details, ("Pictures", where)), actions=(*state.actions, action))

    def _check_camera(self) -> ComponentState:
        if not self.runner.which("uv"):
            return self.state(Health.ERROR, "uv is needed to set up gaze tracking. Install uv (astral.sh/uv), then refresh.")
        microphone = Microphone(self.ctx)  # its listing is cached, and shared with the Microphone unit
        have_ffmpeg = bool(self.runner.which("ffmpeg"))
        cameras = face_cameras(microphone.cameras()) if have_ffmpeg else []
        details = [("Cameras", " | ".join(name for _, name in cameras))] if cameras else []
        if not self.python.exists() or not self.probe([str(self.python), "-c", "import mediapipe"], timeout=20, ttl=60).ok:
            return self.state(
                Health.MISSING,
                "Not installed. Gaze tracking runs on this Mac with your camera. No picture of you is ever saved or sent.",
                details,
                [self._install_action()],
            )
        if not self._model_there():
            return self.state(Health.INSTALLED, "Gaze is installed, but its face model isn't downloaded yet.", details,
                              [self._install_action()])
        if not have_ffmpeg:
            return self.state(Health.ERROR, "ffmpeg is needed to see the camera. Install it from the Microphone unit.", details)
        if not cameras:
            return self.state(Health.ERROR, "No camera found. Plug one in, then pick it in Routing.", details)
        picked = pick_camera(cameras, self.settings.camera_device)
        if picked is None:
            if self.settings.camera_device:
                return self.state(Health.ERROR, f"The camera “{self.settings.camera_device}” isn't connected. "
                                                "Pick another camera in Routing.", details)
            return self.state(Health.ERROR, "No built-in camera found, and gaze won't use a phone's on its own. "
                                            "Plug in a webcam and pick a camera in Routing.", details)
        auto = not self.settings.camera_device
        details.insert(0, ("Using", f"{picked[1]} (auto)" if auto else picked[1]))
        calibration = load_calibration(self.paths.gaze_calibration)
        if calibration is None:
            if self.paths.gaze_calibration.is_file():
                return self.state(Health.INSTALLED, "Your saved calibration is out of date. Calibrate gaze again.", details,
                                  [self._calibrate_action()])
            return self.state(Health.INSTALLED, "Gaze is installed. Calibration comes next: it learns how your eyes "
                                                "map to your screen.", details, [self._calibrate_action()])
        actions = [self._calibrate_action(again=True), ActionSpec("test", "Test", confirm=TEST_CONFIRM)]
        check = calibration.get("check")
        median, p90 = (_percent(check.get(key)) for key in ("median", "p90")) if isinstance(check, dict) else (None, None)
        if median and p90:
            details.append(("Accuracy", f"Typically off by {median} of the screen · 9 in 10 within {p90}"))
            return self.state(Health.READY, f"{picked[1]} · typically off by {median} of the screen", details, actions)
        return self.state(Health.READY, f"{picked[1]} · calibrated", details, actions)

    def action_install(self, job: Job) -> dict:
        if not self.runner.which("uv"):
            raise JobError("uv isn't installed. Install it from astral.sh/uv, then try again.")
        steps = []
        if not self.python.exists():
            steps.append(("Creating a Python 3.12 environment…",
                          ["uv", "venv", "--python", VENV_PYTHON, str(self.venv)], 3600))
        steps.append((f"Installing {MEDIAPIPE}…",
                      ["uv", "pip", "install", "--python", str(self.python), MEDIAPIPE], 3600))
        for message, argv, timeout in steps:
            job.log(message)
            code = self.runner.stream(argv, job.log, timeout=timeout)
            if code != 0:
                raise JobError(f"{message.rstrip('…')} failed (exit {code}). See the log above.")
        if self._model_checks_out(self.model_file):
            job.log("The face model is already downloaded.")
        else:
            self.model_file.unlink(missing_ok=True)  # whatever is there is wrong or half-finished, and is not kept
            self._download_model(job)
        return {"installed": True}

    def _download_model(self, job: Job) -> None:
        """Fetch the face model beside its final place and move it in only once its size and SHA-256 are right."""
        fetched = self.model_file.with_name(self.model_file.name + ".download")
        try:
            download(MODEL_URL, fetched, job, opener=self.opener)
            data = fetched.read_bytes()
            if len(data) != MODEL_SIZE:
                raise JobError(f"The face model came down with the wrong size ({len(data)} bytes, not {MODEL_SIZE}), "
                               "so it was deleted. Try Install again.")
            if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
                raise JobError("The face model didn't match its checksum, so it was deleted and not used. "
                               "Try Install again.")
            fetched.replace(self.model_file)
        finally:
            fetched.unlink(missing_ok=True)
        job.log("The face model checks out.")

    def action_check_pictures(self, job: Job) -> dict:
        """Send a random picture through the describer and search this Mac for a copy. A pass turns pictures on."""
        return run_leftover_check(job, self.paths, runner=self.runner)

    # --- calibrating and testing ---------------------------------------------------------------------
    def _make_tracker(self, camera: str, calibration: dict | None) -> GazeTracker:
        """The real worker. Only a calibrating run, the one with no calibration yet, keeps its numbers: voice never does."""
        keep = ("--readings-file", str(self.paths.gaze_readings)) if calibration is None else ()
        return GazeTracker(self.python, WORKER, camera, calibration, model_file=self.model_file, extra_args=keep)

    @contextlib.contextmanager
    def _camera_for(self, what: str):
        """Hold the camera for the length of a job, or refuse plainly if voice (or another job) has it."""
        holder = self.ctx.camera.take("gaze")
        if holder == "voice":
            raise JobError(f"Voice is on and using the camera. Turn voice off, then {what}.")
        if holder:
            raise JobError("Gaze is already using the camera. Wait for it to finish.")
        try:
            yield
        finally:
            self.ctx.camera.release("gaze")

    def _camera_index(self) -> str:
        """The number of the camera to use now, from a fresh listing: numbers move when something is plugged in."""
        if not (self.python.exists() and self._model_there()):
            raise JobError("Install gaze first. It isn't fully set up yet.")
        if not self.runner.which("ffmpeg"):
            raise JobError("ffmpeg is needed to see the camera. Install it from the Microphone unit.")
        picked = pick_camera(Microphone(self.ctx).cameras(fresh=True), self.settings.camera_device)
        if picked is None:
            if self.settings.camera_device:
                raise JobError(f"The camera “{self.settings.camera_device}” isn't connected. Pick another camera in Routing.")
            raise JobError("No built-in camera found. Plug in a webcam and pick it in Routing.")
        return picked[0]

    @staticmethod
    def _ask_blender(client, request: dict) -> dict:
        try:
            return client.request(request)
        except (OSError, ValueError) as exc:
            raise JobError("Blender isn't answering. Open Blender with the GazeVoice Bridge installed and a 3D viewport "
                           "showing, then try again.") from exc

    def _viewport(self, client, screen: Area) -> Area:
        """Where Blender's 3D viewport is on `screen`, or a JobError that says what to open, update or move."""
        reply = self._ask_blender(client, {"cmd": "gaze_view"})
        if reply.get("error") == "no 3D viewport":
            raise JobError("Blender has no 3D viewport showing. Show one in Blender's first window, then try again.")
        if reply.get("error") == "unknown command" or (reply.get("ok") and "pid" not in reply):
            raise JobError("The GazeVoice Bridge in Blender is out of date. Update it on the Bridge unit, restart "
                           "Blender, then try again.")
        if not reply.get("ok"):
            raise JobError(f"Blender couldn't say where its 3D viewport is ({reply.get('error') or 'no reason given'}).")
        view = viewport_area(reply, self.window_frame)
        if view is None:
            raise JobError("macOS couldn't find Blender's window. Bring Blender to the front, then try again.")
        if not screen.holds(view):
            raise JobError("Blender is on a different screen from the one you calibrated on. Move Blender back, or "
                           "calibrate on that screen.")
        return view

    def _poll(self, client) -> None:
        """Tell the add-on the app is here. It draws gaze dots and rings only while it is being polled (the way voice
        polls it), so that nothing is left on the viewport with nobody to take it down. Voice may be on as well, if it
        isn't using the camera, so this acknowledges nothing: key presses stay queued for voice to read."""
        self._ask_blender(client, {"cmd": "poll", "ack": 0})

    def _show(self, client, dot: list[float] | None = None, ring: list[float] | None = None) -> None:
        """Ask Blender to show a dot and/or a ring on its viewport; with neither, to clear it."""
        reply = self._ask_blender(client, {"cmd": "gaze_show", "dot": dot, "ring": ring, "marks": []})
        if not reply.get("ok"):
            raise JobError(f"Blender couldn't draw on its viewport ({reply.get('error') or 'no reason given'}). "
                           "Update the GazeVoice Bridge, restart Blender, then try again.")

    def _draw(self, client, dot: list[float] | None = None, ring: list[float] | None = None) -> None:
        self._poll(client)
        self._show(client, dot, ring)

    def _let_go(self, tracker, client) -> None:
        """Camera off first, then the drawing off the viewport. Neither can stop the other, or hide the real error."""
        try:
            if tracker is not None:
                tracker.stop()
        finally:
            try:
                self._show(client)
            except JobError:
                pass  # Blender is gone or busy; voice clears the viewport when it next starts
            finally:
                client.close()

    def action_calibrate(self, job: Job) -> dict:
        """Dots on the calibration page: 16 to learn from, with 8 mixed in to see how well it learned. Saves the result."""
        screen = area_from(job.params.get("area"))
        if screen is None:
            raise JobError("Calibrate on its own page: press Calibrate on the Gaze unit.")
        self.page.open(job.id)
        try:
            with self._camera_for("calibrate"):
                camera = self._camera_index()
                tracker = None
                try:
                    tracker = self.tracker_factory(camera, None)  # a fresh worker, so an earlier attempt can't leak rows in
                    job.log("Opening the camera… (the first time, macOS may ask to allow it)")
                    try:
                        tracker.start()
                        job.log("Line your head up with the outline on the screen.")
                        self._line_up_the_head(job, tracker)
                        job.log("Look at each dot as it appears, and keep looking until it moves.")
                        reply = self._look_at_the_dots(job, tracker, self._screen_width_cm(screen))
                    except GazeError as exc:
                        raise JobError(str(exc)) from exc
                    self._report(job, reply)
                    calibration = self._accept(reply, screen)
                finally:
                    if tracker is not None:
                        tracker.stop()
        finally:
            self.page.close()
        check = calibration["check"]
        job.log(f"Saved. Typically off by {_percent(check['median'])} of the screen; 9 in 10 within {_percent(check['p90'])}.")
        return {"calibrated": True, "median": check["median"], "p90": check["p90"]}

    def _head_on_the_page(self, tracker, now: float, began: float) -> dict | None:
        """What the guide shows for the worker's latest reading, or None when it has had no time to say anything yet."""
        place = tracker.place
        if place is None and now - began <= GUIDE_STALE:
            return None
        if place is None or now - place.at > GUIDE_STALE:  # an old reading is no reading
            tracker.guide(True)  # no news may mean the worker has gone: asking again says so, with the reason
        if place is None or not place.found or now - place.at > GUIDE_STALE:
            return {"found": False, "x": None, "y": None, "scale": None, "fits": False, "advice": GUIDE_NO_FACE}
        x = 1 - place.x  # mirrored, so the page acts like a mirror
        advice, fits = guide_advice(x, place.y, place.distance, place.box)  # the box's edges are the same either way
        return {"found": True, "x": round(x, 4), "y": round(place.y, 4), "scale": round(GUIDE_TARGET[2] / place.distance, 3),
                "fits": fits, "advice": advice}

    def _line_up_the_head(self, job: Job, tracker) -> None:
        """Show the outline and a ring where the head is, until the head has been in place for HOLD_SECONDS.

        The person can go on at any time with Start anyway. A page that has gone ends the job at once, and so does a
        head that wouldn't fit in GUIDE_SECONDS, because these limits were never measured on a real face. The worker
        stops looking for the head before the first dot.
        """
        tracker.guide(True)
        began = self.clock()
        in_place_since = None
        while True:
            now = self.clock()
            shown = self._head_on_the_page(tracker, now, began)
            if shown is None:
                self.page.check()
            elif self.page.guide(job, shown):
                job.log("Going on without the outline.")
                break
            if shown is not None and shown["fits"]:
                in_place_since = now if in_place_since is None else in_place_since
                if now - in_place_since >= HOLD_SECONDS:
                    job.log("Your head is in place.")
                    break
            else:
                in_place_since = None
            if now - began >= GUIDE_SECONDS:
                raise JobError(GUIDE_GAVE_UP)
            self.sleep(1 / GUIDE_RATE)
        tracker.guide(False)

    def _screen_width_cm(self, screen: Area) -> float | None:
        """How wide the calibration page is, in centimetres, from the size macOS gives the screen it is on. None if macOS
        can't say or what it says can't be right (see mac_windows.page_width_cm), and then the fit doesn't allow for
        turning the head, as before."""
        return self.page_width_cm(screen.left, screen.top, screen.width, screen.height)

    def _look_at_the_dots(self, job: Job, tracker, screen_width_cm: float | None) -> dict:
        for number, (dot, check) in enumerate(DOT_ORDER, 1):
            job.log(f"Dot {number} of {len(DOT_ORDER)}")
            self.page.show(job, dot, number, len(DOT_ORDER))
            self.sleep(SETTLE_SECONDS)
            tracker.collect(dot, check=check)
            self.sleep(DOT_SECONDS)
            tracker.stop_collect()
            self.page.check()
        return tracker.fit(screen_width_cm=screen_width_cm)

    @staticmethod
    def _report(job: Job, reply: dict) -> None:
        """What the run measured, in the log, whether or not it goes on to be kept."""
        if reply.get("seen"):
            job.log(str(reply["seen"]))
        check = reply.get("check") if isinstance(reply.get("check"), dict) else {}
        typical, guess = _percent(check.get("median")), _percent(reply.get("guess"))
        figure = "No accuracy figure."
        if typical:
            versus = f"; always guessing the middle would be {guess}" if guess else ""
            figure = f"Typically off by {typical} of the screen{versus}."
        model = f" Model: {reply['model']}." if reply.get("model") else ""
        turn = ""
        if "screen_width_cm" in reply:
            width = reply["screen_width_cm"]
            turn = (f" Turning your head is allowed for (the screen is {width:.1f} cm wide)."
                    if isinstance(width, (int, float)) and math.isfinite(width) and width > 0
                    else " The screen's size couldn't be read, or the page was too narrow or too wide to trust, "
                         "so turning your head isn't allowed for.")
        job.log(f"Readings kept: {reply.get('rows')} to learn from, {reply.get('check_rows')} to check. "
                f"{figure}{model}{turn}")
        moved = reply.get("moved") if isinstance(reply.get("moved"), dict) else {}
        cm, degrees = moved.get("cm"), moved.get("degrees")
        if all(isinstance(x, (int, float)) and math.isfinite(x) for x in (cm, degrees)):
            turned = f"{round(degrees)} degree{'' if round(degrees) == 1 else 's'}"
            job.log(f"Your head moved about {cm:.1f} cm and turned about {turned} during the run.")

    def _not_saved(self, why: str) -> JobError:
        """A refusal to keep a calibration. Whatever was saved before is untouched, and the message says so."""
        in_use = load_calibration(self.paths.gaze_calibration) is not None  # a file that is refused isn't in use
        earlier = " Your earlier calibration is still in use." if in_use else ""
        return JobError(f"{why} It wasn't saved.{earlier}", result=dict(REFUSED))

    def _accept(self, reply: dict, screen: Area) -> dict:
        """Turn the worker's fit into the saved calibration, or refuse it with the reason in plain words."""
        if reply.get("held_out") is not True:  # its figure would be measured on dots it was fitted to, and flatter it
            raise self._not_saved("The check dots weren't seen, so there is no honest figure for how accurate this is. "
                                  "Try again, and keep your face in view of the camera.")
        check = reply.get("check")
        check = check if isinstance(check, dict) else {}
        median = check.get("median")
        if not (isinstance(median, (int, float)) and math.isfinite(median)):
            raise self._not_saved("How accurate this calibration is couldn't be measured. Try again.")
        if median > MAX_MEDIAN_MISS:
            raise self._not_saved(
                f"That calibration was typically off by {_percent(median)} of the screen, and anything past "
                f"{_percent(MAX_MEDIAN_MISS)} is too loose to work with. Sit about 30 cm from the screen with your whole "
                "face in view, keep your head level and fairly still, and look right at each dot until it moves.")
        try:
            gaze_model.validate(reply.get("fit"))
        except ValueError as exc:
            raise self._not_saved(f"The calibration the worker made can't be used ({exc}).") from exc
        calibration = {
            "model": reply["fit"],
            "check": {key: check.get(key) for key in ("median", "p90", "radius")},
            "screen": dataclasses.asdict(screen),
            "when": self.clock(),
        }
        try:
            save_calibration(self.paths.gaze_calibration, calibration)
        except (OSError, ValueError) as exc:
            raise self._not_saved(f"Couldn't write the calibration file ({exc}).") from exc
        return calibration

    def action_test(self, job: Job) -> dict:
        """A ring in Blender's viewport that follows where the camera says you are looking, for a few seconds."""
        with self._camera_for("test it"):
            calibration = load_calibration(self.paths.gaze_calibration)
            if calibration is None:
                raise JobError("There is no usable calibration yet. Calibrate gaze first.")
            camera = self._camera_index()
            client = self.bridge_factory()
            tracker = None
            try:
                screen = screen_of(calibration)
                view = self._viewport(client, screen)  # Blender first: no point opening the camera without it
                tracker = self.tracker_factory(camera, calibration)
                job.log("Opening the camera… (the first time, macOS may ask to allow it)")
                try:
                    tracker.start()
                except GazeError as exc:
                    raise JobError(str(exc)) from exc
                job.log(f"A ring in Blender follows where you look, for {TEST_SECONDS:g} seconds.")
                followed = self._follow_with_the_ring(client, tracker, screen, view)
            finally:
                self._let_go(tracker, client)
        return {"tested": True, "seconds": TEST_SECONDS, "followed": followed}

    def _follow_with_the_ring(self, client, tracker, screen: Area, view: Area) -> float:
        """Redraw the ring TEST_RATE times a second at the middle of the last TEST_WINDOW of readings.

        Returns the share of redraws that found a face to put it on; raises if that was none of them.
        """
        reach_u, reach_v = (min(1.0, r) for r in reach_on_viewport(tracker.radius, screen, view))
        end = self.clock() + TEST_SECONDS
        drawn = found = 0
        while self.clock() < end:
            now = self.clock()
            good = [s for s in tracker.track.between(now - TEST_WINDOW, now) if s.confidence >= MIN_CONFIDENCE]
            ring = None
            if len(good) >= TEST_MIN_SAMPLES:
                u, v = on_viewport(statistics.median(s.u for s in good), statistics.median(s.v for s in good), screen, view)
                ring = [min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), reach_u, reach_v]  # the bridge takes 0 to 1 only
            self._draw(client, ring=ring)
            drawn += 1
            found += ring is not None
            self.sleep(1 / TEST_RATE)
        if not found:
            raise JobError("The camera never found your face. Check that the light is on your face and that you are in "
                           "front of the camera, then test again.")
        return found / drawn
