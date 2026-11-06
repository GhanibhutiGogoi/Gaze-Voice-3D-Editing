import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice.components import build_components
from gazevoice.components.base import ComponentState, Health
from gazevoice.components.gaze import (
    CALIBRATE_CONFIRM,
    CALIBRATION_DOTS,
    CHECK_DOTS,
    DOT_SECONDS,
    MAX_MEDIAN_MISS,
    MEDIAPIPE,
    MODEL_SHA256,
    MODEL_SIZE,
    MODEL_URL,
    SETTLE_SECONDS,
    TEST_SECONDS,
    WORKER,
    Gaze,
    load_calibration,
    save_calibration,
)
from gazevoice.components.microphone import LIST_DEVICES
from gazevoice.jobs import JobError, JobState
from gazevoice.probes import ProbeTimedOut
from gazevoice.server.app import ControlCenter
from gazevoice.settings import Settings, update_settings
from gazevoice.voice.gaze import GazeSample, GazeTrack
from gazevoice.voice.gaze_tracker import GazeError, GazeTracker, radius_of
from gazevoice.voice.wiring import voice_readiness
from gazevoice.workflow import workflow_stages

from .helpers import TempCtx, make_job
from .test_bridge_protocol import FakeScene, protocol
from .test_download import FakeResponse
from .test_gaze_tracker import a_calibration

UV = {"uv": "/Users/me/.local/bin/uv"}
FFMPEG = {"ffmpeg": "/opt/homebrew/bin/ffmpeg"}


def listing(*cameras):
    lines = ["[AVFoundation indev @ 0x1] AVFoundation video devices:"]
    lines += [f"[AVFoundation indev @ 0x1] [{i}] {name}" for i, name in enumerate(cameras)]
    lines += ["[AVFoundation indev @ 0x1] AVFoundation audio devices:",
              "[AVFoundation indev @ 0x1] [0] MacBook Air Microphone", ": Input/output error"]
    return "\n".join(lines) + "\n"


MAC = listing("MacBook Air Camera", "Capture screen 0")
PHONE_FIRST = listing("Nokia Unbreakable V Camera", "MacBook Air Camera", "MacBook Air Desk View Camera", "Capture screen 0")


class FakeBlender:
    """The bridge as the add-on answers it (the real protocol over a fake scene), standing in for BridgeClient.

    Every gaze_view and gaze_show is also written into `events`, so a test can see the order things happened in.
    The add-on draws gaze dots and rings only while the app is polling it (EventQueue.live), so this notes in
    `dark` every moment something was meant to be on screen and wasn't, because nobody had polled for too long.
    """

    def __init__(self, events, clock):
        self.events = events
        self.scene = FakeScene()
        self.queue = protocol.EventQueue(clock=clock)
        self.dark = []
        self.polls = []  # when the app polled
        self.clock = clock
        self.down = False  # Blender is closed: connections are refused
        self.old = False  # an add-on from before gaze: it doesn't know the gaze commands
        self.rejected = []  # what the protocol turned away, which would be a bug in the unit
        self.shows_before_down = None  # Blender goes away after this many drawings

    def request(self, request, timeout=None):
        if self.down:
            raise ConnectionRefusedError("Blender is closed")
        command = request["cmd"]
        if self.old and command.startswith("gaze_"):
            return {"ok": False, "error": "unknown command"}
        reply = protocol.handle(request, self.scene, self.queue)
        if command == "poll":
            self.polls.append(self.clock())
        if command == "gaze_view":
            self.events.append(("view",))
        elif command == "gaze_show":
            if reply.get("ok"):
                dot, ring, _marks = self.scene.shown[-1]
                self.events.append(("show", dot, ring))
                self.watch()
                if self.shows_before_down is not None:
                    self.shows_before_down -= 1
                    self.down = self.shows_before_down < 0
            else:
                self.rejected.append(reply)
        return reply

    def watch(self):
        """Called as time passes: is anything on the viewport that the add-on would not be drawing?"""
        if self.scene.shown:
            dot, ring, _marks = self.scene.shown[-1]
            if (dot or ring) and not self.queue.live():
                self.dark.append((dot, ring))

    def close(self):
        pass


class FakeTracker:
    """The gaze worker's end of the conversation, writing what it is asked into `events`."""

    def __init__(self, events, camera, calibration, fit_reply, ctx=None):
        self.events, self.camera, self.calibration = events, camera, calibration
        self.fit_reply = fit_reply
        self.ctx = ctx
        self.held_by = None
        self.track = GazeTrack()
        self.radius = (calibration or {}).get("check", {}).get("radius", (0.1, 0.15))
        self.fail = {}  # method name -> the GazeError it raises

    def _go(self, name):
        if name in self.fail:
            raise GazeError(self.fail[name])

    def start(self, timeout=60.0):
        self.events.append(("start", self.camera, None if self.calibration is None else "loaded"))
        self.held_by = self.ctx.camera.holder  # who the unit says has the camera while it is open
        self._go("start")

    def collect(self, uv, check=False):
        self.events.append(("collect", tuple(uv), check))
        self._go("collect")

    def stop_collect(self):
        self.events.append(("stop_collect",))

    def fit(self, timeout=30.0):
        self.events.append(("fit",))
        self._go("fit")
        return self.fit_reply

    def stop(self):
        self.events.append(("stop",))


