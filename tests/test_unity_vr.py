"""Unity VR: Unity plays the scene to a headset, and the eyes (or the head) say what "here" and "this" mean."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice.components.base import Health
from gazevoice.components.microphone import pick_device
from gazevoice.components.unity_vr import NO_EYES, NO_EYE_FEATURE, UnityVR
from gazevoice.settings import Settings, unity_project_of, update_settings
from gazevoice.voice.brains import TurnResult
from gazevoice.voice.commands import editor_message
from gazevoice.voice.gaze import PointingWord, ground_cast
from gazevoice.voice.service import NO_VR, VR_KEY, VoiceError

from .helpers import TempCtx
from .test_voice_service import FakeBridge, FakeSession, FakeStt, VoiceTestBase, wait_for

TERRAIN, SOFA, WALL = 7, 42, 9


def looking(uid, name, point, normal=(0.0, 1.0, 0.0), start=1001.0, end=1004.0, eyes=True, parent=None):
    """Gaze samples at 45 a second, all on one thing, the way GazeVoiceVR.cs reports them."""
    samples, t = [], start
    while t <= end:
        samples.append({"t": round(t, 4), "uid": uid, "name": name, "parent": parent,
                        "point": list(point) if uid is not None else None,
                        "normal": list(normal) if uid is not None else None, "eyes": eyes})
        t += 1 / 45
    return samples


def word(text, start=1002.5, end=1002.7):
    return PointingWord(text, start, end)


class GroundCastTest(unittest.TestCase):
    def test_here_on_the_terrain_is_a_placement_point_in_the_middle_of_where_they_looked(self):
        samples = looking(TERRAIN, "Terrain", (2.0, 0.1, 5.0))
        samples[10]["point"] = [9.0, 9.0, 9.0]  # a saccade far off doesn't move the middle
        found = ground_cast([word("here")], samples)
        self.assertEqual(found.status, "resolved")
        self.assertEqual(found.targets, [])
        miss = found.misses[0].to_dict()
        self.assertEqual(miss["point"], [2.0, 0.1, 5.0])
        self.assertEqual((miss["plane"], miss["on"]), ("surface", "Terrain"))

    def test_here_on_a_surface_facing_up_is_a_placement_point_too(self):
        found = ground_cast([word("there")], looking(SOFA, "Table", (1.0, 0.8, 2.0)))
        self.assertEqual(found.misses[0].on, "Table")

    def test_this_on_an_object_is_that_object(self):
        found = ground_cast([word("this")], looking(SOFA, "Seat", (1.0, 0.5, 2.0), normal=(0, 0, 1), parent="Sofa"))
        top = found.targets[0].candidates[0]
        self.assertEqual((top.uid, top.name, top.score), (SOFA, "Seat (in Sofa)", 1.0))
        self.assertEqual(top.point, [1.0, 0.5, 2.0])

    def test_here_on_a_wall_is_the_wall(self):
        found = ground_cast([word("here")], looking(WALL, "Wall", (0.0, 1.5, 3.0), normal=(0, 0, -1)))
        self.assertEqual(found.targets[0].candidates[0].uid, WALL)
        self.assertEqual(found.misses, [])

    def test_the_object_looked_at_longest_wins(self):
        samples = looking(SOFA, "Sofa", (1, 0.5, 2), normal=(0, 0, 1), start=1002.0, end=1002.6)
        samples += looking(WALL, "Wall", (0, 1.5, 3), normal=(0, 0, 1), start=1002.62, end=1002.8)
        found = ground_cast([word("that")], samples)
        self.assertEqual([c.uid for c in found.targets[0].candidates], [SOFA, WALL])

    def test_looking_at_the_sky_finds_nothing(self):
        found = ground_cast([word("here")], looking(None, None, (0, 0, 0)))
        self.assertEqual(found.status, "none")
        self.assertIsNone(found.misses[0].point)
        self.assertEqual(found.to_dict()["radius"], [])

    def test_too_few_samples_is_no_gaze(self):
        found = ground_cast([word("here")], looking(TERRAIN, "Terrain", (0, 0, 0), start=1002.5, end=1002.55))
        self.assertEqual((found.status, found.misses, found.targets), ("none", [], []))
        self.assertEqual(found.windows[0].readings, 3)

    def test_odd_samples_are_skipped(self):
        samples = looking(TERRAIN, "Terrain", (2, 0, 5)) + [None, {"t": "x"}, {"t": 1002.5, "uid": 3, "point": "x"}]
        self.assertEqual(ground_cast([word("here")], samples).misses[0].point, [2.0, 0.0, 5.0])


class DescribeTest(unittest.TestCase):
    def test_the_editor_reads_a_placement_point_on_what_was_looked_at(self):
        gaze = {"status": "resolved", "targets": [], "radius": [], "host": "unity", "source": "head",
                "misses": [{"word": "here", "at": 0.9, "point": [2, 0.1, 5], "plane": "surface", "on": "Terrain"}]}
        message = editor_message("Make a sofa here.", "make a sofa here", {"selected": [], "mode": "editor",
                                                                             "gaze": gaze})
        self.assertEqual(message.splitlines()[1:], [
            'Heard: "make a sofa here"',
            'Focus (gaze): "here" at 0.9 s → placement point (2.00, 0.10, 5.00) on Terrain, where they looked '
            '(no object target)',
            "  VR gaze from where the head points (no eye tracking), so it is rough · selected: nothing · mode editor",
        ])


class SettingsTest(unittest.TestCase):
    def test_unity_vr_keeps_a_project_of_its_own(self):
        game, vr = str(Path.home() / "Unity" / "Game"), str(Path.home() / "Unity" / "GazeVoice VR")
        settings = update_settings(Settings(), {"unity_project": game, "unity_vr_project": vr})
        self.assertEqual(unity_project_of(settings), game)
        self.assertEqual(unity_project_of(update_settings(settings, {"voice_host": "unity_vr"})), vr)

    def test_the_vr_project_must_be_an_absolute_path(self):
        with self.assertRaisesRegex(ValueError, "unity_vr_project"):
            update_settings(Settings(), {"unity_vr_project": "GazeVoice VR"})


class HeadsetMicTest(unittest.TestCase):
    DEVICES = [("0", "Microphone Array (Realtek)"), ("1", "Headset Microphone (Oculus Virtual Audio Device)")]

    def test_auto_picks_the_headsets_microphone_in_vr(self):
        self.assertEqual(pick_device(self.DEVICES, "", headset=True), ("1", self.DEVICES[1][1], True))

    def test_a_chosen_microphone_still_wins(self):
        self.assertEqual(pick_device(self.DEVICES, "0", headset=True)[0], "0")


class VRBridge(FakeBridge):
    """The Unity bridge in a VR project: it says it can play to a headset, and keeps where the eyes looked."""

    def __init__(self, samples=(), vr=True):
        super().__init__()
        self.vr, self.samples, self.asked = vr, list(samples), []
        self.selected = []

    def request(self, req, timeout=None):
        if self.down:
            raise ConnectionRefusedError("Unity is closed")
        if req["cmd"] == "ping":
            self.calls.append("ping")
            return {"ok": True, "bridge": "0.3.5", "key": "Ctrl+Alt+Space", "gaze": True, "vr": self.vr}
        if req["cmd"] == "vr_gaze":
            self.calls.append("vr_gaze")
            self.asked.append(dict(req))
            return {"ok": True, "playing": True,
                    "samples": [s for s in self.samples if req["t0"] <= s["t"] <= req["t1"]]}
        if req["cmd"] == "gaze_validate":
            self.calls.append("gaze_validate")
            return {"ok": True}
        return super().request(req, timeout)


HERE_WORDS = [{"text": "make", "start": 0.1, "end": 0.3}, {"text": "a", "start": 0.3, "end": 0.4},
              {"text": "sofa", "start": 0.5, "end": 0.8}, {"text": "here", "start": 0.9, "end": 1.1}]
THIS_WORDS = [{"text": "make", "start": 0.1, "end": 0.3}, {"text": "this", "start": 0.4, "end": 0.6},
              {"text": "red", "start": 0.7, "end": 0.9}]


class VRVoiceTest(VoiceTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = VRBridge(looking(TERRAIN, "Terrain", (2.0, 0.1, 5.0)))
        self.stt = FakeStt(["make a sofa here"], words=HERE_WORDS)
        self.refiner = FakeSession([TurnResult(json.dumps({"kind": "command", "text": "Make a sofa here."}), False)])
        self.editor = FakeSession([TurnResult("Made a sofa.", False, ["execute_csharp"])])

    def run_vr(self, gaze_mode="on"):
        self.build(host="unity", vr=True, gaze_mode=gaze_mode, gaze_source="mouse").start()
        return self.speak()

    def test_make_a_sofa_here_puts_it_where_they_looked(self):
        record = self.run_vr()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.editor.sent[0].splitlines()[2:], [
            'Focus (gaze): "here" at 0.9 s → placement point (2.00, 0.10, 5.00) on Terrain, where they looked '
            '(no object target)',
            "  VR gaze from the headset's eye tracking · selected: nothing · mode OBJECT",
        ])
        self.assertEqual(record["gaze"]["source"], "eyes")
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make a sofa here."])  # one undo step, kept when Play stops

    def test_it_asks_unity_for_the_gaze_around_the_clip_and_nothing_on_screen(self):
        self.run_vr()
        asked = self.bridge.asked[0]
        self.assertLess(asked["t0"], 1002.0 - 0.35)
        self.assertGreater(asked["t1"], 1003.0)
        for desktop_only in ("gaze_pick", "gaze_view", "gaze_ground", "gaze_piece"):
            self.assertNotIn(desktop_only, self.bridge.calls)

    def test_this_is_the_object_looked_at(self):
        self.bridge.samples = looking(SOFA, "Sofa", (1.0, 0.5, 2.0), normal=(0, 0, 1), eyes=False)
        self.stt = FakeStt(["make this red"], words=THIS_WORDS)
        record = self.run_vr()
        lines = self.editor.sent[0].splitlines()
        self.assertEqual(lines[2], 'Focus (gaze): "this" at 0.4 s → Sofa (100%)')
        self.assertIn(f"Unity target instance ID: {SOFA}", lines[3])
        self.assertIn("head points", lines[-1])
        self.assertIn("gaze_validate", self.bridge.calls)
        self.assertEqual(record["gaze"]["source"], "head")

    def test_watch_only_records_the_gaze_but_tells_the_editor_nothing(self):
        record = self.run_vr("shadow")
        self.assertEqual(record["gaze"]["misses"][0]["on"], "Terrain")
        self.assertNotIn("gaze", self.editor.sent[0])

    def test_gaze_off_doesnt_ask(self):
        self.run_vr("off")
        self.assertNotIn("vr_gaze", self.bridge.calls)

    def test_looking_at_nothing_says_so_and_goes_on(self):
        self.bridge.samples = []
        record = self.run_vr()
        self.assertEqual(record["stage"], "done")
        self.assertTrue(any("Couldn't tell where you looked" in text for text, _ in self.bridge.statuses))

    def test_a_controller_button_is_the_talk_key_and_unity_hears_voice_is_in_vr(self):
        self.build(host="unity", vr=True, gaze_mode="on").start()
        self.assertEqual(self.service.key, VR_KEY)
        self.assertTrue(self.service.snapshot()["vr"])
        self.assertIn(f"hold {VR_KEY} to talk", self.bridge.statuses[-1][0])
        wait_for(lambda: self.bridge.polls)
        self.assertTrue(self.bridge.polls[-1]["vr"])

    def test_a_project_that_cant_play_to_a_headset_is_refused(self):
        self.bridge.vr = False
        self.build(host="unity", vr=True)
        with self.assertRaises(VoiceError) as caught:
            self.service.start()
        self.assertEqual(str(caught.exception), NO_VR)


class HeadsetUnitTest(unittest.TestCase):
    STATUS = {"ok": True, "xr": True, "loaders": ["Open XR Loader"], "playing": False, "active": False,
              "key": "B or Y", "eye_feature": True, "eye_runtime": None, "eyes": False,
              "scene": "Assets/Scenes/GazeVoice VR.unity", "vr_scene": "Assets/Scenes/GazeVoice VR.unity"}

    def setUp(self):
        self.t = TempCtx()
        self.addCleanup(self.t.cleanup)
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"voice_host": "unity_vr"})
        self.unit = UnityVR(self.t.ctx)

    def check(self, **status):
        with patch.object(UnityVR, "_runtime", return_value=(None, "Oculus", [])), \
                patch.object(UnityVR, "_vr_status", return_value={**self.STATUS, **status}):
            return self.unit.check()

    def test_ready_offers_enter_vr_and_prepare(self):
        state = self.check()
        self.assertEqual(state.health, Health.READY)
        self.assertTrue(state.offers("enter_vr"))
        self.assertTrue(state.offers("prepare"))

    def test_without_the_vr_scene_it_says_to_prepare_it(self):
        state = self.check(vr_scene=None)
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Prepare VR scene", state.summary)

    def test_eye_tracking_that_link_doesnt_send_says_which_settings_to_turn_on(self):
        state = self.check(playing=True, active=True, headset="Quest Pro", eye_runtime=False)
        self.assertEqual(state.detail("Gaze"), NO_EYES)
        self.assertIn("where your head points", state.summary)

    def test_tracked_eyes_are_named(self):
        state = self.check(playing=True, active=True, eye_runtime=True, eyes=True)
        self.assertIn("Eyes", state.detail("Gaze"))
        self.assertIn("where you look", state.summary)

    def test_eye_gaze_turned_off_in_the_project(self):
        self.assertEqual(self.check(eye_feature=False).detail("Gaze"), NO_EYE_FEATURE)

    def test_prepare_asks_unity_to_build_the_scene(self):
        reply = {"ok": True, "scene": "Assets/Scenes/GazeVoice VR.unity", "made": True,
                 "xr": {"features": ["EyeGazeInteraction"], "repaired": 3}, "notes": []}
        with patch("gazevoice.components.unity_vr.bridge_request", return_value=reply) as request:
            from .helpers import make_job
            result = self.unit.run_action("prepare", make_job("unity_vr", "prepare"))
        self.assertEqual(request.call_args[0][0], {"cmd": "vr_setup"})
        self.assertEqual(result, {"scene": "Assets/Scenes/GazeVoice VR.unity", "made": True})

    def test_it_waits_in_unity_mode(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"voice_host": "unity"})
        self.assertIn("Unity VR", self.unit.check().summary)


if __name__ == "__main__":
    unittest.main()
