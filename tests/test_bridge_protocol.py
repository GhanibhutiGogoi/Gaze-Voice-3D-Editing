import ast
import base64
import importlib.util
import io
import os
import re
import struct
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

    def gaze_crop(self, u, v, radius, size):
        if self.view is None:
            return "no 3D viewport"
        if not self.crop.begin():
            return "a picture is already being taken"
        self.crops_asked.append((u, v, radius, size))
        return None

    def gaze_crop_result(self):
        return self.crop.read()


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

    def test_the_ring_lands_where_the_target_is_in_the_crop(self):
        box = protocol.crop_box(0.5, 0.5, [0.1, 0.2], 1000, 500)
        size = protocol.crop_size(box, 512)
        cx, cy, rx, ry = protocol.ring_in_crop(0.5, 0.5, [0.1, 0.2], 1000, 500, box, size)
        self.assertAlmostEqual(cx, size[0] / 2)
        self.assertAlmostEqual(cy, size[1] / 2)
        self.assertAlmostEqual(rx / size[0], 1 / 2.5 / 2)  # a radius is 1/2.5 of the half-box on each side
        self.assertAlmostEqual(ry / size[1], 1 / 2.5 / 2)

    def test_the_ring_follows_the_target_when_the_box_is_cut_off(self):
        box = protocol.crop_box(0.0, 0.0, [0.1, 0.1], 1000, 1000)  # (0, 0, 250, 250)
        cx, cy, rx, ry = protocol.ring_in_crop(0.0, 0.0, [0.1, 0.1], 1000, 1000, box, (250, 250))
        self.assertEqual((cx, cy, rx, ry), (0.0, 0.0, 100.0, 100.0))

    def test_the_ring_is_scaled_on_each_axis_on_its_own(self):
        # a 100 x 100 box shown 200 wide and 100 tall: the ring's centre and radii stretch the same way
        cx, cy, rx, ry = protocol.ring_in_crop(0.5, 0.25, [0.1, 0.2], 400, 400, (150, 50, 250, 150), (200, 100))
        self.assertEqual((cx, cy, rx, ry), (100.0, 50.0, 80.0, 80.0))

    def test_draw_ring_marks_only_the_outline(self):
        width = height = 100
        pixels = blank(width, height)
        before = bytes(pixels)
        protocol.draw_ring(pixels, width, height, 50, 50, 30, 20)
        self.assertEqual(len(pixels), len(before))
        self.assertEqual(pixel(pixels, width, 50, 50), (40, 40, 40))  # the centre is left alone
        self.assertEqual(pixel(pixels, width, 0, 0), (40, 40, 40))
        self.assertEqual(pixel(pixels, width, 60, 50), (40, 40, 40))  # well inside
        self.assertEqual(pixel(pixels, width, 99, 99), (40, 40, 40))
        self.assertEqual(pixel(pixels, width, 80, 50), protocol.RING_COLOR)  # the right end of the ring
        self.assertEqual(pixel(pixels, width, 20, 50), protocol.RING_COLOR)
        self.assertEqual(pixel(pixels, width, 50, 30), protocol.RING_COLOR)  # the top
        self.assertEqual(pixel(pixels, width, 50, 70), protocol.RING_COLOR)
        # everything it changed sits close to the outline
        changed = 0
        for y in range(height):
            for x in range(width):
                if pixel(pixels, width, x, y) != (40, 40, 40):
                    changed += 1
                    f = (((x + 0.5 - 50) / 30) ** 2 + ((y + 0.5 - 50) / 20) ** 2) ** 0.5
                    self.assertTrue(0.8 < f < 1.2, (x, y, f))
        self.assertGreater(changed, 200)
        self.assertLess(changed, 1500)

    def test_the_ring_has_a_dark_edge_so_it_shows_on_any_background(self):
        for background in ((0, 0, 0), (255, 255, 255), (128, 128, 128)):
            with self.subTest(background=background):
                pixels = blank(100, 100, background)
                protocol.draw_ring(pixels, 100, 100, 50, 50, 30, 30)
                colours = {pixel(pixels, 100, x, 50) for x in range(100)}
                self.assertIn(protocol.RING_COLOR, colours)
                self.assertIn(protocol.RING_EDGE_COLOR, colours)
                # the dark edge touches the background on both sides of the bright band
                row = [pixel(pixels, 100, x, 50) for x in range(100)]
                bright = [i for i, c in enumerate(row) if c == protocol.RING_COLOR]
                self.assertEqual(row[bright[0] - 1], protocol.RING_EDGE_COLOR)
                self.assertEqual(row[bright[-1] + 1], protocol.RING_EDGE_COLOR)

    def test_a_ring_running_off_the_picture_stays_inside_the_buffer(self):
        pixels = blank(40, 30)
        protocol.draw_ring(pixels, 40, 30, 0, 0, 100, 100)  # a corner of a big ring
        protocol.draw_ring(pixels, 40, 30, 20, 15, 500, 400)  # the whole picture inside the ring
        protocol.draw_ring(pixels, 40, 30, 20, 15, 0, 0)  # no size at all
        self.assertEqual(len(pixels), 40 * 30 * 3)

    def test_nothing_but_the_ring_is_drawn(self):
        pixels = blank(60, 60)
        protocol.draw_ring(pixels, 60, 60, 30, 30, 20, 20)
        colours = {pixel(pixels, 60, x, y) for y in range(60) for x in range(60)}
        self.assertEqual(colours, {(40, 40, 40), protocol.RING_COLOR, protocol.RING_EDGE_COLOR})


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
            protocol.draw_ring(pixels, *size, *protocol.ring_in_crop(0.4, 0.3, [0.1, 0.15], 1840, 1100, box, size))
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

    def test_protocol_stays_free_of_blender_and_numpy(self):
        source = PROTOCOL.read_text()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+(bpy|numpy|gpu|PIL)\b", source, re.MULTILINE))
        self.assertFalse(hasattr(protocol, "np") or hasattr(protocol, "numpy") or hasattr(protocol, "bpy"))


if __name__ == "__main__":
    unittest.main()