def fit_reply(median=0.05, held_out=True):
    return {"fit": a_calibration()["model"], "check": {"median": median, "p90": median * 2, "radius": [0.06, 0.1]},
            "rows": 120, "check_rows": 40 if held_out else 0, "held_out": held_out}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


VIEW = {"width": 1840, "height": 1100}


class GazeCase(unittest.TestCase):
    """A Gaze unit with fakes in place of Blender, the camera and the clock, so nothing is slow or real."""

    def setUp(self):
        self.t = TempCtx(which={**UV, **FFMPEG})
        self.t.runner.add(LIST_DEVICES, stderr=MAC)
        self.events = []
        self.clock = FakeClock(1000.0)
        self.blender = FakeBlender(self.events, self.clock)
        self.gaze = Gaze(self.t.ctx)
        self.gaze.bridge_factory = lambda: self.blender
        self.gaze.tracker_factory = self.make_tracker
        self.gaze.sleep = self.pass_time
        self.gaze.clock = self.clock
        self.tracker = None  # the one the unit built, if it did
        self.reply = fit_reply()
        self.venv = self.t.paths.venvs / "gaze"
        self.py = self.venv / "bin" / "python"

    def tearDown(self):
        self.t.cleanup()

    def make_tracker(self, camera, calibration):
        self.tracker = FakeTracker(self.events, camera, calibration, self.reply, self.t.ctx)
        return self.tracker

    def pass_time(self, seconds):
        self.events.append(("wait", seconds))
        self.clock.now += seconds
        self.blender.watch()

    def install(self, importable=True, model=True):
        self.py.parent.mkdir(parents=True)
        self.py.write_text("")
        self.t.runner.add([str(self.py), "-c", "import mediapipe"], returncode=0 if importable else 1)
        if model:
            self.t.paths.gaze_model_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.t.paths.gaze_model_file, "wb") as handle:
                handle.truncate(MODEL_SIZE)

    def calibrate(self, calibration=None):
        self.t.paths.gaze_calibration.parent.mkdir(parents=True, exist_ok=True)
        self.t.paths.gaze_calibration.write_text(json.dumps(calibration or a_calibration()))

    def calibrated_for(self, view=VIEW, radius=(0.07, 0.11)):
        """Save a calibration the way Calibrate does, for a viewport of this size."""
        saved = {**a_calibration(radius), "view": dict(view), "when": 1.0}
        self.calibrate(saved)
        return saved


