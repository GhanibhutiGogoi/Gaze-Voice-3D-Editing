import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from gazevoice import gaze_model, gaze_worker
from gazevoice.components.microphone import parse_video_devices, pick_camera
from gazevoice.voice.gaze import DEFAULT_RADIUS, GazeTrack
from gazevoice.voice.gaze_tracker import GazeError, GazeTracker

from .test_gaze_model import face

WORKER = Path(gaze_worker.__file__)
FAKE = ["--fake"]

# The real list from this Mac while a phone is plugged in, which puts the phone first.
LISTING = """\
[AVFoundation indev @ 0x14be04b80] AVFoundation video devices:
[AVFoundation indev @ 0x14be04b80] [0] Nokia Unbreakable V Camera
[AVFoundation indev @ 0x14be04b80] [1] MacBook Air Camera
[AVFoundation indev @ 0x14be04b80] [2] Nokia Unbreakable V Desk View Camera
[AVFoundation indev @ 0x14be04b80] [3] MacBook Air Desk View Camera
[AVFoundation indev @ 0x14be04b80] [4] Capture screen 0
[AVFoundation indev @ 0x14be04b80] AVFoundation audio devices:
[AVFoundation indev @ 0x14be04b80] [0] Aggregate Device
[AVFoundation indev @ 0x14be04b80] [1] MacBook Air Microphone
: Input/output error
"""
DEVICES = parse_video_devices(LISTING)


def a_calibration(radius=(0.07, 0.11)) -> dict:
    """A saved calibration, as Task 7 will keep it: a usable model and the check that went with it."""
    rows = [[float(i + j) for j in range(gaze_model.FEATURE_COUNT)] for i in range(6)]
    model = gaze_model.fit(rows, [(0.1 * i, 0.9 - 0.1 * i) for i in range(6)])
    return {"model": model, "check": {"median": 0.05, "p90": 0.09, "radius": list(radius)}}


