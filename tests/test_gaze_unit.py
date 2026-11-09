import hashlib
import json
import math
import statistics
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice.components import build_components
from gazevoice.components.base import ComponentState, Health
from gazevoice.components.gaze import (
    CALIBRATION_DOTS,
    CHECK_DOTS,
    DOT_ORDER,
    DOT_SECONDS,
    GUIDE_DISTANCE,
    GUIDE_EDGE,
    GUIDE_OUT_OF_VIEW,
    GUIDE_RATE,
    GUIDE_SECONDS,
    GUIDE_TARGET,
    GUIDE_X,
    GUIDE_Y,
    HOLD_SECONDS,
    INSTALL_CONFIRM,
    MAX_MEDIAN_MISS,
    MEDIAPIPE,
    MODEL_SHA256,
    MODEL_SIZE,
    MODEL_URL,
    PAGE_GONE,
    PAGE_SECONDS,
    SETTLE_SECONDS,
    TEST_SECONDS,
    WORKER,
    DotPage,
    Gaze,
    guide_advice,
    load_calibration,
    save_calibration,
)
from gazevoice.components.microphone import LIST_DEVICES
from gazevoice.events import EventBus
from gazevoice.gaze_worker import Observation, Session
from gazevoice.jobs import Job, JobError, JobState
from gazevoice.probes import ProbeTimedOut
from gazevoice.server.app import ControlCenter
from gazevoice.settings import Settings, update_settings
from gazevoice.voice.gaze import Area, GazeSample, GazeTrack
from gazevoice.voice.gaze_tracker import GazeError, GazeTracker, Place, radius_of
from gazevoice.voice.wiring import voice_readiness
from gazevoice.workflow import workflow_stages

from .helpers import TempCtx, make_job
from .test_bridge_protocol import FakeScene, protocol
from .test_download import FakeResponse
from .test_gaze_model import made_up_run
from .test_gaze_tracker import a_calibration, an_old_calibration

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


class FakeBrowser(EventBus):
    """The calibration page in a browser, listening on the event bus: each dot goes up at once and it says so.

    While the head guide is up it says it is alive after each reading, as the real page does on a timer. What the
    guide showed is kept in `guides`.
    """

    def __init__(self, events, page):
        super().__init__()
        self.events, self.page = events, page
        self.silent = False  # the tab has gone without a word
        self.closes_after = None  # the person shuts the page after this many dots
        self.guides = []  # what the guide told the page, in order
        self.skips_after = None  # the person presses Start anyway after this many readings
        self.closes_the_guide_after = None  # the person shuts the page after this many readings

    def publish(self, kind, data):
        super().publish(kind, data)
        if kind == "gaze.guide":
            return self.guide(data)
        if kind != "gaze.dot":
            return
        self.events.append(("dot", tuple(data["uv"])))
        if self.silent:
            return
        if self.closes_after == 0:
            self.page.left(data["job_id"])
            return
        if self.closes_after is not None:
            self.closes_after -= 1
        self.page.shown(data["job_id"], data["number"])

    def guide(self, data):
        self.guides.append(data)
        if self.silent:
            return
        if self.closes_the_guide_after is not None and len(self.guides) > self.closes_the_guide_after:
            self.page.left(data["job_id"])
            return
        self.page.alive(data["job_id"])
        if self.skips_after is not None and len(self.guides) > self.skips_after:
            self.page.skip(data["job_id"])


IN_VIEW = (0.36, 0.12, 0.64, 0.86)  # a face's box at 30 cm, well inside the picture: left, top, right and bottom


class FakeTracker:
    """The gaze worker's end of the conversation, writing what it is asked into `events`."""

    def __init__(self, events, camera, calibration, fit_reply, ctx=None, clock=None):
        self.events, self.camera, self.calibration = events, camera, calibration
        self.fit_reply = fit_reply
        self.ctx = ctx
        self.clock = clock or FakeClock()
        self.held_by = None
        self.track = GazeTrack()
        self.radius = (calibration or {}).get("check", {}).get("radius", (0.1, 0.15))
        self.fail = {}  # method name -> the GazeError it raises
        # What the worker last said about the head: found, x (not mirrored), y, centimetres from the camera and, if it is
        # given, the face's box in the picture (left, top, right, bottom). Where the outline is by default, with the
        # whole face in view. It can be a function of the time, or None for "nothing yet".
        self.head = (True, 0.5, 0.45, 30.0)
        self.stale = False  # readings have stopped arriving

    @property
    def place(self):
        head = self.head(self.clock()) if callable(self.head) else self.head
        if head is None:
            return None
        found, x, y, distance, *box = head
        box = box[0] if box else (IN_VIEW if found else None)
        return Place(found, x, y, distance, at=0.0 if self.stale else self.clock(), box=box)

    def guide(self, on):
        self.events.append(("guide", on))
        self._go("guide")

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

    def fit(self, timeout=30.0, screen_width_cm=None):
        self.events.append(("fit",))
        self.fit_width = screen_width_cm
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


# A 13-inch MacBook Air: 290.5 mm across 1710 points, as macOS says.
CM_PER_POINT = 29.05 / 1710

