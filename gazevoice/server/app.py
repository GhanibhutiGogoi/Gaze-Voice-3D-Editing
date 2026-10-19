"""Local web server for the control center: JSON API, live events, static UI.

It binds 127.0.0.1 only. Any web page the user visits can still try to send
requests to localhost, so writes need the `X-GazeVoice: 1` header (a cross-site
page can't add one without a CORS preflight we never answer). Requests with a
foreign Host (DNS rebinding) or a foreign Origin are refused.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..components.base import Component, ComponentState, Context, Health
from ..components.brains import brain_pending, pick_brain
from ..events import EventBus
from ..jobs import Job, JobAlreadyRunning, JobError, JobManager
from ..probes import ProbeTimedOut
from ..settings import save_settings, update_settings
from ..voice.service import VoiceError
from ..voice.wiring import build_voice, voice_readiness
from ..workflow import WorkflowCheck, workflow_stages

WORKFLOW_ID = WorkflowCheck.id
PTT_STATES = ("down", "up", "cancel")


class VoiceNotReady(Exception):
    def __init__(self, missing: list[str]):
        super().__init__("Voice can't start yet")
        self.missing = missing

STATIC_DIR = Path(__file__).parent / "static"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
_STATIC_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_ACTION_PATH = re.compile(r"/api/components/([a-z0-9_]+)/actions/([a-z0-9_]+)")
_MAX_BODY = 64 * 1024
_CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"


class ControlCenter:
    """Everything the HTTP layer needs: components, jobs, settings, events."""

    def __init__(
        self,
        ctx: Context,
        components: Sequence[Component],
        jobs: JobManager | None = None,
        bus: EventBus | None = None,
        check_timeout: float = 8.0,
        sse_ping: float = 15.0,
        voice_factory=None,
    ):
        self.ctx = ctx
        self.components = {c.id: c for c in components}
        self.order = [c.id for c in components]
        self.bus = bus or EventBus()
        self.jobs = jobs or JobManager(self.bus)
        self.check_timeout = check_timeout
        self.sse_ping = sse_ping
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="check")
        self._last: dict[str, ComponentState] = {}
        self._start_lock = threading.Lock()
        self._voice_factory = voice_factory or (lambda brain: build_voice(self.ctx, self.bus, brain))
        self.voice = None
        self._voice_config: tuple | None = None

    def _safe_check(self, component: Component) -> ComponentState:
        try:
            return component.check()
        except ProbeTimedOut:
            return component.state(Health.CHECKING, "Still checking…")
        except Exception as exc:  # noqa: BLE001 - a broken check must not break the page
            return component.state(Health.ERROR, f"Check failed: {exc}")

    def check_all(self) -> dict[str, ComponentState]:
        """Check every component in parallel, waiting at most check_timeout in total.

        A check that overruns keeps its last known state on the page (or shows
        "Still checking…" the first time; a busy Mac is not a fault). When it
        finishes later, its result is stored and published as `component.updated`.
        """
        futures = {cid: self._pool.submit(self._safe_check, comp) for cid, comp in self.components.items()}
        deadline = time.monotonic() + self.check_timeout
        previous = dict(self._last)
        states: dict[str, ComponentState] = {}
        for cid, future in futures.items():
            try:
                states[cid] = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FuturesTimeout:
                states[cid] = previous.get(cid) or self.components[cid].state(Health.CHECKING, "Still checking…")
                future.add_done_callback(lambda f, cid=cid: self._late_result(cid, f))
        self._last = states
        return states

    def _late_result(self, cid: str, future) -> None:
        state = future.result()  # _safe_check never raises
        self._last = {**self._last, cid: state}
        self.bus.publish("component.updated", state.to_dict())

    def brain(self, states: dict[str, ComponentState]) -> str | None:
        return pick_brain(states, self.ctx.settings.brain)

    def snapshot(self, fresh: bool = False) -> dict:
        if fresh:
            self.ctx.probes.clear()
        states = self.check_all()
        brain = self.brain(states)
        return {
            "version": __version__,
            "components": [states[cid].to_dict() for cid in self.order],
            "brain": brain,
            "brain_pending": brain_pending(states, self.ctx.settings.brain),
            "workflow": workflow_stages(states, brain),
            "running": self.jobs.running(),
            "settings": self.ctx.settings.to_dict(),
            "voice": self.voice_snapshot(states, brain),
        }

    # --- voice ---------------------------------------------------------------------------
    def _voice_config_now(self, brain: str | None) -> tuple:
        s = self.ctx.settings
        return (brain, s.stt_engine, s.mic_device, s.editor_model, s.editor_effort, s.refiner_model, s.refiner_effort)

    def voice_snapshot(self, states: dict[str, ComponentState] | None = None, brain: str | None = None) -> dict:
        voice = self.voice
        snap = voice.snapshot() if voice is not None else {"state": "off", "reason": "", "recent": []}
        on = snap.get("state") not in ("off", "error")
        if states is not None:
            snap["missing"] = [] if on else voice_readiness(states, brain)
            snap["needs_restart"] = on and self._voice_config != self._voice_config_now(brain)
        return snap

    def voice_start(self) -> Job:
        states = self.check_all()
        brain = self.brain(states)
        missing = voice_readiness(states, brain)
        if missing:
            raise VoiceNotReady(missing)
        config = self._voice_config_now(brain)

        def work(job: Job) -> dict:
            with self._start_lock:
                if self.voice is not None and self.voice.state not in ("off", "error"):
                    raise JobError("Voice is already on.")
                if self.voice is None or self._voice_config != config:
                    if self.voice is not None:
                        self.voice.stop()
                    job.log("Getting the microphone, speech engine and brain ready…")
                    try:
                        self.voice = self._voice_factory(brain)
                    except VoiceError as exc:
                        raise JobError(str(exc)) from exc
                    self._voice_config = config
                voice = self.voice
            job.log("Starting voice: opening the mic, loading speech-to-text, warming up the brain…")
            try:
                voice.start()
            except VoiceError as exc:
                raise JobError(str(exc)) from exc
            job.log(f"Voice is on. In Blender, hold {voice.key}, speak, and let go.")
            return {"voice": "on"}

        with self._start_lock:
            # Like a unit's job, voice waits for "Check everything": both record from the mic and use the plan.
            running = self.jobs.running()
            if WORKFLOW_ID in running:
                raise JobAlreadyRunning(WORKFLOW_ID, running[WORKFLOW_ID])
            return self.jobs.start("voice", "start", work)

    def voice_stop(self) -> dict:
        if self.voice is not None:
            self.voice.stop()
        return self.voice_snapshot()

    def voice_call(self, method: str, *args):
        if self.voice is None:
            raise VoiceError("Turn voice on first.")
        return getattr(self.voice, method)(*args)

    def shutdown(self) -> None:
        """Stop everything this process started (ffmpeg, the speech worker, brain sessions)."""
        if self.voice is not None:
            self.voice.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def run_action(self, component_id: str, action: str) -> Job:
        component = self.components.get(component_id)
        if component is None:
            raise KeyError(component_id)
        state = self._last.get(component_id)
        if state is None or not state.offers(action):
            state = self._safe_check(component)  # the page may be showing an older state
        if not state.offers(action):
            raise ValueError(f"{component.name} can't do “{action}” right now. Refresh and try again.")

        def work(job: Job) -> dict | None:
            try:
                return component.run_action(action, job)
            finally:
                self.ctx.probes.clear()  # whatever the job changed, the next look is fresh

        with self._start_lock:
            # "Check everything" drives every unit itself, so it and unit jobs exclude each other:
            # no second recording on the same mic, no second request spending the plan.
            running = self.jobs.running()
            if component_id == WORKFLOW_ID:
                busy = next((cid for cid in running if cid != WORKFLOW_ID), None)
                if busy is not None:
                    raise JobAlreadyRunning(busy, running[busy])
            elif WORKFLOW_ID in running:
                raise JobAlreadyRunning(WORKFLOW_ID, running[WORKFLOW_ID])
            return self.jobs.start(component_id, action, work)

    def settings_dict(self) -> dict:
        return self.ctx.settings.to_dict()

    def update_settings(self, changes: dict) -> dict:
        updated = update_settings(self.ctx.settings, changes)
        save_settings(self.ctx.paths.settings_file, updated)
        self.ctx.settings = updated
        self.bus.publish("settings.updated", updated.to_dict())
        return updated.to_dict()


class _Handler(BaseHTTPRequestHandler):
    center: ControlCenter
    protocol_version = "HTTP/1.1"
    server_version = "GazeVoice"

    def log_message(self, format, *args):  # noqa: A002 - keep the terminal quiet
        pass

    # --- guards -------------------------------------------------------------
    def _hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self._hosts()

    def _write_ok(self) -> bool:
        if self.headers.get("X-GazeVoice") != "1":
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self._hosts()}

    # --- responses ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, **extra) -> None:
        self._json(status, {"error": message, **extra})

    def _read_body(self) -> bytes | None:
        """The request body, or None after answering 400/413 and closing the connection
        (an unread body would otherwise be parsed as the next keep-alive request)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._error(400, "Bad Content-Length")
            return None
        if length > _MAX_BODY:
            self.close_connection = True
            self._error(413, "Request body too large")
            return None
        return self.rfile.read(length) if length > 0 else b""

    @staticmethod
    def _parse_json(body: bytes) -> dict | None:
        if not body:
            return {}
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def _static(self, name: str) -> None:
        name = unquote(name)
        if not _STATIC_NAME.fullmatch(name):
            return self._error(404, "Not found")
        file = STATIC_DIR / name
        if not file.is_file():
            return self._error(404, "Not found")
        content_type = STATIC_TYPES.get(file.suffix, "application/octet-stream")
        self._send(200, file.read_bytes(), content_type, {"Content-Security-Policy": _CSP})

    def _events(self) -> None:
        subscription = self.center.bus.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                event = subscription.get(timeout=self.center.sse_ping)
                if event is None:
                    self.wfile.write(b": ping\n\n")
                else:
                    data = json.dumps(event.data)
                    self.wfile.write(f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass  # the browser went away
        finally:
            subscription.close()

    # --- routes ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._error(403, "Forbidden")
        url = urlsplit(self.path)
        path = url.path
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/state":
            fresh = parse_qs(url.query).get("fresh") == ["1"]
            return self._json(200, self.center.snapshot(fresh=fresh))
        if path == "/api/settings":
            return self._json(200, self.center.settings_dict())
        if path == "/api/jobs":
            return self._json(200, {"jobs": [j.to_dict(tail=200) for j in self.center.jobs.recent(30)]})
        if path.startswith("/api/jobs/"):
            job = self.center.jobs.get(path.rsplit("/", 1)[1])
            return self._json(200, job.to_dict()) if job else self._error(404, "No such job")
        if path == "/api/events":
            return self._events()
        if path == "/api/voice":
            return self._json(200, self.center.voice_snapshot())
        return self._error(404, "Not found")

    def _voice_post(self, path: str, body: dict) -> None:
        center = self.center
        try:
            if path == "/api/voice/start":
                try:
                    job = center.voice_start()
                except VoiceNotReady as exc:
                    return self._error(409, "Voice can't start yet", missing=exc.missing)
                return self._json(202, {"job": job.to_dict()})
            if path == "/api/voice/stop":
                return self._json(200, center.voice_stop())
            if path == "/api/voice/say":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._error(400, "Send {\"text\": \"…\"}")
                return self._json(202, {"record": center.voice_call("say", text)})
            if path == "/api/voice/ptt":
                state = body.get("state")
                if state not in PTT_STATES:
                    return self._error(400, f"state must be one of {', '.join(PTT_STATES)}")
                center.voice_call("ptt", state)
                return self._json(202, {"ok": True})
            if path == "/api/voice/undo":
                center.voice_call("undo")
                return self._json(202, {"ok": True})
            if path == "/api/voice/reset":
                center.voice_call("reset_conversation")
                return self._json(202, {"ok": True})
        except VoiceError as exc:
            return self._error(409, str(exc))
        except JobAlreadyRunning as exc:
            return self._error(409, "Already running", job_id=exc.job_id)
        return self._error(404, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or not self._write_ok():
            self.close_connection = True
            return self._error(403, "Forbidden")
        raw = self._read_body()
        if raw is None:  # answered already (bad or oversized body)
            return None
        path = urlsplit(self.path).path
        if path.startswith("/api/voice/"):
            body = self._parse_json(raw)
            if body is None:
                return self._error(400, "Send a JSON object")
            return self._voice_post(path, body)
        match = _ACTION_PATH.fullmatch(path)
        if not match:
            return self._error(404, "Not found")
        try:
            job = self.center.run_action(*match.groups())
        except KeyError:
            return self._error(404, "Unknown component")
        except ValueError as exc:
            return self._error(400, str(exc))
        except JobAlreadyRunning as exc:
            return self._error(409, "Already running", job_id=exc.job_id)
        return self._json(202, {"job": job.to_dict()})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._host_ok() or not self._write_ok():
            self.close_connection = True
            return self._error(403, "Forbidden")
        raw = self._read_body()
        if raw is None:
            return None
        if urlsplit(self.path).path != "/api/settings":
            return self._error(404, "Not found")
        body = self._parse_json(raw)
        if body is None:
            return self._error(400, "Send a JSON object")
        try:
            return self._json(200, self.center.update_settings(body))
        except ValueError as exc:
            return self._error(400, str(exc))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # A browser closing a tab mid-request is routine; anything else is worth printing.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(center: ControlCenter, host: str = "127.0.0.1", port: int = 8977) -> ThreadingHTTPServer:
    handler = type("GazeVoiceHandler", (_Handler,), {"center": center})
    return _Server((host, port), handler)
