"""Replay pointer samples through Unity's Voice pipeline, including stale views and duplicate names."""
import json

from gazevoice.voice.gaze import Area
from gazevoice.voice.service import PointerTracker
from .test_voice_service import FakeBridge, FakeStt, FakeSession, VoiceTestBase, THERE_WORDS, THERE_REFINED, EAGLE, PERCH
from gazevoice.voice.brains import TurnResult


class UnityBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.view = "scene-session:1"
        self.valid = True
        self.no_view = False
        self.requests = []

    def request(self, req, timeout=None):
        self.requests.append(dict(req))
        cmd = req["cmd"]
        if cmd == "ping":
            return {**super().request(req, timeout), "gaze": True, "unity": "2022.3"}
        if cmd == "selection":
            return {**super().request(req, timeout), "selected_ids": [999] if self.selected else []}
        if cmd == "gaze_view":
            return {"ok": not self.no_view, "viewport_id": self.view,
                    "screen_area": {"left": 100, "top": 80, "width": 800, "height": 600}}
        if cmd == "gaze_validate":
            return {"ok": self.valid}
        if cmd in ("gaze_pick", "gaze_piece", "gaze_validate", "gaze_ground") or (cmd == "gaze_show" and req.get("marks")):
            if req.get("viewport_id") != self.view:
                return {"ok": False, "error": "stale_viewport"}
            if cmd == "gaze_ground":
                return {"ok": True, "point": [2.0, 0.0, 3.0], "plane": "y=0"}
        return super().request(req, timeout)


