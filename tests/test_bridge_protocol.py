import importlib.util
import unittest
from pathlib import Path

PROTOCOL = Path(__file__).resolve().parents[1] / "blender_addon" / "gazevoice_bridge" / "protocol.py"


def load_protocol():
    # The add-on package imports bpy, so load protocol.py on its own.
    spec = importlib.util.spec_from_file_location("gv_bridge_protocol", PROTOCOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


protocol = load_protocol()


class FakeScene:
    def __init__(self):
        self.status = None
        self.state = None
        self.pushed = []
        self.undone = 0
        self.redone = 0
        self.ops_work = True
        self.is_busy = False
        self.dirty = True
        self.view = (1840, 1100)
        self.picked = []
        self.shown = []

    def version(self):
        return "5.2.2"

    def active_name(self):
        return "Chair"

    def selected_names(self):
        return ["Chair", "Leg.001"]

    def mode(self):
        return "OBJECT"

    def set_status(self, text, state="idle"):
        self.status = text
        self.state = state

    def object_names(self):
        return ["Chair", "Leg.001", "Table", "Camera", "Light"]

    def undo_push(self, message):
        self.pushed.append(message)
        return self.ops_work

    def undo(self):
        self.undone += 1
        return self.ops_work

    def redo(self):
        self.redone += 1
        return self.ops_work

    def key_label(self):
        return "⌥ Space"

    def busy(self):
        return self.is_busy

    def changed(self):
        return self.dirty

    def mark_unchanged(self):
        self.dirty = False

    def gaze_view(self):
        return self.view

    def gaze_pick(self, points):
        if self.view is None:
            return None
        self.picked.append(points)
        # anything right of 0.9 across misses; the rest hits the Eagle
        return [
            None if u > 0.9 else {"uid": 116, "name": "Eagle", "faces": 5120, "point": [u, v, 0.5]}
            for u, v in points
        ]

    def gaze_piece(self, uid, point):
        if uid != 116:
            return None
        return {"face": 3120, "part": "Wing.L", "piece_faces": 480, "object_faces": 5120}

    def gaze_show(self, dot, ring, marks):
        self.shown.append((dot, ring, marks))


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def handle(request, scene=None, events=None):
    return protocol.handle(request, scene or FakeScene(), events or protocol.EventQueue())


class BridgeProtocolTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((protocol.BRIDGE_PORT, protocol.BRIDGE_VERSION), (9877, "0.3.0"))

    def test_decode_lines_keeps_partial(self):
        buf = bytearray(b'{"cmd": "ping"}\n{"cmd": "selection"}\n{"cmd": "sta')
        msgs = protocol.decode_lines(buf)
        self.assertEqual([m["cmd"] for m in msgs], ["ping", "selection"])
        self.assertEqual(bytes(buf), b'{"cmd": "sta')

    def test_decode_bad_json_and_non_objects(self):
        msgs = protocol.decode_lines(bytearray(b"not json\n[1,2]\n\n"))
        self.assertEqual(msgs, [{"cmd": "__invalid__"}, {"cmd": "__invalid__"}])

    def test_encode_is_one_line(self):
        self.assertEqual(protocol.encode({"ok": True}), b'{"ok": true}\n')

    def test_ping(self):
        self.assertEqual(
            handle({"cmd": "ping"}, FakeScene()),
            {"ok": True, "bridge": "0.3.0", "blender": "5.2.2", "key": "⌥ Space"},
        )

    def test_selection(self):
        self.assertEqual(
            handle({"cmd": "selection"}, FakeScene()),
            {"ok": True, "active": "Chair", "selected": ["Chair", "Leg.001"], "mode": "OBJECT"},
        )

    def test_status_sets_text_state_and_truncates(self):
        scene = FakeScene()
        self.assertEqual(handle({"cmd": "status", "text": "x" * 500, "state": "listening"}, scene), {"ok": True})
        self.assertEqual((len(scene.status), scene.state), (200, "listening"))

    def test_status_unknown_state_falls_back_to_idle(self):
        scene = FakeScene()
        handle({"cmd": "status", "text": "hi", "state": "party"}, scene)
        self.assertEqual(scene.state, "idle")

    def test_status_requires_text(self):
        self.assertEqual(
            handle({"cmd": "status", "text": 5}, FakeScene()), {"ok": False, "error": "text must be a string"}
        )

    def test_poll_without_ack_drains_events(self):
        events = protocol.EventQueue()
        events.add("ptt", state="down")
        events.add("ptt", state="up")
        reply = handle({"cmd": "poll"}, events=events)
        self.assertEqual([(e["seq"], e["state"]) for e in reply["events"]], [(1, "down"), (2, "up")])
        self.assertEqual(handle({"cmd": "poll"}, events=events)["events"], [])

    def test_events_stay_until_a_poll_acknowledges_them(self):
        events = protocol.EventQueue()
        events.add("ptt", state="down")
        first = handle({"cmd": "poll", "ack": 0}, events=events)
        self.assertEqual([e["seq"] for e in first["events"]], [1])
        # that reply was lost (the app timed out), so the app polls again with the same ack
        events.add("ptt", state="up")
        again = handle({"cmd": "poll", "ack": 0}, events=events)
        self.assertEqual([e["seq"] for e in again["events"]], [1, 2])
        done = handle({"cmd": "poll", "ack": 2}, events=events)
        self.assertEqual((done["events"], done["boot"]), ([], events.boot))

    def test_an_ack_from_before_blender_restarted_drops_nothing(self):
        events = protocol.EventQueue()
        events.add("ptt", state="down")
        events.add("ptt", state="up")
        reply = handle({"cmd": "poll", "ack": 7, "boot": "an-earlier-blender"}, events=events)
        self.assertEqual([e["seq"] for e in reply["events"]], [1, 2])
        reply = handle({"cmd": "poll", "ack": 1, "boot": events.boot}, events=events)
        self.assertEqual([e["seq"] for e in reply["events"]], [2])

    def test_event_queue_is_bounded_and_knows_when_the_app_is_polling(self):
        clock = Clock()
        events = protocol.EventQueue(limit=3, clock=clock)
        self.assertFalse(events.live())
        for _ in range(5):
            events.add("toggle")
        self.assertEqual([e["seq"] for e in events.poll(None)], [3, 4, 5])
        self.assertTrue(events.live())
        clock.now += protocol.LIVE_SECONDS + 0.1
        self.assertFalse(events.live())
        self.assertNotEqual(protocol.EventQueue().boot, events.boot)

    def test_scene_names(self):
        self.assertEqual(handle({"cmd": "scene_names"}, FakeScene())["names"][:2], ["Chair", "Leg.001"])

    def test_undo_push_undo_redo(self):
        scene = FakeScene()
        self.assertEqual(handle({"cmd": "undo_push", "message": "GazeVoice: grow"}, scene), {"ok": True, "pushed": True})
        self.assertEqual(scene.pushed, ["GazeVoice: grow"])
        self.assertEqual(handle({"cmd": "undo"}, scene), {"ok": True})
        self.assertEqual(handle({"cmd": "redo"}, scene), {"ok": True})
        self.assertEqual((scene.undone, scene.redone), (1, 1))
        scene.ops_work = False
        self.assertEqual(handle({"cmd": "undo"}, scene), {"ok": False})

    def test_undo_push_skips_when_nothing_changed_since_begin_edit(self):
        scene = FakeScene()
        self.assertEqual(handle({"cmd": "begin_edit"}, scene), {"ok": True})
        request = {"cmd": "undo_push", "message": "GazeVoice: how tall is it", "only_if_changed": True}
        self.assertEqual(handle(request, scene), {"ok": True, "pushed": False})
        scene.dirty = True  # the edit changed the scene
        self.assertEqual(handle(request, scene), {"ok": True, "pushed": True})
        self.assertFalse(scene.dirty)
        self.assertEqual(scene.pushed, ["GazeVoice: how tall is it"])

    def test_undo_operations_wait_while_another_modal_operator_runs(self):
        scene = FakeScene()
        scene.is_busy = True
        for request in ({"cmd": "undo_push", "message": "m"}, {"cmd": "undo"}, {"cmd": "redo"}):
            self.assertEqual(handle(request, scene), {"ok": False, "error": "busy"})
        self.assertEqual((scene.pushed, scene.undone, scene.redone), ([], 0, 0))

    def test_undo_names_fit_blenders_limit(self):
        scene = FakeScene()
        handle({"cmd": "undo_push", "message": "GazeVoice: " + "make the chair taller " * 6}, scene)
        name = scene.pushed[0]
        self.assertLessEqual(len(name.encode()), 63)
        self.assertTrue(name.endswith("…") and name.startswith("GazeVoice: make the chair"))
        self.assertEqual(protocol.undo_name("GazeVoice: grow"), "GazeVoice: grow")
        self.assertLessEqual(len(protocol.undo_name("é" * 80).encode()), 63)

    def test_key_labels(self):
        self.assertEqual(protocol.key_label("SPACE", alt=True), "⌥ Space")
        self.assertEqual(protocol.key_label("F5"), "F5")
        self.assertEqual(protocol.key_label("NUMPAD_0", ctrl=True, shift=True), "⌃⇧ Numpad 0")
        self.assertEqual(protocol.key_label("T", oskey=True, alt=True), "⌥⌘ T")

    def test_undo_push_requires_message(self):
        self.assertFalse(handle({"cmd": "undo_push"}, FakeScene())["ok"])

    def test_unknown(self):
        self.assertEqual(handle({"cmd": "explode"}, FakeScene()), {"ok": False, "error": "unknown command"})


class GazeProtocolTest(unittest.TestCase):
    def test_gaze_view_reports_the_viewport_size(self):
        self.assertEqual(handle({"cmd": "gaze_view"}), {"ok": True, "width": 1840, "height": 1100})

    def test_gaze_view_says_when_there_is_no_viewport(self):
        scene = FakeScene()
        scene.view = None
        self.assertEqual(handle({"cmd": "gaze_view"}, scene), {"ok": False, "error": "no 3D viewport"})
        self.assertEqual(
            handle({"cmd": "gaze_pick", "points": [[0.5, 0.5]]}, scene), {"ok": False, "error": "no 3D viewport"}
        )

    def test_gaze_pick_returns_one_hit_per_point(self):
        scene = FakeScene()
        reply = handle({"cmd": "gaze_pick", "points": [[0.4, 0.3], [0.95, 0.3], [0, 1]]}, scene)
        self.assertTrue(reply["ok"])
        self.assertEqual(len(reply["hits"]), 3)
        self.assertEqual(reply["hits"][0], {"uid": 116, "name": "Eagle", "faces": 5120, "point": [0.4, 0.3, 0.5]})
        self.assertIsNone(reply["hits"][1])
        self.assertEqual(scene.picked, [[(0.4, 0.3), (0.95, 0.3), (0.0, 1.0)]])
        self.assertEqual(handle({"cmd": "gaze_pick", "points": []}), {"ok": True, "hits": []})

    def test_gaze_pick_refuses_too_many_points(self):
        scene = FakeScene()
        at_limit = handle({"cmd": "gaze_pick", "points": [[0.5, 0.5]] * protocol.MAX_POINTS}, scene)
        self.assertEqual(len(at_limit["hits"]), protocol.MAX_POINTS)
        over = handle({"cmd": "gaze_pick", "points": [[0.5, 0.5]] * (protocol.MAX_POINTS + 1)}, scene)
        self.assertEqual(over, {"ok": False, "error": f"at most {protocol.MAX_POINTS} points"})
        self.assertEqual(len(scene.picked), 1)

    def test_gaze_pick_refuses_points_off_the_viewport(self):
        scene = FakeScene()
        for points in ([[1.2, 0.5]], [[0.5, -0.1]], [[0.2, 0.2], [0.5, 1.0001]], [[float("nan"), 0.5]]):
            with self.subTest(points=points):
                reply = handle({"cmd": "gaze_pick", "points": points}, scene)
                self.assertEqual(reply, {"ok": False, "error": "points must be inside the viewport (0 to 1)"})
        for points in (None, "center", [[0.5]], [[0.5, 0.5, 0.5]], [["a", "b"]], [[True, 0.5]], [0.5, 0.5]):
            with self.subTest(points=points):
                reply = handle({"cmd": "gaze_pick", "points": points}, scene)
                self.assertEqual(reply, {"ok": False, "error": "points must be a list of [u, v] pairs"})
        self.assertEqual(scene.picked, [])

    def test_gaze_piece_reports_the_face_part_and_piece(self):
        scene = FakeScene()
        reply = handle({"cmd": "gaze_piece", "uid": 116, "point": [0.1, 0.2, 0.3]}, scene)
        self.assertEqual(reply, {"ok": True, "face": 3120, "part": "Wing.L", "piece_faces": 480, "object_faces": 5120})

    def test_gaze_piece_needs_a_uid_and_a_point(self):
        scene = FakeScene()
        bad_uid = {"ok": False, "error": "uid must be an integer"}
        bad_point = {"ok": False, "error": "point must be [x, y, z]"}
        self.assertEqual(handle({"cmd": "gaze_piece", "point": [0, 0, 0]}, scene), bad_uid)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": "116", "point": [0, 0, 0]}, scene), bad_uid)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": True, "point": [0, 0, 0]}, scene), bad_uid)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": 116}, scene), bad_point)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": 116, "point": [0, 0]}, scene), bad_point)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": 116, "point": [0, 0, "up"]}, scene), bad_point)
        self.assertEqual(handle({"cmd": "gaze_piece", "uid": 116, "point": [0, 0, float("inf")]}, scene), bad_point)

    def test_gaze_piece_says_when_the_object_is_gone(self):
        reply = handle({"cmd": "gaze_piece", "uid": 7, "point": [0, 0, 0]})
        self.assertEqual(reply, {"ok": False, "error": "no such mesh object"})

    def test_gaze_show_passes_the_drawing_on(self):
        scene = FakeScene()
        marks = [{"n": 1, "u": 0.4, "v": 0.3, "name": "Chair"}, {"n": 2, "u": 0.6, "v": 0.7, "name": "Table"}]
        request = {"cmd": "gaze_show", "dot": [0.5, 0.5], "ring": [0.5, 0.5, 0.1, 0.15], "marks": marks}
        self.assertEqual(handle(request, scene), {"ok": True})
        self.assertEqual(scene.shown, [([0.5, 0.5], [0.5, 0.5, 0.1, 0.15], marks)])

    def test_gaze_show_with_nothing_clears_the_drawing(self):
        scene = FakeScene()
        self.assertEqual(handle({"cmd": "gaze_show"}, scene), {"ok": True})
        self.assertEqual(handle({"cmd": "gaze_show", "dot": None, "ring": None, "marks": []}, scene), {"ok": True})
        self.assertEqual(scene.shown, [(None, None, []), (None, None, [])])

    def test_gaze_show_keeps_only_what_it_draws(self):
        scene = FakeScene()
        mark = {"n": 1, "u": 0.4, "v": 0.3, "name": "x" * 500, "extra": "ignored"}
        handle({"cmd": "gaze_show", "marks": [mark]}, scene)
        (_, _, marks), = scene.shown
        self.assertEqual(list(marks[0]), ["n", "u", "v", "name"])
        self.assertEqual(len(marks[0]["name"]), protocol.MAX_MARK_NAME)

    def test_gaze_show_refuses_what_it_cannot_draw(self):
        scene = FakeScene()
        good_mark = {"n": 1, "u": 0.4, "v": 0.3, "name": "Chair"}
        for field, value in (
            ("dot", [0.5]),
            ("dot", "here"),
            ("dot", [0.5, "x"]),
            ("dot", [2.0, 0.5]),
            ("ring", [0.5, 0.5, 0.1]),
            ("ring", [0.5, 0.5, -0.1, 0.1]),
            ("marks", "Chair"),
            ("marks", [dict(good_mark, n="one")]),
            ("marks", [dict(good_mark, u=1.5)]),
            ("marks", [dict(good_mark, name=3)]),
            ("marks", [{"n": 1}]),
            ("marks", [good_mark] * (protocol.MAX_MARKS + 1)),
        ):
            with self.subTest(field=field, value=value):
                reply = handle({"cmd": "gaze_show", field: value}, scene)
                self.assertFalse(reply["ok"])
                self.assertIn(field, reply["error"])
        self.assertEqual(scene.shown, [])

    def test_pointer_events_queue_like_the_others(self):
        clock = Clock()
        events = protocol.EventQueue(clock=clock)
        events.add("ptt", state="down")
        self.assertTrue(events.add_pointer(0.4, 0.3))
        reply = handle({"cmd": "poll", "ack": 0}, events=events)
        self.assertEqual(
            reply["events"],
            [
                {"seq": 1, "type": "ptt", "t": 100.0, "state": "down"},
                {"seq": 2, "type": "pointer", "t": 100.0, "u": 0.4, "v": 0.3},
            ],
        )
        self.assertEqual(handle({"cmd": "poll", "ack": 2}, events=events)["events"], [])

    def test_pointer_events_come_at_most_twenty_a_second(self):
        clock = Clock()
        events = protocol.EventQueue(clock=clock)
        queued = 0
        for _ in range(192):  # 3 seconds of a mouse reporting 64 times a second
            queued += events.add_pointer(0.5, 0.5)
            clock.now += 1 / 64
        self.assertLessEqual(queued, 3 * protocol.MAX_POINTER_RATE)
        self.assertGreater(queued, 2 * protocol.MAX_POINTER_RATE)

    def test_a_flood_of_pointer_events_never_pushes_out_the_hold_events(self):
        clock = Clock()
        events = protocol.EventQueue(limit=5, clock=clock)
        events.add("ptt", state="down")
        for _ in range(20):  # the app stopped polling mid-hold
            clock.now += 0.1
            events.add_pointer(0.5, 0.5)
        self.assertEqual(len(events.poll(None)), 5)
        events.add("ptt", state="down")
        events.add("ptt", state="up")
        for _ in range(20):
            clock.now += 0.1
            events.add_pointer(0.5, 0.5)
        events.add("ptt", state="up")
        kept = events.poll(None)
        self.assertEqual([e.get("state") for e in kept if e["type"] == "ptt"], ["down", "up", "up"])
        self.assertEqual(len(kept), 5)
        # with nothing but hold events in the queue, the oldest still goes first
        full = protocol.EventQueue(limit=2, clock=clock)
        for state in ("down", "up", "down"):
            full.add("ptt", state=state)
        self.assertEqual([e["state"] for e in full.poll(None)], ["up", "down"])