def wait_for(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


STAND_IN_CAMERA = """\
import os, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
frame = bytes(960 * 540 * 3)
while True:
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    time.sleep(1 / 30)
"""

STAND_IN_SLEEPER = """\
import os, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
sys.stdout.buffer.write(bytes(960 * 540 * 3))
sys.stdout.buffer.flush()
time.sleep(600)
"""


class CameraListTest(unittest.TestCase):
    def test_only_the_video_devices_are_listed(self):
        self.assertEqual(DEVICES, [
            ("0", "Nokia Unbreakable V Camera"), ("1", "MacBook Air Camera"),
            ("2", "Nokia Unbreakable V Desk View Camera"), ("3", "MacBook Air Desk View Camera"),
            ("4", "Capture screen 0"),
        ])

    def test_a_listing_with_no_video_section_is_empty(self):
        self.assertEqual(parse_video_devices("[AVFoundation indev @ 0x1] AVFoundation audio devices:\n[x] [0] Mic\n"), [])

    def test_auto_prefers_the_built_in_camera(self):
        self.assertEqual(pick_camera(DEVICES, ""), ("1", "MacBook Air Camera"))

    def test_auto_prefers_it_wherever_it_is_in_the_list(self):
        devices = [("0", "MacBook Air Camera"), ("1", "Nokia Unbreakable V Camera")]
        self.assertEqual(pick_camera(devices, ""), ("0", "MacBook Air Camera"))

    def test_without_a_built_in_camera_the_first_real_one_wins(self):
        devices = [("0", "Capture screen 0"), ("1", "Logitech BRIO"), ("2", "Logitech BRIO Desk View Camera")]
        self.assertEqual(pick_camera(devices, ""), ("1", "Logitech BRIO"))

    def test_desk_view_and_screens_are_never_picked(self):
        only_bad = [("0", "MacBook Air Desk View Camera"), ("1", "Capture screen 0"), ("2", "Capture screen 1")]
        self.assertIsNone(pick_camera(only_bad, ""))
        self.assertIsNone(pick_camera([], ""))
        self.assertEqual(pick_camera(only_bad + [("3", "USB Webcam")], ""), ("3", "USB Webcam"))

    def test_a_chosen_camera_is_used_if_it_is_still_there(self):
        self.assertEqual(pick_camera(DEVICES, "0"), ("0", "Nokia Unbreakable V Camera"))

    def test_a_chosen_camera_thats_gone_returns_none(self):
        self.assertIsNone(pick_camera(DEVICES, "7"))
        self.assertIsNone(pick_camera([], "1"))


class CameraCommandTest(unittest.TestCase):
    def test_the_camera_is_scaled_to_a_fixed_size_by_ffmpeg(self):
        self.assertEqual(gaze_worker.CAMERA_SIZE, (960, 540))
        self.assertEqual(
            gaze_worker.camera_argv("1", 960, 540),
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", "30",
             "-i", "1:none", "-vf", "scale=960:540", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        )
        self.assertIn("15", gaze_worker.camera_argv("0", 640, 360, fps=15))
        self.assertIn("scale=640:360", gaze_worker.camera_argv("0", 640, 360, fps=15))

    def test_a_blink_or_a_lost_face_has_no_confidence(self):
        self.assertEqual(gaze_worker.blink_confidence(0.0, 0.0), 1.0)
        self.assertAlmostEqual(gaze_worker.blink_confidence(0.2, 0.1), 0.8)
        self.assertEqual(gaze_worker.blink_confidence(0.1, 0.5), 0.0)
        self.assertEqual(gaze_worker.blink_confidence(0.95, 0.9), 0.0)


class WhatMediaPipeSawTest(unittest.TestCase):
    """The step from MediaPipe's result to a row, with a stand-in result shaped like the real one."""

    @staticmethod
    def result(landmarks=None, transform=None, blinks=(0.0, 0.0), blendshapes=True):
        if landmarks is None:
            return SimpleNamespace(face_landmarks=[], face_blendshapes=[], facial_transformation_matrixes=[])
        points = [SimpleNamespace(x=x, y=y, z=z) for x, y, z in landmarks]
        shapes = [SimpleNamespace(category_name=n, score=s) for n, s in
                  (("eyeLookInLeft", 0.3), ("eyeBlinkLeft", blinks[0]), ("eyeBlinkRight", blinks[1]))]
        matrix = SimpleNamespace(tolist=lambda: transform)
        return SimpleNamespace(
            face_landmarks=[points], face_blendshapes=[shapes] if blendshapes else [],
            facial_transformation_matrixes=[matrix],
        )

    def test_a_blank_frame_is_no_face_with_no_confidence(self):
        seen = gaze_worker.observation_from(self.result(), 16 / 9)
        self.assertEqual((seen.row, seen.confidence), (None, 0.0))

    def test_a_face_gives_its_features_and_a_confidence_from_the_blink_scores(self):
        landmarks, transform = face(iris=(0.2, 0.0))
        seen = gaze_worker.observation_from(self.result(landmarks, transform, blinks=(0.1, 0.25)), 16 / 9)
        self.assertEqual(seen.row, gaze_model.features(landmarks, transform, 16 / 9))
        self.assertAlmostEqual(seen.confidence, 0.75)

    def test_a_blink_keeps_the_row_but_has_no_confidence(self):
        landmarks, transform = face()
        seen = gaze_worker.observation_from(self.result(landmarks, transform, blinks=(0.9, 0.8)), 16 / 9)
        self.assertEqual(len(seen.row), gaze_model.FEATURE_COUNT)
        self.assertEqual(seen.confidence, 0.0)

    def test_a_result_without_blendshapes_or_with_a_flat_eye_is_no_face(self):
        landmarks, transform = face()
        self.assertIsNone(gaze_worker.observation_from(self.result(landmarks, transform, blendshapes=False), 16 / 9).row)
        landmarks[33] = landmarks[133]
        self.assertIsNone(gaze_worker.observation_from(self.result(landmarks, transform), 16 / 9).row)


class FrameSourceTest(unittest.TestCase):
    def source(self, code: str):
        return gaze_worker.FrameSource([sys.executable, "-c", code], 4, 3)

    def test_it_hands_over_one_whole_frame_at_a_time(self):
        source = self.source(
            "import sys; w = sys.stdout.buffer; w.write(bytes([1]) * 36 + bytes([2]) * 36 + bytes([3]) * 10); w.flush()"
        )
        self.addCleanup(source.close)
        self.assertEqual(source.read(), bytes([1]) * 36)
        self.assertEqual(source.read(), bytes([2]) * 36)
        self.assertIsNone(source.read())  # the third was cut short, so it isn't a frame

    def test_close_ends_the_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            source = self.source(f"import os, time, sys; open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
                                 "sys.stdout.buffer.write(bytes(36)); sys.stdout.buffer.flush(); time.sleep(600)")
            self.assertEqual(len(source.read()), 36)
            pid = int(pidfile.read_text())
            source.close()
            self.assertTrue(gone(pid))
            self.assertIsNone(source.read())
            source.close()  # twice is fine

    def test_close_frees_a_child_that_is_stuck_writing_frames(self):
        # Like ffmpeg: it only notices SIGTERM between writes, so it can't while the pipe is full and nobody reads.
        code = ("import signal, sys; stop = []; signal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n"
                "while not stop:\n"
                "    sys.stdout.buffer.write(bytes(36)); sys.stdout.buffer.flush()\n")
        source = self.source(code)
        self.assertEqual(len(source.read()), 36)
        time.sleep(0.3)  # long enough for the pipe to fill, and the child to block
        began = time.monotonic()
        source.close()
        self.assertLess(time.monotonic() - began, gaze_worker.STOP_WAIT * 0.6)
        self.assertEqual(source.exit_code, 0)  # it left by itself, and wasn't killed

    def test_a_command_that_isnt_there_says_so(self):
        with self.assertRaises(gaze_worker.CameraError) as caught:
            gaze_worker.FrameSource(["/no/such/ffmpeg", "-i", "x"], 4, 3)
        self.assertIn("ffmpeg", str(caught.exception))


class GazeTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tracker = None
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if self.tracker is not None:
            self.tracker.stop()
        self.tmp.cleanup()

    def make(self, calibration="default", args=FAKE, **kw) -> GazeTracker:
        if calibration == "default":
            calibration = a_calibration()
        self.tracker = GazeTracker(Path(sys.executable), WORKER, "1", calibration, extra_args=args, **kw)
        return self.tracker

    def test_the_tracker_fills_its_track_from_the_worker(self):
        tracker = self.make()
        before = time.time()
        self.assertGreaterEqual(tracker.start(timeout=20), 0)
        self.assertIsInstance(tracker.track, GazeTrack)
        self.assertTrue(wait_for(lambda: len(tracker.track.between(0, math.inf)) >= 12))
        samples = tracker.track.between(0, math.inf)
        self.assertTrue(all(before - 1 <= s.t <= time.time() for s in samples))
        self.assertTrue(all(0 <= s.u <= 1 and 0 <= s.v <= 1 for s in samples))
        self.assertTrue(all(b.t > a.t for a, b in zip(samples, samples[1:])))
        self.assertGreaterEqual(tracker.track.latest_time, samples[0].t)

    def test_the_fake_stream_repeats_exactly(self):
        runs = []
        for _ in range(2):
            tracker = self.make()
            tracker.start(timeout=20)
            self.assertTrue(wait_for(lambda: len(tracker.track.between(0, math.inf)) >= 30))
            runs.append([(s.u, s.v, s.confidence) for s in tracker.track.between(0, math.inf)][:30])
            tracker.stop()
        self.assertEqual(runs[0], runs[1])
        self.assertIn(0.0, [c for _, _, c in runs[0]])  # the fake blinks now and then
        self.assertTrue(any(c > 0.5 for _, _, c in runs[0]))

    def test_no_samples_come_before_a_calibration_is_loaded(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        time.sleep(0.5)
        self.assertEqual(tracker.track.between(0, math.inf), [])
        self.assertTrue(tracker.alive)

    def test_the_radius_comes_from_the_calibrations_check(self):
        self.assertEqual(self.make().radius, (0.07, 0.11))
        self.assertIsInstance(self.make().radius, tuple)
        self.assertEqual(self.make(calibration=None).radius, DEFAULT_RADIUS)
        self.assertEqual(self.make(calibration={"model": {}}).radius, DEFAULT_RADIUS)
        for bad in ([0.1], "wide", [None, 0.1], [0.1, float("nan")], [0.0, 0.1], [-0.2, 0.1]):
            self.assertEqual(self.make(calibration={"check": {"radius": bad}}).radius, DEFAULT_RADIUS, bad)

    def test_a_dead_worker_reads_as_not_alive(self):
        tracker = self.make()
        self.assertFalse(tracker.alive)  # not started yet
        tracker.start(timeout=20)
        self.assertTrue(tracker.alive)
        tracker.proc.kill()
        tracker.proc.wait()
        self.assertFalse(tracker.alive)
        with self.assertRaises(GazeError):
            tracker.collect((0.5, 0.5))

    def test_stop_makes_it_not_alive_and_is_safe_twice(self):
        tracker = self.make()
        tracker.stop()  # before start
        tracker.start(timeout=20)
        tracker.stop()
        self.assertFalse(tracker.alive)
        tracker.stop()

    def stand_in(self, code: str) -> tuple[list[str], Path]:
        script = Path(self.tmp.name) / "camera.py"
        script.write_text(code)
        pidfile = Path(self.tmp.name) / "pid"
        return ["--fake", "--source-argv", json.dumps([sys.executable, str(script), str(pidfile)])], pidfile

    def test_stop_ends_ffmpeg_too(self):
        for code in (STAND_IN_CAMERA, STAND_IN_SLEEPER):  # a camera that is sending frames, and one that has gone quiet
            args, pidfile = self.stand_in(code)
            tracker = self.make(args=args)
            tracker.start(timeout=20)
            pid = int(pidfile.read_text())
            self.assertFalse(gone(pid))
            tracker.stop()
            self.assertEqual(tracker.proc.returncode, 0)  # the worker finished by itself, not under the sweep
            self.assertTrue(wait_for(lambda: gone(pid), 5), code)

    def test_stop_ends_ffmpeg_even_if_the_worker_was_killed(self):
        for code in (STAND_IN_CAMERA, STAND_IN_SLEEPER):  # a camera that is sending frames, and one that has gone quiet
            args, pidfile = self.stand_in(code)
            tracker = self.make(args=args)
            tracker.start(timeout=20)
            pid = int(pidfile.read_text())
            tracker.proc.kill()
            tracker.proc.wait()
            tracker.stop()
            self.assertTrue(wait_for(lambda: gone(pid), 5), code)

    def test_a_worker_that_cannot_open_the_camera_reports_why(self):
        args = ["--fake", "--source-argv", json.dumps([sys.executable, "-c", "import sys; sys.stderr.write('Permission denied\\n')"])]
        tracker = self.make(args=args)
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=20)
        self.assertIn("Permission denied", str(caught.exception))

    def test_a_worker_that_cannot_load_its_model_reports_why(self):
        tracker = self.make(args=[], model_file=Path(self.tmp.name) / "missing" / "face_landmarker.task")
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=20)
        self.assertIn("face_landmarker.task", str(caught.exception))
        self.assertFalse(tracker.alive)

    def test_a_missing_python_says_gaze_isnt_installed(self):
        tracker = GazeTracker(Path(self.tmp.name) / "no-python", WORKER, "1", None)
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=5)
        self.assertIn("isn't installed", str(caught.exception))

    def test_a_calibration_from_another_version_is_refused(self):
        stale = gaze_model.fit([[1.0, 2.0], [2.0, 1.0], [3.0, 5.0]], [(0, 0), (1, 1), (0, 1)])
        tracker = self.make(calibration={"model": stale, "check": {"radius": [0.1, 0.1]}})
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=20)
        self.assertIn("calibrate", str(caught.exception).lower())
        self.assertFalse(tracker.alive)

    def test_collecting_then_fitting_goes_through_the_worker(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        for uv in ((0.2, 0.3), (0.8, 0.7)):
            tracker.collect(uv)
            time.sleep(1.2)
            tracker.stop_collect()
        tracker.collect((0.5, 0.5), check=True)
        time.sleep(0.5)
        tracker.stop_collect()
        result = tracker.fit(timeout=20)
        gaze_model.validate(result["fit"])
        self.assertEqual(set(result["check"]) >= {"median", "p90", "radius"}, True)
        self.assertEqual(len(result["check"]["radius"]), 2)
        self.assertGreater(result["rows"], 40)
        self.assertGreater(result["check_rows"], 5)
        self.assertTrue(result["held_out"])
        self.assertTrue(tracker.alive)

    def test_a_fit_with_no_check_dots_says_it_was_not_held_out(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        tracker.collect((0.4, 0.4))
        time.sleep(1.5)
        tracker.stop_collect()
        result = tracker.fit(timeout=20)
        self.assertFalse(result["held_out"])
        self.assertEqual(result["check_rows"], 0)
        self.assertEqual(set(result["check"]), {"median", "p90", "radius"})

    def test_a_fit_without_enough_readings_says_so_and_keeps_going(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        with self.assertRaises(GazeError) as caught:
            tracker.fit(timeout=20)
        self.assertIn("enough", str(caught.exception))
        self.assertTrue(tracker.alive)

    def test_a_fitted_model_can_be_loaded_to_start_the_stream(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        tracker.collect((0.3, 0.3))
        time.sleep(1.5)
        tracker.stop_collect()
        result = tracker.fit(timeout=20)
        tracker.load({"model": result["fit"], "check": result["check"]})
        self.assertEqual(tracker.radius, tuple(result["check"]["radius"]))
        self.assertTrue(wait_for(lambda: tracker.track.between(0, math.inf)))

    def test_the_worker_help_works_without_the_camera_libraries(self):
        import subprocess

        done = subprocess.run([sys.executable, str(WORKER), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertIn("--serve", done.stdout)


if __name__ == "__main__":
    unittest.main()
