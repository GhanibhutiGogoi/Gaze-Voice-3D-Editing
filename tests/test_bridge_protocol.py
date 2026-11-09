import ast
import base64
import builtins
import importlib.util
import io
import math
import os
import re
import struct
import symtable
import sys
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

PROTOCOL = Path(__file__).resolve().parents[1] / "blender_addon" / "gazevoice_bridge" / "protocol.py"
ADDON = PROTOCOL.with_name("__init__.py")


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
        self.markers = []
        self.marker_error = None
        self.crop = protocol.CropSlot()
        self.crops_asked = []

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

    def gaze_place(self):
        return {"x": 4, "y": 104, "scale": 2, "window_width": 1000, "window_height": 650, "pid": 4242}

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

    def gaze_marker(self, at):
        if self.marker_error is not None:
            raise self.marker_error
        self.markers.append(at)

    def gaze_crop(self, u, v, radius, size):
        if self.view is None:
            return "no 3D viewport"
        if not self.crop.begin():
            return "a picture is already being taken"
        self.crops_asked.append((u, v, radius, size))
        return None

    def gaze_crop_result(self):
        return self.crop.read()


def undefined_names(source):
    """(function, name) for each global name that `source` uses and neither defines nor takes from the builtins."""
    top = symtable.symtable(source, "<add-on>", "exec")
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    missing = set()

    def walk(table):
        for symbol in table.get_symbols():
            name = symbol.get_name()
            if name.startswith("__") and name.endswith("__"):
                continue  # ones Python adds itself, like __conditional_annotations__
            if symbol.is_global() and symbol.is_referenced() and name not in defined and not hasattr(builtins, name):
                missing.add((table.get_name(), name))
        for child in table.get_children():
            walk(child)

    walk(top)
    return missing


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def handle(request, scene=None, events=None):
    return protocol.handle(request, scene or FakeScene(), events or protocol.EventQueue())


class BridgeProtocolTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((protocol.BRIDGE_PORT, protocol.BRIDGE_VERSION), (9877, "0.3.3"))

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
            {"ok": True, "bridge": "0.3.3", "blender": "5.2.2", "key": "⌥ Space"},
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
        self.assertEqual(handle({"cmd": "gaze_view"}), {
            "ok": True, "width": 1840, "height": 1100, "x": 4, "y": 104, "scale": 2,
            "window_width": 1000, "window_height": 650, "pid": 4242,
        })

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


class GazeMarkerProtocolTest(unittest.TestCase):
    """The marker rides on the poll request: the app already sends one every 50 ms, so nothing else is needed."""

    def poll(self, scene, **fields):
        return handle({"cmd": "poll", "ack": 0, **fields}, scene)

    def test_a_poll_with_a_point_shows_the_marker_there(self):
        scene = FakeScene()
        reply = self.poll(scene, marker=[0.4, 0.3])
        self.assertEqual(scene.markers, [[0.4, 0.3]])
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["events"], [])

    def test_a_poll_with_null_takes_it_away(self):
        scene = FakeScene()
        self.poll(scene, marker=None)
        self.assertEqual(scene.markers, [None])

    def test_a_poll_without_the_key_leaves_it_as_it_is(self):
        scene = FakeScene()
        self.poll(scene)
        self.assertEqual(scene.markers, [])

    def test_whole_numbers_are_fine_and_come_out_as_floats(self):
        scene = FakeScene()
        self.poll(scene, marker=[0, 1])
        self.assertEqual(scene.markers, [[0.0, 1.0]])
        self.assertTrue(all(isinstance(c, float) for c in scene.markers[0]))

    def test_the_poll_still_brings_the_events_with_a_marker(self):
        scene, events = FakeScene(), protocol.EventQueue()
        events.add("ptt", state="down")
        reply = handle({"cmd": "poll", "ack": 0, "boot": events.boot, "marker": [0.5, 0.5]}, scene, events)
        self.assertEqual([e["type"] for e in reply["events"]], ["ptt"])
        self.assertEqual(reply["boot"], events.boot)

    def test_a_marker_that_cannot_be_drawn_is_left_out_and_the_poll_still_answers(self):
        scene, events = FakeScene(), protocol.EventQueue()
        events.add("ptt", state="down")
        for marker in ([2.0, 0.5], [0.5, -0.1], [0.5], [0.5, 0.5, 0.5], "centre", 5, {}, [True, 0.5], ["a", "b"],
                       [float("nan"), 0.5], [float("inf"), 0.5], [[0.5], [0.5]]):
            with self.subTest(marker=marker):
                reply = handle({"cmd": "poll", "ack": 0, "marker": marker}, scene, events)
                self.assertTrue(reply["ok"])
                self.assertEqual(len(reply["events"]), 1)
        self.assertEqual(scene.markers, [])

    def test_a_marker_that_fails_to_draw_cannot_cost_the_talk_key(self):
        scene, events = FakeScene(), protocol.EventQueue()
        scene.marker_error = RuntimeError("the viewport is gone")
        events.add("ptt", state="down")
        reply = handle({"cmd": "poll", "ack": 0, "marker": [0.5, 0.5]}, scene, events)
        self.assertTrue(reply["ok"])
        self.assertEqual([e["state"] for e in reply["events"]], ["down"])

    def test_the_add_on_can_do_everything_the_protocol_asks_of_a_scene(self):
        tree = ast.parse(ADDON.read_text())
        scene = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BpyScene")
        have = {n.name for n in scene.body if isinstance(n, ast.FunctionDef)}
        asked = {name for name, value in vars(protocol.SceneAccess).items()
                 if callable(value) and not name.startswith("_")}
        self.assertIn("gaze_marker", asked)
        self.assertEqual(asked - have, set())