# FakeScene's viewport: 1840 by 1100 pixels at double density, so 920 by 550 points, in a window macOS puts at FRAME.
FRAME = (0.0, 0.0, 1000.0, 682.0)
VIEW_AREA = {"left": 2.0, "top": 80.0, "width": 920.0, "height": 550.0}
SCREEN = {"left": 0, "top": 38, "width": 1710, "height": 1074}


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
        self.frame = FRAME
        self.gaze.window_frame = lambda pid, width, height: self.frame
        self.asked_for_size = []  # the pages the unit asked macOS the width of: left, top, width and height, in points
        self.screen_size = CM_PER_POINT  # or None: macOS can't say

        def page_width_cm(left, top, width, height):
            self.asked_for_size.append((left, top, width, height))
            return None if self.screen_size is None else width * self.screen_size

        self.gaze.page_width_cm = page_width_cm
        self.browser = FakeBrowser(self.events, self.gaze.page)
        self.tracker = None  # the one the unit built, if it did
        self.reply = fit_reply()
        self.venv = self.t.paths.venvs / "gaze"
        self.py = self.venv / "bin" / "python"

    def tearDown(self):
        self.t.cleanup()

    def make_tracker(self, camera, calibration):
        self.tracker = FakeTracker(self.events, camera, calibration, self.reply, self.t.ctx, self.clock)
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

    def calibrated_for(self, screen=SCREEN, radius=(0.07, 0.11)):
        """Save a calibration the way Calibrate does, on this part of the screen."""
        saved = {**a_calibration(radius), "screen": dict(screen), "when": 1.0}
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

    def test_a_calibration_that_reads_where_the_head_sits_says_to_calibrate_again(self):
        self.install()
        self.calibrate(an_old_calibration())  # the right number of features and well formed, but it reads the head's place
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("out of date", state.summary)
        self.assertIn("Calibrate gaze again", state.summary)
        self.assertTrue(state.offers("calibrate"))
        self.assertFalse(state.offers("test"))

    def test_a_calibration_from_before_the_turn_was_allowed_for_says_to_calibrate_again(self):
        # Like the one saved on 2026-11-09: eyes only, well formed, but made before the screen's width was kept with it.
        self.install()
        saved = a_calibration()
        del saved["model"]["screen_width_cm"]
        self.calibrate(saved)
        self.assertIsNone(load_calibration(self.t.paths.gaze_calibration))
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertEqual(state.summary, "Your saved calibration is out of date. Calibrate gaze again.")
        self.assertTrue(state.offers("calibrate"))
        self.assertFalse(state.offers("test"))

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
        self.calibrate({"model": a_calibration()["model"], "screen": SCREEN})
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
    def test_calibrating_is_offered_once_gaze_is_installed_and_opens_its_own_page(self):
        self.install()
        state = self.gaze.check()
        calibrate = next(a for a in state.actions if a.id == "calibrate")
        self.assertEqual(calibrate.href, "/calibrate")
        self.assertIsNone(calibrate.confirm)  # the page says what will happen, and nothing starts until it is clicked
        self.assertFalse(state.offers("test"))  # there is nothing to test yet

    def test_a_calibration_from_another_version_can_be_made_again(self):
        self.install()
        self.calibrate({**a_calibration(), "model": {"kind": "ridge", "features": 3}})
        self.assertTrue(self.gaze.check().offers("calibrate"))

    def test_a_calibration_from_before_there_was_a_page_is_made_again(self):
        self.install()
        self.calibrate({**a_calibration(), "screen": None, "view": {"width": 1840, "height": 1100}})
        state = self.gaze.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("out of date", state.summary)
        self.assertTrue(state.offers("calibrate"))
        self.assertFalse(state.offers("test"))

    def test_a_calibrated_unit_offers_to_calibrate_again_and_to_test(self):
        self.install()
        self.calibrated_for()
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual([a.id for a in state.actions], ["calibrate", "test"])
        self.assertIn("camera", state.actions[1].confirm.lower())  # the test turns it on from the card, so it asks
        self.assertIn("again", state.actions[0].label.lower())

    def test_nothing_is_offered_while_there_is_no_camera_or_nothing_installed(self):
        self.assertEqual(self.gaze.check().actions[0].id, "install")  # nothing to calibrate yet
        self.install()
        self.t.runner.add(LIST_DEVICES, stderr=listing())
        self.t.ctx.probes.clear()  # the first look listed a camera, and listings are kept for a while
        self.assertEqual(self.gaze.check().actions, ())

    def test_the_accuracy_is_said_as_a_share_of_the_screen_in_plain_words(self):
        self.install()
        self.calibrate({**self.calibrated_for(), "check": {"median": 0.042, "p90": 0.091, "radius": [0.07, 0.11]}})
        state = self.gaze.check()
        self.assertIn("typically off by 4% of the screen", state.summary)
        self.assertEqual(state.detail("Accuracy"), "Typically off by 4% of the screen · 9 in 10 within 9%")

    def test_the_calibration_stands_whatever_blender_is_doing(self):
        self.install()
        self.calibrated_for()
        self.blender.scene.view = (1500, 1100)  # resized
        self.assertEqual(self.gaze.check().health, Health.READY)
        self.blender.down = True  # closed
        self.assertEqual(self.gaze.check().health, Health.READY)

    def test_checking_never_opens_the_camera_or_draws_in_blender(self):
        self.install()
        self.calibrated_for()
        self.gaze.tracker_factory = lambda *args: self.fail("the camera was opened to check")
        self.gaze.check()
        self.assertEqual(self.events, [])

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
                                    "float16/1/face_landmarker.task")


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


DOTS = [dot for dot, _ in DOT_ORDER]  # in the order they are shown


class WhatTheInstallSaysTest(unittest.TestCase):
    def test_the_install_promises_only_what_is_true(self):
        self.assertIn("No picture from your camera ever leaves this Mac.", INSTALL_CONFIRM)
        self.assertNotIn("Nothing you look at", INSTALL_CONFIRM)


