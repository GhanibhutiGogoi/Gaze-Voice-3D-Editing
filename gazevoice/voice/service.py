"""The voice loop: hold a key in Blender, speak, and Blender changes.

    Blender key down/up ─► mic ring buffer ─► speech-to-text ─┬─► stop (instant)
                                                               └─► refiner ─► editor (Blender MCP) ─► one undo step

Three threads keep it responsive:
- poll: fetches hold-to-talk events from the Bridge add-on 20 times a second.
- stt: cuts each command from the ring buffer and transcribes it. Because this runs
  apart from editing, "stop" is heard even while an edit is running.
- edit: refines and edits commands one at a time, in the order they were spoken.
  "Undo" and "redo" wait their turn here too, so they never land in the middle of an edit.

Each Start→Stop is one run with its own queues and stop signal, so a thread that
outlives its run can never pick up the next run's commands.

Everything a command did is kept in a record (shown on the dashboard, saved to
logs/commands/<time>-<n>/record.json next to its audio).
"""

from __future__ import annotations

import collections
import itertools
import json
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NoReturn

from ..events import EventBus
from .audio import SILENCE_HELP, MicError, is_digital_silence, write_wav
from .brains import BrainError
from .commands import editor_message, meta_command, parse_refined, refiner_message, vocabulary_prompt
from .stt import SpeechError

PREROLL = 0.35  # seconds of audio kept from before the key went down
TAIL = 0.15  # and after it came up
MIN_HOLD = 0.3
MAX_HOLD = 60.0  # a key held longer than this is stuck: the command is dropped, not sent
POLL_SECONDS = 0.05
IDLE_AFTER = 5.0  # seconds a result stays on the status line
JOIN_SECONDS = 10.0  # how long stop() waits for each loop (and for an interrupted start) to finish
BUSY_WAIT = 10.0  # how long an undo operation waits for Blender to finish a transform
DEFAULT_KEY = "⌥ Space"
UNDO_NAME_BYTES = 63  # Blender keeps 64 bytes of an undo step's name, the closing NUL included
EDIT_SECONDS = 600.0  # a detailed build can take minutes; "stop" still ends it at once
REFINE_SECONDS = 90.0
REFINER_WARMUP = '{"heard": "hello", "focus": {}, "scene_objects": [], "previous_commands": []}'
EDITOR_WARMUP = "Warm-up, not a command: don't use any tools and don't change anything. Reply with exactly: ready"
READ_ONLY_TOOLS = ("get_", "search_", "render_viewport_to_path")  # Blender MCP tools that never change the scene
_COULDNT = re.compile(r"(?:could(?:n['’]t| not)|can(?:['’]t|not))\b", re.IGNORECASE)  # the editor saying it failed


def changes_scene(tools: list[str]) -> bool:
    """Whether any of these Blender MCP tools can have changed the scene (and so needs an undo step)."""
    return any(not tool.startswith(READ_ONLY_TOOLS) for tool in tools)


def undo_name(command: str, suffix: str = "") -> str:
    """ "GazeVoice: <command><suffix>" within Blender's limit; the command is cut, never the suffix."""
    head = "GazeVoice: "
    budget = UNDO_NAME_BYTES - len((head + suffix).encode())
    data = command.encode()
    if len(data) > budget:
        command = data[: budget - len("…".encode())].decode(errors="ignore").rstrip() + "…"
    return f"{head}{command}{suffix}"


class VoiceError(Exception):
    """Voice couldn't start or do what was asked; the message says why."""


class _Cancelled(Exception):
    """Voice was turned off while it was starting."""


@dataclass
class Factories:
    """How the service builds its parts. The control center passes real ones; tests pass fakes."""

    bridge: Callable[[], Any]
    mic: Callable[[], Any]
    stt: Callable[[], Any]
    refiner: Callable[[], Any]
    editor: Callable[[], Any]


@dataclass
class _Run:
    """One Start→Stop of voice: its stop signal, queues and threads."""

    stop: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)  # start() has finished, either way
    audio_q: queue.Queue = field(default_factory=queue.Queue)
    edit_q: queue.Queue = field(default_factory=queue.Queue)
    threads: list[threading.Thread] = field(default_factory=list)


