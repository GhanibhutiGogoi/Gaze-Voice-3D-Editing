import http.client
import json
import socket
import threading
import time
import unittest

from gazevoice.components.base import ActionSpec, Component, Health
from gazevoice.jobs import JobState
from gazevoice.probes import ProbeTimedOut
from gazevoice.server.app import ControlCenter, make_server

from .helpers import TempCtx


class Okay(Component):
    id = "okay"
    name = "Okay"
    role = "fake"
    release = None  # threading.Event set by tests to hold a job open

    def check(self):
        return self.state(Health.READY, "fine", actions=[ActionSpec("go", "Go")])

    def action_go(self, job):
        job.log("going")
        if Okay.release is not None:
            Okay.release.wait(5)
        return {"went": True}


class Slow(Component):
    id = "slow"
    name = "Slow"
    role = "fake"

    def check(self):
        time.sleep(2)
        return self.state(Health.READY, "eventually")


class Broken(Component):
    id = "broken"
    name = "Broken"
    role = "fake"

    def check(self):
        raise RuntimeError("kaboom")


class Stalled(Component):
    """A check whose probe timed out on a busy Mac."""

    id = "stalled"
    name = "Stalled"
    role = "fake"

    def check(self):
        raise ProbeTimedOut(("claude", "auth", "status"))