class CalibrateTest(GazeCase):
    def setUp(self):
        super().setUp()
        self.install()

    def job(self, area=SCREEN):
        return Job(id="job1", component_id="gaze", action="calibrate", _bus=self.browser, params={"area": area})

    def calibrate_now(self, area=SCREEN):
        return self.gaze.run_action("calibrate", self.job(area))

    def saved(self):
        return json.loads(self.t.paths.gaze_calibration.read_text())

    def test_time_follows_neither_axis_of_the_dots(self):
        # A head creeps steadily, so its drift goes with time. If time followed an axis, the drift would read as the eyes
        # moving along it, and leaving a dot out couldn't catch that, because that dot sits on the same line. The check
        # dots are held to it too, so that what they mark isn't the drift either.
        for name, dots in (("learning dots", CALIBRATION_DOTS), ("check dots", CHECK_DOTS), ("all of them", DOTS)):
            for axis, axis_name in enumerate("uv"):
                with self.subTest(dots=name, axis=axis_name):
                    self.assertLess(abs(statistics.correlation(range(len(dots)), [dot[axis] for dot in dots])), 0.1)

    def test_sixteen_learning_dots_reach_the_edges_and_eight_check_dots_sit_between_them(self):
        self.assertEqual((len(CALIBRATION_DOTS), len(CHECK_DOTS)), (16, 8))
        self.assertEqual(len(set(DOTS)), 24)
        self.assertTrue(all(0 < u < 1 and 0 < v < 1 for u, v in DOTS))
        for axis in (0, 1):
            self.assertLessEqual(min(dot[axis] for dot in CALIBRATION_DOTS), 0.05)  # edge to edge, both ways
            self.assertGreaterEqual(max(dot[axis] for dot in CALIBRATION_DOTS), 0.95)
        for dot in CHECK_DOTS:  # the fit is marked where it never saw a dot
            self.assertGreaterEqual(min(math.dist(dot, seen) for seen in CALIBRATION_DOTS), 0.2, dot)

    def test_the_check_dots_are_mixed_in_among_the_learning_dots(self):
        # All at the end, the check dots came after the head had drifted, and marked the drift as well as the fit.
        checks = [number for number, (_, check) in enumerate(DOT_ORDER) if check]
        self.assertEqual([dot for dot, check in DOT_ORDER if not check], list(CALIBRATION_DOTS))
        self.assertEqual([dot for dot, check in DOT_ORDER if check], list(CHECK_DOTS))
        self.assertLess(checks[0], 3)
        self.assertGreater(checks[-1], len(DOT_ORDER) - 4)
        self.assertTrue(all(b - a >= 2 for a, b in zip(checks, checks[1:])))  # never two in a row

    def test_each_dot_is_a_long_way_from_the_one_before(self):
        for number, (before, now) in enumerate(zip(DOTS, DOTS[1:]), 2):
            with self.subTest(dot=number):
                self.assertGreaterEqual(math.dist(before, now), 0.4)

    def test_shorter_dots_take_about_as_long_as_the_fourteen_long_ones_did(self):
        # About a second of looking did as well as two on the run of 2026-11-09, so there is time for more dots.
        self.assertEqual((DOT_SECONDS, SETTLE_SECONDS), (1.0, 0.5))
        self.assertAlmostEqual(len(DOT_ORDER) * (SETTLE_SECONDS + DOT_SECONDS), 14 * (0.5 + 2.0), delta=2.0)

    def test_calibrate_shows_each_dot_and_collects_after_it_settles(self):
        job = self.job()
        result = self.gaze.run_action("calibrate", job)
        expected = [("start", "0", None),  # the camera opens with no calibration loaded
                    ("guide", True), ("guide", False)]  # the head is lined up first, and the guide is off before any dot
        for uv, check in DOT_ORDER:
            expected += [("dot", uv), ("wait", SETTLE_SECONDS),  # the eyes get time to arrive
                         ("collect", uv, check),  # only then are faces kept, and the check dots are held back
                         ("wait", DOT_SECONDS), ("stop_collect",)]
        expected += [("fit",), ("stop",)]
        self.assertEqual([e for e in self.events if e != ("wait", 1 / GUIDE_RATE)], expected)  # leaving out the guide's waits
        self.assertEqual(self.tracker.held_by, "gaze")  # voice can't start while the camera is open
        self.assertIsNone(self.t.ctx.camera.holder)  # and can again afterwards
        self.assertTrue(any("Dot 1 of 24" in line for line in job.to_dict()["lines"]))
        self.assertEqual(result["calibrated"], True)

    def test_the_width_of_the_screen_the_page_is_on_goes_to_the_fit(self):
        self.calibrate_now()
        self.assertEqual(self.asked_for_size, [(0, 38, 1710, 1074)])  # the page itself, in points
        self.assertAlmostEqual(self.tracker.fit_width, 29.05)  # its 1710 points are 29.05 cm

    def test_a_screen_whose_size_cant_be_read_is_fitted_as_before(self):
        self.screen_size = None
        self.calibrate_now()
        self.assertIsNone(self.tracker.fit_width)

    def test_the_run_says_whether_turning_the_head_is_allowed_for(self):
        self.assertTrue(self.reported({"screen_width_cm": 29.0})[0].endswith(
            "Turning your head is allowed for (the screen is 29.0 cm wide)."))
        self.assertTrue(self.reported({"screen_width_cm": None})[0].endswith(
            "The screen's size couldn't be read, or the page was too narrow or too wide to trust, "
            "so turning your head isn't allowed for."))

    def test_what_is_saved_is_the_model_the_check_the_screen_and_when(self):
        self.calibrate_now()
        saved = self.saved()
        self.assertEqual(set(saved), {"model", "check", "screen", "when"})
        self.assertEqual(saved["model"], self.reply["fit"])
        self.assertEqual(saved["check"], self.reply["check"])
        self.assertEqual(saved["screen"], SCREEN)
        self.assertEqual(saved["when"], self.clock.now)  # when it was saved
        self.assertEqual(load_calibration(self.t.paths.gaze_calibration), saved)  # the unit and voice read it back
        state_folder = self.t.paths.gaze_calibration.parent
        self.assertEqual([p.name for p in state_folder.iterdir() if p.name.startswith(".")], [])  # no temp file left

    def test_calibrating_needs_nothing_from_blender(self):
        self.blender.down = True
        result = self.calibrate_now()
        self.assertEqual(result["calibrated"], True)
        self.assertEqual(self.blender.polls, [])

    def test_a_calibration_with_no_place_on_the_screen_is_refused_before_the_camera_opens(self):
        for area in (None, {}, {"left": 0, "top": 0, "width": 0, "height": 100}, "everywhere"):
            with self.assertRaises(JobError) as caught:
                self.calibrate_now(area)
            self.assertIn("Calibrate", str(caught.exception), area)
            self.assertIsNone(self.tracker)
            self.assertIsNone(self.t.ctx.camera.holder)

    def test_closing_the_page_turns_the_camera_off_and_saves_nothing(self):
        self.browser.closes_after = 3
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("page", str(caught.exception))
        self.assertIn("camera", str(caught.exception))
        self.assertEqual(self.events[-1], ("stop",))
        self.assertIsNone(self.t.ctx.camera.holder)
        self.assertFalse(self.t.paths.gaze_calibration.exists())
        self.assertEqual(sum(e[0] == "dot" for e in self.events), 4)  # no dot follows

    def test_a_page_that_stops_answering_turns_the_camera_off_too(self):
        self.gaze.page.timeout = 0.01
        self.browser.silent = True
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("page", str(caught.exception))
        self.assertEqual(self.events[-1], ("stop",))
        self.assertIsNone(self.t.ctx.camera.holder)

    def test_leaving_while_a_dot_is_up_stops_at_once_rather_than_at_the_next_dot(self):
        class Leaving(FakeTracker):
            def stop_collect(inner):
                super().stop_collect()
                self.gaze.page.left("job1")

        self.gaze.tracker_factory = lambda camera, calibration: Leaving(self.events, camera, calibration, self.reply, self.t.ctx,
                                                                         self.clock)
        with self.assertRaises(JobError):
            self.calibrate_now()
        self.assertEqual(sum(e[0] == "dot" for e in self.events), 1)
        self.assertNotIn(("fit",), self.events)

    def test_after_calibrating_the_unit_reports_the_accuracy_and_the_patch_size(self):
        self.reply = fit_reply(median=0.04)
        self.calibrate_now()
        state = self.gaze.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("4%", state.summary)
        self.assertEqual(radius_of(self.saved()), (0.06, 0.1))  # the check's radius is what rays and the ring use

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

    def test_a_poor_result_doesnt_say_an_old_calibration_that_is_refused_is_still_in_use(self):
        self.calibrate(an_old_calibration())  # on disk, but it reads where the head sits, so voice and Test won't use it
        self.reply = fit_reply(median=0.31)
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        message = str(caught.exception)
        self.assertIn("wasn't saved", message)
        self.assertNotIn("still in use", message)

    def test_the_poor_result_message_gives_the_figure_and_the_limit_in_plain_words(self):
        self.reply = fit_reply(median=0.31)
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        message = str(caught.exception)
        self.assertIn("typically off by 31% of the screen", message)
        self.assertIn("25%", message)
        self.assertIn("about 30 cm from the screen", message)
        self.assertNotIn("arm's length", message)
        self.assertNotIn("earlier calibration", message)  # there was none to keep
        self.assertFalse(self.t.paths.gaze_calibration.exists())

    def test_a_run_that_finished_but_was_refused_says_so_in_a_field_the_page_can_read(self):
        refused = {
            "too loose": fit_reply(median=0.31),
            "no figure": fit_reply(median=float("nan")),
            "marked on its own dots": fit_reply(median=0.01, held_out=False),
            "a model it can't use": {**fit_reply(), "fit": {"kind": "ridge", "features": 2}},
        }
        for name, reply in refused.items():
            with self.subTest(name):
                self.reply = reply
                with self.assertRaises(JobError) as caught:
                    self.calibrate_now()
                self.assertEqual(caught.exception.result, {"calibrated": False, "refused": True})

    def test_a_run_that_stopped_says_nothing_of_the_kind(self):
        for name, method, why in (("no face readings", "fit", "Not enough face readings to calibrate: 3, and 30 are needed."),
                                  ("the camera wouldn't open", "start", "Permission denied")):
            with self.subTest(name):
                self.gaze.tracker_factory = lambda camera, calibration, method=method, why=why: self._failing(
                    method, why, camera, calibration)
                with self.assertRaises(JobError) as caught:
                    self.calibrate_now()
                self.assertIsNone(caught.exception.result)

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

    def test_the_camera_goes_off_whatever_fails(self):
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
                self.assertEqual(self.events[-1], ("stop",))  # camera off
                self.assertIsNone(self.t.ctx.camera.holder)
                self.assertFalse(self.t.paths.gaze_calibration.exists())

    def _failing(self, method, why, camera, calibration):
        tracker = FakeTracker(self.events, camera, calibration, self.reply, self.t.ctx, self.clock)
        tracker.fail[method] = why
        return tracker

    def test_a_refused_run_still_shows_what_it_measured(self):
        self.reply = {**fit_reply(median=0.31), "seen": "The camera sent 540 frames while the dots were up."}
        job = self.job()
        with self.assertRaises(JobError):
            self.gaze.run_action("calibrate", job)
        lines = job.to_dict()["lines"]
        self.assertIn("The camera sent 540 frames while the dots were up.", lines)
        self.assertTrue(any("Readings kept: 120 to learn from, 40 to check" in line and "31%" in line for line in lines))

    def test_a_run_says_what_it_learned_and_how_it_compares_with_guessing(self):
        self.reply = {**fit_reply(median=0.12), "guess": 0.283, "model": "eyes and lids",
                      "moved": {"cm": 1.23, "degrees": 2.2}, "rows": 420, "check_rows": 240}
        job = self.job()
        self.gaze.run_action("calibrate", job)
        lines = job.to_dict()["lines"]
        self.assertIn("Readings kept: 420 to learn from, 240 to check. Typically off by 12% of the screen; "
                      "always guessing the middle would be 28%. Model: eyes and lids.", lines)
        self.assertIn("Your head moved about 1.2 cm and turned about 2 degrees during the run.", lines)

    def test_one_degree_is_said_properly(self):
        self.assertIn("about 1 degree during", self.reported({"moved": {"cm": 0.4, "degrees": 0.8}})[-1])

    def test_a_reply_without_the_new_figures_reads_as_it_did(self):
        lines = self.reported({})
        self.assertEqual(lines, ["Readings kept: 120 to learn from, 40 to check. Typically off by 5% of the screen."])

    def test_figures_that_make_no_sense_are_left_out(self):
        for junk in ({"guess": "a lot"}, {"guess": float("nan")}, {"model": ""}, {"moved": "far"}, {"moved": {}},
                     {"moved": {"cm": "far", "degrees": 1.0}}, {"moved": {"cm": float("nan"), "degrees": 1.0}}):
            with self.subTest(junk):
                self.assertEqual(self.reported(junk), self.reported({}))

    def test_with_no_accuracy_figure_guessing_has_nothing_to_stand_beside(self):
        lines = self.reported({"check": None, "guess": 0.28})
        self.assertEqual(lines, ["Readings kept: 120 to learn from, 40 to check. No accuracy figure."])

    def reported(self, change):
        job = self.job()
        Gaze._report(job, {**fit_reply(), **change})
        return job.to_dict()["lines"]

    def test_the_numbers_of_a_run_are_kept_only_when_calibrating(self):
        calibrating = self.gaze._make_tracker("1", None).argv
        at = calibrating.index("--readings-file")
        self.assertEqual(calibrating[at + 1], str(self.t.paths.gaze_readings))
        self.assertNotIn("--readings-file", self.gaze._make_tracker("1", a_calibration()).argv)

    def test_the_last_run_lives_with_the_rest_of_gaze(self):
        self.assertEqual(self.t.paths.gaze_readings, self.t.paths.state / "gaze-last-run.json")

    def test_a_calibration_made_by_the_new_fit_loads(self):
        rows, targets, check_rows, check_targets = made_up_run()
        sent = []
        session = Session(sent.append)
        for group, group_targets, held in ((rows, targets, False), (check_rows, check_targets, True)):
            for row, target in zip(group, group_targets):
                session.handle({"cmd": "collect", "uv": list(target), "check": held})
                session.see(0.0, Observation(row, 0.9))
        session.handle({"cmd": "stop_collect"})
        session.handle({"cmd": "fit"})
        reply = json.loads(json.dumps(sent[-1]))
        saved = {"model": reply["fit"], "check": reply["check"], "screen": SCREEN, "when": 1.0}
        self.calibrate(saved)
        self.assertEqual(load_calibration(self.t.paths.gaze_calibration)["model"], reply["fit"])

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

    def test_calibrating_refuses_while_voice_is_on(self):
        self.t.ctx.camera.take("voice")
        with self.assertRaises(JobError) as caught:
            self.calibrate_now()
        self.assertIn("voice", str(caught.exception).lower())
        self.assertIn("off", str(caught.exception).lower())
        self.assertEqual(self.events, [])  # no camera was built
        self.assertEqual(self.t.ctx.camera.holder, "voice")  # and voice keeps it

    def test_a_second_calibration_cannot_start_while_one_has_the_camera(self):
        self.t.ctx.camera.take("gaze")
        with self.assertRaises(JobError):
            self.calibrate_now()
        self.assertEqual(self.t.ctx.camera.holder, "gaze")