class GazeUnitTest(GazeCase):

    # --- what the unit says --------------------------------------------------------------------------
    def test_it_is_a_unit_on_the_input_shelf_after_speech(self):
        self.assertEqual((Gaze.id, Gaze.name, Gaze.role), ("gaze", "Gaze", "Sees where you're looking, on this Mac"))
        ids = [c.id for c in build_components(self.t.ctx)]
        self.assertEqual(ids[ids.index("speech") + 1], "gaze")

    def test_gaze_is_optional_so_voice_and_the_strip_never_wait_on_it(self):
        states = {cid: ComponentState(cid, cid, "", Health.READY, "") for cid in
                  ("microphone", "speech", "claude", "codex", "blender", "mcp_addon", "mcp_server", "bridge")}
        sources = {"refiner": "claude", "editor": "claude"}
        before = workflow_stages(states, sources, Settings())
        states["gaze"] = ComponentState("gaze", "Gaze", "", Health.ERROR, "No camera found.")
        self.assertEqual(voice_readiness(states, sources, Settings()), [])
        self.assertEqual(workflow_stages(states, sources, Settings()), before)
        self.assertNotIn("gaze", [part for stage in before for part in stage["parts"]])

    def test_needs_uv(self):
        self.t.runner.set_which("uv", None)
        state = self.gaze.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("uv", state.summary)

    def test_not_installed_offers_install(self):
        state = self.gaze.check()
        self.assertEqual(state.health, Health.MISSING)
        install = next(a for a in state.actions if a.id == "install")
        self.assertIn("340 MB", install.confirm)

    def test_mediapipe_that_wont_import_is_not_installed(self):
        self.install(importable=False)
        state = self.gaze.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_a_timed_out_import_check_is_not_reported_as_missing(self):
        self.install()
        self.t.runner.add([str(self.py), "-c", "import mediapipe"], returncode=124, stderr="timed out")
        with self.assertRaises(ProbeTimedOut):
            self.gaze.check()

    def test_a_missing_or_cut_short_model_file_offers_install_again(self):
        self.install(model=False)
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("face model", state.summary)
        self.assertTrue(state.offers("install"))
        self.t.paths.gaze_model_file.parent.mkdir(parents=True, exist_ok=True)
        self.t.paths.gaze_model_file.write_bytes(b"cut short")
        self.assertEqual(self.gaze.check().health, Health.INSTALLED)

    def test_installed_but_uncalibrated_says_to_calibrate(self):
        self.install()
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("alibrat", state.summary)
        self.assertFalse(state.offers("install"))  # nothing is left to install
        self.assertEqual(state.detail("Using"), "MacBook Air Camera (auto)")

    def test_a_calibration_from_another_version_says_to_calibrate_again(self):
        self.install()
        self.calibrate({**a_calibration(), "model": {"kind": "ridge", "features": 3}})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("out of date", state.summary)

    def test_ready_shows_the_measured_accuracy(self):
        self.install()
        self.calibrate({**a_calibration(), "check": {"median": 0.042, "p90": 0.091, "radius": [0.07, 0.11]}})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("4%", state.summary)
        self.assertIn("9%", state.detail("Accuracy"))
        self.assertIn("MacBook Air Camera", state.summary)

    def test_a_calibration_without_a_check_is_still_ready(self):
        self.install()
        self.calibrate({"model": a_calibration()["model"]})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIsNone(state.detail("Accuracy"))

    def test_no_camera_says_so(self):
        self.install()
        self.calibrate()
        self.t.runner.add(LIST_DEVICES, stderr=listing())
        state = self.gaze.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("No camera", state.summary)
        self.assertIn("Routing", state.summary)

    def test_only_a_phone_means_pick_a_camera_in_routing(self):
        self.install()
        self.calibrate()
        self.t.runner.add(LIST_DEVICES, stderr=listing("Nokia Unbreakable V Camera", "Capture screen 0"))
        state = self.gaze.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("pick a camera in Routing", state.summary)
        self.assertEqual(state.detail("Cameras"), "Nokia Unbreakable V Camera")  # Routing can offer it

    def test_a_chosen_camera_that_is_gone_says_so(self):
        self.install()
        self.calibrate()
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"camera_device": "Logitech BRIO"})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("Logitech BRIO", state.summary)
        self.assertIn("Routing", state.summary)

    def test_a_chosen_camera_is_used_by_name_wherever_it_is_numbered(self):
        self.install()
        self.calibrate()
        self.t.runner.add(LIST_DEVICES, stderr=PHONE_FIRST)
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"camera_device": "Nokia Unbreakable V Camera"})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.detail("Using"), "Nokia Unbreakable V Camera")

    def test_the_menu_lists_cameras_but_not_screens_or_desk_view(self):
        self.t.runner.add(LIST_DEVICES, stderr=PHONE_FIRST)
        state = self.gaze.check()  # even before it is installed, so Routing can already offer them
        self.assertEqual(state.detail("Cameras"), "Nokia Unbreakable V Camera | MacBook Air Camera")

    def test_without_ffmpeg_it_says_where_to_get_it(self):
        self.install()
        self.calibrate()
        self.t.runner.set_which("ffmpeg", None)
        state = self.gaze.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("ffmpeg", state.summary)

    def test_checking_only_lists_cameras_and_never_opens_one(self):
        self.install()
        self.calibrate()
        self.assertEqual(self.gaze.check().health, Health.READY)
        self.gaze.check()
        calls = set(self.t.runner.calls)
        self.assertEqual(calls, {tuple(LIST_DEVICES), (str(self.py), "-c", "import mediapipe")})
        self.assertFalse([c for c in calls if "-framerate" in c or str(WORKER) in c])

    def test_the_camera_list_is_the_microphone_units_cached_listing(self):
        self.install()
        self.calibrate()
        self.gaze.check()
        self.gaze.check()
        self.assertEqual(self.t.runner.calls.count(tuple(LIST_DEVICES)), 1)

    # --- calibrating: what the card offers -----------------------------------------------------------
    def test_calibrating_is_offered_once_gaze_is_installed_and_asks_before_opening_the_camera(self):
        self.install()
        state = self.gaze.check()
        calibrate = next(a for a in state.actions if a.id == "calibrate")
        self.assertEqual(calibrate.confirm, "Opens the camera for about 40 seconds. Look at each dot in Blender.")
        self.assertEqual(CALIBRATE_CONFIRM, calibrate.confirm)
        self.assertFalse(state.offers("test"))  # there is nothing to test yet

    def test_a_calibration_from_another_version_can_be_made_again(self):
        self.install()
        self.calibrate({**a_calibration(), "model": {"kind": "ridge", "features": 3}})
        self.assertTrue(self.gaze.check().offers("calibrate"))

    def test_a_calibrated_unit_offers_to_calibrate_again_and_to_test_and_asks_before_each(self):
        self.install()
        self.calibrated_for()
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual([a.id for a in state.actions], ["calibrate", "test"])
        for action in state.actions:
            self.assertIn("camera", action.confirm.lower(), action.id)
        self.assertIn("again", state.actions[0].label.lower())

    def test_nothing_is_offered_while_there_is_no_camera_or_nothing_installed(self):
        self.assertEqual(self.gaze.check().actions[0].id, "install")  # nothing to calibrate yet
        self.install()
        self.t.runner.add(LIST_DEVICES, stderr=listing())
        self.t.ctx.probes.clear()  # the first look listed a camera, and listings are kept for a while
        self.assertEqual(self.gaze.check().actions, ())

    def test_the_accuracy_is_said_as_a_share_of_the_viewport_in_plain_words(self):
        self.install()
        self.calibrate({**self.calibrated_for(), "check": {"median": 0.042, "p90": 0.091, "radius": [0.07, 0.11]}})
        state = self.gaze.check()
        self.assertIn("typically off by 4% of the viewport", state.summary)
        self.assertEqual(state.detail("Accuracy"), "Typically off by 4% of the viewport · 9 in 10 within 9%")

    def test_a_resized_viewport_needs_recalibrating(self):
        self.install()
        self.calibrated_for(view={"width": 1840, "height": 1100})
        self.assertEqual(self.gaze.check().health, Health.READY)  # Blender still shows the same size
        self.blender.scene.view = (1500, 1100)  # the sidebar was opened, or the window resized
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("viewport", state.summary)
        self.assertIn("different size", state.summary)
        self.assertIn("Calibrate", state.summary)
        self.assertTrue(state.offers("calibrate"))
        self.assertFalse(state.offers("test"))  # a ring on a resized viewport would point at the wrong place
        with self.assertRaises(JobError) as caught:
            self.gaze.run_action("test", make_job("gaze", "test"))
        self.assertIn("different size", str(caught.exception))
        self.assertIsNone(self.tracker)  # and the camera was never opened to find that out

    def test_a_resized_viewport_in_either_direction_counts(self):
        self.install()
        self.calibrated_for(view={"width": 1840, "height": 1100})
        for view in ((1840, 900), (2000, 1100), (1100, 1840)):
            self.blender.scene.view = view
            self.assertEqual(self.gaze.check().health, Health.INSTALLED, view)

    def test_when_blender_cant_answer_the_calibration_is_trusted(self):
        self.install()
        self.calibrated_for()
        for broken in ({"down": True}, {"old": True}):
            self.blender.down, self.blender.old = broken.get("down", False), broken.get("old", False)
            self.assertEqual(self.gaze.check().health, Health.READY, broken)
        self.blender.down = self.blender.old = False
        self.blender.scene.view = None  # Blender is open with no 3D viewport showing
        self.assertEqual(self.gaze.check().health, Health.READY)

    def test_a_calibration_that_doesnt_say_what_size_it_was_made_for_is_not_second_guessed(self):
        self.install()
        self.calibrate()  # no "view"
        self.blender.scene.view = (10, 10)
        self.assertEqual(self.gaze.check().health, Health.READY)
        self.assertNotIn(("view",), self.events)  # nothing to compare, so Blender wasn't asked

    def test_checking_never_opens_the_camera_or_draws_in_blender(self):
        self.install()
        self.calibrated_for()
        self.gaze.tracker_factory = lambda *args: self.fail("the camera was opened to check")
        self.gaze.check()
        self.assertEqual([e for e in self.events if e[0] != "view"], [])

    # --- installing ----------------------------------------------------------------------------------
    @staticmethod
    def opener(data, seen=None):
        def open_it(request, timeout):
            if seen is not None:
                seen.append(request.full_url)
            return FakeResponse(data)
        return open_it

    def fake_model(self, data=b"face model " * 1000):
        """Make `data` the file the download should deliver."""
        patches = [patch("gazevoice.components.gaze.MODEL_SIZE", len(data)),
                   patch("gazevoice.components.gaze.MODEL_SHA256", hashlib.sha256(data).hexdigest())]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return data

    def steps(self):
        return [
            ["uv", "venv", "--python", "3.12", str(self.venv)],
            ["uv", "pip", "install", "--python", str(self.py), "mediapipe==1.2.2"],
        ]

    def test_install_uses_its_own_venv_and_checks_the_download(self):
        data = self.fake_model()
        urls = []
        self.gaze.opener = self.opener(data, urls)
        for argv in self.steps():
            self.t.runner.add(argv, stdout="ok\n")
        result = self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertEqual(result, {"installed": True})
        self.assertEqual(MEDIAPIPE, "mediapipe==1.2.2")
        self.assertEqual(self.t.runner.calls, [tuple(s) for s in self.steps()])
        self.assertEqual(urls, [MODEL_URL])
        self.assertEqual(self.t.paths.gaze_model_file.read_bytes(), data)
        self.assertEqual([p.name for p in self.t.paths.gaze_model_file.parent.iterdir()], [self.t.paths.gaze_model_file.name])

    def test_install_leaves_alone_what_is_already_there(self):
        data = self.fake_model()
        self.install(model=False)
        self.t.paths.gaze_model_file.parent.mkdir(parents=True, exist_ok=True)
        self.t.paths.gaze_model_file.write_bytes(data)
        self.gaze.opener = self.opener(b"not fetched")
        pip = self.steps()[1]
        self.t.runner.add(pip)
        self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertEqual(self.t.runner.calls, [tuple(pip)])  # no new venv
        self.assertEqual(self.t.paths.gaze_model_file.read_bytes(), data)  # and no new download

    def test_a_failed_pip_install_stops_before_the_download(self):
        self.fake_model()
        self.gaze.opener = self.opener(b"never asked for")
        self.t.runner.add(self.steps()[0])
        self.t.runner.add(self.steps()[1], returncode=1, stdout="no matching distribution")
        with self.assertRaises(JobError) as caught:
            self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertIn("mediapipe", str(caught.exception))
        self.assertFalse(self.t.paths.gaze_model_file.exists())

    def test_install_without_uv_says_so(self):
        self.t.runner.set_which("uv", None)
        with self.assertRaises(JobError) as caught:
            self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertIn("uv", str(caught.exception))

    def test_a_download_with_the_wrong_checksum_is_refused(self):
        wanted = self.fake_model()
        self.gaze.opener = self.opener(b"X" * len(wanted))  # the right size, the wrong bytes
        for argv in self.steps():
            self.t.runner.add(argv)
        with self.assertRaises(JobError) as caught:
            self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertIn("checksum", str(caught.exception))
        self.assertIn("deleted", str(caught.exception))
        self.assertFalse(self.t.paths.gaze_model_file.exists())
        self.assertEqual(list(self.t.paths.gaze_model_file.parent.iterdir()), [])  # nothing half-saved either

    def test_a_download_of_the_wrong_size_is_refused(self):
        wanted = self.fake_model()
        self.gaze.opener = self.opener(wanted[:-10])
        for argv in self.steps():
            self.t.runner.add(argv)
        with self.assertRaises(JobError) as caught:
            self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertIn("size", str(caught.exception))
        self.assertFalse(self.t.paths.gaze_model_file.exists())

    def test_a_bad_file_already_in_place_is_replaced_not_trusted(self):
        data = self.fake_model()
        self.install(model=False)
        self.t.paths.gaze_model_file.parent.mkdir(parents=True, exist_ok=True)
        self.t.paths.gaze_model_file.write_bytes(b"Y" * len(data))  # the right size, so check() alone would pass it
        self.gaze.opener = self.opener(data)
        self.t.runner.add(self.steps()[1])
        self.gaze.run_action("install", make_job("gaze", "install"))
        self.assertEqual(self.t.paths.gaze_model_file.read_bytes(), data)

    def test_the_real_model_numbers_are_the_ones_from_the_brief(self):
        self.assertEqual(MODEL_SIZE, 3_758_596)
        self.assertEqual(MODEL_SHA256, "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff")
        self.assertEqual(MODEL_URL, "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/"
                                    "float16/latest/face_landmarker.task")


class SavedCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.path = self.t.paths.gaze_calibration

    def tearDown(self):
        self.t.cleanup()

    def test_a_good_file_loads(self):
        saved = a_calibration()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(saved))
        self.assertEqual(load_calibration(self.path), saved)

    def test_nothing_or_nonsense_loads_as_none(self):
        self.assertIsNone(load_calibration(self.path))  # no file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for text in ("", "not json", "[]", "{}", json.dumps({"model": {"kind": "ridge", "features": 2}})):
            self.path.write_text(text)
            self.assertIsNone(load_calibration(self.path), text)


DOTS = list(CALIBRATION_DOTS) + list(CHECK_DOTS)


class CalibrateTest(GazeCase):
    def setUp(self):
        super().setUp()
        self.install()

    def calibrate_now(self):
        return self.gaze.run_action("calibrate", make_job("gaze", "calibrate"))

    def saved(self):
        return json.loads(self.t.paths.gaze_calibration.read_text())

    def test_calibrate_shows_each_dot_and_collects_after_it_settles(self):
        job = make_job("gaze", "calibrate")
        result = self.gaze.run_action("calibrate", job)
        self.assertEqual((DOT_SECONDS, SETTLE_SECONDS), (2.0, 0.5))
        self.assertEqual((len(CALIBRATION_DOTS), len(CHECK_DOTS)), (9, 5))
        self.assertTrue(all(0 < u < 1 and 0 < v < 1 for u, v in DOTS))
        self.assertEqual(len(set(DOTS)), 14)  # the check dots are somewhere the fit was never shown
        expected = [("view",), ("start", "0", None)]  # the camera opens with no calibration loaded
        for number, uv in enumerate(DOTS):
            expected += [("show", list(uv), None), ("wait", SETTLE_SECONDS),  # the eyes get time to arrive
                         ("collect", uv, number >= 9),  # only then are faces kept, and the last five are held back
                         ("wait", DOT_SECONDS), ("stop_collect",)]
        expected += [("fit",), ("view",), ("stop",), ("show", None, None)]  # the camera goes off, then the dot goes
        self.assertEqual(self.events, expected)
        self.assertEqual(self.blender.rejected, [])
        self.assertEqual(self.blender.dark, [])  # every dot was on screen the whole time it was meant to be
        gaps = [later - earlier for earlier, later in zip(self.blender.polls, self.blender.polls[1:])]
        self.assertLessEqual(max(gaps), 2.0 + 1e-6)  # the add-on stops drawing after 3 s without a poll: keep well clear
        self.assertEqual(self.tracker.held_by, "gaze")  # voice can't start while the camera is open
        self.assertIsNone(self.t.ctx.camera.holder)  # and can again afterwards
        self.assertTrue(any("1 of 14" in line for line in job.to_dict()["lines"]))
        self.assertEqual(result["calibrated"], True)

    def test_what_is_saved_is_the_model_the_check_the_viewport_and_when(self):
        self.calibrate_now()
        saved = self.saved()
        self.assertEqual(set(saved), {"model", "check", "view", "when"})
        self.assertEqual(saved["model"], self.reply["fit"])
        self.assertEqual(saved["check"], self.reply["check"])
        self.assertEqual(saved["view"], {"width": 1840, "height": 1100})
        self.assertEqual(saved["when"], self.clock.now)  # when it was saved
        self.assertEqual(load_calibration(self.t.paths.gaze_calibration), saved)  # the unit and voice read it back
        state_folder = self.t.paths.gaze_calibration.parent
        self.assertEqual([p.name for p in state_folder.iterdir() if p.name.startswith(".")], [])  # no temp file left

    def test_after_calibrating_the_unit_reports_the_accuracy_and_the_patch_size(self):
        self.reply = fit_reply(median=0.04)
        self.calibrate_now()
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("4%", state.summary)
        self.assertEqual(radius_of(self.saved()), (0.06, 0.1))  # the check's radius is what rays and the ring use

    def test_calibration_needs_blender_open(self):
        self.blender.down = True
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("Blender", str(caught.exception))
        self.assertIsNone(self.tracker)  # the camera was never opened for nothing
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_calibration_needs_a_3d_viewport_and_an_add_on_that_knows_gaze(self):
        self.blender.scene.view = None
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("3D viewport", str(caught.exception))
        self.blender.scene.view = (1840, 1100)
        self.blender.old = True
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("Update", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_a_poor_result_says_so_and_keeps_the_old_one(self):
        old = self.calibrated_for()
        before = self.t.paths.gaze_calibration.read_bytes()
        self.assertEqual(MAX_MEDIAN_MISS, 0.25)
        for median in (0.26, 0.31, 0.9, float("nan")):
            self.reply = fit_reply(median=median)
            with self.assertRaises(JobError) as caught:
                self.calibrate_now()
            message = str(caught.exception)
            self.assertIn("wasn't saved", message, median)
            self.assertIn("earlier calibration is still in use", message)
            self.assertEqual(self.t.paths.gaze_calibration.read_bytes(), before, median)
        self.assertEqual(load_calibration(self.t.paths.gaze_calibration), old)

    def test_the_poor_result_message_gives_the_figure_and_the_limit_in_plain_words(self):
        self.reply = fit_reply(median=0.31)
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        message = str(caught.exception)
        self.assertIn("typically off by 31% of the viewport", message)
        self.assertIn("25%", message)
        self.assertNotIn("earlier calibration", message)  # there was none to keep
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_a_result_exactly_at_the_limit_is_kept(self):
        self.reply = fit_reply(median=MAX_MEDIAN_MISS)
        self.calibrate_now()
        self.assertEqual(self.saved()["check"]["median"], 0.25)

    def test_a_fit_marked_on_its_own_dots_is_refused_because_it_would_flatter_itself(self):
        self.reply = fit_reply(median=0.01, held_out=False)
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("check", str(caught.exception).lower())
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_a_model_the_app_cant_use_is_refused(self):
        self.reply = {**fit_reply(), "fit": {"kind": "ridge", "features": 2}}
        with self.assertRaises(JobError):
            self.calibrate_now()
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_the_dots_are_cleared_even_if_it_fails(self):
        failures = {
            "no face readings": ("fit", "Not enough face readings to calibrate: 3, and 30 are needed."),
            "the worker died mid-dot": ("collect", "Gaze tracking isn't running: no output"),
            "the camera wouldn't open": ("start", "Permission denied"),
        }
        for name, (method, why) in failures.items():
            with self.subTest(name):
                self.events.clear()
                self.gaze.tracker_factory = lambda camera, calibration, method=method, why=why: self._failing(
                    method, why, camera, calibration)
                with self.assertRaises(JobError) as caught:
                    self.calibrate_now()
                self.assertIn(why, str(caught.exception))
                self.assertEqual(self.events[-2:], [("stop",), ("show", None, None)])  # camera off, dot gone
                self.assertIsNone(self.t.ctx.camera.holder)
                self.assertFalse(self.t.paths.gaze_calibration.exists())

    def _failing(self, method, why, camera, calibration):
        tracker = FakeTracker(self.events, camera, calibration, self.reply, self.t.ctx)
        tracker.fail[method] = why
        return tracker

    def test_blender_going_away_midway_still_turns_the_camera_off(self):
        self.blender.shows_before_down = 3  # three dots are drawn, then Blender is gone
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("Blender", str(caught.exception))
        self.assertEqual(self.events[-1], ("stop",))  # the dot can't be cleared from here, but the camera still goes
        self.assertIsNone(self.t.ctx.camera.holder)

    def test_a_viewport_resized_during_calibration_is_not_saved(self):
        class Resizing(FakeTracker):
            def fit(inner, timeout=30.0):
                self.blender.scene.view = (1500, 1100)  # the person dragged the sidebar open while looking at dots
                return super().fit(timeout)

        self.gaze.tracker_factory = lambda camera, calibration: Resizing(self.events, camera, calibration, self.reply, self.t.ctx)
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("resized", str(caught.exception))
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_the_camera_picked_in_routing_is_the_one_used(self):
        self.t.runner.add(LIST_DEVICES, stderr=PHONE_FIRST)
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"camera_device": "MacBook Air Camera"})
        self.calibrate_now()
        self.assertEqual(self.tracker.camera, "1")

    def test_calibration_refuses_a_camera_that_isnt_there(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"camera_device": "Logitech BRIO"})
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("Logitech BRIO", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_calibration_needs_gaze_installed_first(self):
        self.py.unlink()
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("Install", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_each_calibration_gets_a_fresh_worker_so_an_aborted_one_cant_leak_rows(self):
        self.reply = fit_reply(median=0.9)
        with self.assertRaises(JobError):
            self.calibrate_now()
        first = self.tracker
        self.reply = fit_reply()
        self.calibrate_now()
        self.assertIsNot(self.tracker, first)

    def test_a_key_press_for_voice_survives_a_calibration(self):
        self.blender.queue.add("ptt", state="down")  # voice with gaze off can run beside this, and it reads these
        self.calibrate_now()
        self.assertEqual([event["type"] for event in self.blender.queue.poll(0)], ["ptt"])
        self.assertEqual(self.blender.dark, [])  # asking without taking still counts as the app being there

    def test_calibrating_refuses_while_voice_is_on(self):
        self.t.ctx.camera.take("voice")
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("voice", str(caught.exception).lower())
        self.assertIn("off", str(caught.exception).lower())
        self.assertEqual(self.events, [])  # Blender wasn't asked and no camera was built
        self.assertEqual(self.t.ctx.camera.holder, "voice")  # and voice keeps it

    def test_a_second_calibration_cannot_start_while_one_has_the_camera(self):
        self.t.ctx.camera.take("gaze")
        with self.assertRaises(JobError):
            self.calibrate_now()
        self.assertEqual(self.t.ctx.camera.holder, "gaze")


class SaveCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.path = self.t.paths.gaze_calibration

    def tearDown(self):
        self.t.cleanup()

    def test_it_makes_the_folder_and_what_it_writes_loads_back(self):
        self.assertFalse(self.path.parent.exists())  # state/ isn't made by AppPaths.ensure
        saved = {**a_calibration(), "view": VIEW, "when": 5.0}
        save_calibration(self.path, saved)
        self.assertEqual(load_calibration(self.path), saved)

    def test_a_write_that_fails_leaves_the_old_file_and_no_temp_file(self):
        old = {**a_calibration(), "view": VIEW, "when": 5.0}
        save_calibration(self.path, old)
        with self.assertRaises(ValueError):
            save_calibration(self.path, {**old, "check": {"median": float("nan")}})  # not valid JSON
        self.assertEqual(load_calibration(self.path), old)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])