class UnityGazeTest(VoiceTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = UnityBridge()
        self.gaze = PointerTracker()
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
        self.editor = FakeSession([TurnResult("Made Eagle blue.", False, ["set_color"])])

    def start(self, mode="on"):
        self.build(host="unity", gaze_mode=mode, gaze_source="mouse").start()
        for i in range(8):
            self.gaze.feed(.5, .5, 1002.5 + i * .05, self.bridge.view)

    def test_mouse_replay_resolves_target_and_passes_id_to_both_brains(self):
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(record["focus"]["gaze"]["host"], "unity")
        self.assertIn("Unity target instance ID: 116", self.editor.sent[0])
        self.assertIn("Unity target instance ID: 116", json.loads(self.refiner.sent[0])["gaze"])
        self.assertTrue(record["undo_step"])
        self.assertFalse(self.bridge.crops)
        self.assertEqual(record["gaze"]["viewport_id"], self.bridge.view)
        self.assertFalse(record["gaze"]["matched_selection"])

    def test_empty_here_creates_at_work_plane_without_selection(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.refiner = FakeSession([TurnResult('{"kind":"command","text":"Make a sofa here."}', False)])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(record["gaze"]["status"], "resolved")
        self.assertEqual(record["gaze"]["misses"][0]["point"], [2.0, 0.0, 3.0])
        self.assertIn("placement point (2.00, 0.00, 3.00)", self.editor.sent[0])
        self.assertIn("placement point (2.00, 0.00, 3.00)", json.loads(self.refiner.sent[0])["gaze"])
        self.assertFalse([r for r in self.bridge.requests if r["cmd"] == "gaze_validate"])

    def test_polite_plural_creation_uses_empty_space_point(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Can you add two lamps there"], words=[{"text": "there", "start": 1.0, "end": 1.2}])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("placement point (2.00, 0.00, 3.00)", self.editor.sent[0])

    def test_named_object_move_uses_empty_space_destination(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Move the lamp there"], words=[{"text": "there", "start": 1.0, "end": 1.2}])
        self.refiner = FakeSession([TurnResult('{"kind":"command","text":"Move the lamp there."}', False)])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("placement point (2.00, 0.00, 3.00)", self.editor.sent[0])

    def test_typed_command_does_not_request_a_gaze_view(self):
        self.start()
        self.bridge.requests.clear()
        self.service.say("Make a cube")
        self.assertFalse([r for r in self.bridge.requests if r["cmd"] == "gaze_view"])

    def test_ordinary_creation_with_it_needs_no_gaze_target(self):
        self.bridge.selected = []
        self.bridge.no_view = True
        self.stt = FakeStt(["Add a red cube and call it Bob"], words=[{"text": "it", "start": 1.0, "end": 1.2}])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertTrue(self.editor.sent)

    def test_empty_here_leaves_existing_object_decision_to_editor(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Make this yellow here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertIn("placement point", self.editor.sent[0])

    def test_empty_here_has_no_target_to_validate(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.refiner = FakeSession([TurnResult('{"kind":"command","text":"Make a sofa here."}', False)])
        self.start()
        self.bridge.valid = False
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertFalse([r for r in self.bridge.requests if r["cmd"] == "gaze_validate"])

    def test_empty_here_without_forward_intersection_lets_editor_decide(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        original = self.bridge.request
        def no_plane(req, timeout=None):
            if req["cmd"] == "gaze_ground":
                self.bridge.requests.append(dict(req))
                return {"ok": False, "error": "no_forward_plane_intersection"}
            return original(req, timeout)
        self.bridge.request = no_plane
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertEqual(record["gaze"]["misses"], [{"word": "here", "at": record["gaze"]["misses"][0]["at"]}])
        self.assertTrue(self.editor.sent)

    def test_empty_here_near_neighbor_keeps_empty_destination(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] + [EAGLE] * (len(points) - 1)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.refiner = FakeSession([TurnResult('{"kind":"command","text":"Make a sofa here."}', False)])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(record["gaze"]["targets"], [])
        self.assertEqual(record["gaze"]["misses"][0]["point"], [2.0, 0.0, 3.0])

    def test_unpickable_surface_does_not_become_empty_destination(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [{"occluded": True, "name": "Unpickable surface"}] * len(points)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertFalse([r for r in self.bridge.requests if r["cmd"] == "gaze_ground"])
        self.assertTrue(self.editor.sent)

    def test_view_changed_during_refinement_keeps_resolved_destination(self):
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [None] * len(points)
        self.stt = FakeStt(["Make a sofa here"], words=[{"text": "here", "start": 1.0, "end": 1.2}])
        self.refiner = FakeSession([TurnResult('{"kind":"command","text":"Make a sofa here."}', False)])
        original = self.refiner.send
        def change_view(text, **kwargs):
            self.bridge.view = "scene-session:2"
            return original(text, **kwargs)
        self.refiner.send = change_view
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("placement point", self.editor.sent[0])

    def test_watch_only_records_target_and_leaves_selection_in_the_command(self):
        self.start("shadow")
        record = self.speak()
        self.assertEqual(record["gaze"]["targets"][0]["candidates"][0]["uid"], 116)
        self.assertNotIn("gaze", record["focus"])
        self.assertIn("selected Chair", self.editor.sent[0])

    def test_old_view_samples_are_discarded_before_grounding(self):
        self.start()
        self.bridge.view = "scene-session:2"
        record = self.speak()
        self.assertFalse(record["gaze"]["targets"])
        self.assertNotIn("gaze", record["focus"])
        self.assertIn("selected Chair", self.editor.sent[0])

    def test_deleted_object_after_refining_prevents_the_edit(self):
        self.start()
        self.bridge.valid = False
        record = self.speak()
        self.assertEqual(record["stage"], "failed")
        self.assertIn("gone", record["error"])
        self.assertFalse(self.editor.sent)
        self.assertFalse(self.bridge.pushed)

    def test_no_scene_view_uses_the_explicit_selection(self):
        self.start()
        self.bridge.no_view = True
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertEqual(record["gaze"]["reason"], "no_viewport")
        self.assertNotIn("gaze", record["focus"])

    def test_camera_coordinates_use_unity_gui_screen_rect_without_blender_window_lookup(self):
        self.build(host="unity")
        view = self.service._view_area(self.bridge.request({"cmd": "gaze_view"}))
        self.assertEqual(view, Area(100, 80, 800, 600))
        self.assertIsNone(self.service._view_area({"ok": False}))

    def test_same_named_object_keeps_distinct_instance_id_in_prompt(self):
        self.bridge.pick_reply = lambda points: [{**EAGLE, "name": "Cube", "uid": 222}] * len(points)
        self.start()
        record = self.speak()
        self.assertEqual(record["gaze"]["targets"][0]["candidates"][0]["uid"], 222)
        self.assertIn("Unity target instance ID: 222", self.editor.sent[0])

    def test_poll_declares_mode_source_and_liveness_to_the_scene_indicator(self):
        self.start("shadow")
        self.service._poll_once(self.service._run, 0, self.bridge.boot)
        polls = [r for r in self.bridge.requests if r["cmd"] == "poll" and "gaze_mode" in r]
        self.assertTrue(polls)
        self.assertEqual((polls[-1]["gaze_mode"], polls[-1]["gaze_source"], polls[-1]["gaze_live"]),
                         ("shadow", "mouse", True))

    def test_missing_pointer_and_no_selection_reaches_editor(self):
        self.bridge.selected = []
        self.start()
        self.bridge.no_view = True
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertTrue(self.editor.sent)

    def test_number_key_choice_is_scoped_to_current_view(self):
        from gazevoice.voice.service import _Choice
        self.start()
        self.service._ground_view = self.bridge.view
        choice = _Choice(["Chair", "Eagle"])
        self.service._choice = choice
        self.bridge.add("gaze_choice", number=2, viewport_id="other-view")
        self.service._poll_once(self.service._run, 0, self.bridge.boot)
        self.assertIsNone(choice.number)
        self.bridge.add("gaze_choice", number=2, viewport_id=self.bridge.view)
        self.service._poll_once(self.service._run, 0, self.bridge.boot)
        self.assertEqual(choice.number, 2)
        self.assertTrue(choice.answered.is_set())

    def test_watch_only_missing_view_does_not_change_voice_behavior(self):
        self.bridge.selected = []
        self.start("shadow")
        self.bridge.no_view = True
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertTrue(self.editor.sent)

    def test_unavailable_camera_view_without_selection_reaches_editor(self):
        self.bridge.selected = []
        self.start()
        self.service._placed = lambda *_: None
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertTrue(self.editor.sent)