class WhatTheGuideSaysTest(unittest.TestCase):
    """One piece of advice at a time: the biggest miss first, and "Hold still" when it all fits."""

    def test_the_limits_and_the_target_are_the_ones_the_plan_gives(self):
        self.assertEqual((GUIDE_RATE, GUIDE_SECONDS, HOLD_SECONDS), (10, 60.0, 1.0))
        self.assertEqual((GUIDE_X, GUIDE_Y, GUIDE_DISTANCE), ((0.40, 0.60), (0.30, 0.60), (25.0, 45.0)))
        # About 30 cm makes the face 230 to 280 pixels wide in the 960 by 540 picture, about the face model's own 256.
        self.assertEqual(GUIDE_TARGET, (0.5, 0.45, 30.0))
        for target, (low, high) in zip(GUIDE_TARGET[:2], (GUIDE_X, GUIDE_Y)):
            self.assertAlmostEqual(target, (low + high) / 2)  # the outline sits in the middle of what is allowed
        self.assertTrue(GUIDE_DISTANCE[0] < GUIDE_TARGET[2] < GUIDE_DISTANCE[1])  # and stands for a head near the close end
        self.assertEqual(GUIDE_EDGE, 0.02)

    def test_in_place_it_says_to_hold_still(self):
        self.assertEqual(guide_advice(0.5, 0.45, 30.0), ("Hold still", True))
        self.assertEqual(guide_advice(0.5, 0.45, 30.0, IN_VIEW), ("Hold still", True))

    def test_the_edges_of_the_limits_still_fit(self):
        for x, y, distance in ((0.40, 0.30, 25.0), (0.60, 0.60, 45.0), (0.40, 0.60, 25.0), (0.60, 0.30, 45.0)):
            with self.subTest((x, y, distance)):
                self.assertEqual(guide_advice(x, y, distance), ("Hold still", True))

    def test_each_way_to_miss_has_its_own_words(self):
        for (x, y, distance), words in (
            ((0.65, 0.45, 30.0), "Move left"),  # mirrored x too large: the head shows too far right, like in a mirror
            ((0.35, 0.45, 30.0), "Move right"),
            ((0.5, 0.65, 30.0), "Move up"),  # too low in the picture
            ((0.5, 0.25, 30.0), "Move down"),
            ((0.5, 0.45, 50.0), "Come closer"),
            ((0.5, 0.45, 20.0), "Move back"),
        ):
            with self.subTest(words):
                self.assertEqual(guide_advice(x, y, distance), (words, False))

    def test_the_biggest_miss_comes_first_as_a_share_of_how_much_room_that_limit_allows(self):
        # x is 0.06 over a half-width of 0.1 (0.6 of it), y is 0.07 over a half-width of 0.15 (0.47): x is the bigger miss
        # though 0.07 is more than 0.06 in raw numbers.
        self.assertEqual(guide_advice(0.66, 0.67, 30.0)[0], "Move left")
        # 5 cm too far over a half-width of 10 is 0.5 of it, more than x at 0.3 of its own
        self.assertEqual(guide_advice(0.63, 0.45, 50.0)[0], "Come closer")
        self.assertEqual(guide_advice(0.62, 0.62, 17.5)[0], "Move back")  # 7.5 over 10 is the biggest of the three

    def test_a_face_that_reaches_the_edge_of_the_picture_is_not_in_place(self):
        # Part of it is out of view, and a face cut off at the edge is read worse, or lost. Close up, that is most often
        # the chin or the forehead.
        self.assertEqual(GUIDE_OUT_OF_VIEW, "Move back a little, part of your face is out of view")
        for name, box in (("left", (0.01, 0.12, 0.64, 0.86)), ("top", (0.36, 0.015, 0.64, 0.86)),
                          ("right", (0.36, 0.12, 0.99, 0.86)), ("bottom", (0.36, 0.12, 0.64, 0.985)),
                          ("past the bottom", (0.3, 0.1, 0.7, 1.08)), ("past the left", (-0.04, 0.12, 0.64, 0.86))):
            with self.subTest(name):
                self.assertEqual(guide_advice(0.5, 0.45, 30.0, box), (GUIDE_OUT_OF_VIEW, False))

    def test_a_face_just_inside_the_margin_is_in_view(self):
        inside = (GUIDE_EDGE + 0.001, GUIDE_EDGE + 0.001, 1 - GUIDE_EDGE - 0.001, 1 - GUIDE_EDGE - 0.001)
        self.assertEqual(guide_advice(0.5, 0.45, 30.0, inside), ("Hold still", True))

    def test_a_head_out_of_place_is_told_where_to_go_first(self):
        # A head too far to one side can touch the edge too, and moving over brings it back into view, where moving back
        # wouldn't. So the other advice comes first, and the edge is only spoken of once the rest fits.
        touching = (0.0, 0.12, 0.3, 0.86)
        self.assertEqual(guide_advice(0.85, 0.45, 30.0, touching), ("Move left", False))
        self.assertEqual(guide_advice(0.5, 0.45, 20.0, touching), ("Move back", False))


