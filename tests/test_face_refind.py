"""Does a face that MediaPipe has lost come back, at the size of frame the worker really uses?

MediaPipe follows a face it has already found, which is easy. To find one again after losing it (a hand in front, a
turn of the head) it shrinks the whole frame to a small square and looks. From a bigger frame that shrink is rougher
and its score for a real face can fall under its cut-off, so the face stays lost. Nothing else in the suite can see
this, because it only happens with the real face finder looking at camera-like frames.

So this runs the worker's own FaceDetector, at the worker's CAMERA_SIZE, in the gaze venv's Python (the suite itself
runs under the system Python, which has no MediaPipe). The frames are made in memory from a sample portrait that
ships with the venv's matplotlib: nothing is written to disk.

It is off unless you ask for it. MediaPipe 1.2.2 tries to send usage logs over the network whenever it runs, so it
mustn't run on every run of the suite:

    GAZEVOICE_REAL_MEDIAPIPE=1 python3 -m unittest tests.test_face_refind -v
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from gazevoice import gaze_worker
from gazevoice.components.gaze import MODEL_SHA256, MODEL_SIZE
from gazevoice.paths import default_paths

OPT_IN = "GAZEVOICE_REAL_MEDIAPIPE"
RECOVER_FRAMES = 10  # a face lost for a moment must be back within this many frames: a third of a second at 30 fps
KEEP_SHARE = 0.9  # and, once back, in at least this share of the borderline frames
EPISODES = 5  # the loss and the borderline frames are gone through this many times, each with fresh camera noise,
# so that one lucky run of noise can't make the test pass (or one unlucky one make it fail)
BORDERLINE_FRAMES = 30  # after each loss


def whats_missing() -> tuple[str | None, Path | None, Path | None]:
    """Why the real-MediaPipe test can't run here (or None), then the gaze venv's Python and the sample portrait."""
    if os.environ.get(OPT_IN) != "1":
        return (f"it is opt-in, because MediaPipe tries to send usage logs over the network whenever it runs: "
                f"set {OPT_IN}=1 to run it"), None, None
    paths = default_paths()
    python = paths.venvs / "gaze" / "bin" / "python"
    if not python.exists():
        return f"the gaze venv's Python isn't there ({python})", None, None
    packages = sorted(python.parents[1].glob("lib/python3*/site-packages"))
    if not packages or not (packages[0] / "mediapipe").is_dir() or not (packages[0] / "cv2").is_dir():
        return f"MediaPipe isn't installed in the gaze venv ({python.parents[1]})", None, None
    model = paths.gaze_model_file
    try:
        pinned = model.stat().st_size == MODEL_SIZE and hashlib.sha256(model.read_bytes()).hexdigest() == MODEL_SHA256
    except OSError:
        pinned = False
    if not pinned:
        return f"the pinned face model isn't at {model}", None, None
    portrait = packages[0] / "matplotlib" / "mpl-data" / "sample_data" / "grace_hopper.jpg"
    if not portrait.is_file():
        return f"the sample portrait isn't at {portrait} (it comes with matplotlib)", None, None
    return None, python, portrait


MISSING, GAZE_PYTHON, PORTRAIT = whats_missing()

# Runs in the gaze venv's Python, as `python -I -B -c PROBE <gazevoice folder> <model> <portrait> <width> <height> <episodes> <frames>`,
# and prints one line, "RESULT {json}". The camera is imitated: a 1920 by 1080 picture with a camera's noise, scaled
# to the worker's size the way ffmpeg would scale it. Nothing is written anywhere.
PROBE = '''
import json, sys
from pathlib import Path
import cv2
import numpy as np
sys.path.insert(0, sys.argv[1])  # the way the worker itself is started: gaze_worker beside gaze_model
import gaze_worker

model, portrait, size = Path(sys.argv[2]), sys.argv[3], (int(sys.argv[4]), int(sys.argv[5]))
EPISODES, BORDERLINE = int(sys.argv[6]), int(sys.argv[7])
SENSOR = (1920, 1080)
HARD = (0.3, 40, 0.5)  # dim (exposure), noisy (sensor noise), flat (contrast): the finder alone sits near its cut-off


def room(w, h):
    """A room-ish background: a smooth gradient and low-frequency blotches, mid grey."""
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 110 + 40 * (xx / w) - 20 * (yy / h)
    blot = cv2.resize(rng.normal(0, 25, (9, 16)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    grey = np.clip(base + blot, 0, 255)
    return np.stack([grey * 0.95, grey, grey * 1.05], axis=2).clip(0, 255).astype(np.uint8)


def head_in_room(w, h):
    """The portrait as tall as the frame, its middle at half the width and 62% of the height (clipped at the bottom)."""
    img = room(w, h)
    p = cv2.cvtColor(cv2.imread(portrait), cv2.COLOR_BGR2RGB)
    pw = int(round(p.shape[1] * h / p.shape[0]))
    small = cv2.resize(p, (pw, h), interpolation=cv2.INTER_AREA if h < p.shape[0] else cv2.INTER_CUBIC)
    x0, y0 = int(round(0.5 * w - pw / 2)), int(round(0.62 * h - h / 2))
    sy0, dy0 = max(0, -y0), max(0, y0)
    dy1 = min(h, y0 + h)
    img[dy0:dy1, x0:x0 + pw] = small[sy0:sy0 + (dy1 - dy0)]
    return img


def camera_like(img, gain, noise, rng):
    out = img.astype(np.float32) * gain + rng.normal(0, noise, img.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def flatten(img, contrast):
    blur = cv2.GaussianBlur(img, (0, 0), 25).astype(np.float32)
    return np.clip(blur + contrast * (img.astype(np.float32) - blur), 0, 255).astype(np.uint8)


detector = gaze_worker.FaceDetector(model, size=size)
rng = np.random.default_rng(2026)
head = head_in_room(*SENSOR)
borderline = flatten(head, HARD[2])
empty = room(*SENSOR)


def sees_a_face(sensor_frame):
    small = cv2.resize(sensor_frame, size, interpolation=cv2.INTER_AREA)
    return detector.look(np.ascontiguousarray(small).tobytes()).row is not None


tracked_when_clean, seen_when_empty, back_after, kept = True, False, [], 0
for _ in range(EPISODES):
    # A clean face the finder tracks, two frames of an empty room (a hand, a turn of the head), then borderline frames.
    tracked = [sees_a_face(camera_like(head, 1.0, 6, rng)) for _ in range(15)]
    lost = [sees_a_face(camera_like(empty, 1.0, 6, rng)) for _ in range(2)]
    after = [sees_a_face(camera_like(borderline, HARD[0], HARD[1], rng)) for _ in range(BORDERLINE)]
    tracked_when_clean = tracked_when_clean and all(tracked[5:])
    seen_when_empty = seen_when_empty or any(lost)
    back_after.append(after.index(True) if True in after else len(after))
    kept += sum(after)
print("RESULT " + json.dumps({
    "tracked_when_clean": tracked_when_clean,
    "seen_when_empty": seen_when_empty,
    "back_after": back_after,  # for each episode: borderline frames until the face was seen again
    "kept": kept,
    "frames": EPISODES * BORDERLINE,
}))
'''


@unittest.skipIf(MISSING is not None, MISSING or "")
class LostFaceComesBackTest(unittest.TestCase):
    """A face the tracker had, lost for two frames, then frames the face finder alone finds only now and then. Five times."""

    @classmethod
    def setUpClass(cls):
        width, height = gaze_worker.CAMERA_SIZE
        # -I: nothing from the environment or the current folder is picked up. -B: no .pyc files left in the repo.
        done = subprocess.run(
            [str(GAZE_PYTHON), "-I", "-B", "-c", PROBE, str(Path(gaze_worker.__file__).parent),
             str(default_paths().gaze_model_file), str(PORTRAIT), str(width), str(height),
             str(EPISODES), str(BORDERLINE_FRAMES)],
            capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )
        lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
        if done.returncode != 0 or not lines:
            raise AssertionError(f"the probe failed (exit {done.returncode}):\n{done.stderr[-2000:]}")
        cls.size = (width, height)
        cls.result = json.loads(lines[-1][len("RESULT "):])
        print(f"\n{width}x{height}: back after {cls.result['back_after']} frames, kept "
              f"{cls.result['kept']}/{cls.result['frames']}", file=sys.stderr)

    def test_the_face_is_tracked_when_clean_and_lost_when_the_picture_is_empty(self):
        self.assertTrue(self.result["tracked_when_clean"], "the clean face should be tracked")
        self.assertFalse(self.result["seen_when_empty"], "an empty room should lose the face")

    def test_a_face_that_was_lost_for_a_moment_is_back_within_ten_frames_every_time(self):
        back = self.result["back_after"]
        self.assertEqual(len(back), EPISODES)
        self.assertLessEqual(max(back), RECOVER_FRAMES, f"at {self.size} the face came back after {back} frames")

    def test_once_back_it_is_kept_in_nearly_every_borderline_frame(self):
        kept, frames = self.result["kept"], self.result["frames"]
        self.assertGreaterEqual(kept / frames, KEEP_SHARE, f"at {self.size} the face was in only {kept} of {frames} frames")


if __name__ == "__main__":
    unittest.main()
