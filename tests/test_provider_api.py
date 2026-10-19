import http.client
import json
import socket
import threading
import unittest

from gazevoice.components.base import Component, Health
from gazevoice.providers import KeyStore
from gazevoice.server.app import ControlCenter, make_server

from .fake_openai import FakeOpenAI
from .helpers import TempCtx
from .test_voice_api import UNITS, FakeVoice, unit


def find(pid, secret=False):
    argv = ["security", "find-generic-password", "-s", "GazeVoice", "-a", pid]
    return argv + ["-w"] if secret else argv


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProviderApiTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.keys = KeyStore(self.t.runner)
        components = [unit(cid, Health.READY)(self.t.ctx) for cid in UNITS]
        self.center = ControlCenter(self.t.ctx, components, check_timeout=2,
                                    voice_factory=lambda sources: FakeVoice(), keys=self.keys)
        self.server = make_server(self.center, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.t.cleanup()

    def call(self, method, path, body=None, write=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-GazeVoice": "1", "Content-Type": "application/json"} if write else {}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def keychain_accepts(self, pid):
        self.t.runner.add(["security", "-i"])
        self.t.runner.add(find(pid))

    def test_save_deepseek_key(self):
        self.keychain_accepts("deepseek")
        status, body = self.call("POST", "/api/providers/deepseek", {"kind": "deepseek", "key": " sk-1234 "})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["provider"]["key_hint"], body["provider"]["has_key"]), ("1234", True))
        self.assertNotIn("sk-1234", json.dumps(body))
        self.assertNotIn("sk-1234", self.t.paths.settings_file.read_text())
        sent = [kw["input_text"] for call, kw in zip(self.t.runner.calls, self.t.runner.call_kwargs)
                if call == ("security", "-i")]
        self.assertIn('-w "sk-1234"', sent[0])
        _, state = self.call("GET", "/api/state")
        self.assertIn("deepseek", [c["id"] for c in state["components"]])

    def test_provider_writes_are_guarded(self):
        status, _ = self.call("POST", "/api/providers/deepseek", {"kind": "deepseek", "key": "sk-1234"}, write=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.t.runner.calls, [])

    def test_custom_new_gets_an_id_from_its_name(self):
        self.keychain_accepts("custom_home_box")
        status, body = self.call("POST", "/api/providers/custom_new",
                                 {"name": "Home Box", "base_url": "https://box.test/v1/chat/completions", "key": "sk-9999"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["provider"]["id"], "custom_home_box")
        self.assertEqual(body["provider"]["base_url"], "https://box.test/v1")
        self.assertIn("custom_home_box", [p["id"] for p in body["settings"]["providers"]])
        _, state = self.call("GET", "/api/state")
        self.assertIn("custom_home_box", [c["id"] for c in state["components"]])

    def test_bad_input_is_a_plain_400(self):
        status, body = self.call("POST", "/api/providers/deepseek", {"kind": "deepseek", "key": "sk abc"})
        self.assertEqual(status, 400)
        self.assertNotIn("sk abc", body["error"])
        status, body = self.call("POST", "/api/providers/qwen", {"region": "us-east-1", "key": "sk-1"})
        self.assertEqual(status, 400)
        self.assertIn("Workspace ID", body["error"])
        status, body = self.call("POST", "/api/providers/deepseek", {"kind": "deepseek"})
        self.assertEqual(status, 400)
        self.assertIn("API key", body["error"])
        self.assertEqual(self.call("POST", "/api/providers/nobody", {"key": "sk-1"})[0], 404)

    def test_fields_that_arent_text_are_a_plain_400(self):
        for body in ({"key": 123}, {"key": ["sk-1"]}, {"region": 5, "key": "sk-1"}):
            status, reply = self.call("POST", "/api/providers/qwen", body)
            self.assertEqual(status, 400, (body, reply))
        status, _ = self.call("POST", "/api/providers/custom_new", {"name": "X", "base_url": ["https://a.test"], "key": "sk-1"})
        self.assertEqual(status, 400)

    def test_remove_moves_steps_to_auto(self):
        self.keychain_accepts("deepseek")
        self.call("POST", "/api/providers/deepseek", {"key": "sk-1234"})
        self.assertEqual(self.call("PUT", "/api/settings", {"editor_source": "deepseek"})[0], 200)
        self.t.runner.add(["security", "delete-generic-password", "-s", "GazeVoice", "-a", "deepseek"])
        status, body = self.call("POST", "/api/providers/deepseek/remove", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["moved_to_auto"], ["editor"])
        self.assertEqual(body["settings"]["editor_source"], "auto")
        self.assertNotIn("deepseek", [p["id"] for p in body["settings"]["providers"]])
        self.assertIn(("security", "delete-generic-password", "-s", "GazeVoice", "-a", "deepseek"), self.t.runner.calls)

    def test_models_endpoint_lists_models(self):
        fake = FakeOpenAI()
        self.addCleanup(fake.close)
        self.keychain_accepts("custom_fake")
        self.call("POST", "/api/providers/custom_new", {"name": "Fake", "base_url": fake.url, "key": "sk-1"})
        self.t.runner.add(find("custom_fake", secret=True), stdout="sk-1\n")
        fake.reply({"data": [{"id": "m-small"}, {"id": "m-big"}]})
        status, body = self.call("GET", "/api/providers/custom_fake/models", write=False)
        self.assertEqual((status, body["models"]), (200, ["m-small", "m-big"]))
        self.assertEqual(self.call("GET", "/api/providers/custom_fake/models", write=False)[1]["models"],
                         ["m-small", "m-big"])
        self.assertEqual(len(fake.requests), 1)  # the list is remembered for a while

    def test_a_unit_removed_mid_check_doesnt_break_the_snapshot(self):
        gate = threading.Event()

        class Slowpoke(Component):
            id = "slowpoke"
            name = "Slowpoke"
            role = "fake"

            def check(self):
                gate.wait(3)
                return self.state(Health.READY, "ok")

        center = ControlCenter(self.t.ctx, [Slowpoke(self.t.ctx)], check_timeout=0.3, keys=self.keys)
        self.addCleanup(gate.set)
        threading.Timer(0.1, lambda: setattr(center, "components", {})).start()  # e.g. Remove rebuilt the rack
        states = center.check_all()
        self.assertEqual(states["slowpoke"].health, Health.CHECKING)

    def test_models_endpoint_reports_errors_plainly(self):
        self.keychain_accepts("custom_gone")
        self.call("POST", "/api/providers/custom_new",
                  {"name": "Gone", "base_url": f"http://127.0.0.1:{free_port()}/v1", "key": "sk-1"})
        self.t.runner.add(find("custom_gone", secret=True), stdout="sk-1\n")
        status, body = self.call("GET", "/api/providers/custom_gone/models", write=False)
        self.assertEqual((status, body["models"]), (200, []))
        self.assertIn("Couldn't reach", body["error"])


if __name__ == "__main__":
    unittest.main()
