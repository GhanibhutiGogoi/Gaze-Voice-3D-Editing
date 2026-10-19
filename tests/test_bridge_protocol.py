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


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def handle(request, scene=None, events=None):
    return protocol.handle(request, scene or FakeScene(), events or protocol.EventQueue())


class BridgeProtocolTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((protocol.BRIDGE_PORT, protocol.BRIDGE_VERSION), (9877, "0.2.0"))

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
            {"ok": True, "bridge": "0.2.0", "blender": "5.2.2", "key": "⌥ Space"},
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


if __name__ == "__main__":
    unittest.main()