class HeadGuideTest(GazeCase):
    """Before the dots the page shows an outline and a ring, and they start by themselves once the head has stayed in place."""

    def setUp(self):
        super().setUp()
        self.install()
        until = self.clock.now + GUIDE_SECONDS + 30

        def sleep(seconds):
            if self.clock.now > until:  # a guide with no end would otherwise run for ever on a clock that costs nothing
                raise AssertionError("the guide never gave up")
            self.pass_time(seconds)

        self.gaze.sleep = sleep

    def job(self):
        return Job(id="job1", component_id="gaze", action="calibrate", _bus=self.browser, params={"area": SCREEN})

    def run_it(self):
        return self.gaze.run_action("calibrate", self.job())

    def stops_at_the_guide(self):
        """Run until the guide ends the job, and check the camera went off and no dot was shown."""
        with self.assertRaises(JobError) as caught:
            self.run_it()
        self.assertEqual(self.events[-1], ("stop",))
        self.assertIsNone(self.t.ctx.camera.holder)
        self.assertFalse(any(e[0] == "dot" for e in self.events))
        self.assertFalse(self.t.paths.gaze_calibration.exists())
        return str(caught.exception)

    def seconds_of_guide(self):
        """How much time passed between the guide turning on and turning off, from the waits between them."""
        on = self.events.index(("guide", True))
        end = next((i for i, e in enumerate(self.events) if e == ("guide", False)), len(self.events))
        return sum(e[1] for e in self.events[on:end] if e[0] == "wait")

    def test_a_head_in_place_from_the_start_goes_on_after_the_hold_and_not_before(self):
        self.run_it()
        self.assertLess(self.events.index(("guide", True)), self.events.index(("guide", False)))
        self.assertLess(self.events.index(("guide", False)), self.events.index(("dot", DOTS[0])))
        self.assertGreaterEqual(self.seconds_of_guide(), HOLD_SECONDS)
        self.assertLess(self.seconds_of_guide(), HOLD_SECONDS + 2 / GUIDE_RATE)
        self.assertEqual(self.browser.guides[-1]["advice"], "Hold still")
        self.assertTrue(self.browser.guides[-1]["fits"])

    def test_a_head_that_arrives_late_has_the_hold_counted_from_when_it_arrived(self):
        arrives = self.clock.now + 2.0
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, lambda now: (
            (True, 0.5, 0.45, 30.0) if now >= arrives else (True, 0.3, 0.45, 30.0)))
        self.run_it()
        self.assertGreaterEqual(self.seconds_of_guide(), 2.0 + HOLD_SECONDS)
        self.assertLess(self.seconds_of_guide(), 2.0 + HOLD_SECONDS + 2 / GUIDE_RATE)
        self.assertEqual([g["advice"] for g in self.browser.guides][:3], ["Move left"] * 3)

    def with_head(self, camera, calibration, head):
        tracker = self.make_tracker(camera, calibration)
        tracker.head = head
        return tracker

    def test_drifting_out_of_place_starts_the_hold_again(self):
        began = self.clock.now
        out = (began + 0.5, began + 0.7)  # in place, then 0.2 s out of it, then in place again
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, lambda now: (
            (True, 0.5, 0.45, 50.0) if out[0] <= now < out[1] else (True, 0.5, 0.45, 30.0)))
        self.run_it()
        self.assertGreaterEqual(self.seconds_of_guide(), 0.7 + HOLD_SECONDS)

    def test_a_reading_goes_to_the_page_mirrored_with_its_size_and_one_piece_of_advice(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.3, 0.5, 20.0))
        self.browser.skips_after = 1
        self.run_it()
        self.assertEqual(self.browser.guides[0], {
            "job_id": "job1", "found": True,
            "x": 0.7,  # 1 - 0.3: the page acts like a mirror
            "y": 0.5,
            "scale": 1.5,  # the target distance over the distance: 30 / 20
            "fits": False,
            "advice": "Move left",  # the head shows too far right (mirrored x 0.7, a miss of 1.0), and 20 cm misses by 0.5
        })

    def test_a_face_cut_off_by_the_edge_of_the_picture_is_told_to_move_back_and_never_passes(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(
            camera, calibration, (True, 0.5, 0.45, 26.0, (0.3, 0.05, 0.7, 0.995)))
        message = self.stops_at_the_guide()
        self.assertIn("lined up", message)
        self.assertEqual({g["advice"] for g in self.browser.guides}, {GUIDE_OUT_OF_VIEW})
        self.assertFalse(any(g["fits"] for g in self.browser.guides))

    def test_a_head_on_the_cameras_left_shows_on_the_right_of_the_mirror_and_is_asked_to_move_left(self):
        # In the camera's picture x is 0.3, so the head is on the camera's left. The mirrored page shows it on the right.
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.3, 0.45, 30.0))
        self.browser.skips_after = 2
        self.run_it()
        self.assertEqual({g["advice"] for g in self.browser.guides}, {"Move left"})
        self.assertEqual({g["x"] for g in self.browser.guides}, {0.7})

    def test_with_no_face_the_page_is_told_so(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (False, None, None, None))
        self.browser.skips_after = 1
        self.run_it()
        self.assertEqual(self.browser.guides[0], {
            "job_id": "job1", "found": False, "x": None, "y": None, "scale": None, "fits": False,
            "advice": "I can't see your face. Check the light and look at the screen.",
        })

    def test_nothing_is_said_to_the_page_before_the_worker_has_said_anything(self):
        silent_until = self.clock.now + 0.3
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(
            camera, calibration, lambda now: None if now < silent_until else (True, 0.5, 0.45, 30.0))
        self.run_it()
        self.assertEqual(self.browser.guides[0]["advice"], "Hold still")  # not "I can't see your face" for a moment first

    def test_a_worker_that_never_says_anything_reads_as_no_face_after_a_moment(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, None)
        self.assertIn("lined up", self.stops_at_the_guide())
        self.assertFalse(self.browser.guides[-1]["found"])

    def test_readings_that_have_stopped_arriving_are_no_face_and_cannot_pass_the_guide(self):
        def stale(camera, calibration):
            tracker = self.make_tracker(camera, calibration)
            tracker.stale = True  # the last reading fits, but it is old
            return tracker

        self.gaze.tracker_factory = stale
        self.assertIn("lined up", self.stops_at_the_guide())
        self.assertFalse(any(g["fits"] for g in self.browser.guides))
        self.assertFalse(self.browser.guides[-1]["found"])

    def test_a_head_that_never_fits_ends_the_job_with_what_to_try_and_turns_the_camera_off(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.5, 0.45, 80.0))
        message = self.stops_at_the_guide()
        for words in ("lined up", "camera was turned off", "light", "about 30 cm from the screen", "whole face"):
            self.assertIn(words, message)
        self.assertNotIn("arm's length", message)
        # It is read on the result card, which has "Try again" and no Start anyway key. So it says when that key is there.
        self.assertIn("when you try again, press Start anyway to skip the outline", message)
        self.assertNotIn("Or press Start anyway", message)
        self.assertGreaterEqual(self.seconds_of_guide(), GUIDE_SECONDS)
        self.assertLess(self.seconds_of_guide(), GUIDE_SECONDS + 2 / GUIDE_RATE)
        self.assertEqual({g["advice"] for g in self.browser.guides}, {"Come closer"})

    def test_start_anyway_goes_straight_to_the_dots_with_the_guide_off(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.5, 0.45, 80.0))
        self.browser.skips_after = 4
        self.run_it()
        self.assertLess(self.events.index(("guide", False)), self.events.index(("dot", DOTS[0])))
        self.assertLess(self.seconds_of_guide(), 1.0)  # nothing like the minute it would have waited
        self.assertTrue(self.t.paths.gaze_calibration.exists())

    def test_closing_the_page_during_the_guide_turns_the_camera_off_at_once(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.3, 0.45, 30.0))
        self.browser.closes_the_guide_after = 3
        self.assertEqual(self.stops_at_the_guide(), PAGE_GONE)
        self.assertEqual(len(self.browser.guides), 4)  # no reading follows
        self.assertLess(self.seconds_of_guide(), 1.0)

    def test_a_page_that_has_left_before_the_first_reading_is_noticed_too(self):
        def leaves(now):
            self.gaze.page.left("job1")
            return None

        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, leaves)
        self.assertEqual(self.stops_at_the_guide(), PAGE_GONE)
        self.assertEqual(self.browser.guides, [])

    def test_a_page_that_goes_quiet_during_the_guide_turns_the_camera_off_within_the_page_seconds(self):
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, (True, 0.3, 0.45, 30.0))
        self.browser.silent = True  # a hidden or frozen tab: no word, not even "left"
        began = self.clock.now
        self.assertEqual(self.stops_at_the_guide(), PAGE_GONE)
        self.assertLessEqual(self.clock.now - began, PAGE_SECONDS + 2 / GUIDE_RATE)

    def test_a_page_that_says_it_is_there_is_not_cut_off_however_long_the_guide_takes(self):
        arrives = self.clock.now + 4 * PAGE_SECONDS  # longer than the page may stay quiet, and it never does
        self.gaze.tracker_factory = lambda camera, calibration: self.with_head(camera, calibration, lambda now: (
            (True, 0.5, 0.45, 30.0) if now >= arrives else (True, 0.3, 0.45, 30.0)))
        self.run_it()
        self.assertTrue(self.t.paths.gaze_calibration.exists())

    def test_a_worker_that_dies_while_the_guide_is_up_is_found_out_within_a_moment_not_a_minute(self):
        class Dying(FakeTracker):
            def guide(inner, on):
                super().guide(on)
                if inner.events.count(("guide", True)) > 1:  # it took the first, and has gone by the time it is asked again
                    raise GazeError("Gaze tracking isn't running: The camera stopped sending pictures (exit code 1).")

        def make(camera, calibration):
            self.tracker = Dying(self.events, camera, calibration, self.reply, self.t.ctx, self.clock)
            self.tracker.head = None  # and no reading comes any more
            return self.tracker

        self.gaze.tracker_factory = make
        began = self.clock.now
        self.assertIn("camera stopped sending pictures", self.stops_at_the_guide())
        self.assertLess(self.clock.now - began, 2.0)  # not GUIDE_SECONDS

    def test_a_worker_that_has_died_during_the_guide_is_a_plain_job_error_and_the_camera_goes_off(self):
        def dying(camera, calibration):
            tracker = self.make_tracker(camera, calibration)
            tracker.fail["guide"] = "Gaze tracking isn't running: no output"
            return tracker

        self.gaze.tracker_factory = dying
        self.assertIn("isn't running", self.stops_at_the_guide())

    def test_the_guide_tells_the_log_what_is_happening(self):
        job = self.job()
        self.gaze.run_action("calibrate", job)
        lines = job.to_dict()["lines"]
        self.assertTrue(any("outline" in line for line in lines), lines)
        self.assertLess(next(i for i, line in enumerate(lines) if "outline" in line),
                        next(i for i, line in enumerate(lines) if "Dot 1 of 24" in line))


