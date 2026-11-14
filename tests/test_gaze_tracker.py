import contextlib
import io
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from gazevoice import gaze_model, gaze_worker
from gazevoice.components.gaze import CALIBRATION_DOTS, CHECK_DOTS, DOT_ORDER, guide_advice
from gazevoice.components.microphone import parse_video_devices, pick_camera
from gazevoice.voice.gaze import DEFAULT_RADIUS, GazeTrack, fixate, ground, mostly_off_screen
from gazevoice.voice.gaze import Area
from gazevoice.voice.gaze_tracker import LOOKING_AWAY_MARGIN, GazeError, GazeTracker, Place, screen_of

from .test_gaze_model import face, made_up_run

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


SCREEN = {"left": 0, "top": 38, "width": 1710, "height": 1074}


def a_calibration(radius=(0.07, 0.11)) -> dict:
    """A saved calibration, as Task 7 will keep it: a usable model and the check that went with it."""
    rows = [[float(i + j) for j in range(gaze_model.FEATURE_COUNT)] for i in range(6)]
    model = gaze_model.fit(rows, [(0.1 * i, 0.9 - 0.1 * i) for i in range(6)], columns=gaze_model.FEATURE_SETS["eyes"])
    return {"model": model, "check": {"median": 0.05, "p90": 0.09, "radius": list(radius)}, "screen": dict(SCREEN)}


def an_old_calibration(radius=(0.07, 0.11)) -> dict:
    """A calibration like the one made on 2026-11-08: a fit on all 90 numbers, head position and all. It has the right
    number of features and is well-formed, but it reads where the head sits, so it is refused and must be made again."""
    calibration = a_calibration(radius)
    rows = [[float(i + j) for j in range(gaze_model.FEATURE_COUNT)] for i in range(6)]
    calibration["model"] = gaze_model.fit(rows, [(0.1 * i, 0.9 - 0.1 * i) for i in range(6)])
    return calibration


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

    def test_auto_takes_a_facetime_camera_too(self):
        self.assertEqual(pick_camera([("0", "FaceTime HD Camera")], ""), ("0", "FaceTime HD Camera"))

    def test_auto_takes_a_chinese_macbook_camera_and_excludes_desk_view(self):
        devices = [("0", "MacBook Air相机"), ("1", "MacBook Air桌上视角相机")]
        self.assertEqual(pick_camera(devices, ""), ("0", "MacBook Air相机"))
        self.assertIsNone(pick_camera(devices, "MacBook Air桌上视角相机"))

    def test_auto_takes_a_traditional_chinese_camera(self):
        self.assertEqual(pick_camera([("0", "MacBook Air相機")], ""), ("0", "MacBook Air相機"))

    def test_auto_never_falls_back_to_a_phone_or_any_other_camera(self):
        phone = [("0", "Nokia Unbreakable V Camera")]
        self.assertIsNone(pick_camera(phone, ""))
        self.assertIsNone(pick_camera([("0", "iPhone Camera"), ("1", "Logitech BRIO")], ""))
        self.assertIsNone(pick_camera([("0", "Capture screen 0"), ("1", "Logitech BRIO")], ""))

    def test_desk_view_and_screens_are_never_picked(self):
        only_bad = [("0", "MacBook Air Desk View Camera"), ("1", "Capture screen 0"), ("2", "Capture screen 1")]
        self.assertIsNone(pick_camera(only_bad, ""))
        self.assertIsNone(pick_camera([], ""))
        self.assertEqual(pick_camera(only_bad + [("3", "MacBook Air Camera")], ""), ("3", "MacBook Air Camera"))
        self.assertIsNone(pick_camera(only_bad + [("3", "USB Webcam")], ""))

    def test_a_chosen_camera_is_matched_by_name(self):
        self.assertEqual(pick_camera(DEVICES, "Nokia Unbreakable V Camera"), ("0", "Nokia Unbreakable V Camera"))

    def test_a_chosen_camera_is_found_at_its_new_number_when_things_are_plugged_in(self):
        before = [("0", "MacBook Air Camera"), ("1", "Logitech BRIO")]
        after = [("0", "Nokia Unbreakable V Camera"), ("1", "MacBook Air Camera"), ("2", "Logitech BRIO")]
        self.assertEqual(pick_camera(before, "Logitech BRIO"), ("1", "Logitech BRIO"))
        self.assertEqual(pick_camera(after, "Logitech BRIO"), ("2", "Logitech BRIO"))

    def test_a_chosen_camera_thats_gone_returns_none_and_is_not_swapped_for_another(self):
        self.assertIsNone(pick_camera(DEVICES, "Logitech BRIO"))
        self.assertIsNone(pick_camera(DEVICES, "1"))  # a number is no longer a way to name a camera
        self.assertIsNone(pick_camera([], "MacBook Air Camera"))

    def test_a_chosen_camera_cannot_be_a_screen(self):
        self.assertIsNone(pick_camera(DEVICES, "Capture screen 0"))


class CameraCommandTest(unittest.TestCase):
    def test_the_camera_is_scaled_to_a_fixed_size_by_ffmpeg(self):
        self.assertEqual(gaze_worker.CAMERA_SIZE, (960, 540))
        self.assertEqual(
            gaze_worker.camera_argv("1", 960, 540),
            # info, so that it says what the camera really sends; each line tagged with its level, so that only what is
            # worth an error message is passed on; and no running count of frames
            ["ffmpeg", "-hide_banner", "-loglevel", "level+info", "-nostats", "-f", "avfoundation", "-framerate", "30",
             "-i", "1:none", "-vf", "scale=960:540", "-fps_mode", "passthrough",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        )
        self.assertIn("15", gaze_worker.camera_argv("0", 640, 360, fps=15))
        self.assertIn("scale=640:360", gaze_worker.camera_argv("0", 640, 360, fps=15))

    def test_the_aspect_the_face_is_measured_with_matches_the_frames(self):
        width, height = gaze_worker.CAMERA_SIZE
        self.assertEqual(width / height, gaze_model.CAMERA_ASPECT)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg isn't installed")
    def test_frames_that_never_arrived_are_not_made_up(self):
        # The real command with a stand-in for the camera: 45 frames over 3 s, with holes in their timestamps.
        argv = gaze_worker.camera_argv("0", 64, 36)
        argv[argv.index("-f"):argv.index("-vf")] = [
            "-f", "lavfi", "-i", "testsrc=size=64x36:rate=30:duration=3,select='lt(mod(n,10),5)'"]
        self.assertNotIn("avfoundation", argv)  # never the real camera
        done = subprocess.run(argv, capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout) / (64 * 36 * 3), 45)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg isn't installed")
    def test_what_ffmpeg_says_of_a_source_it_opens_is_read_for_its_size_and_rate(self):
        # The real command with a stand-in for the camera, 1280 by 720 at 25 a second, scaled to 64 by 36 as always.
        argv = gaze_worker.camera_argv("0", 64, 36)
        argv[argv.index("-f"):argv.index("-vf")] = ["-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25:duration=0.5"]
        self.assertNotIn("avfoundation", argv)  # never the real camera
        done = subprocess.run(argv, capture_output=True, timeout=30)  # stdout is the frames: only stderr is text
        self.assertEqual(done.returncode, 0, done.stderr)
        found, passed_on = [], io.StringIO()
        log = gaze_worker.CameraLog(found.append, passed_on)
        for line in done.stderr.decode("utf-8", "replace").splitlines():
            log.line(line)
        self.assertEqual(found, [{"width": 1280, "height": 720, "fps": 25.0}])  # the source's, not the scaled 64 by 36
        self.assertEqual(passed_on.getvalue(), "")  # ffmpeg's chatter stays out of the error messages


    def test_a_blink_or_a_lost_face_has_no_confidence(self):
        self.assertEqual(gaze_worker.blink_confidence(0.0, 0.0), 1.0)
        self.assertAlmostEqual(gaze_worker.blink_confidence(0.2, 0.1), 0.8)
        self.assertEqual(gaze_worker.blink_confidence(0.1, 0.5), 0.0)
        self.assertEqual(gaze_worker.blink_confidence(0.95, 0.9), 0.0)


