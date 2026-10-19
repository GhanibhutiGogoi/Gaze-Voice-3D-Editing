import json
import socket
import socketserver
import threading
import time
import unittest
import zipfile

from gazevoice.blender_socket import BRIDGE_VERSION, BridgeClient, bridge_request, port_open
from gazevoice.components.base import Health
from gazevoice.components.blender import extensions_dir
from gazevoice.components.bridge import BRIDGE_SRC, Bridge, build_bridge_zip
from gazevoice.jobs import JobError

from .helpers import TempCtx, make_job, with_blender
from .test_bridge_protocol import FakeScene, protocol


class FakeBridge:
    """A real TCP server speaking the bridge protocol, on an ephemeral port."""

    def __init__(self, delay: float = 0.0):
        scene = self.scene = FakeScene()
        events = self.events = protocol.EventQueue()

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    for request in protocol.decode_lines(bytearray(line)):
                        time.sleep(delay)
                        self.wfile.write(protocol.encode(protocol.handle(request, scene, events)))

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SocketHelpersTest(unittest.TestCase):
    def test_bridge_request_round_trip(self):
        fake = FakeBridge()
        try:
            self.assertEqual(bridge_request({"cmd": "ping"}, port=fake.port)["blender"], "5.2.2")
            self.assertTrue(port_open("127.0.0.1", fake.port))
        finally:
            fake.close()

    def test_bridge_client_reuses_one_connection(self):
        fake = FakeBridge()
        accepted = []
        original = fake.server.RequestHandlerClass.setup

        def counting_setup(handler):
            accepted.append(1)
            original(handler)

        fake.server.RequestHandlerClass.setup = counting_setup
        client = BridgeClient(port=fake.port)
        try:
            for _ in range(3):
                self.assertTrue(client.request({"cmd": "ping"})["ok"])
            self.assertEqual(len(accepted), 1)
        finally:
            client.close()
            fake.close()

    def test_bridge_client_raises_when_bridge_is_gone(self):
        client = BridgeClient(port=free_port(), timeout=0.5)
        with self.assertRaises(OSError):
            client.request({"cmd": "ping"})

    def test_a_request_can_wait_longer_than_the_default(self):
        fake = FakeBridge(delay=0.5)
        client = BridgeClient(port=fake.port, timeout=0.2)
        try:
            with self.assertRaises(OSError):
                client.request({"cmd": "ping"})
            self.assertTrue(client.request({"cmd": "ping"}, timeout=3)["ok"])
            with self.assertRaises(OSError):  # and the default applies again afterwards
                client.request({"cmd": "ping"})
        finally:
            client.close()
            fake.close()

    def test_port_open_false_when_closed(self):
        self.assertFalse(port_open("127.0.0.1", free_port()))


class BridgeComponentTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.bridge = Bridge(self.t.ctx)
        self.bridge.candidates = [self.t.root / "Blender.app"]
        self.bridge.home = self.t.root / "home"
        self.bridge.port = free_port()

    def tearDown(self):
        self.t.cleanup()

    def addon_dir(self):
        return extensions_dir((5, 2, 2), home=self.bridge.home) / "gazevoice_bridge"

    def test_needs_blender_first(self):
        state = self.bridge.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Install Blender first", state.summary)
        self.assertEqual(state.actions, ())

    def test_not_installed_offers_install(self):
        with_blender(self.t)
        state = self.bridge.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_installed_but_blender_not_running(self):
        with_blender(self.t)
        self.addon_dir().mkdir(parents=True)
        state = self.bridge.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Start Blender", state.summary)

    def test_installed_while_blender_runs_says_restart(self):
        with_blender(self.t, running=True)
        self.addon_dir().mkdir(parents=True)
        state = self.bridge.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Restart Blender", state.summary)

    def test_build_zip_refuses_missing_source(self):
        with self.assertRaises(JobError):
            build_bridge_zip(self.t.root / "nowhere", self.t.root / "bridge.zip")

    def test_ready_when_bridge_answers(self):
        with_blender(self.t, running=True)
        self.addon_dir().mkdir(parents=True)
        fake = FakeBridge()
        self.bridge.port = fake.port
        try:
            state = self.bridge.check()
            self.assertEqual(state.health, Health.READY)
            self.assertIn("5.2.2", state.summary)
            result = self.bridge.run_action("test", make_job("bridge"))
            self.assertEqual(result["selection"]["selected"], ["Chair", "Leg.001"])
            self.assertEqual(result["objects"], 5)
            self.assertEqual(fake.scene.status, "GazeVoice connected")
        finally:
            fake.close()

    def test_outdated_bridge_offers_update(self):
        with_blender(self.t, running=True)
        self.addon_dir().mkdir(parents=True)
        fake = FakeBridge()
        self.bridge.port = fake.port
        self.bridge.expected_version = "9.9.9"
        try:
            state = self.bridge.check()
        finally:
            fake.close()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Update needed", state.summary)
        update = next(a for a in state.actions if a.id == "install")
        self.assertEqual(update.label, "Update add-on")

    def test_build_zip_has_manifest_at_root(self):
        zip_path = build_bridge_zip(BRIDGE_SRC, self.t.root / "bridge.zip")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        self.assertIn("blender_manifest.toml", names)
        self.assertIn("__init__.py", names)
        self.assertIn("protocol.py", names)
        self.assertFalse(any("__pycache__" in n for n in names))

    def test_install_builds_zip_and_installs_extension(self):
        binary = with_blender(self.t)
        zip_path = self.t.paths.downloads / f"gazevoice_bridge-{BRIDGE_VERSION}.zip"
        argv = [str(binary), "--command", "extension", "install-file", "-r", "user_default", "-e", str(zip_path)]
        self.t.runner.add(argv, stdout="Installed\n")
        self.bridge.run_action("install", make_job("bridge", "install"))
        self.assertTrue(zip_path.is_file())
        self.assertIn(tuple(argv), self.t.runner.calls)


class ManifestTest(unittest.TestCase):
    def test_manifest_fields(self):
        text = (BRIDGE_SRC / "blender_manifest.toml").read_text()
        for needle in ('id = "gazevoice_bridge"', f'version = "{BRIDGE_VERSION}"', 'blender_version_min = "5.1.0"', "network ="):
            self.assertIn(needle, text)
        json.dumps(text)  # plain text, no surprises


if __name__ == "__main__":
    unittest.main()
