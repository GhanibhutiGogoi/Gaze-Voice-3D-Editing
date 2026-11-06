"""Gaze on this Mac: the camera, MediaPipe's face model and a calibration, in their own Python environment.

The unit never opens the camera to check on it. It lists the cameras through the Microphone unit's cached
listing, which only reads the names. The camera is opened by the gaze worker, and only while voice runs, or
while the person presses Calibrate or Test and has confirmed it. Those two take the camera for the length of
the job and give it back whatever happens, and they can't run while voice is on (see CameraClaim).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import statistics
import tempfile
import time
import urllib.request
from pathlib import Path

from .. import gaze_model
from ..blender_socket import BridgeClient
from ..download import download
from ..jobs import Job, JobError
from ..paths import AppPaths
from ..voice.gaze import MIN_CONFIDENCE, viewport_size
from ..voice.gaze_tracker import GazeError, GazeTracker, view_of
from .base import ActionSpec, Component, ComponentState, Health
from .microphone import Microphone, face_cameras, pick_camera

WORKER = Path(__file__).resolve().parents[1] / "gaze_worker.py"
VENV_PYTHON = "3.12"
MEDIAPIPE = "mediapipe==1.2.2"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/"
             "face_landmarker.task")
MODEL_SIZE = 3_758_596
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
INSTALL_CONFIRM = (
    "Install gaze tracking? This sets up its own Python environment and downloads about 340 MB "
    "(MediaPipe and its face model). Nothing you look at ever leaves this Mac."
)
CALIBRATE_CONFIRM = "Opens the camera for about 40 seconds. Look at each dot in Blender."
TEST_CONFIRM = "Opens the camera for about 15 seconds. A ring in Blender follows where you look."

# Calibration: dots on Blender's viewport, as shares of its width and height from the top left.
DOT_SECONDS = 2.0  # how long each dot is looked at and its readings kept
SETTLE_SECONDS = 0.5  # before that, the eyes get this long to find a dot that has just appeared
CALIBRATION_DOTS = (  # a three by three grid, pulled in from the edges and walked in a snake so the eyes never leap
    (0.1, 0.1), (0.5, 0.1), (0.9, 0.1),
    (0.9, 0.5), (0.5, 0.5), (0.1, 0.5),
    (0.1, 0.9), (0.5, 0.9), (0.9, 0.9),
)
CHECK_DOTS = ((0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7), (0.5, 0.2))  # in between: the fit never saw these
MAX_MEDIAN_MISS = 0.25  # a calibration typically further off than this share of the viewport isn't saved

# The test ring: shown for a while, redrawn often, at the middle of the most recent readings.
TEST_SECONDS = 10.0
TEST_RATE = 10  # redraws a second
TEST_WINDOW = 0.3  # seconds of readings the ring's middle is taken from
TEST_MIN_SAMPLES = 3  # fewer good readings than this in the window and the ring is taken down


def load_calibration(path: Path) -> dict | None:
    """The saved calibration, `{"model": …, "check": {"median", "p90", "radius"}}`, or None.

    None means there is no file, it can't be read, or its model was made for a different feature list (so
    it would give wrong positions). The file holds numbers only.
    """
    try:
        data = json.loads(Path(path).read_text())
        gaze_model.validate(data["model"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return data


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


class Gaze(Component):
    id = "gaze"
    name = "Gaze"
    role = "Sees where you're looking, on this Mac"

    def __init__(self, ctx):
        super().__init__(ctx)
        # Tests swap these in: a fake download, the bridge to Blender, the gaze worker, and the passing of time.
        self.opener = urllib.request.urlopen
        self.bridge_factory = BridgeClient
        self.tracker_factory = self._make_tracker
        self.sleep = time.sleep
        self.clock = time.time

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
        return ActionSpec("calibrate", "Calibrate again" if again else "Calibrate", confirm=CALIBRATE_CONFIRM)

    def _live_view(self) -> tuple[int, int] | None:
        """The size of Blender's 3D viewport now, or None when Blender can't say (closed, no viewport, old add-on)."""
        client = self.bridge_factory()
        try:
            reply = client.request({"cmd": "gaze_view"}, timeout=1.0)
        except (OSError, ValueError):
            return None
        finally:
            client.close()
        return viewport_size(reply) if reply.get("ok") else None

    def check(self) -> ComponentState:
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
                                                "map to the Blender viewport.", details, [self._calibrate_action()])
        saved_view = view_of(calibration)
        if saved_view is not None and self._live_view() not in (None, saved_view):
            return self.state(Health.INSTALLED, "The Blender viewport is a different size from when you calibrated, so "
                                                "gaze would point at the wrong place. Calibrate gaze again.", details,
                              [self._calibrate_action()])
        actions = [self._calibrate_action(again=True), ActionSpec("test", "Test", confirm=TEST_CONFIRM)]
        check = calibration.get("check")
        median, p90 = (_percent(check.get(key)) for key in ("median", "p90")) if isinstance(check, dict) else (None, None)
        if median and p90:
            details.append(("Accuracy", f"Typically off by {median} of the viewport · 9 in 10 within {p90}"))
            return self.state(Health.READY, f"{picked[1]} · typically off by {median} of the viewport", details, actions)
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

    # --- calibrating and testing ---------------------------------------------------------------------
    def _make_tracker(self, camera: str, calibration: dict | None) -> GazeTracker:
        return GazeTracker(self.python, WORKER, camera, calibration, model_file=self.model_file)

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

    def _viewport(self, client) -> tuple[int, int]:
        """The size of Blender's 3D viewport, or a JobError that says what to open or update."""
        reply = self._ask_blender(client, {"cmd": "gaze_view"})
        if reply.get("ok"):
            size = viewport_size(reply)
            if size is not None:
                return size
        elif reply.get("error") == "no 3D viewport":
            raise JobError("Blender has no 3D viewport showing. Show one in Blender's first window, then try again.")
        elif reply.get("error") == "unknown command":
            raise JobError("The GazeVoice Bridge in Blender is out of date. Update it on the Bridge unit, restart "
                           "Blender, then try again.")
        raise JobError(f"Blender couldn't say how big its 3D viewport is ({reply.get('error') or 'no reason given'}).")

    def _poll(self, client) -> None:
        """Tell the add-on the app is here. It draws gaze dots and rings only while it is being polled (the way voice
        polls it), so that nothing is left on the viewport with nobody to take it down. Voice is off now, so we poll."""
        self._ask_blender(client, {"cmd": "poll"})

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
        """Dots in Blender's viewport: nine to learn from, then five to see how well it learned. Saves the result."""
        with self._camera_for("calibrate"):
            camera = self._camera_index()
            client = self.bridge_factory()
            tracker = None
            try:
                view = self._viewport(client)  # Blender first: no point opening the camera without it
                tracker = self.tracker_factory(camera, None)  # a fresh worker, so an earlier attempt can't leak rows in
                job.log("Opening the camera… (the first time, macOS may ask to allow it)")
                try:
                    tracker.start()
                    job.log("Look at each dot in Blender as it appears, and keep looking until it moves.")
                    reply = self._look_at_the_dots(job, client, tracker)
                except GazeError as exc:
                    raise JobError(str(exc)) from exc
                if self._viewport(client) != view:
                    raise self._not_saved("The Blender viewport was resized while you calibrated. Leave it alone and try again.")
                calibration = self._accept(reply, view)
            finally:
                self._let_go(tracker, client)
        check = calibration["check"]
        job.log(f"Saved. Typically off by {_percent(check['median'])} of the viewport; 9 in 10 within {_percent(check['p90'])}.")
        return {"calibrated": True, "median": check["median"], "p90": check["p90"]}

    def _look_at_the_dots(self, job: Job, client, tracker) -> dict:
        dots = [(dot, False) for dot in CALIBRATION_DOTS] + [(dot, True) for dot in CHECK_DOTS]
        for number, (dot, check) in enumerate(dots, 1):
            if check and number == len(CALIBRATION_DOTS) + 1:
                job.log(f"Now {len(CHECK_DOTS)} more, to see how well it learned.")
            job.log(f"Dot {number} of {len(dots)}")
            self._draw(client, dot=list(dot))
            self.sleep(SETTLE_SECONDS)
            self._poll(client)  # so that polls come at least every couple of seconds, well inside the add-on's three
            tracker.collect(dot, check=check)
            self.sleep(DOT_SECONDS)
            tracker.stop_collect()
        return tracker.fit()

    def _not_saved(self, why: str) -> JobError:
        """A refusal to keep a calibration. Whatever was saved before is untouched, and the message says so."""
        earlier = " Your earlier calibration is still in use." if self.paths.gaze_calibration.is_file() else ""
        return JobError(f"{why} It wasn't saved.{earlier}")

    def _accept(self, reply: dict, view: tuple[int, int]) -> dict:
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
                f"That calibration was typically off by {_percent(median)} of the viewport, and anything past "
                f"{_percent(MAX_MEDIAN_MISS)} is too loose to work with. Sit about an arm's length from the screen, keep "
                "your head fairly still, and look right at each dot until it moves.")
        try:
            gaze_model.validate(reply.get("fit"))
        except ValueError as exc:
            raise self._not_saved(f"The calibration the worker made can't be used ({exc}).") from exc
        calibration = {
            "model": reply["fit"],
            "check": {key: check.get(key) for key in ("median", "p90", "radius")},
            "view": {"width": view[0], "height": view[1]},
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
                saved_view = view_of(calibration)
                if saved_view is not None and self._viewport(client) != saved_view:
                    raise JobError("The Blender viewport is a different size from when you calibrated, so the ring would "
                                   "be in the wrong place. Calibrate gaze again.")
                tracker = self.tracker_factory(camera, calibration)
                job.log("Opening the camera… (the first time, macOS may ask to allow it)")
                try:
                    tracker.start()
                except GazeError as exc:
                    raise JobError(str(exc)) from exc
                job.log(f"A ring in Blender follows where you look, for {TEST_SECONDS:g} seconds.")
                followed = self._follow_with_the_ring(client, tracker)
            finally:
                self._let_go(tracker, client)
        return {"tested": True, "seconds": TEST_SECONDS, "followed": followed}

    def _follow_with_the_ring(self, client, tracker) -> float:
        """Redraw the ring TEST_RATE times a second at the middle of the last TEST_WINDOW of readings.

        Returns the share of redraws that found a face to put it on; raises if that was none of them.
        """
        reach_u, reach_v = (min(1.0, r) for r in tracker.radius)
        end = self.clock() + TEST_SECONDS
        drawn = found = 0
        while self.clock() < end:
            now = self.clock()
            good = [s for s in tracker.track.between(now - TEST_WINDOW, now) if s.confidence >= MIN_CONFIDENCE]
            ring = None
            if len(good) >= TEST_MIN_SAMPLES:
                u, v = statistics.median(s.u for s in good), statistics.median(s.v for s in good)
                ring = [min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), reach_u, reach_v]  # the bridge takes 0 to 1 only
            self._draw(client, ring=ring)
            drawn += 1
            found += ring is not None
            self.sleep(1 / TEST_RATE)
        if not found:
            raise JobError("The camera never found your face. Check that the light is on your face and that you are in "
                           "front of the camera, then test again.")
        return found / drawn