def _end(part) -> None:
    """Close or stop a part, whichever it has. Safe to call twice."""
    for method in ("close", "stop"):
        if hasattr(part, method):
            try:
                getattr(part, method)()
            except Exception:  # noqa: BLE001 - best effort while shutting down
                pass
            return


class VoiceService:
    def __init__(
        self,
        logs_dir: Path,
        bus: EventBus,
        factories: Factories,
        engine: str = "parakeet",
        brain: str = "claude",
        clock: Callable[[], float] = time.time,
    ):
        self.logs_dir = Path(logs_dir)
        self.bus = bus
        self.factories = factories
        self.engine = engine
        self.brain = brain
        self.clock = clock
        self.state = "off"
        self.reason = ""
        self.blender_connected = False
        self.key = DEFAULT_KEY  # the hold-to-talk key, as Blender reports it
        self.recent: collections.deque[dict] = collections.deque(maxlen=20)
        self._lock = threading.RLock()
        self._run: _Run | None = None
        self._listening = False
        self._down_t = 0.0
        self._stt_busy = False
        self._edit_stage: str | None = None
        self._current: dict | None = None  # the command being understood or edited
        self._reset_pending = False
        self._conversation_since = 0.0
        self._bridge = self._mic = self._stt = self._refiner = self._editor = None
        self._numbers = itertools.count(1)
        self._status_seq = 0

    # --- what the dashboard sees ---------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            run = self._run
            return {
                "state": self.state,
                "reason": self.reason,
                "brain": self.brain,
                "engine": self.engine,
                "key": self.key,
                "listening": self._listening,
                "blender_connected": self.blender_connected,
                "queued": run.edit_q.qsize() if run else 0,
                "recent": [self._public(r) for r in reversed(self.recent)],
            }

    def _derived_state(self) -> str:
        if self.state in ("off", "starting", "error"):
            return self.state
        if self._listening:
            return "listening"
        if self._edit_stage:
            return self._edit_stage
        if self._stt_busy:
            return "transcribing"
        return "ready"

    def _publish_state(self) -> None:
        with self._lock:
            if self.state not in ("off", "starting", "error"):
                self.state = self._derived_state()
            payload = {k: v for k, v in self.snapshot().items() if k != "recent"}
        self.bus.publish("voice.state", payload)

    def _set(self, state: str, reason: str = "") -> None:
        with self._lock:
            self.state = state
            self.reason = reason
        self._publish_state()

    # --- Blender ---------------------------------------------------------------------------
    def _bridge_call(self, request: dict, timeout: float | None = None) -> dict | None:
        bridge = self._bridge
        if bridge is None:
            return None
        try:
            return bridge.request(request, timeout=timeout) if timeout else bridge.request(request)
        except (OSError, ValueError):
            return None

    def _blender_op(self, request: dict, run: _Run, wait: float = BUSY_WAIT) -> dict | None:
        """An undo operation. While Blender says it's busy (mid-transform), try again for a while."""
        deadline = time.monotonic() + wait
        while True:
            reply = self._bridge_call(request, timeout=wait)
            busy = bool(reply) and reply.get("error") == "busy"
            if not busy or time.monotonic() > deadline or run.stop.wait(0.25):
                return reply

    def _idle_hint(self) -> str:
        return f"Ready · hold {self.key} to talk"

    def _status(self, text: str, state: str = "working", revert: bool = False) -> None:
        with self._lock:
            self._status_seq += 1
            seq = self._status_seq
        self._bridge_call({"cmd": "status", "text": text[:200], "state": state})
        if revert:
            timer = threading.Timer(IDLE_AFTER, self._revert_status, args=(seq,))
            timer.daemon = True
            timer.start()

    def _revert_status(self, seq: int) -> None:
        with self._lock:
            current = seq == self._status_seq and self.state == "ready"
        if current:
            self._status(self._idle_hint(), "idle")

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            if self.blender_connected == connected:
                return
            self.blender_connected = connected
            self.reason = "" if connected else "Blender isn't connected. Voice picks up again when it's back."
            editing = self._edit_stage is not None
        self._publish_state()
        if connected and not editing:  # mid-edit, "Working…" is still the truth
            self._status(self._idle_hint(), "idle")

    # --- starting and stopping -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.state not in ("off", "error"):
                raise VoiceError("Voice is already on.")
            run = self._run = _Run()
            self._reset_pending = False
            self._conversation_since = self.clock()
            self.state, self.reason = "starting", "Connecting to Blender…"
        self._publish_state()
        try:
            try:
                self._start_parts(run)
            except Exception as exc:  # noqa: BLE001 - whatever failed, nothing may be left running
                self._abort_start(run, exc)
            with self._lock:
                cancelled = run.stop.is_set()
                if not cancelled:
                    run.threads = [
                        threading.Thread(target=self._poll_loop, args=(run,), daemon=True, name="voice-poll"),
                        threading.Thread(target=self._stt_loop, args=(run,), daemon=True, name="voice-stt"),
                        threading.Thread(target=self._edit_loop, args=(run,), daemon=True, name="voice-edit"),
                    ]
                    for thread in run.threads:
                        thread.start()
                    self.state, self.reason = "ready", ""
            if cancelled:
                self._abort_start(run, _Cancelled())
        finally:
            run.started.set()
        self._publish_state()
        self._status(self._idle_hint(), "idle")

    def _start_parts(self, run: _Run) -> None:
        bridge = self.factories.bridge()
        self._bridge = bridge
        try:
            hello = bridge.request({"cmd": "ping"})
        except (OSError, ValueError) as exc:
            raise VoiceError(
                "Blender isn't connected. Launch Blender from the dashboard with the GazeVoice Bridge "
                "installed, then turn voice on."
            ) from exc
        self.key = hello.get("key") or DEFAULT_KEY
        self.blender_connected = True
        self._step(run, "Opening the microphone…")
        mic = self._attach(run, "_mic", self.factories.mic())
        mic.start()
        self._step(run, "Loading speech-to-text (the first time takes a while)…")
        stt = self._attach(run, "_stt", self.factories.stt())
        stt.start()
        self._step(run, "Starting the brain and connecting it to Blender…")
        sessions = [self._attach(run, "_refiner", self.factories.refiner()),
                    self._attach(run, "_editor", self.factories.editor())]
        self._start_sessions(sessions)
        self._step(run, "Checking the microphone…")
        if mic.heard_only_silence():
            raise MicError(SILENCE_HELP)
        self._bridge_call({"cmd": "poll"})  # drop key presses Blender queued while voice was off

    def _step(self, run: _Run, reason: str) -> None:
        with self._lock:
            if run.stop.is_set():
                raise _Cancelled()
            self.reason = reason
        self._publish_state()

    def _attach(self, run: _Run, name: str, part):
        """Make a part stop()-able before it starts, so stopping mid-start reaches it."""
        with self._lock:
            cancelled = run.stop.is_set()
            if not cancelled:
                setattr(self, name, part)
        if cancelled:
            _end(part)
            raise _Cancelled()
        return part

    def _start_sessions(self, sessions: list) -> None:
        errors: list[Exception] = []

        def start(session) -> None:
            try:
                session.start(**getattr(session, "start_kwargs", {}))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=start, args=(s,)) for s in sessions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0] if isinstance(errors[0], BrainError) else BrainError(str(errors[0]))

    def _abort_start(self, run: _Run, exc: Exception) -> NoReturn:
        self._teardown_parts()
        self._close_bridge()
        with self._lock:
            if self._run is run:
                self._run = None
            cancelled = run.stop.is_set()
        if cancelled:
            self._set("off", "")
            raise VoiceError("Voice was turned off before it finished starting.") from exc
        known = isinstance(exc, (VoiceError, MicError, SpeechError, BrainError))
        message = str(exc) if known else f"Voice couldn't start: {exc}"
        self._set("error", message)
        raise VoiceError(message) from exc

    def _teardown_parts(self) -> None:
        with self._lock:
            parts = [p for p in (self._editor, self._refiner, self._stt, self._mic) if p is not None]
            self._mic = self._stt = self._refiner = self._editor = None
        for part in parts:
            _end(part)

    def _close_bridge(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            _end(bridge)

    def stop(self) -> None:
        with self._lock:
            run = self._run
            if self.state == "off":
                return
            if run is None:  # a failed start: nothing is running, just clear the error
                self.state, self.reason = "off", ""
            else:
                run.stop.set()
                self._listening = False
                starting = self.state == "starting"
                if starting:
                    self.reason = "Stopping…"
        self._publish_state()
        if run is None:
            return
        if starting:
            self._teardown_parts()  # unblocks whatever start() is waiting on; start() then turns voice off
            run.started.wait(JOIN_SECONDS)
            return
        for q in (run.audio_q, run.edit_q):
            q.put(None)
        self._interrupt_current()
        self._teardown_parts()  # ends brain turns, speech and the mic, so the loops can finish
        for thread in run.threads:
            thread.join(timeout=JOIN_SECONDS)
        self._status("Voice is off", "idle")
        self._close_bridge()
        with self._lock:
            if self._run is run:
                self._run = None
            self._listening = self._stt_busy = False
            self._edit_stage = self._current = None
            self.state, self.reason = "off", ""
        self._publish_state()

    @staticmethod
    def _drain(q: queue.Queue, keep_marker: bool = False) -> list:
        """Empty a queue and return its records. With keep_marker, a shutdown marker (None) stays queued."""
        items = []
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        if keep_marker and any(i is None for i in items):
            q.put(None)
        return [i for i in items if i is not None]

    # --- hold to talk --------------------------------------------------------------------------
    def _require_on(self) -> _Run:
        with self._lock:
            run = self._run
            if run is None or self.state in ("off", "starting", "error"):
                raise VoiceError("Turn voice on first.")
            return run

    def ptt(self, state: str, t: float | None = None, via: str = "key", reason: str = "") -> None:
        t = t if t is not None else self.clock()
        with self._lock:
            run = self._run
            if run is None or self.state in ("off", "starting", "error"):
                return
            if state == "down":
                if self._listening:
                    return
                self._listening = True
                self._down_t = t
            elif state == "cancel":
                if not self._listening:
                    return
                self._listening = False
            elif state == "up":
                if not self._listening:
                    return
                self._listening = False
                held = t - self._down_t
                t0, t1 = self._down_t, min(t, self._down_t + MAX_HOLD)
            else:
                return
        self._publish_state()
        if state == "down":
            if not self._ensure_mic(run):
                with self._lock:
                    self._listening = False
                self._publish_state()
                return
            how = "click Talk again" if via == "toggle" else "release the key"
            self._status(f"Listening… {how} to send", "listening")
        elif state == "cancel":
            if reason == "too_long":
                self._status("Held for over a minute, so that was dropped.", "error", revert=True)
            else:
                self._status(self._idle_hint(), "idle")
        elif held < MIN_HOLD:
            self._status("Too short: hold the key while you speak", "error", revert=True)
        else:
            record = self._new_record("voice")
            record["_t0"], record["_t1"] = t0, t1
            self._capture_context(record)
            run.audio_q.put(record)

    def _ensure_mic(self, run: _Run) -> bool:
        """The mic is streaming, or was just restarted (ffmpeg exits when the device goes away)."""
        mic = self._mic
        if mic is not None and mic.alive:
            return True
        self._status("Restarting the microphone…", "working")
        try:
            fresh = self.factories.mic()
            fresh.start()
        except Exception as exc:  # noqa: BLE001 - reported on Blender's status line
            self._status(f"✗ The microphone stopped: {exc}", "error", revert=True)
            return False
        with self._lock:
            current = not run.stop.is_set()
            old = self._mic
            if current:
                self._mic = fresh
        if not current:
            _end(fresh)
            return False
        if old is not None and old is not fresh:
            _end(old)
        return True

    def say(self, text: str) -> dict:
        """A typed command: skips speech-to-text and the refiner."""
        run = self._require_on()
        text = text.strip()
        if not text:
            raise VoiceError("Type a command first.")
        record = self._new_record("typed")
        record["heard"] = text
        self._capture_context(record)
        self._update(record)
        self._route(record, run)
        return self._public(record)

    def undo(self) -> None:
        run = self._require_on()
        record = self._new_record("button")
        record["heard"] = "undo"
        self._update(record)
        self._route(record, run)

    def reset_conversation(self) -> None:
        """Start fresh before the next command (a running edit finishes first)."""
        self._require_on()
        with self._lock:
            self._reset_pending = True
            self._conversation_since = self.clock()
        self._status("Started a new conversation", "done", revert=True)
        self._publish_state()

    # --- records ------------------------------------------------------------------------------
    def _new_record(self, source: str) -> dict:
        number = next(self._numbers)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        record = {  # every key exists from the start, so other threads can read it while it changes
            "id": f"{stamp}-{number}",
            "source": source,
            "created": self.clock(),
            "stage": "queued",
            "heard": "",
            "words": [],
            "refined": "",  # the short title shown everywhere
            "prompt": "",  # what the editor was asked: a detailed brief for builds
            "kind": "",
            "fallback": False,
            "focus": None,
            "names": [],
            "tools": [],
            "reply": "",
            "error": None,
            "undo_step": None,
            "model": None,  # the model that did the edit, as the brain reports it
            "provider": None,  # the API provider it ran on, if any
            "effort": None,
            "audio": None,
            "refine_error": None,
            "refiner_reply": None,
            "timings": {},
            "_t0": 0.0,
            "_t1": 0.0,
            "_stop": False,
        }
        with self._lock:
            self.recent.append(record)
        return record

    @staticmethod
    def _copy(record: dict) -> dict:
        copy = {k: v for k, v in list(record.items()) if not k.startswith("_")}
        for key in ("timings", "tools", "words"):
            copy[key] = type(record[key])(record[key])
        return copy

    def _public(self, record: dict) -> dict:
        public = self._copy(record)
        public["objects"] = len(public.pop("names") or [])
        return public

    def _record_dir(self, record: dict) -> Path:
        return self.logs_dir / "commands" / record["id"]

    def _update(self, record: dict) -> None:
        self.bus.publish("voice.command", self._public(record))

    def _save(self, record: dict) -> None:
        try:
            folder = self._record_dir(record)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "record.json").write_text(json.dumps(self._copy(record), indent=2))
        except OSError as exc:  # a full disk mustn't stop voice
            print(f"GazeVoice: couldn't save command {record['id']}: {exc}", file=sys.stderr)

    def _capture_context(self, record: dict) -> None:
        """What was in focus when the command was spoken (selection now; gaze later)."""
        selection = self._bridge_call({"cmd": "selection"})
        if selection and selection.get("ok"):
            record["focus"] = {k: selection.get(k) for k in ("active", "selected", "mode")}
        names = self._bridge_call({"cmd": "scene_names"})
        if names and names.get("ok"):
            record["names"] = list(names.get("names") or [])

    def _finish(self, record: dict, stage: str) -> None:
        record["stage"] = stage
        record["timings"]["total"] = round(self.clock() - record["created"], 2)
        self._save(record)
        self._update(record)

    def _done(self, record: dict, line: str, state: str = "done") -> None:
        self._status(line, state, revert=True)
        self._finish(record, "done")

    def _fail(self, record: dict, message: str) -> None:
        record["error"] = message
        self._status(f"✗ {message}", "error", revert=True)
        self._finish(record, "failed")

    def _stopped(self, record: dict, reply: str, run: _Run) -> None:
        if changes_scene(record["tools"]) and self._push_undo(record, run, " (stopped)"):
            reply = f"{reply} Undo takes back what it changed so far."
            self._status("■ Stopped · undo takes back what it changed so far", "done", revert=True)
        record["reply"] = reply
        self._finish(record, "stopped")

    def _push_undo(self, record: dict, run: _Run, suffix: str = "") -> bool:
        """One undo step for this command, if the scene changed since it began."""
        request = {"cmd": "undo_push", "message": undo_name(record["refined"], suffix), "only_if_changed": True}
        reply = self._blender_op(request, run)
        record["undo_step"] = bool(reply and reply.get("ok") and reply.get("pushed", True))
        return record["undo_step"]

    # --- the three loops --------------------------------------------------------------------------
    def _poll_loop(self, run: _Run) -> None:
        ack, boot = 0, None
        while not run.stop.is_set():
            try:
                ack, boot = self._poll_once(run, ack, boot)
                if self._listening and self.clock() - self._down_t > MAX_HOLD:
                    self.ptt("cancel", reason="too_long")
            except Exception as exc:  # noqa: BLE001 - Blender's key only reaches us through this loop
                print(f"GazeVoice: polling Blender failed: {exc!r}", file=sys.stderr)
                run.stop.wait(0.5)
                continue
            run.stop.wait(POLL_SECONDS)

    def _poll_once(self, run: _Run, ack: int, boot: str | None) -> tuple[int, str | None]:
        bridge = self._bridge
        if bridge is None:
            return ack, boot
        try:
            reply = bridge.request({"cmd": "poll", "ack": ack, "boot": boot})
        except TimeoutError:  # Blender is busy (a heavy edit), not gone; its events wait for the next poll
            return ack, boot
        except (OSError, ValueError):
            self._set_connected(False)
            run.stop.wait(1.0)
            return ack, boot
        self._set_connected(True)
        if reply.get("boot") != boot:  # Blender restarted: its events count from 1 again
            ack, boot = 0, reply.get("boot")
        for event in reply.get("events") or []:
            seq = event.get("seq")
            if isinstance(seq, int):
                if seq <= ack:
                    continue
                ack = seq
            if event.get("type") == "ptt":
                self.ptt(event.get("state", ""), event.get("t"))
            elif event.get("type") == "toggle":
                self.ptt("up" if self._listening else "down", event.get("t"), via="toggle")
        return ack, boot

    def _stt_loop(self, run: _Run) -> None:
        while True:
            record = run.audio_q.get()
            if record is None or run.stop.is_set():
                return
            with self._lock:
                self._stt_busy = self._run is run
            self._publish_state()
            try:
                self._transcribe(record, run)
            except Exception as exc:  # noqa: BLE001 - one bad command mustn't stop voice
                self._fail(record, f"Unexpected error: {exc}")
            finally:
                with self._lock:
                    if self._run is run:
                        self._stt_busy = False
                self._publish_state()

    def _transcribe(self, record: dict, run: _Run) -> None:
        mic = self._mic
        if mic is None:
            self._fail(record, "The microphone isn't running.")
            return
        t0, t1 = record["_t0"], record["_t1"]
        deadline = time.monotonic() + 1.0
        while mic.buffer.latest_time < t1 + TAIL and time.monotonic() < deadline:
            time.sleep(0.02)
        pcm = mic.buffer.capture(t0 - PREROLL, t1 + TAIL)
        if not pcm:
            self._fail(record, "No audio arrived from the microphone.")
            return
        if is_digital_silence(pcm):
            self._fail(record, SILENCE_HELP)
            return
        audio = write_wav(self._record_dir(record) / "audio.wav", pcm)
        record["audio"] = str(audio)
        record["stage"] = "transcribing"
        self._update(record)
        self._status("Transcribing…", "working")
        stt = self._speech(record, run)
        if stt is None:
            return
        prompt = vocabulary_prompt(record["names"]) if self.engine == "whisper" else None
        began = time.monotonic()
        try:
            result = stt.transcribe(audio, prompt=prompt)
        except SpeechError as exc:
            self._fail(record, str(exc))
            return
        record["timings"]["stt"] = round(time.monotonic() - began, 2)
        record["heard"] = (result.get("text") or "").strip()
        record["words"] = result.get("words") or []
        if not record["heard"]:
            self._fail(record, "Didn't catch that. Hold the key while you speak.")
            return
        self._status(f"Heard: {record['heard']}", "working")
        self._update(record)
        self._route(record, run)

    def _speech(self, record: dict, run: _Run):
        """The speech worker, restarted if it died (it holds the model, so this takes a few seconds)."""
        stt = self._stt
        if stt is not None and stt.alive:
            return stt
        self._status("Restarting speech-to-text…", "working")
        try:
            fresh = self.factories.stt()
            fresh.start()
        except Exception as exc:  # noqa: BLE001 - reported on the command
            self._fail(record, f"Speech-to-text stopped and couldn't restart: {exc}")
            return None
        with self._lock:
            current = not run.stop.is_set()
            old = self._stt
            if current:
                self._stt = fresh
        if not current:
            _end(fresh)
            self._fail(record, "Voice is turning off.")
            return None
        if old is not None and old is not fresh:
            _end(old)
        return fresh

    def _route(self, record: dict, run: _Run) -> None:
        meta = meta_command(record["heard"])
        if meta == "stop":
            self._meta("stop", record, run)
            return
        if meta:  # undo and redo wait their turn behind the commands before them
            record["kind"] = record["refined"] = meta
        record["stage"] = "queued"
        self._update(record)
        run.edit_q.put(record)

    def _interrupt_current(self, skip: dict | None = None) -> dict | None:
        """Stop the command being understood or edited. Returns it, or None if nothing was running."""
        with self._lock:
            current = self._current
        if current is None or current is skip:
            return None
        current["_stop"] = True
        for session in (self._refiner, self._editor):
            if session is not None:
                session.interrupt()
        return current

    def _meta(self, kind: str, record: dict, run: _Run) -> None:
        record["kind"] = record["refined"] = kind
        if kind == "stop":
            queued = self._drain(run.edit_q, keep_marker=True)
            running = self._interrupt_current(skip=record)
            for waiting in queued:
                self._stopped(waiting, "Stopped before it started.", run)
            stopped = running is not None or bool(queued)
            record["reply"] = "Stopped." if stopped else "Nothing was running."
            self._done(record, "■ Stopped" if stopped else record["reply"])
            return
        reply = self._blender_op({"cmd": kind}, run)
        if reply and reply.get("ok"):
            record["reply"] = "Undid the last change." if kind == "undo" else "Redid the change."
            self._done(record, "↶ Undid the last change" if kind == "undo" else "↷ Redid the change")
        elif reply and reply.get("error") == "busy":
            self._fail(record, f"Blender is busy. Finish what you're doing, then say {kind} again.")
        else:
            self._fail(record, f"Blender couldn't {kind}.")

    def _edit_loop(self, run: _Run) -> None:
        while True:
            record = run.edit_q.get()
            if record is None or run.stop.is_set():
                return
            self._apply_reset()
            with self._lock:
                if self._run is run:
                    self._current = record
            try:
                self._edit(record, run)
            except Exception as exc:  # noqa: BLE001 - one bad command mustn't stop voice
                self._fail(record, f"Unexpected error: {exc}")
            finally:
                with self._lock:
                    if self._run is run:
                        self._edit_stage = None
                        self._current = None
                self._publish_state()

    def _apply_reset(self) -> None:
        with self._lock:
            pending, self._reset_pending = self._reset_pending, False
            sessions = [self._refiner, self._editor] if pending else []
            if pending:
                self._refiner = self._editor = None
        for session in sessions:
            if session is not None:
                _end(session)

    def _stage(self, stage: str, record: dict) -> None:
        with self._lock:
            self._edit_stage = stage
        record["stage"] = stage
        self._update(record)
        self._publish_state()

    def _session(self, name: str, factory: Callable[[], Any], run: _Run):
        session = getattr(self, name)
        if session is not None and session.alive:
            return session
        if run.stop.is_set():
            raise BrainError("Voice is turning off.")
        fresh = factory()
        try:
            fresh.start()  # a restart skips the warm-up: the next command does the talking
        except Exception:
            _end(fresh)
            raise
        with self._lock:
            current = not run.stop.is_set()
            old = getattr(self, name)
            if current:
                setattr(self, name, fresh)
        if not current:
            _end(fresh)
            raise BrainError("Voice is turning off.")
        if old is not None and old is not fresh:
            _end(old)
        return fresh

    def _discard(self, name: str, session) -> None:
        """A session whose turn failed may still answer later; it never gets another command."""
        with self._lock:
            if getattr(self, name) is session:
                setattr(self, name, None)
        _end(session)

    def _refine(self, record: dict, run: _Run):
        with self._lock:
            since = self._conversation_since
            recent = list(self.recent)
        previous = [r["refined"] for r in recent
                    if r["stage"] == "done" and r["kind"] == "command" and r["created"] >= since and r is not record]
        began = time.monotonic()
        refiner = None
        try:
            refiner = self._session("_refiner", self.factories.refiner, run)
            turn = refiner.send(refiner_message(record["heard"], record["focus"], record["names"], previous),
                                timeout=REFINE_SECONDS)
            reply = "" if turn.is_error else turn.text
            if turn.interrupted:
                record["_stop"] = True
        except BrainError as exc:
            record["refine_error"] = str(exc)
            reply = ""
            if refiner is not None:
                self._discard("_refiner", refiner)
        record["timings"]["refine"] = round(time.monotonic() - began, 2)
        refined = parse_refined(reply, record["heard"])
        if refined.fallback and reply:
            record["refiner_reply"] = reply[:500]
        return refined

    def _edit(self, record: dict, run: _Run) -> None:
        if record["kind"] in ("undo", "redo"):
            self._meta(record["kind"], record, run)
            return
        if record["source"] == "voice":
            self._stage("refining", record)
            self._status("Understanding…", "working")
            refined = self._refine(record, run)
            record["refined"], record["prompt"] = refined.title or refined.text, refined.text
            record["kind"], record["fallback"] = refined.kind, refined.fallback
            if record["_stop"] or run.stop.is_set():
                self._stopped(record, "Stopped.", run)
                return
            if refined.kind in ("undo", "redo", "stop"):
                self._meta(refined.kind, record, run)
                return
            if refined.kind == "ignore":
                record["reply"] = "That didn't sound like a Blender command, so nothing changed."
                self._done(record, record["reply"])
                return
        else:
            record["refined"] = record["prompt"] = record["heard"]
            record["kind"] = "command"
        if record["_stop"] or run.stop.is_set():
            self._stopped(record, "Stopped.", run)
            return
        self._stage("editing", record)
        self._status(f"Working: {record['refined']}", "working")
        self._bridge_call({"cmd": "begin_edit"})  # from here, Blender notes whether the scene changes

        def on_tool(name: str) -> None:
            record["tools"].append(name)
            self._update(record)
            self._status(f"Working: {name.replace('_', ' ')}…", "working")

        began = time.monotonic()
        editor = None
        try:
            editor = self._session("_editor", self.factories.editor, run)
            record["effort"] = getattr(editor, "effort", None)
            turn = editor.send(editor_message(record["prompt"] or record["refined"], record["heard"], record["focus"]),
                               on_tool=on_tool, timeout=EDIT_SECONDS)
            init = getattr(editor, "init", None) or {}
            record["model"], record["provider"] = init.get("model"), init.get("provider")
        except BrainError as exc:
            record["timings"]["edit"] = round(time.monotonic() - began, 2)
            if editor is not None:
                self._discard("_editor", editor)
            if record["_stop"]:  # it had to be stopped the hard way; the next command starts a new one
                self._stopped(record, "Stopped.", run)
                return
            if changes_scene(record["tools"]):
                self._push_undo(record, run, " (failed)")
            self._fail(record, str(exc))
            return
        record["timings"]["edit"] = round(time.monotonic() - began, 2)
        if turn.interrupted or (turn.is_error and record["_stop"]):
            self._stopped(record, "Stopped.", run)
            return
        record["reply"] = turn.text
        failed = turn.is_error or bool(_COULDNT.match(turn.text))
        if changes_scene(turn.tools):
            self._push_undo(record, run, " (failed)" if failed else "")
        if failed:
            self._fail(record, turn.text or "The edit didn't finish.")
            return
        self._done(record, f"✓ {turn.text or 'Done'}")