class DotPageTest(unittest.TestCase):
    """The page is a tab the person can close, so the job holding the camera never waits on it for long."""

    def setUp(self):
        self.page = DotPage(timeout=0.05)
        self.published = []
        self.job = Job(id="j1", component_id="gaze", action="calibrate", _bus=self)
        self.page.open("j1")

    def publish(self, kind, data):
        self.published.append((kind, data))

    def test_a_dot_goes_to_the_page_with_what_it_needs_to_draw_it(self):
        self.page.shown("j1", 1)
        self.page.show(self.job, (0.1, 0.2), 1, 14)
        self.assertEqual(self.published, [("gaze.dot", {"job_id": "j1", "uv": [0.1, 0.2], "number": 1, "total": 14})])

    def test_a_dot_nobody_put_up_ends_the_wait(self):
        with self.assertRaises(JobError):
            self.page.show(self.job, (0.1, 0.2), 1, 14)

    def test_word_from_another_job_doesnt_count(self):
        self.page.shown("old", 1)
        with self.assertRaises(JobError):
            self.page.show(self.job, (0.1, 0.2), 1, 14)
        self.page.left("old")
        self.page.check()

    def test_the_wait_ends_when_the_word_arrives(self):
        page = DotPage(timeout=1.0)
        page.open("j1")
        threading.Timer(0.01, page.shown, ("j1", 2)).start()
        page.show(self.job, (0.1, 0.2), 2, 14)

    def test_once_left_it_stays_left(self):
        self.page.left("j1")
        with self.assertRaises(JobError):
            self.page.check()
        with self.assertRaises(JobError):
            self.page.show(self.job, (0.1, 0.2), 1, 14)

    def test_a_number_that_isnt_one_is_ignored(self):
        for number in (None, "1", True, 1.5):
            self.page.shown("j1", number)
        with self.assertRaises(JobError):
            self.page.show(self.job, (0.1, 0.2), 1, 14)