class MarkerGlideTest(unittest.TestCase):
    """How the marker moves: it glides, fades in and out, and stretches a little while it travels."""

    def steps(self, glide, target, count):
        for _ in range(count):
            glide.step(target)

    def test_the_first_point_appears_where_the_look_is_and_fades_in(self):
        glide = protocol.MarkerGlide()
        self.assertFalse(glide.showing)
        self.assertTrue(glide.step([0.8, 0.2]))
        self.assertEqual((glide.x, glide.y), (0.8, 0.2))  # not gliding in from wherever it was last
        self.assertTrue(glide.showing)
        self.assertTrue(0.0 < glide.alpha < 1.0)
        self.steps(glide, [0.8, 0.2], int(protocol.MARKER_FADE_IN / protocol.MARKER_TICK) + 1)
        self.assertEqual(glide.alpha, 1.0)

    def test_it_glides_to_a_new_point_a_share_of_the_gap_each_step(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.2, 0.5], 12)
        glide.step([0.8, 0.5])
        self.assertAlmostEqual(glide.x, 0.2 + 0.6 * protocol.MARKER_EASE)
        self.assertLess(glide.x, 0.8)
        seen = [glide.x]
        for _ in range(60):
            glide.step([0.8, 0.5])
            seen.append(glide.x)
        self.assertEqual(seen, sorted(seen))  # always forward, never back
        self.assertLessEqual(max(seen), 0.8)  # and never past
        self.assertEqual(glide.x, 0.8)  # it gets there, and stays
        jumps = [b - a for a, b in zip(seen, seen[1:])]
        self.assertLess(max(jumps), 0.6 * protocol.MARKER_EASE + 1e-9)

    def test_a_marker_at_rest_asks_for_no_repaint(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.5, 0.5], 40)
        self.assertEqual((glide.alpha, glide.x, glide.y), (1.0, 0.5, 0.5))
        self.assertFalse(glide.step([0.5, 0.5]))
        self.assertFalse(glide.step([0.5, 0.5]))

    def test_it_stretches_along_its_direction_of_travel_and_relaxes_when_it_stops(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.2, 0.5], 12)
        self.assertEqual((glide.vx, glide.vy), (0.0, 0.0))
        glide.step([0.8, 0.5])
        self.assertGreater(glide.vx, 0)
        self.assertEqual(glide.vy, 0.0)
        glide.step([0.2, 0.9])  # turned round and downward
        self.steps(glide, [0.2, 0.9], 3)
        self.assertLess(glide.vx, 0)
        self.assertGreater(glide.vy, 0)
        self.steps(glide, [0.2, 0.9], 80)
        self.assertEqual((glide.vx, glide.vy), (0.0, 0.0))

    def test_without_a_point_it_fades_out_where_it_is_and_then_is_gone(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.3, 0.6], 12)
        count = 0
        while glide.showing and count < 100:
            self.assertTrue(glide.step(None))
            count += 1
        self.assertAlmostEqual(count * protocol.MARKER_TICK, protocol.MARKER_FADE_OUT, delta=protocol.MARKER_TICK)
        self.assertEqual((glide.x, glide.y, glide.alpha), (0.3, 0.6, 0.0))
        self.assertFalse(glide.step(None))  # nothing left to repaint, so the timer can stop

    def test_a_point_during_the_fade_brings_it_back_from_where_it_is(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.3, 0.6], 12)
        self.steps(glide, None, 4)
        faded = glide.alpha
        self.assertTrue(0.0 < faded < 1.0)
        glide.step([0.9, 0.6])
        self.assertGreater(glide.alpha, faded)  # picks up from the fade, not from nothing
        self.assertTrue(0.3 < glide.x < 0.9)  # and glides there

    def test_a_point_after_it_has_gone_appears_there_not_where_it_left(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.3, 0.6], 12)
        self.steps(glide, None, 30)
        self.assertFalse(glide.showing)
        glide.step([0.9, 0.1])
        self.assertEqual((glide.x, glide.y), (0.9, 0.1))

    def test_a_reset_takes_it_away_at_once(self):
        glide = protocol.MarkerGlide()
        self.steps(glide, [0.3, 0.6], 12)
        glide.reset()
        self.assertEqual((glide.alpha, glide.vx, glide.vy), (0.0, 0.0, 0.0))
        self.assertFalse(glide.showing)

    def test_it_takes_whole_numbers_and_lists_or_tuples(self):
        glide = protocol.MarkerGlide()
        glide.step((0, 1))
        self.assertEqual((glide.x, glide.y), (0.0, 1.0))


class RepaintsTest(unittest.TestCase):
    """The marker repaints the viewport, so it asks gently: one at a time, and no faster than the viewport can draw."""

    def test_nothing_is_asked_for_until_something_changes(self):
        repaints = protocol.Repaints()
        self.assertFalse(repaints.ask(1.0))
        repaints.want()
        self.assertTrue(repaints.ask(1.0))
        self.assertFalse(repaints.ask(1.001))  # one change is one repaint

    def test_a_change_while_a_repaint_is_waiting_is_not_a_second_request(self):
        repaints = protocol.Repaints()
        repaints.want()
        self.assertTrue(repaints.ask(1.0))
        repaints.want()
        self.assertFalse(repaints.ask(1.05))
        repaints.drawn(1.06)
        self.assertTrue(repaints.ask(1.2))  # the change is still wanted once the viewport has drawn

    def test_a_viewport_that_is_slow_to_draw_is_asked_less_often(self):
        repaints = protocol.Repaints()
        repaints.want()
        self.assertTrue(repaints.ask(10.0))
        repaints.drawn(10.2)  # the scene took 0.2 s
        repaints.want()
        self.assertFalse(repaints.ask(10.3))
        self.assertFalse(repaints.ask(10.39))
        self.assertTrue(repaints.ask(10.41))

    def test_a_quick_viewport_is_never_held_back(self):
        repaints = protocol.Repaints()
        now = 10.0
        for _ in range(30):
            repaints.want()
            self.assertTrue(repaints.ask(now))
            repaints.drawn(now + 0.004)
            now += protocol.MARKER_TICK

    def test_a_repaint_that_never_comes_is_asked_for_again(self):
        repaints = protocol.Repaints()
        repaints.want()
        self.assertTrue(repaints.ask(10.0))  # say the window is hidden
        repaints.want()
        self.assertFalse(repaints.ask(10.5))
        self.assertTrue(repaints.ask(11.1))

    def test_it_says_what_is_waiting(self):
        repaints = protocol.Repaints()
        self.assertEqual((repaints.pending, repaints.waiting), (False, False))
        repaints.want()
        self.assertEqual((repaints.pending, repaints.waiting), (True, False))  # wanted, not asked for yet
        repaints.ask(1.0)
        self.assertEqual((repaints.pending, repaints.waiting), (False, True))  # asked for, not drawn yet
        repaints.drawn(1.01)
        self.assertEqual((repaints.pending, repaints.waiting), (False, False))

    def test_a_draw_nobody_asked_for_changes_nothing(self):
        repaints = protocol.Repaints()
        repaints.drawn(5.0)
        repaints.want()
        self.assertTrue(repaints.ask(5.0))


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


