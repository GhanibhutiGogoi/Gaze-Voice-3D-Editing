import http.client
import json
import threading
import unittest

from gazevoice.components.base import Component, ComponentState, Health
from gazevoice.server.app import WORKFLOW_ID, ControlCenter, make_server
from gazevoice.settings import Settings, update_settings
from gazevoice.voice.service import VoiceError
from gazevoice.voice.wiring import voice_readiness

from .helpers import TempCtx

UNITS = ["microphone", "speech", "claude", "codex", "blender", "mcp_addon", "mcp_server", "bridge"]


def unit(cid, health):
    class Unit(Component):
        id = cid
        name = cid.replace("_", " ").title()
        role = "fake"

        def check(self):
            return self.state(health, f"{cid} summary")

    return Unit


class FakeVoice:
    def __init__(self):
        self.calls = []
        self.state = "off"
        self.key = "⌥ Space"

    def start(self):
        self.calls.append("start")
        self.state = "ready"

    def stop(self):
        self.calls.append("stop")
        self.state = "off"

    def say(self, text):
        if self.state == "off":
            raise VoiceError("Turn voice on first.")
        self.calls.append(("say", text))
        return {"id": "r1", "heard": text}

    def ptt(self, state, t=None):
        self.calls.append(("ptt", state))

    def undo(self):
        self.calls.append("undo")

    def reset_conversation(self):
        self.calls.append("reset")

    def snapshot(self):
        return {"state": self.state, "reason": "", "recent": []}


def states(**overrides):
    result = {cid: ComponentState(cid, cid, "", Health.READY, "") for cid in UNITS}
    for cid, health in overrides.items():
        result[cid] = ComponentState(cid, cid.replace("_", " ").title(), "", health, f"{cid} summary")
    return result


class ReadinessTest(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(voice_readiness(states(), "claude"), [])

    def test_untested_microphone_is_fine(self):
        self.assertEqual(voice_readiness(states(microphone=Health.INSTALLED), "claude"), [])

    def test_lists_what_is_missing(self):
        missing = voice_readiness(states(speech=Health.MISSING, bridge=Health.INSTALLED), None)
        self.assertEqual(len(missing), 3)
        self.assertTrue(any("Speech" in m for m in missing))
        self.assertTrue(any("brain" in m.lower() for m in missing))
        self.assertTrue(any("Bridge" in m for m in missing))

    def test_still_checking_units_are_named(self):
        missing = voice_readiness(states(mcp_addon=Health.CHECKING), "claude")
        self.assertIn("still being checked", missing[0])


class SettingsTest(unittest.TestCase):
    def test_model_settings(self):
        s = update_settings(Settings(), {"editor_model": "sonnet", "refiner_model": "default"})
        self.assertEqual((s.editor_model, s.refiner_model), ("sonnet", "default"))
        self.assertEqual((Settings().editor_model, Settings().refiner_model), ("default", "haiku"))
        with self.assertRaises(ValueError):
            update_settings(Settings(), {"editor_model": "gpt"})


class VoiceApiTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.voice = FakeVoice()
        self.healths = {cid: Health.READY for cid in UNITS}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.t.cleanup()

    def start_server(self):
        components = [unit(cid, self.healths[cid])(self.t.ctx) for cid in UNITS]
        self.center = ControlCenter(self.t.ctx, components, check_timeout=2, voice_factory=lambda brain: self.voice)
        self.server = make_server(self.center, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def post(self, path, body=None, write=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-GazeVoice": "1", "Content-Type": "application/json"} if write else {}
        conn.request("POST", path, body=json.dumps(body or {}), headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_start_refuses_with_the_missing_list(self):
        self.healths["speech"] = Health.MISSING
        self.start_server()
        status, body = self.post("/api/voice/start")
        self.assertEqual(status, 409)
        self.assertTrue(any("Speech" in m for m in body["missing"]))
        self.assertEqual(self.voice.calls, [])

    def test_start_runs_as_a_job_and_commands_reach_the_service(self):
        self.start_server()
        status, body = self.post("/api/voice/start")
        self.assertEqual(status, 202)
        job = self.center.jobs.wait(body["job"]["id"], 5)
        self.assertEqual(job.state, "succeeded", job.error)
        self.assertEqual(self.voice.calls, ["start"])
        self.assertEqual(self.post("/api/voice/say", {"text": "add a cube"})[0], 202)
        self.assertEqual(self.post("/api/voice/ptt", {"state": "down"})[0], 202)
        self.assertEqual(self.post("/api/voice/ptt", {"state": "sideways"})[0], 400)
        self.assertEqual(self.post("/api/voice/undo")[0], 202)
        self.assertEqual(self.post("/api/voice/reset")[0], 202)
        self.assertEqual(self.post("/api/voice/stop")[0], 200)
        self.assertEqual(
            self.voice.calls,
            ["start", ("say", "add a cube"), ("ptt", "down"), "undo", "reset", "stop"],
        )
        status, snapshot = self.get("/api/voice")
        self.assertEqual((status, snapshot["state"]), (200, "off"))

    def test_start_waits_while_check_everything_runs(self):
        self.start_server()
        gate = threading.Event()
        check = self.center.jobs.start(WORKFLOW_ID, "check", lambda job: gate.wait(5))
        try:
            status, body = self.post("/api/voice/start")
            self.assertEqual((status, body.get("job_id")), (409, check.id))
            self.assertEqual(self.voice.calls, [])
        finally:
            gate.set()

    def test_say_while_off_is_409(self):
        self.start_server()
        status, body = self.post("/api/voice/say", {"text": "add a cube"})
        self.assertEqual(status, 409)
        self.assertIn("Turn voice on", body["error"])

    def test_voice_routes_are_guarded(self):
        self.start_server()
        self.assertEqual(self.post("/api/voice/start", write=False)[0], 403)

    def test_state_includes_voice(self):
        self.start_server()
        _, body = self.get("/api/state")
        self.assertEqual(body["voice"]["state"], "off")
        self.assertEqual(body["voice"]["missing"], [])


if __name__ == "__main__":
    unittest.main()