class DotPageGuideTest(unittest.TestCase):
    """During the guide the page says it is alive and may say "Start anyway"; if it says nothing for too long it has gone."""

    def setUp(self):
        self.now = 100.0
        self.page = DotPage(timeout=3.0, clock=lambda: self.now)
        self.published = []
        self.job = Job(id="j1", component_id="gaze", action="calibrate", _bus=self)
        self.page.open("j1")

    def publish(self, kind, data):
        self.published.append((kind, data))

    def test_a_reading_goes_to_the_page_with_the_jobs_id(self):
        self.assertFalse(self.page.guide(self.job, {"found": False}))
        self.assertEqual(self.published, [("gaze.guide", {"job_id": "j1", "found": False})])

    def test_start_anyway_is_passed_to_the_job_and_stays_pressed(self):
        self.page.skip("j1")
        self.assertTrue(self.page.guide(self.job, {"found": False}))
        self.assertTrue(self.page.guide(self.job, {"found": False}))

    def test_a_new_job_starts_with_nothing_pressed(self):
        self.page.skip("j1")
        self.page.open("j2")
        self.assertFalse(self.page.guide(Job(id="j2", component_id="gaze", action="calibrate", _bus=self), {}))

    def test_word_from_another_job_or_nonsense_is_ignored(self):
        for other in ("old", None, 5, ["j1"]):
            self.page.skip(other)
            self.page.alive(other)
            self.page.left(other)
        self.assertFalse(self.page.guide(self.job, {}))

    def test_a_page_that_has_said_nothing_for_the_timeout_has_gone(self):
        self.page.guide(self.job, {})  # the first reading: the page has the whole timeout to answer it
        self.now += 3.0
        self.page.guide(self.job, {})
        self.now += 0.1
        with self.assertRaises(JobError) as caught:
            self.page.guide(self.job, {})
        self.assertEqual(str(caught.exception), PAGE_GONE)

    def test_alive_starts_the_count_again(self):
        self.page.guide(self.job, {})
        for _ in range(10):
            self.now += 2.9
            self.page.alive("j1")
            self.page.guide(self.job, {})

    def test_the_time_before_the_first_reading_does_not_count_against_the_page(self):
        self.now += 30.0  # the camera took its time to open
        self.page.guide(self.job, {})

    def test_a_page_that_left_has_gone_at_once(self):
        self.page.left("j1")
        with self.assertRaises(JobError):
            self.page.guide(self.job, {})

    def test_a_page_that_has_gone_says_so_in_words_that_are_true_for_esc_too(self):
        self.page.left("j1")
        with self.assertRaises(JobError) as caught:
            self.page.guide(self.job, {})
        message = str(caught.exception)
        for words in ("stopped", "Esc", "closed or hidden", "camera was turned off", "Nothing was saved"):
            self.assertIn(words, message)
        self.assertIsNone(caught.exception.result)  # a run that stopped is not a run that was refused

    def test_once_closed_it_hears_nothing(self):
        self.page.close()
        self.page.skip("j1")
        self.page.alive("j1")
        self.page.left("j1")
        self.page.open("j1")
        self.assertFalse(self.page.guide(self.job, {}))


class SaveCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.path = self.t.paths.gaze_calibration

    def tearDown(self):
        self.t.cleanup()

    def test_it_makes_the_folder_and_what_it_writes_loads_back(self):
        self.assertFalse(self.path.parent.exists())  # state/ isn't made by AppPaths.ensure
        saved = {**a_calibration(), "when": 5.0}
        save_calibration(self.path, saved)
        self.assertEqual(load_calibration(self.path), saved)

    def test_a_write_that_fails_leaves_the_old_file_and_no_temp_file(self):
        old = {**a_calibration(), "when": 5.0}
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
        self.calibrated_for(VIEW_AREA, radius=(0.07, 0.11))  # here the screen is the viewport, so places stay as they are
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
        return [ring and [round(share, 6) for share in ring] for ring in drawn[:-1]]

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

    def test_the_ring_is_moved_from_the_screen_onto_the_viewport(self):
        self.calibrated_for({"left": 2.0, "top": 80.0, "width": 1840.0, "height": 550.0}, radius=(0.05, 0.11))  # twice as wide
        self.where = lambda t: (0.25, 0.4, 0.9)
        self.run_test()
        self.assertEqual(self.rings()[20], [0.5, 0.4, 0.1, 0.11])

    def test_blender_being_resized_or_moved_changes_nothing_for_the_test(self):
        self.blender.scene.view = (1500, 1000)
        self.calibrated_for({"left": 0, "top": 0, "width": 1000, "height": 700})
        self.assertTrue(self.run_test()["tested"])

    def test_testing_needs_blender_on_the_screen_it_was_calibrated_on(self):
        self.calibrated_for({"left": -2000, "top": 0, "width": 1000, "height": 700})
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("screen", str(caught.exception))
        self.assertIsNone(self.tracker)  # the camera was never opened to find that out

    def test_testing_needs_macos_to_find_blenders_window(self):
        self.frame = None
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("window", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_testing_needs_an_add_on_that_says_where_its_viewport_is(self):
        self.blender.scene.gaze_place = lambda: None
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("Update", str(caught.exception))
        self.assertIsNone(self.tracker)

    def test_testing_needs_a_3d_viewport(self):
        self.blender.scene.view = None
        with self.assertRaises(JobError) as caught:
            self.run_test()
        self.assertIn("3D viewport", str(caught.exception))
        self.assertIsNone(self.tracker)

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

    def run_job(self, action, params=None):
        center = ControlCenter(self.t.ctx, [self.gaze], bus=self.browser)
        job = center.run_action("gaze", action, params)
        center.jobs.wait(job.id, 30)
        self.assertIsNot(job.state, JobState.RUNNING)
        return job

    def test_calibrate_and_test_run_as_jobs_from_the_card(self):
        self.install()
        job = self.run_job("calibrate", {"area": SCREEN})
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
        job = self.run_job("calibrate", {"area": SCREEN})
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
        self.gaze.clock = time.time  # the guide counts real seconds, because the worker's readings are stamped with them
        for name, value in (("DOT_SECONDS", 0.25), ("SETTLE_SECONDS", 0.05), ("HOLD_SECONDS", 0.3)):
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

    def test_the_fake_face_passes_the_guide_by_itself_and_the_dots_follow(self):
        job = Job(id="job1", component_id="gaze", action="calibrate", _bus=self.browser, params={"area": SCREEN})
        try:
            self.gaze.run_action("calibrate", job)
        except JobError as exc:  # a poor fit is a fair outcome for a face that doesn't follow the dots
            self.assertIn("wasn't saved", str(exc))
        self.assertTrue(self.browser.guides)
        self.assertEqual(self.browser.guides[-1]["advice"], "Hold still")
        self.assertTrue(self.browser.guides[-1]["fits"])
        self.assertEqual(self.browser.guides[-1]["x"], 0.5)
        self.assertEqual(self.browser.guides[-1]["scale"], 1.0)
        self.assertEqual(sum(e[0] == "dot" for e in self.events), len(DOT_ORDER))  # no one pressed anything, and every dot came up
        self.assertFalse(self.workers[0].alive)
        self.assertIsNone(self.t.ctx.camera.holder)

    def test_calibrating_sends_the_check_dots_as_check_dots_and_leaves_no_worker_running(self):
        try:
            job = Job(id="job1", component_id="gaze", action="calibrate", _bus=self.browser, params={"area": SCREEN})
            result = self.gaze.run_action("calibrate", job)
            message = ""
        except JobError as exc:  # the fake face doesn't follow the dots, so a poor fit is a fair outcome here
            result, message = None, str(exc)
        self.assertNotIn("check dots weren't seen", message)  # they were held back from the fit, as the worker needs
        self.assertNotIn("Not enough face readings", message)
        if result is not None:
            self.assertEqual(load_calibration(self.t.paths.gaze_calibration)["screen"], SCREEN)
        else:
            self.assertIn("wasn't saved", message)
            self.assertFalse(self.t.paths.gaze_calibration.exists())
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(self.workers[0].alive)  # the camera's process is gone, whichever way it went
        self.assertIsNone(self.t.ctx.camera.holder)


if __name__ == "__main__":
    unittest.main()
