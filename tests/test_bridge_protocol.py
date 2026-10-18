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

    def version(self):
        return "5.2.2"

    def active_name(self):
        return "Chair"

    def selected_names(self):
        return ["Chair", "Leg.001"]

    def mode(self):
        return "OBJECT"

    def set_status(self, text):
        self.status = text


class BridgeProtocolTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((protocol.BRIDGE_PORT, protocol.BRIDGE_VERSION), (9877, "0.1.0"))

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
            protocol.handle({"cmd": "ping"}, FakeScene()), {"ok": True, "bridge": "0.1.0", "blender": "5.2.2"}
        )

    def test_selection(self):
        self.assertEqual(
            protocol.handle({"cmd": "selection"}, FakeScene()),
            {"ok": True, "active": "Chair", "selected": ["Chair", "Leg.001"], "mode": "OBJECT"},
        )

    def test_status_sets_and_truncates(self):
        scene = FakeScene()
        self.assertEqual(protocol.handle({"cmd": "status", "text": "x" * 500}, scene), {"ok": True})
        self.assertEqual(len(scene.status), 200)

    def test_status_requires_text(self):
        self.assertEqual(
            protocol.handle({"cmd": "status", "text": 5}, FakeScene()), {"ok": False, "error": "text must be a string"}
        )

    def test_unknown(self):
        self.assertEqual(protocol.handle({"cmd": "explode"}, FakeScene()), {"ok": False, "error": "unknown command"})


if __name__ == "__main__":
    unittest.main()