class CameraLogTest(unittest.TestCase):
    """What ffmpeg says on stderr: the camera's real size and rate are read once, and only trouble is passed on."""

    OPENED = [  # as ffmpeg 7 says it with "-loglevel level+info", for a camera on AVFoundation
        "[info] Input #0, avfoundation, from '1:none':",
        "[info]   Duration: N/A, start: 41529.512000, bitrate: N/A",
        "[info]   Stream #0:0: Video: rawvideo (UYVY / 0x59565955), uyvy422, 1920x1080, 30 fps, 30 tbr, 1000k tbn",
        "[info] Stream mapping:",
        "[info]   Stream #0:0 -> #0:0 (rawvideo (native) -> rawvideo (native))",
        "[info] Output #0, rawvideo, to 'pipe:1':",
        "[info]   Stream #0:0: Video: rawvideo (RGB[24] / 0x18424752), rgb24, 960x540, q=2-31, 373248 kb/s, 30 fps, 30 tbn",
    ]

    def read(self, lines):
        found, passed_on = [], io.StringIO()
        log = gaze_worker.CameraLog(found.append, passed_on)
        for line in lines:
            log.line(line)
        return found, passed_on.getvalue()

    def test_the_cameras_own_size_and_rate_are_read_from_the_line_it_opens_with(self):
        self.assertEqual(self.read(self.OPENED), ([{"width": 1920, "height": 1080, "fps": 30.0}], ""))

    def test_they_are_read_once_and_never_from_what_ffmpeg_sends_on(self):
        found, _ = self.read(self.OPENED + self.OPENED[2:3])
        self.assertEqual(found, [{"width": 1920, "height": 1080, "fps": 30.0}])
        found, _ = self.read([line for line in self.OPENED if "1920x1080" not in line])
        self.assertEqual(found, [])  # the 960 by 540 after "Output" is ffmpeg's own, not the camera's

    def test_a_rate_said_only_as_tbr_is_taken_and_one_that_cant_be_a_cameras_isnt(self):
        line = "[info]   Stream #0:0: Video: rawvideo (UYVY / 0x59565955), uyvy422(tv, bt709), 1280x720, 29.97 tbr, 1000k tbn"
        self.assertEqual(self.read([line])[0], [{"width": 1280, "height": 720, "fps": 29.97}])
        line = "[info]   Stream #0:0: Video: rawvideo (UYVY / 0x59565955), uyvy422, 1280x720, 1000k tbr, 1000k tbn"
        self.assertEqual(self.read([line])[0], [{"width": 1280, "height": 720, "fps": None}])

    def test_trouble_is_passed_on_as_ffmpeg_said_it_before(self):
        _, passed_on = self.read(self.OPENED + [
            "[avfoundation @ 0x7f9] [error] Selected framerate (30.000000) is not supported by the device.",
            "[fatal] Error opening input files: Input/output error",
        ])
        self.assertEqual(passed_on.splitlines(), [
            "[avfoundation @ 0x7f9] Selected framerate (30.000000) is not supported by the device.",
            "Error opening input files: Input/output error",
        ])

    def test_warnings_are_left_out_as_they_were_when_ffmpeg_said_only_errors(self):
        # One like this can come with every frame. Passed on, they would fill the tracker's 30 lines and push the reason
        # for an error out of the last six, which are all its messages show.
        warning = "[rawvideo @ 0x7f8] [warning] Application provided invalid, non monotonically increasing dts to muxer"
        _, passed_on = self.read(self.OPENED + [warning] * 50 + ["[warning] Something odd.",
                                                                  "[fatal] Error opening input files: Input/output error"])
        self.assertEqual(passed_on.splitlines(), ["Error opening input files: Input/output error"])

    def test_a_line_that_names_where_it_came_from_twice_is_read_as_ffmpegs_too(self):
        # ffmpeg names a part and the part above it, as "[parent @ 0x1] [child @ 0x2] [level] …".
        two = "[in#0/avfoundation @ 0x7f1] [avfoundation @ 0x7f2] "
        found, passed_on = self.read([
            two + "[info]   Stream #0:0: Video: rawvideo (UYVY / 0x59565955), uyvy422, 1280x720, 30 fps, 1000k tbn",
            two + "[info] chatter",
            two + "[warning] odd",
            two + "[error] The camera is busy",
            "[in#0/avfoundation @ 0x7f1] [error] Error opening input: Input/output error",
        ])
        self.assertEqual(found, [{"width": 1280, "height": 720, "fps": 30.0}])
        self.assertEqual(passed_on.splitlines(), [two + "The camera is busy",
                                                  "[in#0/avfoundation @ 0x7f1] Error opening input: Input/output error"])

    def test_a_line_with_no_level_is_passed_on_whole(self):
        # A stand-in camera isn't ffmpeg, and what it says may be why it stopped.
        self.assertEqual(self.read(["Permission denied"]), ([], "Permission denied\n"))


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