def decode_png(data):
    """A small PNG reader built on the standard library only: (width, height, rgb bytes, top row first)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, chunks = 8, []
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body), f"bad checksum in {kind!r}"
        chunks.append((kind, body))
        pos += 12 + length
    assert [kind for kind, _ in chunks][0] == b"IHDR" and chunks[-1][0] == b"IEND"
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, colour, compression, filtering, interlace) == (8, 2, 0, 0, 0), "expected plain 8-bit RGB"
    raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    stride = width * 3
    assert len(raw) == height * (stride + 1)
    rows = []
    for y in range(height):
        row = raw[y * (stride + 1) : (y + 1) * (stride + 1)]
        assert row[0] == 0, "expected filter type 0"
        rows.append(row[1:])
    return width, height, b"".join(rows)


def blank(width, height, value=(40, 40, 40)):
    return bytearray(bytes(value) * (width * height))


def pixel(pixels, width, x, y):
    return tuple(pixels[(y * width + x) * 3 : (y * width + x) * 3 + 3])


class CropPictureTest(unittest.TestCase):
    def test_encode_png_makes_a_readable_file(self):
        width, height = 7, 5
        rgb = bytes((x * 31 + y * 7 + c * 50) % 256 for y in range(height) for x in range(width) for c in range(3))
        png = protocol.encode_png(width, height, rgb)
        self.assertIsInstance(png, bytes)
        self.assertEqual(decode_png(png), (width, height, rgb))

    def test_encode_png_refuses_pixels_that_do_not_fit_the_size(self):
        with self.assertRaises(ValueError):
            protocol.encode_png(4, 4, bytes(4 * 4 * 3 - 1))
        with self.assertRaises(ValueError):
            protocol.encode_png(0, 4, b"")

    def test_encode_png_squeezes_a_flat_picture(self):
        png = protocol.encode_png(512, 512, bytes(512 * 512 * 3))
        self.assertLess(len(png), 5000)
        self.assertEqual(decode_png(png)[:2], (512, 512))

    def test_crop_box_is_a_margin_around_the_ring(self):
        # the ring's radii are 0.1 of the width and 0.1 of the height; the box reaches 2.5 radii each way
        self.assertEqual(protocol.crop_box(0.5, 0.5, [0.1, 0.1], 1000, 500), (250, 125, 750, 375))
        self.assertEqual(protocol.crop_box(0.5, 0.5, [0.1, 0.1], 1000, 500, margin=1.0), (400, 200, 600, 300))

    def test_crop_box_stays_inside_the_image(self):
        for u, v in ((0, 0), (1, 1), (0, 1), (1, 0), (0.5, 0.5), (0.02, 0.97), (0.999, 0.001)):
            for radius in ([0, 0], [0.0001, 0.0001], [0.05, 0.2], [0.3, 0.1], [1, 1]):
                for width, height in ((1840, 1100), (3, 2), (1, 1)):
                    with self.subTest(u=u, v=v, radius=radius, size=(width, height)):
                        left, top, right, bottom = protocol.crop_box(u, v, radius, width, height)
                        self.assertTrue(0 <= left < right <= width)
                        self.assertTrue(0 <= top < bottom <= height)
                        self.assertTrue(all(isinstance(n, int) for n in (left, top, right, bottom)))

    def test_crop_box_keeps_the_point_it_is_centred_on(self):
        left, top, right, bottom = protocol.crop_box(0.01, 0.99, [0.05, 0.05], 1000, 1000)
        self.assertEqual((left, bottom), (0, 1000))  # cut off at the corner, not moved
        self.assertTrue(left <= 10 < right and top <= 990 < bottom)

    def test_crop_size_puts_the_long_side_at_size(self):
        self.assertEqual(protocol.crop_size((250, 125, 750, 375), 512), (512, 256))
        self.assertEqual(protocol.crop_size((0, 0, 100, 300), 600), (200, 600))
        self.assertEqual(protocol.crop_size((0, 0, 1, 1), 64), (64, 64))
        self.assertEqual(protocol.crop_size((0, 0, 1000, 1), 100), (100, 1))  # never a zero side

    def test_the_bubble_lands_where_the_target_is_in_the_crop(self):
        box = protocol.crop_box(0.5, 0.5, [0.1, 0.2], 1000, 500)
        size = protocol.crop_size(box, 512)
        cx, cy, rx, ry = protocol.bubble_in_crop(0.5, 0.5, [0.1, 0.2], 1000, 500, box, size)
        self.assertAlmostEqual(cx, size[0] / 2)
        self.assertAlmostEqual(cy, size[1] / 2)
        self.assertAlmostEqual(rx / size[0], 1 / 2.5 / 2)  # a radius is 1/2.5 of the half-box on each side
        self.assertAlmostEqual(ry / size[1], 1 / 2.5 / 2)

    def test_the_bubble_follows_the_target_when_the_box_is_cut_off(self):
        box = protocol.crop_box(0.0, 0.0, [0.1, 0.1], 1000, 1000)  # (0, 0, 250, 250)
        cx, cy, rx, ry = protocol.bubble_in_crop(0.0, 0.0, [0.1, 0.1], 1000, 1000, box, (250, 250))
        self.assertEqual((cx, cy, rx, ry), (0.0, 0.0, 100.0, 100.0))

    def test_the_bubble_is_scaled_on_each_axis_on_its_own(self):
        # a 100 x 100 box shown 200 wide and 100 tall: the bubble's centre and radii stretch the same way
        cx, cy, rx, ry = protocol.bubble_in_crop(0.5, 0.25, [0.1, 0.2], 400, 400, (150, 50, 250, 150), (200, 100))
        self.assertEqual((cx, cy, rx, ry), (100.0, 50.0, 80.0, 80.0))

    @staticmethod
    def towards_bubble(before, after):
        """How far a pixel went from `before` towards the bubble's colour, as a share of the way (0 to 1), read off
        the channel where the two differ most."""
        channel = max(range(3), key=lambda c: abs(protocol.BUBBLE_COLOR[c] - before[c]))
        return (after[channel] - before[channel]) / (protocol.BUBBLE_COLOR[channel] - before[channel])

    def test_the_bubble_lays_a_light_tint_over_its_inside(self):
        for background in ((40, 40, 40), (0, 0, 0), (255, 255, 255), (200, 120, 60)):
            with self.subTest(background=background):
                pixels = blank(100, 100, background)
                protocol.draw_bubble(pixels, 100, 100, 50, 50, 30, 30)
                for x, y in ((50, 50), (40, 55), (65, 50), (50, 30)):  # the middle, and well inside the rim
                    share = self.towards_bubble(background, pixel(pixels, 100, x, y))
                    self.assertGreater(share, 0.08, (x, y))  # enough to tell inside from outside...
                    self.assertLess(share, 0.3, (x, y))  # ...and no more: it is a tint, not paint
                self.assertEqual(pixel(pixels, 100, 50, 50), pixel(pixels, 100, 60, 45))  # the same all over

    def test_what_is_under_the_bubble_still_shows(self):
        # stripes a column wide: after the tint, neighbours differ almost as much as before, and in the same way
        width = height = 100
        pixels = bytearray(b"".join(bytes((30, 30, 30) if x % 2 else (220, 220, 220))
                                    for _y in range(height) for x in range(width)))
        protocol.draw_bubble(pixels, width, height, 50, 50, 35, 35)
        for y in (30, 50, 70):
            for x in range(30, 70):
                left, right = pixel(pixels, width, x, y), pixel(pixels, width, x + 1, y)
                for c in range(3):
                    gap = right[c] - left[c]
                    self.assertGreater(abs(gap), 0.75 * 190, (x, y, c))
                    self.assertEqual(gap > 0, x % 2 == 1, (x, y, c))  # light stays light, dark stays dark

    def test_the_rim_shows_on_any_background(self):
        for background in ((0, 0, 0), (255, 255, 255), (128, 128, 128), protocol.BUBBLE_COLOR, (12, 12, 16)):
            with self.subTest(background=background):
                pixels = blank(100, 100, background)
                protocol.draw_bubble(pixels, 100, 100, 50, 50, 30, 30)
                middle = max(abs(a - b) for a, b in zip(pixel(pixels, 100, 50, 50), background))
                for x, y in ((80, 50), (20, 50), (50, 80), (50, 20), (71, 71)):  # on the outline, all round
                    near = [pixel(pixels, 100, x + dx, y + dy) for dx in range(-3, 4) for dy in range(-3, 4)
                            if 0 <= x + dx < 100 and 0 <= y + dy < 100]
                    strongest = max(max(abs(a - b) for a, b in zip(colour, background)) for colour in near)
                    self.assertGreater(strongest, 80, (x, y))  # stands out from what it is drawn on
                    self.assertGreater(strongest, 3 * middle, (x, y))  # and far more than the tint does

    def test_the_bubble_is_an_ellipse_with_its_own_width_and_height(self):
        pixels = blank(100, 100)
        protocol.draw_bubble(pixels, 100, 100, 50, 50, 30, 15)
        self.assertNotEqual(pixel(pixels, 100, 74, 50), (40, 40, 40))  # inside, across
        self.assertEqual(pixel(pixels, 100, 50, 74), (40, 40, 40))  # outside, down
        self.assertEqual(pixel(pixels, 100, 74, 63), (40, 40, 40))  # outside, beyond the dark edge

    def test_nothing_outside_the_bubble_changes(self):
        width, height, cx, cy, rx, ry = 120, 90, 60, 45, 30, 20
        pixels = blank(width, height)
        protocol.draw_bubble(pixels, width, height, cx, cy, rx, ry)
        changed = 0
        for y in range(height):
            for x in range(width):
                f = (((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / ry) ** 2) ** 0.5
                if pixel(pixels, width, x, y) != (40, 40, 40):
                    changed += 1
                    self.assertLess(f, 1.2, (x, y, f))  # at most a few pixels of dark edge outside the outline
                elif f < 0.9:
                    self.fail(f"({x}, {y}) is inside the bubble and wasn't tinted")
        self.assertGreater(changed, 3.14 * rx * ry * 0.9)

    def test_a_bubble_bigger_than_the_picture_tints_all_of_it(self):
        pixels = blank(40, 30)
        protocol.draw_bubble(pixels, 40, 30, 20, 15, 500, 400)
        self.assertEqual(len(pixels), 40 * 30 * 3)
        colours = {pixel(pixels, 40, x, y) for y in range(30) for x in range(40)}
        self.assertEqual(len(colours), 1)
        self.assertNotEqual(colours, {(40, 40, 40)})

    def test_a_bubble_running_off_the_picture_stays_inside_the_buffer(self):
        pixels = blank(40, 30)
        protocol.draw_bubble(pixels, 40, 30, 0, 0, 100, 100)  # a corner of a big bubble
        protocol.draw_bubble(pixels, 40, 30, 39.9, 29.9, 8, 6)  # a small one at the far corner
        protocol.draw_bubble(pixels, 40, 30, -50, 15, 20, 20)  # one wholly off the picture
        self.assertEqual(len(pixels), 40 * 30 * 3)
        self.assertNotEqual(pixel(pixels, 40, 0, 0), (40, 40, 40))
        self.assertNotEqual(pixel(pixels, 40, 39, 29), (40, 40, 40))

    @staticmethod
    def plain_bubble(pixels, width, height, cx, cy, rx, ry):
        """The same formula as draw_bubble, worked out the slow way: for every pixel of the picture, one by one."""
        color, edge_color = protocol.BUBBLE_COLOR, protocol.BUBBLE_EDGE_COLOR
        for row in range(height):
            for column in range(width):
                d = protocol._outline_distance(column + 0.5 - cx, row + 0.5 - cy, rx, ry)
                inside = protocol._covered(d, -math.inf, 0.0)
                rim = protocol._covered(d, -protocol.BUBBLE_RIM, 0.0) * protocol.BUBBLE_RIM_OPACITY
                edge = protocol._covered(d, 0.0, protocol.BUBBLE_EDGE) * protocol.BUBBLE_EDGE_OPACITY
                if inside <= 0.0 and edge <= 0.0:
                    continue
                at = (row * width + column) * 3
                for c in range(3):
                    level = pixels[at + c]
                    level += (color[c] - level) * protocol.BUBBLE_TINT * inside
                    level += (edge_color[c] - level) * edge
                    level += (color[c] - level) * rim
                    pixels[at + c] = min(255, max(0, round(level)))

    def test_the_fast_bubble_matches_the_slow_one_pixel_for_pixel(self):
        # Circles and ovals as wide as 3 to 1 either way, in the middle, touching an edge and running off the picture.
        width, height = 90, 70
        shapes = [(45, 35, 20, 20), (45, 35, 30, 10), (45, 35, 10, 30), (45, 35, 33, 11), (45, 35, 11.5, 34.5),
                  (10.5, 8.25, 25, 25), (80, 60, 22, 14), (-5, 35, 30, 15), (45, 72.5, 18, 18),
                  (0, 0, 40, 13), (89.5, 69.5, 9, 27), (45, 35, 120, 90), (30.3, 20.7, 6.2, 18.3), (50, 40, 3, 3)]
        background = bytes((17 * x + 5 * y) % 256 for y in range(height) for x in range(width * 3))  # no two runs alike
        for shape in shapes:
            with self.subTest(bubble=shape):
                fast, slow = bytearray(background), bytearray(background)
                protocol.draw_bubble(fast, width, height, *shape)
                self.plain_bubble(slow, width, height, *shape)
                self.assertEqual(fast, slow)

    def test_a_bubble_with_no_size_or_no_place_draws_nothing(self):
        for cx, cy, rx, ry in ((20, 15, 0, 0), (20, 15, -5, 5), (math.nan, 15, 5, 5), (20, 15, math.inf, 5)):
            with self.subTest(bubble=(cx, cy, rx, ry)):
                pixels = blank(40, 30)
                protocol.draw_bubble(pixels, 40, 30, cx, cy, rx, ry)
                self.assertEqual(pixels, blank(40, 30))

    def test_a_full_size_bubble_takes_a_moment_not_a_pause(self):
        # The picture is made inside one of Blender's redraws, so the bubble mustn't hold Blender's window up.
        size = protocol.MAX_CROP_SIZE
        pixels = blank(size, size)
        began = time.perf_counter()
        protocol.draw_bubble(pixels, size, size, size / 2, size / 2, size / 5, size / 5)
        self.assertLess(time.perf_counter() - began, 0.5)


class CropSlotTest(unittest.TestCase):
    def test_a_picture_is_read_once(self):
        slot = protocol.CropSlot()
        self.assertTrue(slot.begin())
        self.assertEqual(slot.read(), {"pending": True})
        slot.finish(b"png bytes")
        self.assertEqual(slot.state, "ready")
        self.assertEqual(slot.read(), {"png": b"png bytes"})
        self.assertEqual(slot.state, "idle")
        self.assertIsNone(slot._png)  # the add-on lets go of the bytes the moment they are read
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_an_error_is_read_once_too(self):
        slot = protocol.CropSlot()
        slot.begin()
        slot.fail("the GPU said no")
        self.assertEqual(slot.read(), {"error": "the GPU said no"})
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_a_second_crop_is_refused_while_one_is_pending(self):
        slot = protocol.CropSlot()
        self.assertTrue(slot.begin())
        self.assertFalse(slot.begin())
        self.assertEqual(slot.state, "pending")
        slot.finish(b"first")
        self.assertEqual(slot.read(), {"png": b"first"})
        self.assertTrue(slot.begin())

    def test_a_new_crop_drops_a_picture_nobody_read(self):
        slot = protocol.CropSlot()
        slot.begin()
        slot.finish(b"old")
        self.assertTrue(slot.begin())
        self.assertEqual(slot.read(), {"pending": True})
        slot.finish(b"new")
        self.assertEqual(slot.read(), {"png": b"new"})

    def test_it_gives_up_when_the_viewport_never_draws(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        clock.now += protocol.CROP_SECONDS - 0.1
        self.assertEqual(slot.read(), {"pending": True})
        clock.now += 0.2
        self.assertEqual(slot.state, "failed")
        self.assertIn("didn't draw", slot.read()["error"])
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_a_picture_that_comes_after_giving_up_is_thrown_away(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        clock.now += protocol.CROP_SECONDS + 1
        slot.finish(b"too late")
        self.assertIn("error", slot.read())
        self.assertEqual(slot.state, "idle")

    def test_a_timed_out_crop_does_not_block_the_next_one(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        clock.now += protocol.CROP_SECONDS + 1
        self.assertTrue(slot.begin())
        self.assertEqual(slot.read(), {"pending": True})

    def test_drop_forgets_everything(self):
        slot = protocol.CropSlot()
        slot.begin()
        slot.finish(b"secret")
        slot.drop()
        self.assertIsNone(slot._png)
        self.assertEqual((slot.state, slot.read()), ("idle", {"error": "no picture is waiting"}))
        slot.begin()
        slot.drop()
        slot.finish(b"after the drop")  # nobody is waiting for it
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_an_unread_picture_is_dropped_after_a_while(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        slot.finish(b"nobody comes for this")
        clock.now += protocol.CROP_KEEP_SECONDS - 0.1
        self.assertEqual(slot.state, "ready")
        clock.now += 0.2
        self.assertEqual(slot.state, "idle")  # gone without anyone reading it
        self.assertIsNone(slot._png)
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_a_picture_read_inside_the_limit_still_comes_once(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        slot.finish(b"in time")
        clock.now += protocol.CROP_KEEP_SECONDS - 0.1
        self.assertEqual(slot.read(), {"png": b"in time"})
        self.assertEqual(slot.read(), {"error": "no picture is waiting"})

    def test_a_picture_is_kept_from_when_it_was_made_not_from_when_it_was_asked_for(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        clock.now += protocol.CROP_SECONDS - 0.5  # the viewport took a while to draw
        slot.finish(b"late but fresh")
        clock.now += protocol.CROP_KEEP_SECONDS - 0.1
        self.assertEqual(slot.read(), {"png": b"late but fresh"})

    def test_a_dropped_picture_does_not_block_the_next_crop(self):
        clock = Clock(10.0)
        slot = protocol.CropSlot(clock=clock)
        slot.begin()
        slot.finish(b"old")
        clock.now += protocol.CROP_KEEP_SECONDS + 1
        self.assertTrue(slot.begin())
        slot.finish(b"new")
        self.assertEqual(slot.read(), {"png": b"new"})

    def test_the_timeout_is_a_few_seconds(self):
        self.assertEqual(protocol.CROP_SECONDS, 3.0)
        self.assertEqual(protocol.CROP_KEEP_SECONDS, 30.0)
        self.assertEqual(protocol.MAX_CROP_SIZE, 1024)


class GazeCropTest(unittest.TestCase):
    ASK = {"cmd": "gaze_crop", "u": 0.4, "v": 0.3, "radius": [0.1, 0.15], "size": 512}

    def test_a_crop_starts_and_answers_pending(self):
        scene = FakeScene()
        self.assertEqual(handle(self.ASK, scene), {"ok": True, "pending": True})
        self.assertEqual(scene.crops_asked, [(0.4, 0.3, [0.1, 0.15], 512)])
        self.assertEqual(handle({"cmd": "gaze_crop_result"}, scene), {"ok": True, "pending": True})

    def test_a_crop_result_can_only_be_read_once(self):
        scene = FakeScene()
        handle(self.ASK, scene)
        png = protocol.encode_png(4, 4, bytes(range(48)))
        scene.crop.finish(png)  # the viewport drew and the picture was made
        reply = handle({"cmd": "gaze_crop_result"}, scene)
        self.assertEqual(set(reply), {"ok", "png"})
        self.assertTrue(reply["ok"])
        self.assertEqual(base64.b64decode(reply["png"], validate=True), png)
        self.assertNotIn("\n", reply["png"])
        self.assertEqual(handle({"cmd": "gaze_crop_result"}, scene), {"ok": False, "error": "no picture is waiting"})
        self.assertEqual(scene.crop.state, "idle")

    def test_the_reply_is_one_line(self):
        scene = FakeScene()
        handle(self.ASK, scene)
        scene.crop.finish(protocol.encode_png(200, 200, os.urandom(200 * 200 * 3)))
        line = protocol.encode(handle({"cmd": "gaze_crop_result"}, scene))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertTrue(line.endswith(b"\n"))
        self.assertGreater(len(line), protocol.MAX_LINE)  # replies may be longer than a request is allowed to be

    def test_asking_before_any_crop_says_nothing_is_waiting(self):
        self.assertEqual(handle({"cmd": "gaze_crop_result"}), {"ok": False, "error": "no picture is waiting"})

    def test_a_crop_while_one_is_pending_is_refused(self):
        scene = FakeScene()
        handle(self.ASK, scene)
        self.assertEqual(
            handle(dict(self.ASK, u=0.9), scene), {"ok": False, "error": "a picture is already being taken"}
        )
        self.assertEqual(len(scene.crops_asked), 1)
        self.assertEqual(handle({"cmd": "gaze_crop_result"}, scene), {"ok": True, "pending": True})

    def test_a_crop_that_never_finishes_ends_in_an_error(self):
        clock = Clock(10.0)
        scene = FakeScene()
        scene.crop = protocol.CropSlot(clock=clock)
        handle(self.ASK, scene)
        clock.now += protocol.CROP_SECONDS + 0.5
        reply = handle({"cmd": "gaze_crop_result"}, scene)
        self.assertFalse(reply["ok"])
        self.assertIn("didn't draw", reply["error"])
        self.assertEqual(handle(self.ASK, scene), {"ok": True, "pending": True})  # and the next one can go ahead

    def test_a_crop_that_failed_says_why(self):
        scene = FakeScene()
        handle(self.ASK, scene)
        scene.crop.fail("couldn't read the viewport")
        self.assertEqual(handle({"cmd": "gaze_crop_result"}, scene), {"ok": False, "error": "couldn't read the viewport"})

    def test_a_crop_needs_a_viewport(self):
        scene = FakeScene()
        scene.view = None
        self.assertEqual(handle(self.ASK, scene), {"ok": False, "error": "no 3D viewport"})
        self.assertEqual(scene.crop.state, "idle")

    def test_size_is_capped(self):
        scene = FakeScene()
        handle(dict(self.ASK, size=5000), scene)
        scene.crop.drop()
        handle(dict(self.ASK, size=protocol.MAX_CROP_SIZE), scene)
        self.assertEqual([size for *_, size in scene.crops_asked], [protocol.MAX_CROP_SIZE] * 2)

    def test_the_numbers_are_checked(self):
        scene = FakeScene()
        bad_where = {"ok": False, "error": "u and v must be 0 to 1"}
        bad_radius = {"ok": False, "error": "radius must be [ru, rv], each above 0 and at most 1"}
        bad_size = {"ok": False, "error": "size must be a whole number of pixels, 1 or more"}
        cases = (
            (dict(self.ASK, u=1.2), bad_where),
            (dict(self.ASK, v=-0.1), bad_where),
            (dict(self.ASK, u="0.4"), bad_where),
            (dict(self.ASK, v=float("nan")), bad_where),
            ({k: v for k, v in self.ASK.items() if k != "u"}, bad_where),
            (dict(self.ASK, radius=[0.1]), bad_radius),
            (dict(self.ASK, radius=0.1), bad_radius),
            (dict(self.ASK, radius=[0.1, 0]), bad_radius),
            (dict(self.ASK, radius=[0.1, 1.5]), bad_radius),
            (dict(self.ASK, radius=[0.1, "x"]), bad_radius),
            (dict(self.ASK, radius=[True, 0.1]), bad_radius),
            (dict(self.ASK, radius=[0.1, float("inf")]), bad_radius),
            ({k: v for k, v in self.ASK.items() if k != "radius"}, bad_radius),
            (dict(self.ASK, size=0), bad_size),
            (dict(self.ASK, size=-5), bad_size),
            (dict(self.ASK, size=512.5), bad_size),
            (dict(self.ASK, size="512"), bad_size),
            (dict(self.ASK, size=True), bad_size),
            ({k: v for k, v in self.ASK.items() if k != "size"}, bad_size),
        )
        for request, expected in cases:
            with self.subTest(request=request):
                self.assertEqual(handle(request, scene), expected)
        self.assertEqual(scene.crops_asked, [])
        self.assertEqual(scene.crop.state, "idle")

    def test_protocol_never_touches_the_file_system(self):
        scene = FakeScene()

        def refuse(*args, **kwargs):
            raise AssertionError(f"tried to open {args[:1]}")

        with mock.patch("builtins.open", refuse), mock.patch("io.open", refuse), mock.patch("os.open", refuse):
            self.assertEqual(handle(self.ASK, scene), {"ok": True, "pending": True})
            box = protocol.crop_box(0.4, 0.3, [0.1, 0.15], 1840, 1100)
            size = protocol.crop_size(box, 128)
            pixels = blank(*size)
            protocol.draw_bubble(pixels, *size, *protocol.bubble_in_crop(0.4, 0.3, [0.1, 0.15], 1840, 1100, box, size))
            scene.crop.finish(protocol.encode_png(*size, bytes(pixels)))
            reply = handle({"cmd": "gaze_crop_result"}, scene)
        self.assertTrue(reply["ok"])
        self.assertEqual(decode_png(base64.b64decode(reply["png"]))[:2], size)

    def test_a_picture_nobody_reads_is_gone_by_the_next_ask(self):
        clock = Clock(10.0)
        scene = FakeScene()
        scene.crop = protocol.CropSlot(clock=clock)
        handle(self.ASK, scene)
        scene.crop.finish(protocol.encode_png(2, 2, bytes(12)))
        clock.now += protocol.CROP_KEEP_SECONDS + 0.1
        self.assertEqual(handle({"cmd": "gaze_crop_result"}, scene), {"ok": False, "error": "no picture is waiting"})

    def test_the_addon_does_not_need_numpy_to_start(self):
        # A missing numpy must only fail a crop, never the add-on's register(), which would take voice down with it.
        tree = ast.parse(ADDON.read_text())
        top_level = [
            name.split(".")[0]
            for node in tree.body
            for name in (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
        ]
        self.assertIn("bpy", top_level)  # the scan sees the real imports
        self.assertNotIn("numpy", top_level)
        crop_png = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_crop_png")
        inside = [alias.name for node in ast.walk(crop_png) if isinstance(node, ast.Import) for alias in node.names]
        self.assertEqual(inside, ["numpy"])

    def test_the_add_on_uses_no_name_it_never_defines(self):
        # The add-on can't run here (it needs bpy), so a name that is used and never defined, which Python only finds
        # out when that line runs inside Blender, is looked for in the source instead.
        names = undefined_names(ADDON.read_text())
        self.assertEqual(names, set())
        self.assertEqual(undefined_names("def f():\n    return MARKER_TICK\n"), {("f", "MARKER_TICK")})  # and it can

    def test_everything_the_add_on_asks_of_the_protocol_is_there(self):
        # Like a name never defined, a protocol.something that isn't there only shows inside Blender, when that line
        # runs: say, while a picture is being taken.
        used = {node.attr for node in ast.walk(ast.parse(ADDON.read_text()))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "protocol"}
        self.assertIn("MarkerGlide", used)  # the scan sees the real uses
        self.assertEqual({name for name in used if not hasattr(protocol, name)}, set())

    def test_protocol_stays_free_of_blender_and_numpy(self):
        source = PROTOCOL.read_text()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+(bpy|numpy|gpu|PIL)\b", source, re.MULTILINE))
        self.assertFalse(hasattr(protocol, "np") or hasattr(protocol, "numpy") or hasattr(protocol, "bpy"))


STAND_INS = ("blf", "bpy", "gpu", "bpy_extras", "bpy_extras.view3d_utils", "gpu_extras", "gpu_extras.batch",
             "mathutils", "mathutils.bvhtree")


def load_addon(test):
    """The add-on, loaded against stand-ins for Blender's modules, which go away when the test ends."""
    patched = mock.patch.dict("sys.modules", {name: mock.MagicMock(name=name) for name in STAND_INS})
    patched.start()
    test.addCleanup(patched.stop)
    spec = importlib.util.spec_from_file_location("gazevoice_bridge", ADDON, submodule_search_locations=[str(ADDON.parent)])
    addon = importlib.util.module_from_spec(spec)
    sys.modules["gazevoice_bridge"] = addon  # its `from . import protocol` needs the package by name
    spec.loader.exec_module(addon)
    return addon


