"""Gaze on this Mac: the camera, MediaPipe's face model and a calibration, in their own Python environment.

The unit never opens the camera to check on it. It lists the cameras through the Microphone unit's cached
listing, which only reads the names. The camera is opened by the gaze worker, and only while voice runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from pathlib import Path

from .. import gaze_model
from ..download import download
from ..jobs import Job, JobError
from ..paths import AppPaths
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
        self.opener = urllib.request.urlopen  # tests swap in a fake download

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
                return self.state(Health.INSTALLED, "Your saved calibration is out of date. Calibrate gaze again.", details)
            return self.state(Health.INSTALLED, "Gaze is installed. Calibration comes next: it learns how your eyes "
                                                "map to the Blender viewport.", details)
        check = calibration.get("check")
        median, p90 = (_percent(check.get(key)) for key in ("median", "p90")) if isinstance(check, dict) else (None, None)
        if median and p90:
            details.append(("Accuracy", f"Typically off by {median} of the viewport · 9 in 10 within {p90}"))
            return self.state(Health.READY, f"{picked[1]} · typically off by {median} of the viewport", details)
        return self.state(Health.READY, f"{picked[1]} · calibrated", details)

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
