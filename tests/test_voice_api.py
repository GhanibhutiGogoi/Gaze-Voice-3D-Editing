import http.client
import json
import threading
import unittest

from gazevoice.components.base import Component, ComponentState, Health
from gazevoice.providers import preset
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
        self.assertEqual(len(missing), 4)
        self.assertTrue(any("Speech" in m for m in missing))
        self.assertTrue(any(m.startswith("Cleanup:") and "brain" in m.lower() for m in missing))
        self.assertTrue(any(m.startswith("Editing:") for m in missing))
        self.assertTrue(any("Bridge" in m for m in missing))

    def test_readiness_names_the_step(self):
        deepseek = dict(states(), deepseek=ComponentState("deepseek", "DeepSeek", "", Health.MISSING, "no key"))
        settings = update_settings(Settings(), {"providers": [preset("deepseek").to_dict()], "editor_source": "deepseek"})
        missing = voice_readiness(deepseek, {"refiner": "claude", "editor": "deepseek"}, settings)
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].startswith("Editing: DeepSeek"))
        keyed = dict(deepseek, deepseek=ComponentState("deepseek", "DeepSeek", "", Health.READY, "key"))
        missing = voice_readiness(keyed, {"refiner": "claude", "editor": "deepseek"}, settings)
        self.assertIn("model", missing[0])
        both = update_settings(settings, {"refiner_source": "deepseek", "editor_api_model": "deepseek-v4-pro",
                                          "refiner_api_model": "deepseek-flash"})
        no_cli = dict(keyed, claude=ComponentState("claude", "Claude", "", Health.MISSING, "x"),
                      codex=ComponentState("codex", "Codex", "", Health.MISSING, "x"))
        self.assertEqual(voice_readiness(no_cli, {"refiner": "deepseek", "editor": "deepseek"}, both), [])

    def test_still_checking_units_are_named(self):
        missing = voice_readiness(states(mcp_addon=Health.CHECKING), "claude")
        self.assertIn("still being checked", missing[0])


class SettingsTest(unittest.TestCase):
    def test_model_and_thinking_settings(self):
        s = update_settings(Settings(), {"editor_model": "claude-opus-5-5", "editor_effort": "max",
                                         "refiner_model": "claude-fable-5-1", "refiner_effort": "low"})
        self.assertEqual((s.editor_model, s.editor_effort, s.refiner_model, s.refiner_effort),
                         ("claude-opus-5-5", "max", "claude-fable-5-1", "low"))
        d = Settings()
        self.assertEqual((d.editor_model, d.editor_effort, d.refiner_model, d.refiner_effort),
                         ("default", "default", "claude-haiku-5-5", "default"))
        for model in ("default", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
            self.assertEqual(update_settings(Settings(), {"refiner_model": model}).refiner_model, model)
        for effort in ("default", "low", "medium", "high", "xhigh", "max"):
            self.assertEqual(update_settings(Settings(), {"editor_effort": effort}).editor_effort, effort)
        for bad in ({"editor_model": "gpt"}, {"editor_model": "opus"}, {"editor_effort": "ludicrous"},
                    {"refiner_effort": ""}):
            with self.assertRaises(ValueError):
                update_settings(Settings(), bad)


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

    def test_changing_thinking_while_voice_is_on_asks_for_a_restart(self):
        self.start_server()
        _, body = self.post("/api/voice/start")
        self.center.jobs.wait(body["job"]["id"], 5)
        self.assertFalse(self.get("/api/state")[1]["voice"]["needs_restart"])
        self.center.update_settings({"editor_effort": "max"})
        self.assertTrue(self.get("/api/state")[1]["voice"]["needs_restart"])

    def test_changing_a_source_asks_for_a_restart(self):
        self.start_server()
        _, body = self.post("/api/voice/start")
        self.center.jobs.wait(body["job"]["id"], 5)
        self.assertFalse(self.get("/api/state")[1]["voice"]["needs_restart"])
        self.center.update_settings({"refiner_source": "codex"})
        self.assertTrue(self.get("/api/state")[1]["voice"]["needs_restart"])

    def test_state_names_each_steps_brain(self):
        self.start_server()
        _, body = self.get("/api/state")
        self.assertEqual(body["sources"], {"refiner": "claude", "editor": "claude"})
        self.assertEqual(body["brain"], "claude")

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