class Workflow(Component):
    id = "workflow"
    name = "Workflow check"
    role = "fake"
    release = None

    def check(self):
        return self.state(Health.INSTALLED, "", actions=[ActionSpec("check", "Check everything")])

    def action_check(self, job):
        if Workflow.release is not None:
            Workflow.release.wait(5)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        Okay.release = None
        Workflow.release = None
        components = [Okay(self.t.ctx), Slow(self.t.ctx), Broken(self.t.ctx), Stalled(self.t.ctx), Workflow(self.t.ctx)]
        self.center = ControlCenter(self.t.ctx, components, check_timeout=0.3, sse_ping=0.2)
        self.server = make_server(self.center, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        for cls in (Okay, Workflow):
            if cls.release is not None:
                cls.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.t.cleanup()

    def request(self, method, path, body=None, headers=None, write=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"X-GazeVoice": "1"} if write else {}
        hdrs.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return resp.status, parsed

    def test_state_lists_components_and_workflow(self):
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in body["components"]], ["okay", "slow", "broken", "stalled", "workflow"])
        self.assertEqual(len(body["workflow"]), 5)
        self.assertIn("brain", body)

    def test_slow_check_shows_checking_without_blocking(self):
        started = time.monotonic()
        status, body = self.request("GET", "/api/state")
        self.assertLess(time.monotonic() - started, 1.5)
        slow = next(c for c in body["components"] if c["id"] == "slow")
        self.assertEqual((slow["health"], slow["summary"]), ("checking", "Still checking…"))

    def test_fresh_state_clears_probe_cache(self):
        self.t.runner.add(["probe"], stdout="x")
        self.center.ctx.probes.run(self.t.runner, ["probe"])
        self.request("GET", "/api/state?fresh=1")
        self.center.ctx.probes.run(self.t.runner, ["probe"])
        self.assertEqual(self.t.runner.calls.count(("probe",)), 2)

    def test_finished_job_clears_probe_cache(self):
        self.t.runner.add(["probe"], stdout="x")
        self.center.ctx.probes.run(self.t.runner, ["probe"])
        _, body = self.request("POST", "/api/components/okay/actions/go")
        self.center.jobs.wait(body["job"]["id"], 2)
        self.center.ctx.probes.run(self.t.runner, ["probe"])
        self.assertEqual(self.t.runner.calls.count(("probe",)), 2)

    def test_slow_check_keeps_last_known_state_and_announces_late_result(self):
        sub = self.center.bus.subscribe()
        self.request("GET", "/api/state")  # first call: "Still checking…"
        deadline = time.monotonic() + 5
        late = None
        while time.monotonic() < deadline and late is None:
            event = sub.get(timeout=0.5)
            if event and event.kind == "component.updated" and event.data["id"] == "slow":
                late = event
        self.assertIsNotNone(late, "the late result should be published")
        self.assertEqual(late.data["health"], "ready")
        # The next snapshot times out again, but shows the last known state instead.
        _, body = self.request("GET", "/api/state")
        slow = next(c for c in body["components"] if c["id"] == "slow")
        self.assertEqual((slow["health"], slow["summary"]), ("ready", "eventually"))

    def test_timed_out_probe_reads_as_checking_not_a_fault(self):
        _, body = self.request("GET", "/api/state")
        stalled = next(c for c in body["components"] if c["id"] == "stalled")
        self.assertEqual((stalled["health"], stalled["summary"]), ("checking", "Still checking…"))

    def test_units_are_locked_while_check_everything_runs(self):
        Workflow.release = threading.Event()
        self.assertEqual(self.request("POST", "/api/components/workflow/actions/check")[0], 202)
        status, body = self.request("POST", "/api/components/okay/actions/go")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "Already running")

    def test_check_everything_waits_for_running_units(self):
        Okay.release = threading.Event()
        self.assertEqual(self.request("POST", "/api/components/okay/actions/go")[0], 202)
        self.assertEqual(self.request("POST", "/api/components/workflow/actions/check")[0], 409)

    def test_crashing_check_reports_error(self):
        _, body = self.request("GET", "/api/state")
        broken = next(c for c in body["components"] if c["id"] == "broken")
        self.assertEqual(broken["health"], "error")
        self.assertIn("kaboom", broken["summary"])

    def test_post_without_header_is_forbidden(self):
        status, _ = self.request("POST", "/api/components/okay/actions/go", write=False)
        self.assertEqual(status, 403)

    def test_foreign_host_is_forbidden(self):
        status, _ = self.request("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_foreign_origin_is_forbidden(self):
        status, _ = self.request(
            "POST", "/api/components/okay/actions/go", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)

    def test_same_origin_post_runs_job(self):
        status, body = self.request(
            "POST", "/api/components/okay/actions/go", headers={"Origin": f"http://127.0.0.1:{self.port}"}
        )
        self.assertEqual(status, 202)
        job = self.center.jobs.wait(body["job"]["id"], 2)
        self.assertEqual(job.state, JobState.SUCCEEDED)
        status, fetched = self.request("GET", f"/api/jobs/{job.id}")
        self.assertEqual((status, fetched["lines"]), (200, ["going"]))

    def test_second_post_while_running_conflicts(self):
        Okay.release = threading.Event()
        first, _ = self.request("POST", "/api/components/okay/actions/go")
        second, body = self.request("POST", "/api/components/okay/actions/go")
        self.assertEqual((first, second), (202, 409))
        self.assertIn("job_id", body)

    def test_unknown_component_404_and_unoffered_action_400(self):
        self.assertEqual(self.request("POST", "/api/components/nope/actions/go")[0], 404)
        self.assertEqual(self.request("POST", "/api/components/okay/actions/install")[0], 400)

    def raw(self, request_bytes):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(request_bytes)
            return sock.recv(65536).decode("latin-1").split("\r\n", 1)[0]
        finally:
            sock.close()

    def test_bad_content_length_is_400(self):
        line = self.raw(
            f"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-GazeVoice: 1\r\n"
            "Content-Length: abc\r\n\r\n".encode()
        )
        self.assertIn(" 400 ", line)

    def test_oversized_body_is_413(self):
        line = self.raw(
            f"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-GazeVoice: 1\r\n"
            "Content-Length: 999999\r\n\r\n{}".encode()
        )
        self.assertIn(" 413 ", line)

    def test_static_path_traversal_is_404(self):
        self.assertEqual(self.request("GET", "/static/../settings.json")[0], 404)
        self.assertEqual(self.request("GET", "/static/%2e%2e/settings.json")[0], 404)

    def test_index_is_served(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower() if isinstance(body, bytes) else b"")

    def test_settings_round_trip_and_validation(self):
        status, body = self.request("PUT", "/api/settings", body={"editor_source": "codex"})
        self.assertEqual((status, body["editor_source"]), (200, "codex"))
        self.assertEqual(self.request("GET", "/api/settings")[1]["editor_source"], "codex")
        self.assertEqual(self.request("PUT", "/api/settings", body={"editor_source": "gpt"})[0], 400)
        self.assertEqual(self.request("PUT", "/api/settings", body={"editor_source": "auto"}, write=False)[0], 403)

    def test_events_stream_delivers_job_started(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        self.request("POST", "/api/components/okay/actions/go")
        seen = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = resp.fp.readline().decode().strip()
            seen.append(line)
            if line == "event: job.started":
                break
        resp.close()
        conn.close()
        self.assertIn("event: job.started", seen)


if __name__ == "__main__":
    unittest.main()