class WhatTheDotsSawTest(unittest.TestCase):
    """A calibration says which of the reasons it lost its readings to: no face, eyes counted as shut, or an error."""

    FACE = [0.0] * gaze_model.FEATURE_COUNT

    def setUp(self):
        self.sent = []
        self.session = gaze_worker.Session(self.sent.append)

    def fit(self):
        self.session.handle({"cmd": "fit"})
        return self.sent[-1]

    def test_each_kind_of_frame_is_counted_and_the_refusal_names_them(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.session.see(1.0, gaze_worker.Observation(None, 0.0, why="no face"), light=20)
        self.session.see(1.1, gaze_worker.Observation(None, 0.0, why="half-read"), light=30)
        self.session.see(1.2, gaze_worker.Observation(self.FACE, 0.0, blink=0.7), light=40)
        self.session.see(1.3, gaze_worker.Observation(None, 0.0, why="error: bad frame"), light=30)
        reply = self.fit()
        self.assertEqual(reply["cmd"], "fit")
        for words in ("Not enough face readings", "4 frames", "no face in 1", "half-read in 1", "shut in 1",
                      "typical blink score 0.70", "limit 0.5", "errors in 1", "bad frame", "brightness 30 of 255"):
            self.assertIn(words, reply["error"])

    def test_frames_with_no_dot_up_are_not_counted(self):
        self.session.see(1.0, gaze_worker.Observation(None, 0.0, why="no face"), light=20)
        self.assertIn("0 frames", self.fit()["error"])

    def test_the_count_starts_again_after_a_fit(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.session.see(1.0, gaze_worker.Observation(None, 0.0, why="no face"), light=20)
        self.fit()
        self.assertIn("0 frames", self.fit()["error"])

    def test_a_fit_that_works_reports_the_same_counts(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        for frame in range(gaze_worker.MIN_FIT_ROWS):
            self.session.see(frame / 30, gaze_worker.Observation(self.FACE, 0.9, blink=0.1), light=100)
        reply = self.fit()
        self.assertIn("fit", reply)
        self.assertIn(f"{gaze_worker.MIN_FIT_ROWS} frames", reply["seen"])
        self.assertIn("brightness 100 of 255", reply["seen"])

    def test_a_frame_with_no_light_figure_is_still_counted(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.session.see(1.0, gaze_worker.Observation(None, 0.0, why="no face"))
        self.assertIn("1 frames", self.fit()["error"])

    def test_brightness_is_one_number_read_from_the_frame(self):
        self.assertEqual(gaze_worker.brightness(bytes([100]) * 9000), 100)
        self.assertIsNone(gaze_worker.brightness(b""))

    def test_what_mediapipe_saw_says_why_there_is_no_row_and_how_shut_the_eyes_looked(self):
        make = WhatMediaPipeSawTest.result
        landmarks, transform = face()
        self.assertEqual(gaze_worker.observation_from(make(), 16 / 9).why, "no face")
        self.assertEqual(gaze_worker.observation_from(make(landmarks, transform, blendshapes=False), 16 / 9).why, "half-read")
        self.assertEqual(gaze_worker.observation_from(make(landmarks, transform, blinks=(0.9, 0.8)), 16 / 9).blink, 0.9)


class WhereTheHeadIsTest(unittest.TestCase):
    """The guide before the dots: where the middle of the eyes is in the picture, and how far the head is from the camera."""

    BACK_60CM = [[1.0, 0, 0, 20.0], [0, 1.0, 0, 40.0], [0, 0, 1.0, -40.0], [0, 0, 0, 1.0]]  # 20, 40 and 40: 60 from the camera

    def seen(self, **kw):
        landmarks, transform = face(transform=self.BACK_60CM)
        return gaze_worker.observation_from(WhatMediaPipeSawTest.result(landmarks, transform, **kw), 16 / 9)

    def test_the_place_is_the_middle_of_the_eyes_and_the_head_distance(self):
        seen = self.seen()
        landmarks, _ = face()
        x = (landmarks[33][0] + landmarks[263][0]) / 2  # the outer corners of the two eyes
        y = (landmarks[33][1] + landmarks[263][1]) / 2
        self.assertAlmostEqual(seen.place[0], x)
        self.assertAlmostEqual(seen.place[1], y)
        self.assertAlmostEqual(seen.place[2], 60.0)
        # One source for the distance: it is the number the fit reads, not a second sum that could drift from it.
        self.assertEqual(seen.place[2], seen.row[gaze_model.BASE_NAMES.index("head distance")])

    def test_the_place_is_not_mirrored(self):
        landmarks, transform = face(transform=self.BACK_60CM)
        landmarks[33], landmarks[263] = (0.2, 0.3, 0.0), (0.3, 0.3, 0.0)
        seen = gaze_worker.observation_from(WhatMediaPipeSawTest.result(landmarks, transform), 16 / 9)
        self.assertAlmostEqual(seen.place[0], 0.25)

    def test_a_blink_still_says_where_the_head_is(self):
        seen = self.seen(blinks=(0.9, 0.8))
        self.assertEqual(seen.confidence, 0.0)
        self.assertIsNotNone(seen.place)

    def test_with_no_face_or_no_measurement_there_is_no_place(self):
        landmarks, transform = face(transform=self.BACK_60CM)
        self.assertIsNone(gaze_worker.observation_from(WhatMediaPipeSawTest.result(), 16 / 9).place)
        self.assertIsNone(gaze_worker.observation_from(
            WhatMediaPipeSawTest.result(landmarks, transform, blendshapes=False), 16 / 9).place)

    def test_an_observation_built_the_old_way_has_no_place(self):
        self.assertIsNone(gaze_worker.Observation(None, 0.0).place)
        self.assertIsNone(gaze_worker.Observation([0.0], 0.9, (0.5, 0.5), "", 0.1).place)
        self.assertIsNone(gaze_worker.Observation([0.0], 0.9, (0.5, 0.5), "", 0.1).box)

    def test_the_box_is_how_far_the_face_reaches_across_and_down_the_picture(self):
        landmarks, transform = face(transform=self.BACK_60CM)
        landmarks[10], landmarks[152] = (0.47, 0.08, 0.0), (0.52, 0.93, 0.0)  # the top of the forehead and the chin
        landmarks[234], landmarks[454] = (0.31, 0.5, 0.0), (0.69, 0.5, 0.0)  # the sides of the face
        seen = gaze_worker.observation_from(WhatMediaPipeSawTest.result(landmarks, transform), 16 / 9)
        self.assertEqual(seen.box, (0.31, 0.08, 0.69, 0.93))  # left, top, right and bottom, not mirrored

    def test_a_face_partly_out_of_the_picture_reaches_past_its_edge(self):
        landmarks, transform = face(transform=self.BACK_60CM)
        landmarks[152] = (0.5, 1.07, 0.0)  # MediaPipe places what it can't see where it would be
        seen = gaze_worker.observation_from(WhatMediaPipeSawTest.result(landmarks, transform), 16 / 9)
        self.assertEqual(seen.box[3], 1.07)

    def test_with_no_face_there_is_no_box(self):
        self.assertIsNone(gaze_worker.observation_from(WhatMediaPipeSawTest.result(), 16 / 9).box)

    def test_the_fake_face_sits_where_the_outline_is_so_a_fake_run_passes_the_guide(self):
        seen = gaze_worker.FakeDetector().look(b"")
        x, y, distance = seen.place
        self.assertEqual(guide_advice(1 - x, y, distance, seen.box), ("Hold still", True))
        self.assertEqual(gaze_worker.FakeDetector().look(b"").place, seen.place)  # steady, every frame and every run
        self.assertEqual(gaze_worker.FakeDetector().look(b"").box, seen.box)


class TheGuideInTheWorkerTest(unittest.TestCase):
    """`guide` turns on looking at frames for the head's place, and a message of at most GUIDE_RATE a second."""

    PLACE = (0.55, 0.4, 32.0)
    BOX = (0.411112, 0.1, 0.689, 0.87)

    def setUp(self):
        self.sent = []
        self.session = gaze_worker.Session(self.sent.append)

    def frames(self, seconds, place=PLACE, start=100.0, step=0.01):
        for i in range(round(seconds / step)):
            self.session.see(start + i * step, gaze_worker.Observation([0.0], 0.9, place=place, box=self.BOX))

    def test_frames_are_worth_looking_at_while_the_guide_is_on_and_not_before_or_after(self):
        self.assertFalse(self.session.wants_frames)
        self.session.handle({"cmd": "guide", "on": True})
        self.assertTrue(self.session.wants_frames)
        self.session.handle({"cmd": "guide", "on": False})
        self.assertFalse(self.session.wants_frames)

    def test_it_says_where_the_head_is(self):
        self.session.handle({"cmd": "guide", "on": True})
        self.session.see(100.0, gaze_worker.Observation([0.0], 0.9, place=self.PLACE, box=self.BOX))
        # the box's left, top, right and bottom, as shares of the picture: numbers only, so the guide can tell a face
        # that reaches the edge of the picture
        self.assertEqual(self.sent, [{"guide": {"found": True, "x": 0.55, "y": 0.4, "distance": 32.0,
                                                "box": [0.4111, 0.1, 0.689, 0.87]}}])

    def test_with_no_face_it_says_only_that(self):
        self.session.handle({"cmd": "guide", "on": True})
        self.session.see(100.0, gaze_worker.Observation(None, 0.0, why="no face"))
        self.assertEqual(self.sent, [{"guide": {"found": False}}])

    def test_it_says_nothing_before_the_guide_is_on_or_after_it_is_off(self):
        self.frames(0.5)
        self.assertEqual(self.sent, [])
        self.session.handle({"cmd": "guide", "on": True})
        self.session.handle({"cmd": "guide", "on": False})
        self.frames(0.5)
        self.assertEqual(self.sent, [])

    def test_it_says_it_at_most_guide_rate_times_a_second(self):
        self.assertEqual(gaze_worker.GUIDE_RATE, 10)
        self.session.handle({"cmd": "guide", "on": True})
        self.frames(2.0)  # a frame every 10 ms, which is faster than any camera
        self.assertLessEqual(len(self.sent), 2 * gaze_worker.GUIDE_RATE)
        self.assertGreaterEqual(len(self.sent), 2 * gaze_worker.GUIDE_RATE - 2)  # and it isn't starved

    def test_turning_it_on_again_says_where_the_head_is_at_once(self):
        self.session.handle({"cmd": "guide", "on": True})
        self.frames(0.05)
        self.session.handle({"cmd": "guide", "on": False})
        self.session.handle({"cmd": "guide", "on": True})
        before = len(self.sent)
        self.session.see(100.06, gaze_worker.Observation([0.0], 0.9, place=self.PLACE, box=self.BOX))
        self.assertEqual(len(self.sent), before + 1)

    def test_it_is_said_whether_or_not_a_calibration_is_loaded_and_the_stream_goes_on_as_before(self):
        self.session.handle({"cmd": "load", "model": a_calibration()["model"]})
        self.session.handle({"cmd": "guide", "on": True})
        self.session.see(100.0, gaze_worker.Observation([float(i) for i in range(gaze_model.FEATURE_COUNT)], 0.9,
                                                        place=self.PLACE, box=self.BOX))
        kinds = [next(iter(m)) for m in self.sent]
        self.assertEqual(kinds, ["loaded", "guide", "t"])  # the position stream is not stopped by the guide

    def test_the_guide_is_a_command_the_worker_understands(self):
        self.session.handle({"cmd": "guide", "on": "yes"})
        self.assertTrue(self.session.wants_frames)
        self.session.handle({"cmd": "guide"})  # no "on" means off
        self.assertFalse(self.session.wants_frames)


class WholeRunTest(unittest.TestCase):
    """A whole run of dots going through the worker's fit."""

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.targets, cls.check_rows, cls.check_targets = made_up_run()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.readings = Path(self.tmp.name) / "state" / "gaze-last-run.json"
        self.sent = []
        self.session = gaze_worker.Session(self.sent.append, self.readings)

    def look(self, rows, targets, check=False):
        for at, (row, target) in enumerate(zip(rows, targets)):
            if at == 0 or target != targets[at - 1]:
                self.session.handle({"cmd": "collect", "uv": list(target), "check": check})
            self.session.see(at / 30, gaze_worker.Observation(row, 0.9, blink=0.1), light=100)
        self.session.handle({"cmd": "stop_collect"})

    def whole_run(self, **fit):
        self.look(self.rows, self.targets)
        self.look(self.check_rows, self.check_targets, check=True)
        self.session.handle({"cmd": "fit", **fit})
        return self.sent[-1]

    def test_given_the_screens_width_the_fit_allows_for_the_heads_turn(self):
        reply = self.whole_run(screen_width_cm=29.05)
        self.assertEqual(reply["screen_width_cm"], 29.05)  # said in the reply, so the card can say whether it was
        self.assertEqual(reply["fit"]["screen_width_cm"], 29.05)
        choice = gaze_model.choose(self.rows, self.targets, 29.05)
        honest = gaze_model.fit_choice(self.rows, self.targets, choice, 29.05)
        self.assertEqual(reply["check"], gaze_model.check(honest, self.check_rows, self.check_targets))
        self.assertEqual(reply["model"], choice.name)
        gaze_model.validate(reply["fit"])

    def test_without_the_screens_width_the_fit_is_as_it_was_and_says_so(self):
        reply = self.whole_run()
        self.assertIsNone(reply["screen_width_cm"])
        self.assertIsNone(reply["fit"]["screen_width_cm"])

    def test_a_width_that_makes_no_sense_is_no_width(self):
        for junk in ("wide", -29.0, 0, True, [29.0], None):
            with self.subTest(junk=junk):
                self.sent.clear()
                with contextlib.redirect_stderr(io.StringIO()):
                    reply = self.whole_run(screen_width_cm=junk)
                self.assertIn("fit", reply)
                self.assertIsNone(reply["screen_width_cm"])

    def test_the_mark_comes_from_before_the_check_dots_were_learned_from(self):
        reply = self.whole_run()
        choice = gaze_model.choose(self.rows, self.targets)
        honest = gaze_model.check(gaze_model.fit_choice(self.rows, self.targets, choice), self.check_rows, self.check_targets)
        self.assertEqual(reply["check"], honest)
        self.assertEqual(reply["model"], choice.name)
        self.assertTrue(reply["held_out"])

    def test_the_model_that_is_kept_learned_from_all_twenty_four_places(self):
        reply = self.whole_run()
        self.assertEqual(reply["fit"]["rows"], 30 * len(DOT_ORDER))  # a second a dot is under KEEP_PER_DOT: all kept
        choice = gaze_model.choose(self.rows, self.targets)
        everything = gaze_model.fit_choice(self.rows + self.check_rows, self.targets + self.check_targets, choice)
        self.assertEqual(reply["fit"], everything)
        self.assertEqual((reply["rows"], reply["check_rows"]), (len(self.rows), len(self.check_rows)))
        gaze_model.validate(reply["fit"])

    # The learning dots' middle is the screen's, and the check dots ring it: 0.3 away at the sides, 0.42 at the corners.
    RING = statistics.median(math.dist((0.5, 0.5), dot) for dot in CHECK_DOTS)

    def test_it_says_what_always_guessing_the_middle_would_miss(self):
        self.assertAlmostEqual(self.RING, (0.3 + math.hypot(0.3, 0.3)) / 2)
        self.assertAlmostEqual(self.whole_run()["guess"], self.RING)

    def test_the_middle_is_of_the_dots_however_many_rows_each_gave(self):
        corner = CALIBRATION_DOTS[0]
        some = [(row, at) for row, at in zip(self.rows, self.targets) if at != corner or row is self.rows[0]]
        self.look([row for row, _ in some], [at for _, at in some])
        self.look(self.check_rows, self.check_targets, check=True)
        self.session.handle({"cmd": "fit"})
        self.assertAlmostEqual(self.sent[-1]["guess"], self.RING)

    def test_it_says_how_far_the_head_went_from_the_first_dot_to_the_last(self):
        moved = self.whole_run()["moved"]
        self.assertEqual(set(moved), {"cm", "degrees"})
        # 23 dots of the 24 gone by, of a creep of 1.2 cm and 2 degrees
        self.assertAlmostEqual(moved["cm"], 1.2 * 23 / 24, delta=0.15)
        self.assertAlmostEqual(moved["degrees"], 2.0 * 23 / 24, delta=0.2)

    def test_the_head_is_measured_from_the_first_dot_of_the_run_to_the_last_whichever_kind_they_are(self):
        at = gaze_model.BASE_NAMES.index("head x")

        def face(x):
            row = list(self.rows[0])
            row[at] = x
            return row

        for number, (x, check) in enumerate(((0.0, True), (1.0, False), (2.0, False), (3.0, True), (5.0, False))):
            self.session.handle({"cmd": "collect", "uv": [0.1 * number, 0.5], "check": check})
            for frame in range(10):  # three learning dots of ten: MIN_FIT_ROWS
                self.session.see(number + frame / 30, gaze_worker.Observation(face(x), 0.9))
        self.session.handle({"cmd": "fit"})
        self.assertAlmostEqual(self.sent[-1]["moved"]["cm"], 5.0)

    def test_the_rows_are_kept_in_the_order_they_came(self):
        # So that each row lines up with its dot's frames, and the check dots mixed in among the others are told apart.
        self.look(self.rows[:2], self.targets[:2])
        self.look(self.check_rows[:3], self.check_targets[:3], check=True)
        self.look(self.rows[-1:], self.targets[-1:])
        self.session.handle({"cmd": "fit"})
        kept = json.loads(self.readings.read_text())
        self.assertEqual(kept["held_back"], [False, False, True, True, True, False])
        self.assertEqual(kept["rows"], [[round(v, 5) for v in row[:12]]
                                        for row in self.rows[:2] + self.check_rows[:3] + self.rows[-1:]])
        self.assertEqual(kept["targets"], [list(t) for t in self.targets[:2] + self.check_targets[:3] + self.targets[-1:]])

    def test_the_head_is_measured_from_its_middle_so_one_odd_frame_counts_for_nothing(self):
        at = gaze_model.BASE_NAMES.index("head x")
        rows = [list(row) for row in self.rows]
        rows[3][at] += 40.0
        self.look(rows, self.targets)
        self.session.handle({"cmd": "fit"})
        self.assertLess(self.sent[-1]["moved"]["cm"], 2.0)

    def test_with_no_check_dots_there_is_nothing_to_compare_guessing_with(self):
        self.look(self.rows, self.targets)
        self.session.handle({"cmd": "fit"})
        reply = self.sent[-1]
        self.assertFalse(reply["held_out"])
        self.assertIsNone(reply["guess"])
        self.assertEqual(reply["fit"]["rows"], len(self.rows))
        self.assertEqual(set(reply["moved"]), {"cm", "degrees"})

    def test_the_numbers_of_the_run_are_kept(self):
        self.whole_run()
        kept = json.loads(self.readings.read_text())
        count = len(self.rows) + len(self.check_rows)
        self.assertEqual(set(kept), {"names", "rows", "targets", "held_back", "counts", "when",
                                     "size", "frame_names", "frames", "row_ms", "eye_score_names", "eye_scores",
                                     "transforms", "face_px", "eye_point_landmarks", "eye_points", "boxes",
                                     "camera"})
        self.assertEqual(kept["size"], [960, 540])
        self.assertEqual(kept["frame_names"], ["ms", "dot", "seen", "light"])
        self.assertEqual(kept["names"], gaze_model.BASE_NAMES)
        self.assertEqual({len(row) for row in kept["rows"]}, {12})
        self.assertEqual(kept["rows"][1], [round(v, 5) for v in self.rows[1][:12]])
        self.assertEqual(len(kept["rows"]), count)
        self.assertEqual(kept["targets"], [list(t) for t in self.targets + self.check_targets])
        self.assertEqual(kept["held_back"], [False] * len(self.rows) + [True] * len(self.check_rows))
        self.assertEqual(kept["counts"], {"frames": count, "no_face": 0, "half_read": 0, "shut": 0, "errors": 0})

    def test_the_file_says_when_the_run_was_made_so_an_old_one_can_be_told_from_this_one(self):
        before = time.time()
        self.whole_run()
        after = time.time()
        self.assertTrue(before <= json.loads(self.readings.read_text())["when"] <= after)

    def test_a_run_that_failed_is_kept_too(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.session.see(1.0, gaze_worker.Observation(None, 0.0, why="no face"), light=20)
        self.session.see(1.1, gaze_worker.Observation(self.rows[0], 0.9), light=20)
        self.session.handle({"cmd": "fit"})
        self.assertIn("Not enough face readings", self.sent[-1]["error"])
        kept = json.loads(self.readings.read_text())
        self.assertEqual(len(kept["rows"]), 1)
        self.assertEqual(kept["counts"]["no_face"], 1)

    def test_check_rows_alone_are_still_worth_keeping(self):
        self.look(self.check_rows[:3], self.check_targets[:3], check=True)
        self.session.handle({"cmd": "fit"})
        self.assertEqual(json.loads(self.readings.read_text())["held_back"], [True] * 3)

    def test_each_run_replaces_the_last(self):
        self.whole_run()
        self.look(self.rows[:2], self.targets[:2])
        self.session.handle({"cmd": "fit"})
        self.assertEqual(len(json.loads(self.readings.read_text())["rows"]), 2)

    def test_a_run_with_no_rows_and_no_frames_leaves_the_last_one_alone(self):
        self.readings.parent.mkdir()
        self.readings.write_text("the last run")
        self.session.handle({"cmd": "fit"})
        self.assertEqual(self.readings.read_text(), "the last run")

    def test_without_the_flag_nothing_is_kept(self):
        session = gaze_worker.Session(self.sent.append)
        session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        session.see(1.0, gaze_worker.Observation(self.rows[0], 0.9))
        session.handle({"cmd": "fit"})
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_the_file_is_written_whole_or_not_at_all(self):
        self.readings.parent.mkdir()
        self.readings.write_text("the last run")
        with unittest.mock.patch("os.replace", side_effect=OSError("disk full")), contextlib.redirect_stderr(io.StringIO()):
            reply = self.whole_run()
        self.assertIn("fit", reply)  # keeping the numbers is never worth losing the calibration for
        self.assertEqual(self.readings.read_text(), "the last run")
        self.assertEqual(os.listdir(self.readings.parent), [self.readings.name])

    def test_a_place_that_cant_be_written_doesnt_stop_the_fit(self):
        blocked = Path(self.tmp.name) / "a file"
        blocked.write_text("")
        self.session = gaze_worker.Session(self.sent.append, blocked / "state" / "gaze-last-run.json")
        with contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertIn("fit", self.whole_run())
        self.assertIn("readings", said.getvalue())

    def test_only_this_person_can_read_it(self):
        self.whole_run()
        self.assertEqual(self.readings.stat().st_mode & 0o777, 0o600)


class EachFrameKeptTest(unittest.TestCase):
    """The readings file also says, for every frame looked at while the guide or a dot was up, when it came, which dot
    it went with, what it showed and how bright it was. Nothing of the picture itself."""

    FACE = [0.0] * gaze_model.FEATURE_COUNT

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.readings = Path(self.tmp.name) / "gaze-last-run.json"
        self.sent = []
        self.session = gaze_worker.Session(self.sent.append, self.readings)

    def frame(self, t, kind="face", light=100.0):
        seen = {
            "face": gaze_worker.Observation(self.FACE, 0.9, blink=0.1),
            "shut": gaze_worker.Observation(self.FACE, 0.0, blink=0.7),
            "no face": gaze_worker.Observation(None, 0.0, why="no face"),
            "half-read": gaze_worker.Observation(None, 0.0, why="half-read"),
            "error": gaze_worker.Observation(None, 0.0, why="error: bad frame"),
        }[kind]
        self.session.see(t, seen, light=light)

    def fit(self):
        self.session.handle({"cmd": "fit"})
        return json.loads(self.readings.read_text())

    def test_the_guide_frames_are_dot_0_and_the_dots_count_up_from_1(self):
        self.session.handle({"cmd": "guide", "on": True})
        self.frame(10.0)
        self.frame(10.125, "no face", light=None)
        self.session.handle({"cmd": "guide", "on": False})
        self.frame(10.25)  # neither the guide nor a dot: not looked at for the run
        self.session.handle({"cmd": "collect", "uv": [0.1, 0.1]})
        self.frame(11.0, light=90.26)
        self.frame(11.5, "half-read")
        self.session.handle({"cmd": "stop_collect"})
        self.frame(11.75)  # between dots
        self.session.handle({"cmd": "collect", "uv": [0.9, 0.1]})
        self.frame(12.0, "shut")
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5], "check": True})  # a check dot counts as a dot too
        self.frame(12.25, "error", light=0.0)
        kept = self.fit()
        self.assertEqual(kept["frame_names"], ["ms", "dot", "seen", "light"])
        self.assertEqual(kept["frames"], [
            [0, 0, "face", 100.0],
            [125, 0, "no face", None],
            [1000, 1, "face", 90.3],
            [1500, 1, "half-read", 100.0],
            [2000, 2, "shut", 100.0],
            [2250, 3, "error", 0.0],
        ])

    def test_a_run_that_never_found_a_face_still_keeps_its_frames(self):
        self.readings.write_text("the last run")
        self.session.handle({"cmd": "guide", "on": True})
        self.frame(1.0, "no face")
        self.session.handle({"cmd": "guide", "on": False})
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(2.0, "no face", light=12.0)
        self.frame(2.5, "half-read", light=12.0)
        self.session.handle({"cmd": "fit"})
        self.assertIn("Not enough face readings", self.sent[-1]["error"])
        kept = json.loads(self.readings.read_text())  # the timeline of the run where nothing was seen is the one wanted
        self.assertEqual(kept["frames"], [[0, 0, "no face", 100.0], [1000, 1, "no face", 12.0], [1500, 1, "half-read", 12.0]])
        self.assertEqual((kept["rows"], kept["targets"], kept["held_back"]), ([], [], []))
        self.assertEqual((kept["counts"]["frames"], kept["counts"]["no_face"], kept["counts"]["half_read"]), (2, 1, 1))

    def test_the_times_start_at_the_first_frame_of_the_run(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(1794000000.5)
        self.frame(1794000001.0)
        self.assertEqual([frame[0] for frame in self.fit()["frames"]], [0, 500])

    def test_the_counts_and_the_kinds_of_frame_agree(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        for at, kind in enumerate(["face", "no face", "no face", "half-read", "shut", "error", "face"]):
            self.frame(at / 8, kind)
        kept = self.fit()
        kinds = [frame[2] for frame in kept["frames"]]
        self.assertEqual(len(kinds), kept["counts"]["frames"])
        self.assertEqual(kinds.count("no face"), kept["counts"]["no_face"])
        self.assertEqual(kinds.count("half-read"), kept["counts"]["half_read"])
        self.assertEqual(kinds.count("shut"), kept["counts"]["shut"])
        self.assertEqual(kinds.count("error"), kept["counts"]["errors"])

    def test_a_new_run_starts_its_dots_and_its_clock_again(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(1.0)
        self.session.handle({"cmd": "collect", "uv": [0.4, 0.4]})
        self.frame(2.0)
        self.assertEqual([frame[1] for frame in self.fit()["frames"]], [1, 2])
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(50.0)
        self.assertEqual(self.fit()["frames"], [[0, 1, "face", 100.0]])

    def test_a_collect_that_makes_no_sense_is_not_a_dot(self):
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.session.handle({"cmd": "collect", "uv": "nonsense"})
        self.frame(1.0)
        self.assertEqual([frame[1] for frame in self.fit()["frames"]], [1])

    def test_only_the_first_four_thousand_frames_are_kept_so_the_file_stays_small(self):
        self.assertEqual(gaze_worker.FRAME_LIMIT, 4000)
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(0.0)
        for at in range(1, 4005):
            self.frame(at * 0.5, "no face")
        kept = self.fit()
        self.assertEqual(len(kept["frames"]), 4000)
        self.assertEqual(kept["frames"][-1], [1999500, 1, "no face", 100.0])  # the 4000th, not the last
        self.assertEqual(kept["counts"]["frames"], 4005)  # the counts still cover every frame
        self.assertEqual(len(kept["rows"]), 1)

    def test_each_frame_is_four_plain_values(self):
        self.session.handle({"cmd": "guide", "on": True})
        self.frame(1.0)
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(2.0, "shut", light=None)
        self.frame(2.125)  # a face that counts, so there are readings worth keeping
        kept = self.fit()
        self.assertEqual(len(kept["frames"]), 3)
        for ms, dot, seen, light in kept["frames"]:
            self.assertIs(type(ms), int)
            self.assertIs(type(dot), int)
            self.assertIn(seen, {"face", "no face", "half-read", "shut", "error"})
            self.assertTrue(light is None or type(light) is float)
        self.assertEqual(set(kept), {"names", "rows", "targets", "held_back", "counts", "when",
                                     "size", "frame_names", "frames", "row_ms", "eye_score_names", "eye_scores",
                                     "transforms", "face_px", "eye_point_landmarks", "eye_points", "boxes",
                                     "camera"})

    def test_frames_with_neither_the_guide_nor_a_dot_up_leave_no_trace(self):
        self.frame(1.0)
        self.session.handle({"cmd": "collect", "uv": [0.5, 0.5]})
        self.frame(2.0)
        self.session.handle({"cmd": "stop_collect"})
        self.frame(3.0)
        self.assertEqual(len(self.fit()["frames"]), 1)


EYE_SCORES = ["eyeLookUpLeft", "eyeLookUpRight", "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight",
              "eyeLookOutLeft", "eyeLookOutRight", "eyeBlinkLeft", "eyeBlinkRight", "eyeSquintLeft", "eyeSquintRight",
              "eyeWideLeft", "eyeWideRight"]
# MediaPipe 1.2.2's own lists, from mediapipe.tasks.python.vision.FaceLandmarksConnections in the gaze venv: each eye's
# outline (FACE_LANDMARKS_RIGHT_EYE, FACE_LANDMARKS_LEFT_EYE), then its iris (the middle, then FACE_LANDMARKS_*_IRIS).
RIGHT_EYE_OUTLINE = [33, 7, 163, 144, 145, 153, 154, 155, 133, 246, 161, 160, 159, 158, 157, 173]
LEFT_EYE_OUTLINE = [263, 249, 390, 373, 374, 380, 381, 382, 362, 466, 388, 387, 386, 385, 384, 398]


class WhatEachRowKeepsTest(unittest.TestCase):
    """For the next study, the readings file keeps more of each row than the fit reads: when it came, MediaPipe's eye
    scores, the head's whole transform, how wide the face was and where the eyes' outlines and irises were. Numbers
    only, never a picture."""

    TRANSFORM = [[0.99, 0.0, 0.14, 1.5], [0.0, 1.0, 0.0, -2.0], [-0.14, 0.0, 0.99, -30.0], [0.0, 0.0, 0.0, 1.0]]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.readings = Path(self.tmp.name) / "gaze-last-run.json"
        self.sent = []
        self.session = gaze_worker.Session(self.sent.append, self.readings)
        landmarks, _ = face(iris=(0.1, 0.05))
        landmarks = [(x + 0.0001 * i, y - 0.0001 * i, z) for i, (x, y, z) in enumerate(landmarks)]  # every point its own
        landmarks[234], landmarks[454] = (0.36, 0.5, 0.0), (0.64, 0.5, 0.0)  # the sides of the face: 0.28 of the width
        self.landmarks = landmarks
        points = [SimpleNamespace(x=x, y=y, z=z) for x, y, z in landmarks]
        scores = [SimpleNamespace(category_name=name, score=round(0.01 * (k + 1), 2)) for k, name in enumerate(EYE_SCORES)]
        scores += [SimpleNamespace(category_name="jawOpen", score=0.5)]  # not an eye's, so not kept
        matrix = SimpleNamespace(tolist=lambda: self.TRANSFORM)
        self.result = SimpleNamespace(face_landmarks=[points], face_blendshapes=[scores],
                                      facial_transformation_matrixes=[matrix])

    def seen(self):
        return gaze_worker.observation_from(self.result, 16 / 9, width=960)

    def run_of(self, *at):
        """Collect one dot, a frame at each time in `at`, then fit; the file as it was written."""
        self.session.handle({"cmd": "guide", "on": True})
        self.session.see(at[0] - 1.0, self.seen())  # the guide's frame starts the run's clock
        self.session.handle({"cmd": "guide", "on": False})
        self.session.handle({"cmd": "collect", "uv": [0.3, 0.3]})
        for t in at:
            self.session.see(t, self.seen())
        self.session.handle({"cmd": "fit"})
        return json.loads(self.readings.read_text())

    def test_each_row_says_when_it_came_on_the_frames_clock(self):
        kept = self.run_of(10.0, 10.25, 10.5)
        self.assertEqual(kept["row_ms"], [1000, 1250, 1500])
        self.assertEqual([frame[0] for frame in kept["frames"] if frame[1] == 1], kept["row_ms"])

    def test_each_row_keeps_the_fourteen_eye_scores_by_name(self):
        kept = self.run_of(10.0)
        self.assertEqual(kept["eye_score_names"], EYE_SCORES)
        self.assertEqual(kept["eye_scores"], [[round(0.01 * (k + 1), 2) for k in range(14)]])

    def test_each_row_keeps_the_heads_whole_transform_row_by_row(self):
        kept = self.run_of(10.0)
        self.assertEqual(kept["transforms"], [[value for line in self.TRANSFORM for value in line]])

    def test_each_row_keeps_how_wide_the_face_was_in_pixels(self):
        kept = self.run_of(10.0)
        across = [x for x, _, _ in self.landmarks]
        self.assertAlmostEqual(kept["face_px"][0], (max(across) - min(across)) * 960, places=1)

    def test_each_row_keeps_the_eyes_outlines_and_irises_as_shares_of_the_picture(self):
        kept = self.run_of(10.0)
        expected = RIGHT_EYE_OUTLINE + [468, 469, 470, 471, 472] + LEFT_EYE_OUTLINE + [473, 474, 475, 476, 477]
        self.assertEqual(kept["eye_point_landmarks"], expected)
        self.assertEqual(len(kept["eye_points"][0]), 2 * len(expected))  # x then y of each
        for at, landmark in enumerate(expected):
            self.assertAlmostEqual(kept["eye_points"][0][2 * at], self.landmarks[landmark][0], delta=1e-5)
            self.assertAlmostEqual(kept["eye_points"][0][2 * at + 1], self.landmarks[landmark][1], delta=1e-5)

    def test_a_row_with_none_of_it_says_so_rather_than_making_it_up(self):
        self.session.handle({"cmd": "collect", "uv": [0.3, 0.3]})
        self.session.see(1.0, gaze_worker.Observation([0.0] * gaze_model.FEATURE_COUNT, 0.9))  # as the fake face is
        self.session.handle({"cmd": "fit"})
        kept = json.loads(self.readings.read_text())
        self.assertEqual((kept["row_ms"], kept["eye_scores"], kept["transforms"], kept["face_px"], kept["eye_points"],
                          kept["boxes"]), ([0], [None], [None], [None], [None], [None]))

    def test_each_row_keeps_the_faces_box_as_the_guide_sees_it(self):
        # So that the next real run can show how near the edges a face that reads well comes, for the guide's limit.
        kept = self.run_of(10.0)
        box = self.seen().box
        self.assertEqual(kept["boxes"], [[round(edge, 4) for edge in box]])
        self.assertEqual(len(kept["boxes"][0]), 4)  # left, top, right and bottom, as shares of the picture
        self.assertAlmostEqual(kept["boxes"][0][0], 0.36, places=4)  # the side of the face, at 234
        self.assertAlmostEqual(kept["boxes"][0][2], 0.64, places=4)

    def test_the_fake_faces_box_is_kept_too(self):
        self.session.handle({"cmd": "collect", "uv": [0.3, 0.3]})
        self.session.see(1.0, gaze_worker.FakeDetector().look(b""))
        self.session.handle({"cmd": "fit"})
        self.assertEqual(json.loads(self.readings.read_text())["boxes"], [list(gaze_worker.FAKE_BOX)])

    def test_a_number_that_isnt_one_is_none_and_the_file_is_still_written(self):
        # JSON has no not-a-number, and the file is written whole or not at all: one would lose the run.
        nan, inf = float("nan"), float("inf")
        self.result.face_blendshapes[0][0].score = nan  # eyeLookUpLeft
        points = self.result.face_landmarks[0]
        points[7] = SimpleNamespace(x=nan, y=0.45, z=0.0)  # on the right eye's outline, which the fit doesn't read
        points[10] = SimpleNamespace(x=0.5, y=inf, z=0.0)  # the top of the forehead
        kept = self.run_of(10.0)
        self.assertIsNone(kept["eye_scores"][0][0])
        self.assertEqual(kept["eye_scores"][0][1:], [round(0.01 * (k + 1), 2) for k in range(1, 14)])
        at = gaze_worker.EYE_POINTS.index(7)
        self.assertEqual(kept["eye_points"][0][2 * at:2 * at + 2], [None, 0.45])
        self.assertIsNone(kept["face_px"][0])  # a face with a point nowhere has no width, and no box
        self.assertIsNone(kept["boxes"][0])
        self.assertEqual(len(kept["rows"]), 1)

    def test_a_transform_that_isnt_all_numbers_keeps_what_is(self):
        self.TRANSFORM = [list(line) for line in self.TRANSFORM]
        self.TRANSFORM[3][0] = float("nan")  # the bottom line, which the fit doesn't read
        kept = self.run_of(10.0)
        self.assertIsNone(kept["transforms"][0][12])
        self.assertEqual(kept["transforms"][0][:12], [value for line in self.TRANSFORM[:3] for value in line])

    def test_every_list_has_one_entry_for_each_row_and_holds_numbers_only(self):
        kept = self.run_of(10.0, 10.1, 10.2, 10.3)
        for key in ("rows", "targets", "held_back", "row_ms", "eye_scores", "transforms", "face_px", "eye_points",
                    "boxes"):
            self.assertEqual(len(kept[key]), 4, key)

        def numbers_only(value):
            if isinstance(value, list):
                return all(numbers_only(v) for v in value)
            return value is None or type(value) in (int, float, bool)

        for key in ("rows", "row_ms", "eye_scores", "transforms", "face_px", "eye_points", "boxes"):
            self.assertTrue(numbers_only(kept[key]), key)


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

    def test_what_the_command_says_is_read_and_only_trouble_is_passed_on(self):
        code = ("import sys; e = sys.stderr\n"
                "e.write('[info] Input #0, avfoundation, from 1:none:\\n')\n"
                "e.write('[info]   Stream #0:0: Video: rawvideo (UYVY / 0x59565955), uyvy422, 1280x720, 30 fps\\n')\n"
                "e.write('[avfoundation @ 0x1] [error] The camera is busy\\n'); e.flush()\n"
                "sys.stdout.buffer.write(bytes(36)); sys.stdout.buffer.flush()\n")
        found = []
        with contextlib.redirect_stderr(io.StringIO()) as said:
            source = gaze_worker.FrameSource([sys.executable, "-c", code], 4, 3, on_camera=found.append)
            self.assertEqual(len(source.read()), 36)
            source.close()  # all that it said has been read by the time this returns
        self.assertEqual(found, [{"width": 1280, "height": 720, "fps": 30.0}])
        self.assertEqual(said.getvalue(), "[avfoundation @ 0x1] The camera is busy\n")


# The worker with a face model that takes a second to load, and a spy where the camera would be opened.
SLOW_LOAD = """\
import sys, time
from gazevoice import gaze_worker

class SlowFake(gaze_worker.FakeDetector):
    def __init__(self):
        time.sleep(float(sys.argv[2]))
        super().__init__()

class OpenedSpy(gaze_worker.FrameSource):
    def __init__(self, *args):
        open(sys.argv[1], "w").write("the camera was opened")
        super().__init__(*args)

gaze_worker.FakeDetector, gaze_worker.FrameSource = SlowFake, OpenedSpy
sys.exit(gaze_worker.main(["--serve", "--fake", "--source-argv", sys.argv[3]]))
"""


class StopDuringLoadTest(unittest.TestCase):
    """A stop that arrives while the face model is still loading must not open the camera, not even briefly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opened = Path(self.tmp.name) / "opened"
        one_frame = "import sys, time; sys.stdout.buffer.write(bytes(960 * 540 * 3)); sys.stdout.buffer.flush(); time.sleep(60)"
        self.camera = json.dumps([sys.executable, "-c", one_frame])

    def worker(self, load_seconds: float):
        import subprocess

        env = dict(os.environ, PYTHONPATH=str(WORKER.parents[1]))
        proc = subprocess.Popen(
            [sys.executable, "-c", SLOW_LOAD, str(self.opened), str(load_seconds), self.camera],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            start_new_session=True,
        )
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)  # the worker leads its own group, and the stand-in camera is in it
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def test_closing_stdin_while_the_model_loads_never_opens_the_camera(self):
        proc = self.worker(1.0)
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertFalse(self.opened.exists())
        self.assertEqual(proc.stdout.read(), "")  # it never got as far as saying ready

    def test_without_a_stop_the_camera_is_opened_after_the_model_loads(self):
        proc = self.worker(0.3)
        self.assertEqual(json.loads(proc.stdout.readline())["ready"], True)
        self.assertTrue(self.opened.exists())
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=15), 0)


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
        # The worker rounds its stamps to the millisecond, so a brand new one can be just ahead of the clock.
        self.assertTrue(all(before - 1 <= s.t <= time.time() + 0.001 for s in samples))
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

    def test_the_screen_it_was_calibrated_on_comes_from_the_calibration(self):
        saved = a_calibration()
        self.assertEqual(self.make(calibration=saved).screen, Area(0, 38, 1710, 1074))
        self.assertIsNone(self.make(calibration=None).screen)
        for bad in ({"width": 1840}, {**SCREEN, "width": 0}, "big", None, [1840, 1100]):
            self.assertIsNone(self.make(calibration={**a_calibration(), "screen": bad}).screen, bad)
        self.assertEqual(screen_of(saved), Area(0, 38, 1710, 1074))
        self.assertIsNone(screen_of("not a calibration"))

    def test_loading_a_new_calibration_changes_the_screen_too(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        tracker.load({**a_calibration(), "screen": {"left": 5, "top": 6, "width": 1500, "height": 900}})
        self.assertEqual(tracker.screen, Area(5, 6, 1500, 900))

    def test_looking_far_off_the_screen_is_looking_away_and_is_ignored(self):
        self.assertEqual(LOOKING_AWAY_MARGIN, 0.1)
        tracker = self.make()

        def keep(u, v, confidence=0.9):
            tracker.track = GazeTrack()
            tracker._keep({"t": 5.0, "uv": [u, v], "confidence": confidence})
            return [s.confidence for s in tracker.track.between(0, math.inf)]

        self.assertEqual(keep(0.5, 0.5), [0.9])
        self.assertEqual(keep(0.0, 1.0), [0.9])  # the edges of the viewport
        self.assertEqual(keep(-0.1, 1.1), [0.9])  # and a little past them, which is just noise in the estimate
        for u, v in ((1.2, 0.5), (-0.2, 0.5), (0.5, 1.5), (0.5, -0.11), (3.0, 3.0)):
            self.assertEqual(keep(u, v), [0.0], (u, v))  # kept as a sample, but with no confidence: not clamped to an edge

    def test_only_a_confident_reading_off_the_screen_is_marked_as_away(self):
        tracker = self.make()

        def kept(u, v, confidence):
            tracker.track = GazeTrack()
            tracker._keep({"t": 5.0, "uv": [u, v], "confidence": confidence})
            (sample,) = tracker.track.between(0, math.inf)
            return sample.away, sample.confidence

        self.assertEqual(kept(0.5, 0.5, 0.9), (False, 0.9))
        self.assertEqual(kept(0.5, 0.5, 0.0), (False, 0.0))  # a blink: no confidence, but it isn't off the screen
        for u, v in ((1.4, 0.5), (0.5, -0.3), (2.0, 2.0)):
            self.assertEqual(kept(u, v, 0.9), (True, 0.0), (u, v))
        self.assertEqual(kept(-0.1, 1.1, 0.9), (False, 0.9))  # inside the margin: noise, not away
        # With no face the worker says the last place again at confidence 0. That is a lost face, not a look away.
        self.assertEqual(kept(2.0, 0.5, 0.0), (False, 0.0))

    def test_one_look_off_the_screen_then_a_lost_face_is_not_reading_off_the_screen(self):
        tracker = self.make()
        tracker._keep({"t": 100.0, "uv": [2.0, 0.5], "confidence": 0.9})  # one real glance away
        for i in range(1, 30):  # then no face: the worker repeats that place at confidence 0
            tracker._keep({"t": 100.0 + i * 0.02, "uv": [2.0, 0.5], "confidence": 0.0})
        words = [{"text": "this", "start": 0.4, "end": 0.6}]  # the clip began at 100.0, so the window takes in all 30
        grounding = ground(words, 100.0, tracker.track, DEFAULT_RADIUS, lambda points: None, lambda c: None)
        (window,) = grounding.windows
        self.assertEqual((window.readings, window.good, window.off_screen), (30, 0, 1))
        self.assertFalse(mostly_off_screen(grounding.windows))

    def test_a_reading_that_is_off_the_viewport_cannot_become_a_fixation(self):
        tracker = self.make()
        for i in range(10):
            tracker._keep({"t": 5.0 + i * 0.03, "uv": [2.0, 0.5], "confidence": 0.9})
        self.assertIsNone(fixate(tracker.track.between(0, math.inf)))  # no edge pick comes from looking away

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

    def test_ffmpegs_reason_comes_before_the_workers_last_word_and_its_chatter_is_left_out(self):
        code = ("import sys; e = sys.stderr\n"
                "for n in range(20): e.write(f'[info] chatter {n}\\n')\n"
                "for n in range(40): e.write(f'[rawvideo @ 0x2] [warning] non monotonically increasing dts {n}\\n')\n"
                "e.write('[avfoundation @ 0x1] [error] Selected framerate (30.000000) is not supported by the device.\\n')\n"
                "e.write('[fatal] Error opening input files: Input/output error\\n'); sys.exit(1)\n")
        tracker = self.make(args=["--fake", "--source-argv", json.dumps([sys.executable, "-c", code])])
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=20)
        message = str(caught.exception)
        self.assertIn("is not supported by the device", message)
        self.assertIn("Error opening input files", message)
        self.assertNotIn("chatter", message)
        self.assertNotIn("dts", message)
        self.assertLess(message.index("Error opening input files"), message.index("never sent a picture"))

    def test_the_worker_keeps_what_the_camera_really_sends(self):
        readings = Path(self.tmp.name) / "gaze-last-run.json"
        script = Path(self.tmp.name) / "camera.py"
        script.write_text("import sys, time\n"
                          "sys.stderr.write('[info]   Stream #0:0: Video: rawvideo, uyvy422, 1920x1080, 30 fps\\n')\n"
                          "sys.stderr.flush()\n" + STAND_IN_CAMERA.split("\n", 2)[2])
        args = [*FAKE, "--source-argv", json.dumps([sys.executable, str(script), str(Path(self.tmp.name) / "pid")]),
                "--readings-file", str(readings)]
        tracker = self.make(calibration=None, args=args)
        tracker.start(timeout=20)
        tracker.collect((0.4, 0.4))
        time.sleep(0.4)
        tracker.stop_collect()
        with contextlib.suppress(GazeError):  # too short to fit; the file is written either way
            tracker.fit(timeout=20)
        self.assertEqual(json.loads(readings.read_text())["camera"], {"width": 1920, "height": 1080, "fps": 30.0})

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

    def test_a_calibration_that_reads_where_the_head_sits_is_refused_and_says_to_calibrate_again(self):
        tracker = self.make(calibration=an_old_calibration())
        with self.assertRaises(GazeError) as caught:
            tracker.start(timeout=20)
        message = str(caught.exception)
        self.assertIn("numbers gaze no longer uses", message)  # the worker's reason, passed on
        self.assertIn("Calibrate gaze again", message)
        self.assertEqual(message.count("Calibrate gaze again"), 1)  # said once, not once by the worker and again by the tracker
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
        result = tracker.fit(timeout=20, screen_width_cm=29.05)
        gaze_model.validate(result["fit"])
        self.assertEqual(result["screen_width_cm"], 29.05)  # the width crossed the pipe to the worker
        self.assertEqual(result["fit"]["screen_width_cm"], 29.05)
        self.assertEqual(set(result["check"]) >= {"median", "p90", "radius"}, True)
        self.assertEqual(len(result["check"]["radius"]), 2)
        self.assertGreater(result["rows"], 40)
        self.assertGreater(result["check_rows"], 5)
        self.assertTrue(result["held_out"])
        self.assertTrue(tracker.alive)

    def test_the_worker_keeps_the_numbers_when_told_where(self):
        readings = Path(self.tmp.name) / "gaze-last-run.json"
        tracker = self.make(calibration=None, args=[*FAKE, "--readings-file", str(readings)])
        tracker.start(timeout=20)
        tracker.collect((0.4, 0.4))
        time.sleep(0.5)  # too short to fit, which is when the numbers matter most
        tracker.stop_collect()
        with self.assertRaises(GazeError):
            tracker.fit(timeout=20)
        self.assertGreater(len(json.loads(readings.read_text())["rows"]), 0)

    def test_the_worker_keeps_each_frame_of_the_guide_and_the_dots_too(self):
        readings = Path(self.tmp.name) / "gaze-last-run.json"
        tracker = self.make(calibration=None, args=[*FAKE, "--readings-file", str(readings)])
        tracker.start(timeout=20)
        tracker.guide(True)
        time.sleep(0.4)
        tracker.guide(False)
        for uv in ((0.4, 0.4), (0.6, 0.6)):
            tracker.collect(uv)
            time.sleep(0.4)
            tracker.stop_collect()
        with contextlib.suppress(GazeError):  # two short dots won't fit; the file is written whether or not they do
            tracker.fit(timeout=20)
        kept = json.loads(readings.read_text())
        dots = [frame[1] for frame in kept["frames"]]
        self.assertEqual(dots, sorted(dots))  # the guide's frames, then dot 1's, then dot 2's
        self.assertEqual(sorted(set(dots)), [0, 1, 2])
        times = [frame[0] for frame in kept["frames"]]
        self.assertEqual(times[0], 0)
        self.assertEqual(times, sorted(times))
        self.assertLessEqual({frame[2] for frame in kept["frames"]}, {"face", "shut"})
        self.assertEqual(kept["counts"]["frames"], len([d for d in dots if d]))

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

    def test_before_the_guide_there_is_no_place(self):
        tracker = self.make()
        self.assertIsNone(tracker.place)
        tracker.start(timeout=20)
        time.sleep(0.3)
        self.assertIsNone(tracker.place)  # nothing was asked for

    def test_the_guide_brings_the_place_of_the_head_and_when_it_came(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        before = time.time()
        tracker.guide(True)
        self.assertTrue(wait_for(lambda: tracker.place is not None))
        place = tracker.place
        self.assertEqual(place[:4], (True, 0.5, 0.45, 30.0))  # the fake face sits where the outline is
        self.assertEqual(place.box, gaze_worker.FAKE_BOX)  # with the whole face in view
        self.assertTrue(before <= place.at <= time.time())
        self.assertTrue(wait_for(lambda: tracker.place.at > place.at))  # and it is kept up to date

    def test_turning_the_guide_off_stops_the_readings(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        tracker.guide(True)
        self.assertTrue(wait_for(lambda: tracker.place is not None))
        tracker.guide(False)
        time.sleep(0.3)  # one that was already on its way may still land
        arrived = tracker.place.at
        time.sleep(0.4)
        self.assertEqual(tracker.place.at, arrived)

    def test_the_readings_never_pile_up_among_the_replies_a_command_waits_for(self):
        tracker = self.make(calibration=None)
        tracker.start(timeout=20)
        tracker.guide(True)
        self.assertTrue(wait_for(lambda: tracker.place is not None))
        time.sleep(0.5)
        tracker.guide(False)
        time.sleep(0.3)
        self.assertTrue(tracker._replies.empty())  # a reading is not a reply, and nothing reads them off this queue

    def test_with_no_face_the_place_says_so(self):
        tracker = self.make()
        tracker._keep_guide({"found": False})
        self.assertEqual(tracker.place[:4], (False, None, None, None))

    BOX = [0.36, 0.12, 0.64, 0.86]

    def test_the_place_keeps_the_faces_box(self):
        tracker = self.make()
        tracker._keep_guide({"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": self.BOX})
        self.assertEqual(tracker.place.box, tuple(self.BOX))
        tracker._keep_guide({"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": [-0.05, 0.1, 0.6, 1.1]})
        self.assertEqual(tracker.place.box, (-0.05, 0.1, 0.6, 1.1))  # past the edges is a real reading: out of view

    def test_a_reading_that_makes_no_sense_is_dropped_and_the_last_good_one_stays(self):
        tracker = self.make()
        box = self.BOX
        tracker._keep_guide({"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": box})
        good = tracker.place
        for junk in ({"found": "yes", "x": 0.5, "y": 0.4, "distance": 30.0, "box": box},
                     {"found": True, "x": "left", "y": 0.4, "distance": 30.0, "box": box},
                     {"found": True, "x": 0.5, "y": 0.4, "box": box},
                     {"found": True, "x": float("nan"), "y": 0.4, "distance": 30.0, "box": box},
                     {"found": True, "x": 0.5, "y": float("inf"), "distance": 30.0, "box": box},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 0.0, "box": box},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": -5.0, "box": box},
                     {"found": True, "x": True, "y": 0.4, "distance": 30.0, "box": box},
                     # with no box, the guide couldn't tell a face cut off by the edge of the picture
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": None},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": box[:3]},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": [0.3, 0.1, "right", 0.9]},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": [0.3, 0.1, float("nan"), 0.9]},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": [0.3, True, 0.7, 0.9]},
                     {"found": True, "x": 0.5, "y": 0.4, "distance": 30.0, "box": "box"},
                     "found", None, {}):
            tracker._keep_guide(junk)
            self.assertEqual(tracker.place, good, junk)

    def test_the_place_can_be_read_from_another_thread_while_it_changes(self):
        tracker = self.make()
        stop = threading.Event()
        seen = []

        def writer():
            i = 0
            while not stop.is_set():
                i += 1
                tracker._keep_guide({"found": True, "x": i, "y": i, "distance": float(i), "box": [0.3, 0.1, 0.7, 0.9]})

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(2000):
                place = tracker.place
                if place is not None:
                    seen.append(place)
        finally:
            stop.set()
            thread.join()
        self.assertTrue(seen)
        self.assertTrue(all(isinstance(p, Place) and p.x == p.y == p.distance for p in seen))  # never half of one and half of another

    def test_asking_for_the_guide_of_a_worker_that_has_gone_says_so(self):
        tracker = self.make()
        with self.assertRaises(GazeError):
            tracker.guide(True)

    def test_the_worker_help_works_without_the_camera_libraries(self):
        import subprocess

        done = subprocess.run([sys.executable, str(WORKER), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertIn("--serve", done.stdout)


if __name__ == "__main__":
    unittest.main()