class FaceTest(unittest.TestCase):
    def test_linked_faces_stop_at_a_gap(self):
        # two islands in one mesh: a strip of two quads, and a separate quad
        faces = [[0, 1, 2, 3], [2, 3, 4, 5], [6, 7, 8, 9]]
        self.assertEqual(protocol.linked_face_count(faces, 0), 2)
        self.assertEqual(protocol.linked_face_count(faces, 1), 2)
        self.assertEqual(protocol.linked_face_count(faces, 2), 1)

    def test_linked_faces_connect_through_a_single_shared_vertex(self):
        self.assertEqual(protocol.linked_face_count([[0, 1, 2], [2, 3, 4], [4, 5, 6]], 0), 3)

    def test_linked_faces_of_a_face_that_is_not_there(self):
        self.assertEqual(protocol.linked_face_count([[0, 1, 2]], 5), 0)
        self.assertEqual(protocol.linked_face_count([[0, 1, 2]], -1), 0)
        self.assertEqual(protocol.linked_face_count([], 0), 0)

    def test_linked_faces_in_a_long_chain(self):
        chain = [[i, i + 1, i + 2] for i in range(0, 30000, 2)]  # each face shares one vertex with the next
        self.assertEqual(protocol.linked_face_count(chain, 0), len(chain))

    def test_the_part_is_the_group_with_the_most_weight(self):
        self.assertEqual(protocol.face_part({"Wing.L": 2.0, "Body": 0.5}, "Feather", 3), "Wing.L")
        self.assertEqual(protocol.face_part({"Body": 0.25, "Wing.L": 0.75}, None, 0), "Wing.L")

    def test_without_groups_the_part_is_the_material_when_there_are_two_or_more(self):
        self.assertEqual(protocol.face_part({}, "Feather", 2), "Feather")
        self.assertIsNone(protocol.face_part({}, "Feather", 1))
        self.assertIsNone(protocol.face_part({}, None, 3))
        self.assertIsNone(protocol.face_part({}, None, 0))

    def test_a_group_with_no_weight_is_not_a_part(self):
        self.assertEqual(protocol.face_part({"Wing.L": 0.0}, "Feather", 2), "Feather")
        self.assertIsNone(protocol.face_part({"Wing.L": 0.0}, None, 2))


if __name__ == "__main__":
    unittest.main()