class RingTest(GazeCase):
    """`test` shows a ring on the viewport that follows where the camera says you are looking."""

    def setUp(self):
        super().setUp()
        self.install()
        self.calibrated_for(radius=(0.07, 0.11))
        self.where = lambda t: (0.3, 0.4, 0.9)  # (u, v, confidence) the face is looking at, or None for no face

    def pass_time(self, seconds):
        """Time passes, and the camera sends 30 readings a second meanwhile."""
        began = self.clock.now
        for i in range(1, round(seconds * 30) + 1):
            t = began + i / 30
            seen = self.where(t)
            if seen:
                self.tracker.track.add(GazeSample(t, *seen))
        super().pass_time(seconds)

    def run_test(self):
        return self.gaze.run_action("test", make_job("gaze", "test"))

    def rings(self):
        """What was drawn on each update, with the final clearing taken off."""
        drawn = [e[2] for e in self.events if e[0] == "show"]
        self.assertEqual(drawn[-1], None)
        return drawn[:-1]

    def test_a_key_press_for_voice_survives_a_test(self):
        self.blender.queue.add("ptt", state="down")
        self.run_test()
        self.assertEqual([event["type"] for event in self.blender.queue.poll(0)], ["ptt"])

    def test_the_test_ring_follows_the_track(self):
        # Looking at one place, then another, with now and then a stray reading and a blink in between.
        def where(t):
            index = round((t - 1000.0) * 30)
            if index % 7 == 0:
                return 0.95, 0.05, 0.9  # a flick of the eyes somewhere else
            if index % 11 == 0:
                return 0.9, 0.9, 0.0  # a blink
            return (0.3, 0.4, 0.9) if t < 1005.0 else (0.7, 0.6, 0.9)
        self.where = where
        result = self.run_test()
        self.assertEqual(TEST_SECONDS, 10.0)
        rings = self.rings()
        self.assertAlmostEqual(len(rings), 100, delta=1)  # about ten a second, for ten seconds
        self.assertEqual(rings[0], None)  # the camera hasn't sent anything yet
        self.assertEqual(rings[20], [0.3, 0.4, 0.07, 0.11])  # the middle of the last 0.3 s, sized by the saved radius
        self.assertEqual(rings[-1], [0.7, 0.6, 0.07, 0.11])
        self.assertEqual(self.blender.rejected, [])
        self.assertEqual(self.blender.dark, [])
        self.assertEqual(self.events[-2:], [("stop",), ("show", None, None)])  # ring gone, camera off
        self.assertEqual(self.tracker.calibration, load_calibration(self.t.paths.gaze_calibration))  # the saved one
        self.assertEqual(self.tracker.held_by, "gaze")
        self.assertIsNone(self.t.ctx.camera.holder)
        self.assertEqual(result["tested"], True)
        self.assertGreater(result["followed"], 0.9)

    def test_the_ring_asks_blender_ten_times_a_second(self):
        self.run_test()
        waits = {e[1] for e in self.events if e[0] == "wait"}
        self.assertEqual(waits, {0.1})

    def test_the_ring_disappears_while_the_face_is_lost_and_comes_back(self):
        self.where = lambda t: None if 1003.0 <= t <= 1004.0 else (0.3, 0.4, 0.9)
        self.run_test()
        rings = self.rings()
        self.assertIsNotNone(rings[20])
        self.assertIsNone(rings[36])  # the last 0.3 s had nothing in it
        self.assertIsNotNone(rings[60])

    def test_readings_from_closed_eyes_or_a_half_seen_face_are_not_followed(self):
        self.where = lambda t: (0.9, 0.9, 0.2) if 1003.0 <= t <= 1004.0 else (0.3, 0.4, 0.9)
        self.run_test()
        rings = self.rings()
        self.assertEqual(rings[20], [0.3, 0.4, 0.07, 0.11])
        self.assertIsNone(rings[36])  # a second of nothing but blinks: no ring, rather than a ring in the corner

    def test_a_ring_never_leaves_the_viewport(self):
        self.where = lambda t: (1.08, -0.05, 0.9)  # looking a little past the edge, which the tracker still passes on
        self.run_test()
        self.assertEqual(self.rings()[20], [1.0, 0.0, 0.07, 0.11])
        self.assertEqual(self.blender.rejected, [])

    def test_a_test_that_never_sees_a_face_says_so_and_still_cleans_up(self):
        self.where = lambda t: None
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("face", str(caught.exception))
        self.assertEqual(self.events[-2:], [("stop",), ("show", None, None)])
        self.assertIsNone(self.t.ctx.camera.holder)

    def test_the_ring_is_cleared_even_if_the_camera_fails(self):
        self.gaze.tracker_factory = lambda camera, calibration: self._failing(camera, calibration)
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("Permission denied", str(caught.exception))
        self.assertEqual(self.events[-2:], [("stop",), ("show", None, None)])
        self.assertIsNone(self.t.ctx.camera.holder)

    def _failing(self, camera, calibration):
        self.tracker = FakeTracker(self.events, camera, calibration, self.reply, self.t.ctx)
        self.tracker.fail["start"] = "Permission denied"
        return self.tracker

    def test_testing_needs_a_calibration_first(self):
        self.t.paths.gaze_calibration.unlink()
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("Calibrate", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_testing_needs_blender_open(self):
        self.blender.down = True
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("Blender", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_testing_refuses_while_voice_is_on(self):
        self.t.ctx.camera.take("voice")
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("voice", str(caught.exception).lower())
        self.assertEqual(self.events, [])
        self.assertEqual(self.t.ctx.camera.holder, "voice")


class ThroughTheControlCenterTest(GazeCase):
    """The card's keys really run the actions, and a refusal reaches the person as the job's plain error."""

    def run_job(self, action):
        center = ControlCenter(self.t.ctx, [self.gaze])
        job = center.run_action("gaze", action)
        center.jobs.wait(job.id, 30)
        self.assertIsNot(job.state, JobState.RUNNING)
        return job

    def test_calibrate_and_test_run_as_jobs_from_the_card(self):
        self.install()
        job = self.run_job("calibrate")
        self.assertEqual((job.state, job.error), (JobState.SUCCEEDED, None))
        self.assertTrue(job.result["calibrated"])

        def with_a_face_in_view(camera, calibration):
            tracker = self.make_tracker(camera, calibration)
            for i in range(900):  # a face looking at one place, from just after the calibration to well past the test
                tracker.track.add(GazeSample(1030.0 + i / 30, 0.4, 0.5, 0.9))
            return tracker

        self.gaze.tracker_factory = with_a_face_in_view
        job = self.run_job("test")
        self.assertEqual(job.state, JobState.SUCCEEDED, job.error)
        self.assertTrue(job.result["tested"])

    def test_while_voice_is_on_the_job_fails_with_the_reason(self):
        self.install()
        self.t.ctx.camera.take("voice")
        job = self.run_job("calibrate")
        self.assertEqual(job.state, JobState.FAILED)
        self.assertIn("Turn voice off", job.error)


class WithTheRealWorkerTest(GazeCase):
    """The unit driving the real gaze worker in its --fake mode: no camera, but every message really crosses the pipe."""

    def setUp(self):
        super().setUp()
        self.install()
        self.workers = []
        self.gaze.tracker_factory = self.real_fake_worker
        self.gaze.sleep = time.sleep
        for name, value in (("DOT_SECONDS", 0.25), ("SETTLE_SECONDS", 0.05)):
            patcher = patch(f"gazevoice.components.gaze.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def real_fake_worker(self, camera, calibration):
        tracker = GazeTracker(Path(sys.executable), WORKER, camera, calibration, extra_args=["--fake"])
        self.workers.append(tracker)
        return tracker

    def tearDown(self):
        for tracker in self.workers:
            tracker.stop()
        super().tearDown()

    def test_calibrating_sends_the_check_dots_as_check_dots_and_leaves_no_worker_running(self):
        try:
            result = self.gaze.run_action("calibrate", make_job("gaze", "calibrate"))
            message = ""
        except JobError as exc:  # the fake face doesn't follow the dots, so a poor fit is a fair outcome here
            result, message = None, str(exc)
        self.assertNotIn("check dots weren't seen", message)  # they were held back from the fit, as the worker needs
        self.assertNotIn("Not enough face readings", message)
        if result is not None:
            self.assertEqual(load_calibration(self.t.paths.gaze_calibration)["view"], VIEW)
        else:
            self.assertIn("wasn't saved", message)
            self.assertFalse(self.t.paths.gaze_calibration.exists())
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(self.workers[0].alive)  # the camera's process is gone, whichever way it went
        self.assertIsNone(self.t.ctx.camera.holder)
        self.assertEqual(self.blender.scene.shown[-1], (None, None, []))


if __name__ == "__main__":
    unittest.main()
