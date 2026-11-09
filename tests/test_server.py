import http.client
import json
import socket
import threading
import time
import unittest
from types import SimpleNamespace

from gazevoice.components.base import ActionSpec, Component, Health
from gazevoice.jobs import JobState
from gazevoice.probes import ProbeTimedOut
from gazevoice.server.app import ControlCenter, make_server
from gazevoice.voice.leftovers import make_noise_picture
from gazevoice.voice.picture import SentPictures

from .helpers import TempCtx


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


if __name__ == "__main__":
    unittest.main()
