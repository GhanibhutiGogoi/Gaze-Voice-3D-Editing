import hashlib
import json
import unittest
from unittest.mock import patch

from gazevoice.components import build_components
from gazevoice.components.base import ComponentState, Health
from gazevoice.components.gaze import (
    MEDIAPIPE,
    MODEL_SHA256,
    MODEL_SIZE,
    MODEL_URL,
    WORKER,
    Gaze,
    load_calibration,
)
from gazevoice.components.microphone import LIST_DEVICES
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut
from gazevoice.settings import Settings, update_settings
from gazevoice.voice.wiring import voice_readiness
from gazevoice.workflow import workflow_stages

from .helpers import TempCtx, make_job
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


class GazeUnitTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={**UV, **FFMPEG})
        self.t.runner.add(LIST_DEVICES, stderr=MAC)
        self.gaze = Gaze(self.t.ctx)
        self.venv = self.t.paths.venvs / "gaze"
        self.py = self.venv / "bin" / "python"

    def tearDown(self):
        self.t.cleanup()

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


if __name__ == "__main__":
    unittest.main()
