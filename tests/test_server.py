import dataclasses
import http.client
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gazevoice.components.base import ActionSpec, Component, Health
from gazevoice.components.mlagents import MLAgents
from gazevoice.jobs import JobState
from gazevoice.mlagents import TrainingError
from gazevoice.probes import ProbeTimedOut
from gazevoice.server import app as server_app
from gazevoice.server.app import STOP_WAIT_SECONDS, ControlCenter, make_server
from gazevoice.voice.leftovers import make_noise_picture
from gazevoice.voice.picture import SentPictures

from .helpers import TempCtx, fake_unity_project
from .test_mlagents import FIXTURE, process_gone, wait_for


class Okay(Component):
    id = "okay"
    name = "Okay"
    role = "fake"
    release = None  # threading.Event set by tests to hold a job open
    params = None  # what the last job was sent

    def check(self):
        return self.state(Health.READY, "fine", actions=[ActionSpec("go", "Go")])

    def action_go(self, job):
        job.log("going")
        Okay.params = job.params
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

    def test_the_calibration_page_is_served_with_the_dashboards_protections(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/calibrate")
        response = conn.getresponse()
        body = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))  # no other page may frame it
        self.assertIn(b"Calibrate gaze", body)

    def test_the_calibration_page_has_the_head_guide_and_nothing_the_dashboards_protections_would_block(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/calibrate")
        body = conn.getresponse().read().decode()
        conn.close()
        self.assertIn("Start anyway", body)
        self.assertIn("<svg", body)  # the outline is drawn inline, so the policy needs no image source
        self.assertNotRegex(body, r"\sstyle\s*=", "the policy has no inline styles")
        self.assertNotIn("<script>", body)
        self.assertIn("No picture is saved", body)  # the ready card still says so
        self.assertIn("stay on this Mac", body)  # and where the numbers go
        for word in ("outline", "dots", "click the circle"):
            self.assertIn(word, body)

    def test_the_calibration_pages_own_files_are_served(self):
        for name in ("calibrate.js", "calibrate.css"):
            self.assertEqual(self.request("GET", f"/static/{name}", write=False)[0], 200, name)

    def test_what_a_page_sends_with_an_action_reaches_the_job(self):
        status, body = self.request("POST", "/api/components/okay/actions/go", {"area": {"left": 1}})
        self.assertEqual(status, 202)
        self.center.jobs.wait(body["job"]["id"], 5)
        self.assertEqual(Okay.params, {"area": {"left": 1}})

    def test_an_action_sent_nothing_gets_nothing(self):
        status, body = self.request("POST", "/api/components/okay/actions/go")
        self.assertEqual(status, 202)
        self.center.jobs.wait(body["job"]["id"], 5)
        self.assertEqual(Okay.params, {})

    def page_that_records(self):
        said = []
        page = SimpleNamespace(shown=lambda job, number: said.append(("shown", job, number)),
                               left=lambda job: said.append(("left", job)),
                               alive=lambda job: said.append(("alive", job)),
                               skip=lambda job: said.append(("skip", job)))
        self.center.components["gaze"] = SimpleNamespace(page=page)
        return said

    def test_the_calibration_page_reports_to_the_gaze_unit(self):
        said = self.page_that_records()
        self.assertEqual(self.request("POST", "/api/gaze/page", {"job": "j1", "number": 3})[0], 202)
        self.assertEqual(self.request("POST", "/api/gaze/page", {"job": "j1", "left": True})[0], 202)
        self.assertEqual(said, [("shown", "j1", 3), ("left", "j1")])

    def test_the_head_guide_reports_to_the_gaze_unit_too(self):
        said = self.page_that_records()
        self.assertEqual(self.request("POST", "/api/gaze/page", {"job": "j1", "alive": True})[0], 202)
        self.assertEqual(self.request("POST", "/api/gaze/page", {"job": "j1", "skip": True})[0], 202)
        self.assertEqual(said, [("alive", "j1"), ("skip", "j1")])

    def test_reporting_from_the_page_is_guarded_like_any_other_write(self):
        said = self.page_that_records()
        for body in ({"job": "j1", "left": True}, {"job": "j1", "alive": True}, {"job": "j1", "skip": True}):
            self.assertEqual(self.request("POST", "/api/gaze/page", body, write=False)[0], 403)
        self.assertEqual(said, [])

    def test_with_no_gaze_unit_there_is_no_page_to_report_to(self):
        self.assertEqual(self.request("POST", "/api/gaze/page", {"job": "j1", "number": 1})[0], 404)

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


class SentPictureRouteTest(unittest.TestCase):
    """GET /api/voice/pictures/<command id>/<n>: a picture that was sent, from the voice service's memory."""

    ID = "20261108-224739-5"

    def setUp(self):
        self.t = TempCtx()
        self.center = ControlCenter(self.t.ctx, [Okay(self.t.ctx)], check_timeout=0.3, sse_ping=0.2)
        self.shelf = SentPictures()
        self.asked = []
        shelf, asked = self.shelf, self.asked

        def picture(command_id, number):
            asked.append((command_id, number))
            return shelf.get(command_id, number)

        self.center.voice = SimpleNamespace(picture=picture)
        self.png = make_noise_picture(16).png
        self.shelf.keep(self.ID, 0, self.png)
        self.server = make_server(self.center, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.t.cleanup()

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body

    def test_a_kept_picture_comes_back_as_the_png_it_was(self):
        response, body = self.get(f"/api/voice/pictures/{self.ID}/0")
        self.assertEqual((response.status, body), (200, self.png))
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertEqual(response.getheader("Content-Length"), str(len(self.png)))
        self.assertEqual(self.asked, [(self.ID, 0)])

    def test_the_browser_is_told_to_keep_it_nowhere(self):
        response, _ = self.get(f"/api/voice/pictures/{self.ID}/0")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")  # not in the browser's disk cache either
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin")  # no other site may embed it
        self.assertIsNone(response.getheader("ETag"))
        self.assertIsNone(response.getheader("Last-Modified"))

    def test_a_picture_that_is_gone_is_a_plain_404(self):
        for path in (f"/api/voice/pictures/{self.ID}/1", "/api/voice/pictures/20261108-224739-6/0"):
            response, body = self.get(path)
            self.assertEqual(response.status, 404, path)
            self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertNotIn(b"PNG", body)

    def test_a_missing_picture_answers_with_the_same_embedding_rule_so_no_site_can_tell_if_it_exists(self):
        self.center.voice = None
        for path in (f"/api/voice/pictures/{self.ID}/1", "/api/voice/pictures/20261108-224739-6/0",
                     f"/api/voice/pictures/{self.ID}/0"):  # gone, never kept, and (with no voice) not kept any more
            response, _ = self.get(path)
            self.assertEqual(response.status, 404, path)
            self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin", path)
        self.center.voice = SimpleNamespace(picture=lambda command_id, number: self.shelf.get(command_id, number))
        found, _ = self.get(f"/api/voice/pictures/{self.ID}/0")
        gone, _ = self.get(f"/api/voice/pictures/{self.ID}/1")
        self.assertEqual((found.status, gone.status), (200, 404))
        for header in ("Cross-Origin-Resource-Policy", "Cache-Control", "X-Content-Type-Options", "Referrer-Policy"):
            self.assertEqual(found.getheader(header), gone.getheader(header), header)  # the two differ only in the body

    def test_when_the_picture_is_dropped_the_same_address_stops_working(self):
        self.assertEqual(self.get(f"/api/voice/pictures/{self.ID}/0")[0].status, 200)
        self.shelf.drop(self.ID)
        self.assertEqual(self.get(f"/api/voice/pictures/{self.ID}/0")[0].status, 404)

    def test_with_voice_never_built_there_are_no_pictures(self):
        self.center.voice = None
        self.assertEqual(self.get(f"/api/voice/pictures/{self.ID}/0")[0].status, 404)

    def test_only_a_command_id_and_a_plain_number_get_as_far_as_the_store(self):
        base = "/api/voice/pictures"
        bad = [
            f"{base}/../0", f"{base}/{self.ID}/../0", f"{base}/{self.ID}/-1", f"{base}/{self.ID}/01",
            f"{base}/{self.ID}/0/", f"{base}/{self.ID}/0/x", f"{base}/{self.ID}/1000", f"{base}/{self.ID}/", f"{base}/{self.ID}",
            f"{base}/{self.ID}/%30", f"{base}/{self.ID}/0%00", f"{base}/{self.ID}/%D9%A0",  # nothing is decoded for it
            f"{base}/", f"{base}//0", f"{base}/%2e%2e%2f%2e%2e/0", f"{base}/20261108-224739/0", f"{base}/2026110-224739-5/0",
            f"{base}/20261108-224739-5-6/0", f"{base}/20261108_224739_5/0", f"{base}/20261108-224739-5%0a/0",
            f"{base}/%D9%A2%D9%A0%D9%A2%D9%A6%D9%A1%D9%A1%D9%A0%D9%A8-224739-5/0",  # Arabic-Indic digits
            f"{base}/" + "9" * 5000 + "/0", f"{base}/{self.ID}/" + "9" * 5000, f"{base}/{self.ID.replace('-', '_')}/0",
            f"/api/voice/picture/{self.ID}/0", f"{base}{self.ID}/0", f"/api/voice/pictures/{self.ID}/0x",
        ]
        for path in bad:
            with self.subTest(path=path[:80]):
                self.assertEqual(self.get(path)[0].status, 404)
        self.assertEqual(self.asked, [])

    def test_the_address_also_works_with_a_query_but_only_for_that_one_picture(self):
        self.assertEqual(self.get(f"/api/voice/pictures/{self.ID}/0?t=1")[0].status, 200)
        self.assertEqual(self.asked, [(self.ID, 0)])

    def test_only_this_computers_own_page_may_ask(self):
        response, _ = self.get(f"/api/voice/pictures/{self.ID}/0", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.asked, [])

    def test_a_picture_cannot_be_posted_or_put_through_the_same_address(self):
        for method in ("POST", "PUT", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(method, f"/api/voice/pictures/{self.ID}/0", body=b"{}", headers={"X-GazeVoice": "1"})
            status = conn.getresponse().status
            conn.close()
            self.assertIn(status, (404, 501), method)
        self.assertEqual(self.shelf.get(self.ID, 0), self.png)

    def test_the_page_may_show_pictures_from_here_and_nowhere_else(self):
        response, _ = self.get("/")
        policy = response.getheader("Content-Security-Policy")
        directives = {part.split()[0]: part.split()[1:] for part in policy.split(";") if part.strip()}
        self.assertIn("'self'", directives["img-src"])  # so <img src="/api/voice/pictures/…"> loads
        self.assertNotIn("*", directives["img-src"])
        self.assertFalse([source for source in directives["img-src"] if source.startswith(("http", "blob", "//"))])
        self.assertEqual(directives["default-src"], ["'self'"])
        self.assertEqual(directives["script-src"], ["'self'"])  # and nothing else about the page has loosened


class FakeTraining:
    """The training service as the control center sees it: something to subscribe to, ask and shut down."""

    def __init__(self):
        self.callbacks = []
        self.message = "idle"
        self.shutdowns = 0

    def subscribe(self, callback):
        self.callbacks.append(callback)

    def status(self):
        return {"state": "idle", "message": self.message}

    def shutdown(self):
        self.shutdowns += 1

    def change(self, message):
        self.message = message
        for callback in list(self.callbacks):
            callback()


class Trainer(Component):
    """A stand-in for the ML-Agents unit: its summary is whatever the training service says."""

    id = "mlagents"
    name = "ML-Agents"
    role = "fake"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.checks = 0
        self.started = threading.Event()
        self.gate = None  # when set, the first check waits for it, after it has read the status

    def check(self):
        self.checks += 1
        message = self.ctx.training.status()["message"]
        if self.checks == 1 and self.gate is not None:
            self.started.set()
            self.gate.wait(5)
        return self.state(Health.READY, message)


class TrainingEventsTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.training = self.t.ctx.training = FakeTraining()

    def tearDown(self):
        self.t.cleanup()

    def center(self, *components):
        center = ControlCenter(self.t.ctx, list(components), check_timeout=0.3, sse_ping=0.2)
        self.addCleanup(center.shutdown)
        return center

    def updates(self, sub, until, timeout=5.0):
        """The summaries of the ML-Agents `component.updated` events, up to and including the one that says `until`."""
        seen = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = sub.get(timeout=0.2)
            if event and event.kind == "component.updated" and event.data["id"] == "mlagents":
                seen.append(event.data["summary"])
                if event.data["summary"] == until:
                    break
        return seen

    def test_the_control_center_listens_for_training_changes_once_when_there_is_an_ml_agents_unit(self):
        self.center(Trainer(self.t.ctx), Okay(self.t.ctx))
        self.assertEqual(len(self.training.callbacks), 1)

    def test_it_does_not_listen_when_there_is_no_ml_agents_unit(self):
        self.center(Okay(self.t.ctx))
        self.assertEqual(self.training.callbacks, [])

    def test_a_training_change_publishes_the_unit_again(self):
        center = self.center(Trainer(self.t.ctx), Okay(self.t.ctx))
        sub = center.bus.subscribe()
        self.training.change("Waiting for Unity: press Play in the Editor to start training.")
        self.assertEqual(self.updates(sub, "Waiting for Unity: press Play in the Editor to start training."),
                         ["Waiting for Unity: press Play in the Editor to start training."])
        self.assertEqual(center._last["mlagents"].summary, "Waiting for Unity: press Play in the Editor to start training.")
        self.training.change("Training run1: step 10000, mean reward 0.500.")
        self.assertEqual(self.updates(sub, "Training run1: step 10000, mean reward 0.500."),
                         ["Training run1: step 10000, mean reward 0.500."])

    def test_a_change_during_a_check_is_not_lost_and_not_published_out_of_order(self):
        trainer = Trainer(self.t.ctx)
        trainer.gate = threading.Event()
        center = self.center(trainer)
        sub = center.bus.subscribe()
        self.training.change("one")
        self.assertTrue(trainer.started.wait(5), "the first check should have started")
        self.training.change("two")
        self.training.change("three")
        trainer.gate.set()
        self.assertEqual(self.updates(sub, "three"), ["one", "three"])  # the newest last, and one more check, not two
        self.assertIsNone(sub.get(timeout=0.3))
        self.assertEqual(trainer.checks, 2)
        self.assertEqual(center._last["mlagents"].summary, "three")

    def test_a_failure_while_showing_one_change_does_not_stop_the_next_from_being_shown(self):
        center = self.center(Trainer(self.t.ctx))
        sub = center.bus.subscribe()
        publish = center.bus.publish
        failures = []

        def publish_once_badly(kind, data):
            if not failures:
                failures.append(kind)
                raise RuntimeError("the page's connection broke")
            return publish(kind, data)

        with mock.patch.object(center.bus, "publish", publish_once_badly), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as shown:
            self.training.change("one")
            wait_for(self, lambda: failures, what="the first update to fail")
            wait_for(self, lambda: not center._training_checking, what="the failed update to be given up")
            self.training.change("two")
            self.assertEqual(self.updates(sub, "two"), ["two"])
        self.assertIn("the page's connection broke", shown.getvalue())  # it is reported, not swallowed

    def test_a_check_that_raises_once_does_not_stop_later_updates(self):
        trainer = Trainer(self.t.ctx)
        real_check = trainer.check
        calls = []

        def check_badly():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("kaboom")
            return real_check()

        trainer.check = check_badly
        center = self.center(trainer)
        sub = center.bus.subscribe()
        self.training.change("one")
        self.assertEqual(self.updates(sub, "Check failed: kaboom"), ["Check failed: kaboom"])
        self.training.change("two")
        self.assertEqual(self.updates(sub, "two"), ["two"])

    def test_a_late_change_after_the_control_center_closed_is_ignored(self):
        center = self.center(Trainer(self.t.ctx))
        center.shutdown()
        self.training.change("late")  # raises if it tries to use the closed pool

    def test_shutdown_shuts_the_training_service_down(self):
        center = self.center(Okay(self.t.ctx))
        center.shutdown()
        self.assertEqual(self.training.shutdowns, 1)

    def test_shutdown_still_shuts_training_down_when_voice_cant_stop(self):
        center = self.center(Okay(self.t.ctx))

        def broken():
            raise RuntimeError("voice is stuck")

        center.voice = SimpleNamespace(stop=broken)
        with self.assertRaises(RuntimeError):
            center.shutdown()
        center.voice = None  # so the clean-up can close it again
        self.assertEqual(self.training.shutdowns, 1)


class StubTraining(FakeTraining):
    """Training that records what the routes ask of it, and answers with what a test sets."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.state = "idle"
        self.error = None  # a TrainingError to raise from the next start, stop or import
        self.stop_gate = None  # when set, stop() takes until the test sets it

    def status(self):
        return {"state": self.state, "run_id": "run1" if self.state != "idle" else None,
                "project": "/p/Game" if self.state != "idle" else None, "behaviors": {},
                "message": self.message, "log": None, "log_tail": [], "models": [], "exit_code": None}

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    def start(self, mlagents_learn, project, run_id, config_yaml=None, resume=False, overwrite=False):
        self.calls.append(("start", mlagents_learn, project, run_id, config_yaml, resume, overwrite))
        self._maybe_fail()
        self.state = "waiting"
        return self.status()

    def stop(self):
        self.calls.append(("stop",))
        self._maybe_fail()
        if self.stop_gate is not None:  # the model is still being saved until a test lets go
            self.state = "stopping"
            self.stop_gate.wait(10)
        self.state = "stopped"
        return self.status()

    def import_model(self, project, run_id, behavior=None):
        self.calls.append(("import_model", project, run_id, behavior))
        self._maybe_fail()
        return {"asset_path": f"Assets/GazeVoice/MLAgents/{run_id}/Mover.onnx", "behavior": behavior or "Mover"}


class Installable(Component):
    """A stand-in for the ML-Agents unit: whether it is installed is whatever a test says."""

    id = "mlagents"
    name = "ML-Agents"
    role = "fake"

    def __init__(self, ctx, installed=True):
        super().__init__(ctx)
        self.is_installed = installed
        self.mlagents_learn = Path("/fake/venvs/mlagents/bin/mlagents-learn")
        self.installs = 0

    def installed(self):
        if isinstance(self.is_installed, Exception):
            raise self.is_installed
        return self.is_installed

    def check(self):
        return self.state(Health.READY if self.is_installed is True else Health.MISSING, "fake",
                          actions=[ActionSpec("install", "Install")])

    def action_install(self, job):
        self.installs += 1


class MLAgentsRoutesTest(unittest.TestCase):
    """What the Unity AI's training tools reach: the dashboard's own routes, guarded like every other write."""

    NOT_INSTALLED = "ML-Agents' Python side isn't installed. Press Install on the ML-Agents unit in the dashboard."
    NO_PROJECT = "Pick your Unity project in the dashboard first."

    def setUp(self):
        self.t = TempCtx()
        self.addCleanup(self.t.cleanup)
        self.training = self.t.ctx.training = StubTraining()
        self.unit = Installable(self.t.ctx)
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="/p/Game")
        self.center = ControlCenter(self.t.ctx, [self.unit], check_timeout=0.3, sse_ping=0.2)
        self.server = make_server(self.center, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    request = ServerTest.request

    def test_make_server_tells_the_control_center_where_it_is(self):
        self.assertEqual(self.center.url, f"http://127.0.0.1:{self.port}")

    def test_before_the_server_is_made_there_is_no_address(self):
        other = ControlCenter(self.t.ctx, [self.unit])
        self.addCleanup(other.shutdown)
        self.assertIsNone(other.url)

    def test_the_voice_factory_passes_the_address_to_build_voice(self):
        with mock.patch("gazevoice.server.app.build_voice") as build:
            self.center._voice_factory({"refiner": "claude", "editor": "claude"})
        self.assertEqual(build.call_args.kwargs["control_url"], f"http://127.0.0.1:{self.port}")

    def test_status_says_whether_it_is_installed_and_which_project(self):
        status, body = self.request("GET", "/api/mlagents", write=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["installed"])
        self.assertEqual(body["project"], "/p/Game")
        self.assertEqual(body["state"], "idle")
        self.assertEqual(body["message"], "idle")

    def test_status_names_the_runs_project_while_a_run_exists(self):
        self.training.state = "training"
        self.assertEqual(self.request("GET", "/api/mlagents", write=False)[1]["project"], "/p/Game")
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="/p/Other")
        body = self.request("GET", "/api/mlagents", write=False)[1]
        self.assertEqual((body["state"], body["run_id"], body["project"]), ("training", "run1", "/p/Game"))

    def test_status_with_no_project_set(self):
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="")
        body = self.request("GET", "/api/mlagents", write=False)[1]
        self.assertIsNone(body["project"])
        self.assertEqual(body["runs"], [])

    def save_models(self, project, run_id, *names, at):
        folder = project / "MLAgents" / "results" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / f"{name}.onnx").write_bytes(b"model")
            os.utime(folder / f"{name}.onnx", (at, at))

    def test_status_lists_the_earlier_runs_that_have_a_model_newest_first(self):
        project = fake_unity_project(self.t.root / "Game")
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project=str(project))
        (project / "MLAgents" / "results" / "no-model").mkdir(parents=True)
        self.save_models(project, "Mover-1", "Mover", at=1_760_000_000.0)
        self.save_models(project, "Mover-2", "Jumper", "Mover", at=1_760_001_000.0)
        body = self.request("GET", "/api/mlagents", write=False)[1]
        self.assertEqual(body["state"], "idle")  # as after a restart: no run, but the runs are there
        self.assertIsNone(body["run_id"])
        self.assertEqual([(run["run_id"], run["behaviors"]) for run in body["runs"]],
                         [("Mover-2", ["Jumper", "Mover"]), ("Mover-1", ["Mover"])])
        self.assertTrue(all(isinstance(run["modified"], str) for run in body["runs"]))

    def test_status_lists_at_most_10_runs(self):
        project = fake_unity_project(self.t.root / "Game")
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project=str(project))
        for number in range(12):
            self.save_models(project, f"run{number:02d}", "A", at=1_760_000_000.0 + number)
        runs = self.request("GET", "/api/mlagents", write=False)[1]["runs"]
        self.assertEqual([run["run_id"] for run in runs], [f"run{n:02d}" for n in range(11, 1, -1)])

    def test_status_has_no_runs_when_the_project_has_none_or_does_not_exist(self):
        self.assertEqual(self.request("GET", "/api/mlagents", write=False)[1]["runs"], [])  # /p/Game isn't there
        project = fake_unity_project(self.t.root / "Game")
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project=str(project))
        self.assertEqual(self.request("GET", "/api/mlagents", write=False)[1]["runs"], [])
        self.assertFalse((project / "MLAgents").exists())  # asking creates nothing

    def test_status_says_when_it_is_not_installed(self):
        self.unit.is_installed = False
        status, body = self.request("GET", "/api/mlagents", write=False)
        self.assertEqual((status, body["ok"], body["installed"]), (200, True, False))

    def test_status_that_cannot_tell_yet_says_to_try_again(self):
        self.unit.is_installed = ProbeTimedOut(("python", "-c", "import mlagents"))
        status, body = self.request("GET", "/api/mlagents", write=False)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "Still checking whether ML-Agents is installed. Try again in a moment.")

    def test_status_with_a_foreign_host_is_refused(self):
        self.assertEqual(self.request("GET", "/api/mlagents", headers={"Host": "evil.example"}, write=False)[0], 403)

    def test_train_starts_a_run_in_the_chosen_project_and_passes_its_arguments_through(self):
        status, body = self.request("POST", "/api/mlagents/train", {
            "run_id": "Mover-1", "config_yaml": "behaviors: {}", "resume": False, "overwrite": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "waiting")
        self.assertEqual(self.training.calls, [
            ("start", self.unit.mlagents_learn, Path("/p/Game"), "Mover-1", "behaviors: {}", False, True)])

    def test_train_needs_only_a_run_id(self):
        self.assertEqual(self.request("POST", "/api/mlagents/train", {"run_id": "r1"})[0], 200)
        self.assertEqual(self.training.calls, [("start", self.unit.mlagents_learn, Path("/p/Game"), "r1", None, False, False)])

    def test_train_never_passes_a_wait_time_of_its_own(self):
        self.request("POST", "/api/mlagents/train", {"run_id": "r1", "timeout_wait": 9999})
        self.assertEqual(len(self.training.calls[0]), 7)

    def test_train_when_not_installed_says_to_press_install_and_starts_nothing(self):
        self.unit.is_installed = False
        status, body = self.request("POST", "/api/mlagents/train", {"run_id": "r1"})
        self.assertEqual((status, body["error"]), (409, self.NOT_INSTALLED))
        self.assertEqual(self.training.calls, [])
        self.assertEqual(self.unit.installs, 0)
        self.assertEqual(self.center.jobs.recent(10), [])
        self.assertEqual(self.center.jobs.running(), {})

    def test_train_without_a_unit_is_the_same_as_not_installed(self):
        self.center.components.pop("mlagents")
        status, body = self.request("POST", "/api/mlagents/train", {"run_id": "r1"})
        self.assertEqual((status, body["error"]), (409, self.NOT_INSTALLED))
        self.assertEqual(self.training.calls, [])

    def test_train_that_cannot_tell_if_it_is_installed_starts_nothing(self):
        self.unit.is_installed = ProbeTimedOut(("python", "-c", "import mlagents"))
        status, body = self.request("POST", "/api/mlagents/train", {"run_id": "r1"})
        self.assertEqual((status, body["error"]),
                         (409, "Still checking whether ML-Agents is installed. Try again in a moment."))
        self.assertEqual(self.training.calls, [])
        self.assertEqual(self.unit.installs, 0)

    def test_train_with_no_project_says_to_pick_one(self):
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="")
        status, body = self.request("POST", "/api/mlagents/train", {"run_id": "r1"})
        self.assertEqual((status, body["error"]), (409, self.NO_PROJECT))
        self.assertEqual(self.training.calls, [])

    def test_train_asks_if_it_is_installed_before_it_asks_for_a_project(self):
        self.unit.is_installed = False
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="")
        self.assertEqual(self.request("POST", "/api/mlagents/train", {"run_id": "r1"})[1]["error"], self.NOT_INSTALLED)

    def test_a_refusal_from_training_is_a_409_with_its_own_sentence(self):
        self.training.error = TrainingError("A training run is already going. Stop it first.")
        status, body = self.request("POST", "/api/mlagents/train", {"run_id": "r1"})
        self.assertEqual((status, body["error"]), (409, "A training run is already going. Stop it first."))

    def test_train_needs_a_run_id_and_sensible_arguments(self):
        for bad in ({}, {"run_id": 7}, {"run_id": ""}, {"run_id": "r1", "config_yaml": 5},
                    {"run_id": "r1", "resume": "yes"}, {"run_id": "r1", "overwrite": 1}):
            with self.subTest(bad):
                self.assertEqual(self.request("POST", "/api/mlagents/train", bad)[0], 400)
        self.assertEqual(self.training.calls, [])

    def test_stop_stops_and_answers_with_the_status(self):
        self.training.state = "training"
        status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertEqual((status, body["state"]), (200, "stopped"))
        self.assertEqual(self.training.calls, [("stop",)])

    def test_stop_that_is_still_saving_answers_stopping_and_carries_on(self):
        self.training.state = "training"
        self.training.stop_gate = threading.Event()
        self.addCleanup(self.training.stop_gate.set)
        began = time.monotonic()
        with mock.patch("gazevoice.server.app.STOP_WAIT_SECONDS", 0.3):
            status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertLess(time.monotonic() - began, 3.0)
        self.assertEqual((status, body["state"]), (200, "stopping"))
        self.assertEqual(self.training.calls, [("stop",)])
        self.assertEqual(self.training.state, "stopping")  # the stop is still going
        self.training.stop_gate.set()
        wait_for(self, lambda: self.training.state == "stopped", what="the stop to finish after it was answered")
        self.assertEqual(self.request("GET", "/api/mlagents", write=False)[1]["state"], "stopped")

    def test_stop_that_finishes_in_time_answers_with_the_final_status(self):
        self.training.state = "training"
        self.training.stop_gate = threading.Event()
        self.training.stop_gate.set()
        status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertEqual((status, body["state"]), (200, "stopped"))

    def test_a_stop_that_training_refuses_is_a_409(self):
        self.training.error = TrainingError("Couldn't stop it.")
        status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertEqual((status, body["error"]), (409, "Couldn't stop it."))

    def just_too_late(self):
        """The stop finishes a moment after the wait gave up: wait() says it timed out, and then the stop is over."""
        class LateEvent(threading.Event):
            def wait(self, timeout=None):
                super().wait(5)
                return False

        class Late:
            Event = LateEvent

            def __getattr__(self, name):
                return getattr(threading, name)

        return mock.patch.object(server_app, "threading", Late())

    def test_a_stop_that_is_refused_just_after_the_wait_ran_out_is_still_a_409_with_its_sentence(self):
        self.training.error = TrainingError("Couldn't stop it.")
        with self.just_too_late():
            status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertEqual((status, body), (409, {"error": "Couldn't stop it."}))

    def test_a_stop_that_finishes_just_after_the_wait_ran_out_answers_with_its_final_status(self):
        self.training.state = "training"
        with self.just_too_late():
            status, body = self.request("POST", "/api/mlagents/stop", {})
        self.assertEqual((status, body["state"]), (200, "stopped"))

    def test_the_dashboard_answers_stop_before_codexs_60_second_tool_limit_and_before_the_tool_gives_up(self):
        from gazevoice.unity_mcp import DASHBOARD_SECONDS

        self.assertEqual(STOP_WAIT_SECONDS, 40.0)
        self.assertGreater(DASHBOARD_SECONDS["mlagents_stop"], STOP_WAIT_SECONDS)
        self.assertLess(DASHBOARD_SECONDS["mlagents_stop"], 60.0)

    def test_stop_with_no_body_works(self):
        self.assertEqual(self.request("POST", "/api/mlagents/stop")[0], 200)

    def test_import_copies_the_model_in(self):
        status, body = self.request("POST", "/api/mlagents/import", {"run_id": "Mover-1", "behavior": "Mover"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"asset_path": "Assets/GazeVoice/MLAgents/Mover-1/Mover.onnx", "behavior": "Mover"})
        self.assertEqual(self.training.calls, [("import_model", Path("/p/Game"), "Mover-1", "Mover")])

    def test_import_without_a_behavior_lets_training_pick(self):
        self.request("POST", "/api/mlagents/import", {"run_id": "Mover-1"})
        self.assertEqual(self.training.calls, [("import_model", Path("/p/Game"), "Mover-1", None)])

    def test_import_that_cannot_is_a_409_with_its_sentence(self):
        self.training.error = TrainingError("Run Mover-1 hasn't saved a model yet.")
        status, body = self.request("POST", "/api/mlagents/import", {"run_id": "Mover-1"})
        self.assertEqual((status, body["error"]), (409, "Run Mover-1 hasn't saved a model yet."))

    def test_import_with_no_project_says_to_pick_one(self):
        self.t.ctx.settings = dataclasses.replace(self.t.ctx.settings, unity_project="")
        status, body = self.request("POST", "/api/mlagents/import", {"run_id": "r1"})
        self.assertEqual((status, body["error"]), (409, self.NO_PROJECT))

    def test_import_needs_a_run_id(self):
        for bad in ({}, {"run_id": 3}, {"run_id": "r1", "behavior": 4}):
            with self.subTest(bad):
                self.assertEqual(self.request("POST", "/api/mlagents/import", bad)[0], 400)

    def test_every_post_without_the_header_is_refused(self):
        for path in ("train", "stop", "import"):
            with self.subTest(path):
                status, _ = self.request("POST", f"/api/mlagents/{path}", {"run_id": "r1"}, write=False)
                self.assertEqual(status, 403)
        self.assertEqual(self.training.calls, [])

    def test_a_post_from_another_site_is_refused(self):
        status, _ = self.request("POST", "/api/mlagents/stop", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.training.calls, [])

    def test_a_post_with_a_foreign_host_is_refused(self):
        status, _ = self.request("POST", "/api/mlagents/stop", {}, headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.training.calls, [])

    def test_an_unknown_ml_agents_route_is_not_found(self):
        self.assertEqual(self.request("POST", "/api/mlagents/install", {})[0], 404)
        self.assertEqual(self.request("GET", "/api/mlagents/train", write=False)[0], 404)

    def test_a_body_that_is_not_an_object_is_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/mlagents/train", body=b"[1]", headers={"X-GazeVoice": "1"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 400)

    def test_the_unity_ai_tools_work_against_the_real_routes(self):
        from gazevoice.unity_mcp import call_dashboard

        with mock.patch.dict(os.environ, {"GAZEVOICE_URL": self.center.url}):
            status = call_dashboard("mlagents_status", {})
            trained = call_dashboard("mlagents_train", {"run_id": "r1", "config_yaml": "behaviors: {}"})
            imported = call_dashboard("mlagents_import_model", {"run_id": "r1", "behavior": "Mover"})
            stopped = call_dashboard("mlagents_stop", {})
            self.unit.is_installed = False
            refused = call_dashboard("mlagents_train", {"run_id": "r2"})
        self.assertEqual((status["ok"], status["installed"], status["state"]), (True, True, "idle"))
        self.assertEqual((trained["ok"], trained["state"], trained["run_id"]), (True, "waiting", "run1"))
        self.assertEqual(imported, {"ok": True, "asset_path": "Assets/GazeVoice/MLAgents/r1/Mover.onnx", "behavior": "Mover"})
        self.assertEqual((stopped["ok"], stopped["state"]), (True, "stopped"))
        self.assertEqual(refused, {"ok": False, "error": self.NOT_INSTALLED})

    def test_no_route_ever_installs_anything(self):
        self.unit.is_installed = False
        for path, body in (("train", {"run_id": "r1"}), ("stop", {}), ("import", {"run_id": "r1"})):
            self.request("POST", f"/api/mlagents/{path}", body)
        self.request("GET", "/api/mlagents", write=False)
        self.assertEqual(self.unit.installs, 0)
        self.assertEqual(self.center.jobs.recent(10), [])


class TrainingShutdownTest(unittest.TestCase):
    """Closing the control center in the middle of a real run: the model is saved and nothing is left on port 5004."""

    @classmethod
    def setUpClass(cls):
        cls._wrapper_dir = tempfile.TemporaryDirectory()
        cls.wrapper = Path(cls._wrapper_dir.name) / "mlagents-learn"
        cls.wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FIXTURE))} \"$@\"\n")
        cls.wrapper.chmod(0o755)
        subprocess.run([str(cls.wrapper)], env={"FAKE_ML_SCRIPT": "crash"}, capture_output=True, check=False)  # vetting

    @classmethod
    def tearDownClass(cls):
        cls._wrapper_dir.cleanup()

    def setUp(self):
        self.t = TempCtx(which={"uv": "/Users/me/.local/bin/uv"})
        self.addCleanup(self.t.cleanup)
        self.unit = MLAgents(self.t.ctx)
        self.unit.python.parent.mkdir(parents=True)
        self.unit.python.write_text("")
        self.unit.mlagents_learn.symlink_to(self.wrapper)
        self.t.runner.add([str(self.unit.python), "-c", "import mlagents.trainers"])
        self.project = fake_unity_project(self.t.root / "Game")
        self.fake = self.t.root / "fake"
        self.fake.mkdir()
        self.center = ControlCenter(self.t.ctx, [self.unit], check_timeout=2.0, sse_ping=0.2)
        self.addCleanup(self.center.shutdown)

    def test_closing_the_control_center_mid_training_saves_the_model_and_leaves_no_helper(self):
        sub = self.center.bus.subscribe()
        env = {"FAKE_ML_SCRIPT": "wait_interrupt", "FAKE_ML_DIR": str(self.fake), "FAKE_ML_HELPER": "sigint"}
        with mock.patch.dict(os.environ, env):
            self.t.ctx.training.start(self.unit.mlagents_learn, self.project, "run1")
        waiting = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and waiting is None:
            event = sub.get(timeout=0.2)
            if event and event.kind == "component.updated" and event.data["id"] == "mlagents" \
                    and event.data["summary"].startswith("Waiting for Unity") \
                    and any(a["id"] == "stop" for a in event.data["actions"]):
                waiting = event
        self.assertIsNotNone(waiting, "the unit should say it is waiting, and offer Stop")
        wait_for(self, (self.fake / "ready").exists, what="the fake mlagents-learn to listen")
        main, helper = int((self.fake / "main.pid").read_text()), int((self.fake / "helper.pid").read_text())

        self.center.shutdown()

        status = self.t.ctx.training.status()
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["models"], ["Mover"])  # SIGINT was heard, so the model was saved
        self.assertEqual(status["message"], "Training run1 stopped. Its model was saved and is ready to use.")
        self.assertTrue(process_gone(main))
        self.assertTrue(process_gone(helper))
        self.assertTrue((self.fake / "helper.sigint").exists(), "the helper should have been sent SIGINT too")


if __name__ == "__main__":
    unittest.main()