class DrawMarkerTest(unittest.TestCase):
    """The live marker's look, read off what the add-on hands the GPU to draw (stand-ins here, so no GPU is needed):
    a see-through bubble about MARKER_SIZE points across, soft at its edge, with a faint rim and no solid core."""

    WIDTH, HEIGHT = 1000, 600

    def setUp(self):
        self.addon = load_addon(self)
        self.batches = []

        def batch_for_shader(_shader, kind, content, **_kwargs):
            self.batches.append((kind, content))
            return mock.MagicMock()

        self.addon.batch_for_shader = batch_for_shader
        window, area, region = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        area.as_pointer.return_value = 7
        region.width, region.height = self.WIDTH, self.HEIGHT
        self.addon.bpy.context.area, self.addon.bpy.context.region = area, region
        self.addon._target = lambda: (window, area, region)
        self.addon._events.live = lambda *_args, **_kwargs: True
        self.scale = 1
        self.addon._pixels_per_point = lambda _window: self.scale
        glide = self.addon._marker_glide
        glide.x, glide.y, glide.alpha, glide.vx, glide.vy = 0.5, 0.5, 1.0, 0.0, 0.0

    def drawn(self):
        """Every vertex drawn, as (x, y, opacity), from the region's bottom left in pixels."""
        self.batches.clear()
        self.addon._draw_marker()
        points = []
        for _kind, content in self.batches:
            self.assertIn("color", content, "the marker is drawn with a colour on each vertex")
            points += [(x, y, colour[3]) for (x, y), colour in zip(content["pos"], content["color"])]
        self.assertTrue(points, "nothing was drawn")
        return points

    def test_it_is_a_bubble_about_ninety_points_across(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                self.scale = scale
                points = self.drawn()
                reach = max(math.hypot(x - 500, y - 300) for x, y, _a in points)
                self.assertTrue(40 * scale <= reach <= 50 * scale, reach)  # points, so twice the pixels on Retina

    def test_the_model_shows_through_every_part_of_it(self):
        self.assertLessEqual(max(a for _x, _y, a in self.drawn()), 0.35)

    def test_it_has_no_solid_core_and_a_rim_stronger_than_its_middle(self):
        points = self.drawn()
        reach = max(math.hypot(x - 500, y - 300) for x, y, _a in points)
        middle = max(a for x, y, a in points if math.hypot(x - 500, y - 300) < 0.4 * reach)
        rim = max(a for x, y, a in points if 0.6 * reach < math.hypot(x - 500, y - 300) < reach)
        self.assertLessEqual(middle, 0.12)
        self.assertGreaterEqual(rim, 2 * middle)

    def test_its_edge_is_soft(self):
        points = self.drawn()
        reach = max(math.hypot(x - 500, y - 300) for x, y, _a in points)
        outermost = [a for x, y, a in points if math.hypot(x - 500, y - 300) > 0.99 * reach]
        self.assertEqual(max(outermost), 0.0)  # it fades to nothing, so it has no hard outline

    def test_it_fades_in_and_out_as_a_whole(self):
        full = self.drawn()
        self.addon._marker_glide.alpha = 0.5
        half = self.drawn()
        self.assertEqual(len(full), len(half))
        for (_x, _y, a), (_x2, _y2, b) in zip(full, half):
            self.assertAlmostEqual(b, a / 2)

    def test_it_stretches_along_its_way_while_it_moves(self):
        rest = self.drawn()
        self.addon._marker_glide.vx = 20 / self.WIDTH  # 20 pixels a step, to the right

        def spans(points):
            xs, ys = [x for x, _y, _a in points], [y for _x, y, _a in points]
            return max(xs) - min(xs), max(ys) - min(ys)

        moving = self.drawn()
        (rest_along, rest_across), (along, across) = spans(rest), spans(moving)
        self.assertGreater(along, rest_along * 1.1)  # longer along its way...
        self.assertLess(across, rest_across)  # ...and narrower across it
        self.assertLess(sum(x for x, _y, _a in moving) / len(moving), 500)  # trailing back the way it came

    def test_nothing_is_drawn_while_it_is_hidden(self):
        self.addon._marker_glide.alpha = 0.0
        self.addon._draw_marker()
        self.assertEqual(self.batches, [])


class DrawStatusTest(unittest.TestCase):
    """The add-on's draw handler runs inside Blender, so here it runs against stand-ins for Blender's modules. What is
    pinned is what must hold wherever it runs: a fault in the marker never stops the rest of the drawing."""

    def setUp(self):
        self.addon = load_addon(self)
        self.addon._status.update(text="Heard: add a cube", state="working", at=0.0)
        self.addon._status_visible = lambda: True
        self.addon._draw_gaze = mock.Mock()
        self.addon._draw_marker = mock.Mock(side_effect=RuntimeError("gpu said no"))

    def draw(self, times=1):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            for _ in range(times):
                self.addon._draw_status()
        return out.getvalue()

    def test_a_fault_in_the_marker_still_lets_the_dot_ring_numbers_and_status_line_draw(self):
        self.draw()
        self.addon._draw_gaze.assert_called_once()
        self.addon.blf.draw.assert_called_once_with(0, "GazeVoice · Heard: add a cube")

    def test_the_fault_is_printed_once_not_on_every_redraw(self):
        said = self.draw(times=5)
        self.assertEqual(said.count("gpu said no"), 1)
        self.assertEqual(self.addon._draw_gaze.call_count, 5)  # and the rest drew every time

    def test_a_marker_that_works_draws_as_it_did_and_prints_nothing(self):
        self.addon._draw_marker = mock.Mock()
        self.assertEqual(self.draw(times=3), "")
        self.assertEqual(self.addon._draw_marker.call_count, 3)


if __name__ == "__main__":
    unittest.main()
