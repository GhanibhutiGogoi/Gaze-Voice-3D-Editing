"""The voice loop: hold to speak, then the selected 3D editor changes.

    Host key down/up ─► mic ring buffer ─► speech-to-text ─┬─► stop (instant)
                                                           └─► refiner ─► editor MCP ─► one undo step

Three threads keep it responsive:
- poll: fetches hold-to-talk events from the Bridge add-on 20 times a second.
- stt: cuts each command from the ring buffer and transcribes it. Because this runs
  apart from editing, "stop" is heard even while an edit is running.
- edit: refines and edits commands one at a time, in the order they were spoken.
  "Undo" and "redo" wait their turn here too, so they never land in the middle of an edit.

Each Start→Stop is one run with its own queues and stop signal, so a thread that
outlives its run can never pick up the next run's commands.

A fourth part is optional: gaze (the camera, or the mouse standing in for it). It only keeps a short track of
where the person looked. When a spoken command has a pointing word like "there", the edit thread asks the host
what was under that spot and, in "on" mode, tells the brains in the focus. When two guesses are too close to
call, the edit thread puts numbers on them in the viewport and waits for the person to say one; the stt thread
keeps running, so the answer is heard. Gaze never edits anything, and whatever goes wrong with it, the command
runs on the selection. A target that is a big stretch of mesh with nothing to name it can also be looked at closer, when
the person has turned pictures on: a crop of the viewport goes from Blender's memory to one short-lived process that says
in a few words what is inside the ring (see picture.py). Only that sentence joins the focus, and if anything about it
fails or takes too long the command goes on without it.

Everything a command did is kept in a record (shown on the dashboard, saved to
logs/commands/<time>-<n>/record.json next to its audio).
"""

from __future__ import annotations

import collections
import itertools
import json
import math
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
from .commands import choice_number, editor_message, meta_command, parse_refined, refiner_message, vocabulary_prompt
from .gaze import (
    MAX_CANDIDATES,
    Candidate,
    GazeSample,
    GazeTrack,
    Grounding,
    Target,
    clean_description,
    ground,
    is_clear,
    pointing_words,
    viewport_size,
)
from .picture import PICTURE_SECONDS, PICTURE_SIZE, needs_picture
from .stt import SpeechError

PREROLL = 0.35  # seconds of audio kept from before the key went down
TAIL = 0.15  # and after it came up
MIN_HOLD = 0.3
MAX_HOLD = 60.0  # a key held longer than this is stuck: the command is dropped, not sent
POLL_SECONDS = 0.05
IDLE_AFTER = 5.0  # seconds a result stays on the status line
JOIN_SECONDS = 10.0  # how long stop() waits for each loop (and for an interrupted start) to finish
BUSY_WAIT = 10.0  # how long an undo operation waits for the host to finish an edit
UNITY_BUSY_WAIT = 60.0  # Unity may have to finish compiling, then reload
DEFAULT_KEY = "⌥ Space"
UNDO_NAME_BYTES = 63  # Blender keeps 64 bytes of an undo step's name, the closing NUL included
EDIT_SECONDS = 600.0  # a detailed build can take minutes; "stop" still ends it at once
REFINE_SECONDS = 90.0
REFINER_WARMUP = '{"heard": "hello", "focus": {}, "scene_objects": [], "previous_commands": []}'
EDITOR_WARMUP = "Warm-up, not a command: don't use any tools and don't change anything. Reply with exactly: ready"
READ_ONLY_TOOLS = ("get_", "search_", "render_viewport_to_path")
POINTER_RADIUS = (0.02, 0.03)  # a mouse is nearly exact: the rays cover this share of the viewport's width and height
GAZE_LOST = "Gaze lost, using your selection"
VIEW_CHANGED = "Viewport changed, calibrate gaze again. Using your selection"
CAMERA_BUSY = "Gaze is using the camera to calibrate or test. Wait for it to finish, then turn voice on."
CHOICE_SECONDS = 8.0  # how long numbers stay on screen waiting to be answered
WHICH_ONE = "Which one? Say the number."
NO_ANSWER = "No answer, using your selection"
LOOKING_CLOSER = "Looking closer…"
NO_PICTURE = "GazeVoice: no picture, going on with text only"  # never says why: what failed may have held the picture
CROP_POLL_SECONDS = 0.05  # between asking Blender whether the crop is ready
BRIDGE_REPLY_SECONDS = 2.0  # the longest Blender is given to answer one crop request
COULDNT_TELL = "Couldn't tell what you meant. Select it or say its name."
_COULDNT = re.compile(r"(?:could(?:n['’]t| not)|can(?:['’]t|not))\b", re.IGNORECASE)  # the editor saying it failed


def changes_scene(tools: list[str]) -> bool:
    """Whether a tool may have changed the scene and might need an undo step."""
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
    # Builds a gaze part: .start(), .stop(), .alive, .track (a GazeTrack), .radius and .view (the viewport size it was
    # calibrated for, or None when it needs no calibration). None means no gaze.
    gaze: Callable[[], Any] | None = None
    # Builds the picture part, or returns None when pictures are off: .refresh() starts finding out in the background
    # whether pictures may be used, .ready() answers from what it last found without ever waiting, and
    # .describe(png_base64, word, object_name, timeout=…, cancelled=…) gives one clean sentence, or None.
    # It is called every time voice starts, so it is also where leftovers from a crash are swept. None means neither.
    pictures: Callable[[], Any] | None = None


class PointerTracker:
    """Gaze from the mouse: the pointer's place in the 3D viewport while the key is held.

    The poll loop feeds it the Bridge's pointer events. It needs no camera and no calibration.
    """

    radius = POINTER_RADIUS
    view = None  # nothing was calibrated, so there is no viewport size to go out of date

    def __init__(self):
        self.track = GazeTrack()
        self.alive = False

    def start(self) -> None:
        self.alive = True

    def stop(self) -> None:
        self.alive = False

    def feed(self, u: float, v: float, t: float) -> None:
        self.track.add(GazeSample(t, u, v, 1.0))


@dataclass
class _Run:
    """One Start→Stop of voice: its stop signal, queues and threads."""

    stop: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)  # start() has finished, either way
    audio_q: queue.Queue = field(default_factory=queue.Queue)
    edit_q: queue.Queue = field(default_factory=queue.Queue)
    threads: list[threading.Thread] = field(default_factory=list)


@dataclass
class _Choice:
    """Numbers on screen, and the command waiting for the person to say one."""

    names: list[str]  # what each number stands for, number 1 first
    answered: threading.Event = field(default_factory=threading.Event)  # a number was said, or the wait was ended
    number: int | None = None  # the number that was said; None if the wait ended without one


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
        host: str = "blender",
        gaze_mode: str = "off",
        gaze_source: str = "camera",
        camera=None,
    ):
        self.logs_dir = Path(logs_dir)
        self.bus = bus
        self.factories = factories
        self.engine = engine
        self.brain = brain
        self.host = host
        self.gaze_mode = gaze_mode  # "off", "shadow" (record only) or "on" (tell the brains)
        self.gaze_source = gaze_source
        self.camera = camera  # a CameraClaim shared with gaze calibration: voice holds it from start to stop
        self.host_label = "Unity" if host == "unity" else "Blender"
        self.busy_wait = UNITY_BUSY_WAIT if host == "unity" else BUSY_WAIT
        self.choice_seconds = CHOICE_SECONDS
        self.clock = clock
        self.state = "off"
        self.reason = ""
        self.host_connected = False
        self.key = DEFAULT_KEY
        self.recent: collections.deque[dict] = collections.deque(maxlen=20)
        self._lock = threading.RLock()
        self._run: _Run | None = None
        self._listening = False
        self._down_t = 0.0
        self._stt_busy = False
        self._edit_stage: str | None = None
        self._current: dict | None = None  # the command being understood or edited
        self._choice: _Choice | None = None  # set while numbers are on screen
        self._reset_pending = False
        self._conversation_since = 0.0
        self._bridge = self._mic = self._stt = self._refiner = self._editor = self._gaze = None
        self._pictures = None  # describes big unnamed targets from a picture, when pictures are on
        self._gaze_problem = ""  # why gaze didn't start, if it didn't
        # Commands whose gaze could be compared with the selection, and how many of those agreed with it.
        self._gaze_checked = self._gaze_matched = 0
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
                "host": self.host,
                "key": self.key,
                "listening": self._listening,
                "host_connected": self.host_connected,
                "blender_connected": self.host_connected,  # legacy dashboard clients
                "queued": run.edit_q.qsize() if run else 0,
                "gaze": {
                    "mode": self.gaze_mode,
                    "source": self.gaze_source,
                    "live": self._gaze is not None and bool(self._gaze.alive),
                    "checked": self._gaze_checked,
                    "matched": self._gaze_matched,
                },
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

    # --- host bridge -----------------------------------------------------------------------
    def _bridge_call(self, request: dict, timeout: float | None = None) -> dict | None:
        bridge = self._bridge
        if bridge is None:
            return None
        try:
            return bridge.request(request, timeout=timeout) if timeout else bridge.request(request)
        except (OSError, ValueError):
            return None

    def _host_op(self, request: dict, run: _Run) -> dict | None:
        """An undo operation. Retry while the host reports that an edit is in progress."""
        deadline = time.monotonic() + self.busy_wait
        while True:
            reply = self._bridge_call(request, timeout=self.busy_wait)
            busy = bool(reply) and reply.get("error") == "busy"
            reloading = reply is None and self.host == "unity"  # its socket is down while it reloads scripts
            if not (busy or reloading) or time.monotonic() > deadline or run.stop.wait(0.25):
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
            if self.host_connected == connected:
                return
            self.host_connected = connected
            self.reason = "" if connected else f"{self.host_label} isn't connected. Voice picks up again when it's back."
            editing = self._edit_stage is not None
        self._publish_state()
        if connected and not editing:  # mid-edit, "Working…" is still the truth
            self._status(self._idle_hint(), "idle")

    # --- starting and stopping -------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.state not in ("off", "error"):
                raise VoiceError("Voice is already on.")
            if self.camera is not None and self.camera.take("voice"):
                raise VoiceError(CAMERA_BUSY)
            run = self._run = _Run()
            self._reset_pending = False
            self._conversation_since = self.clock()
            self._gaze_problem = ""
            self._gaze_checked = self._gaze_matched = 0
            self.state, self.reason = "starting", f"Connecting to {self.host_label}…"
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
        if self._gaze_problem:
            problem = f"Gaze couldn't start ({self._gaze_problem}), so voice uses your selection."
            self._status(problem, "error", revert=True)

    def _start_parts(self, run: _Run) -> None:
        bridge = self.factories.bridge()
        self._bridge = bridge
        try:
            hello = bridge.request({"cmd": "ping"})
        except (OSError, ValueError) as exc:
            raise VoiceError(
                f"{self.host_label} isn't connected. Open a {self.host_label} project with the GazeVoice Bridge "
                "installed, then turn voice on."
            ) from exc
        self.key = hello.get("key") or DEFAULT_KEY
        self.host_connected = True
        self._step(run, "Opening the microphone…")
        mic = self._attach(run, "_mic", self.factories.mic())
        mic.start()
        self._step(run, "Loading speech-to-text (the first time takes a while)…")
        stt = self._attach(run, "_stt", self.factories.stt())
        stt.start()
        self._step(run, f"Starting the brain and connecting it to {self.host_label}…")
        sessions = [self._attach(run, "_refiner", self.factories.refiner()),
                    self._attach(run, "_editor", self.factories.editor())]
        self._start_sessions(sessions)
        self._step(run, "Checking the microphone…")
        if mic.heard_only_silence():
            raise MicError(SILENCE_HELP)
        if self.host == "blender":
            self._hide_gaze()  # nothing stale on screen
            self._start_gaze(run)
            self._start_pictures(run)
        self._bridge_call({"cmd": "poll"})  # drop key presses (and mouse positions) Blender queued while voice was off

    def _start_gaze(self, run: _Run) -> None:
        """Gaze is an extra: if it can't start, voice goes on without it, using the selection."""
        factory = self.factories.gaze
        if factory is None or self.gaze_mode == "off":
            return
        self._step(run, "Starting gaze…")
        try:
            part = factory()
        except Exception as exc:  # noqa: BLE001 - gaze never stops voice
            self._gaze_unavailable(run, exc)
            return
        part = self._attach(run, "_gaze", part)
        try:
            part.start()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if self._gaze is part:
                    self._gaze = None
            _end(part)
            self._gaze_unavailable(run, exc)

    def _start_pictures(self, run: _Run) -> None:
        """Pictures are an extra too: if they can't be set up, voice goes on without them."""
        factory = self.factories.pictures
        if factory is None:
            return
        try:
            part = factory()
            if part is not None:
                part.refresh()  # starts finding out, off this thread, whether pictures may be used; never waits for it
        except Exception:  # noqa: BLE001 - pictures never stop voice
            print(NO_PICTURE, file=sys.stderr)
            return
        with self._lock:
            if not run.stop.is_set():
                self._pictures = part

    def _gaze_unavailable(self, run: _Run, exc: Exception) -> None:
        if run.stop.is_set():  # it was stopped while starting, which is why it failed
            raise _Cancelled() from exc
        self._gaze_problem = str(exc) or type(exc).__name__

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

    def _give_back_camera(self) -> None:
        if self.camera is not None:
            self.camera.release("voice")

    def _abort_start(self, run: _Run, exc: Exception) -> NoReturn:
        self._teardown_parts()
        self._close_bridge()
        self._give_back_camera()  # the gaze worker, if there was one, has let go of it by now
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
            parts = [p for p in (self._gaze, self._editor, self._refiner, self._stt, self._mic) if p is not None]
            self._mic = self._stt = self._refiner = self._editor = self._gaze = self._pictures = None
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
        self._give_back_camera()  # teardown stopped the gaze worker above
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
        except Exception as exc:  # noqa: BLE001 - reported on the host status line
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
            "gaze": None,  # what the pointing words referred to, when gaze was asked
            "timings": {},
            "_gaze_note": "",  # shown beside the status when gaze didn't help
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
        pushed = self._push_undo(record, run, " (stopped)") if self.host == "unity" or changes_scene(record["tools"]) else False
        if pushed:
            reply = f"{reply} Undo takes back what it changed so far."
            self._status("■ Stopped · undo takes back what it changed so far", "done", revert=True)
        record["reply"] = reply
        self._finish(record, "stopped")

    def _push_undo(self, record: dict, run: _Run, suffix: str = "") -> bool:
        """One undo step for this command, if the scene changed since it began."""
        request = {"cmd": "undo_push", "message": undo_name(record["refined"], suffix), "only_if_changed": True}
        reply = self._host_op(request, run)
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
            except Exception as exc:  # noqa: BLE001 - host events only reach us through this loop
                print(f"GazeVoice: polling {self.host_label} failed: {exc!r}", file=sys.stderr)
                run.stop.wait(0.5)
                continue
            run.stop.wait(POLL_SECONDS)

    def _poll_once(self, run: _Run, ack: int, boot: str | None) -> tuple[int, str | None]:
        bridge = self._bridge
        if bridge is None:
            return ack, boot
        try:
            reply = bridge.request({"cmd": "poll", "ack": ack, "boot": boot})
        except TimeoutError:  # host is busy; its events wait for the next poll
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
            elif event.get("type") == "pointer":
                self._pointer(event)
        return ack, boot

    def _pointer(self, event: dict) -> None:
        """The mouse stands in for gaze. An odd event is skipped: this runs inside the poll loop."""
        gaze = self._gaze
        if not isinstance(gaze, PointerTracker):
            return
        try:
            t, u, v = (float(event[key]) for key in ("t", "u", "v"))
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if math.isfinite(t) and math.isfinite(u) and math.isfinite(v):
            gaze.feed(u, v, t)

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
        prompt = vocabulary_prompt(record["names"], self.host) if self.engine == "whisper" else None
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
        elif self._answer_choice(record):  # a number only means something while numbers are on screen
            return
        record["stage"] = "queued"
        self._update(record)
        run.edit_q.put(record)

    def _answer_choice(self, record: dict) -> bool:
        """Give a spoken number to the command waiting for one. False when none is waiting: it's an ordinary command."""
        number = choice_number(record["heard"])
        if number is None:
            return False
        with self._lock:
            choice = self._choice
            if choice is None or choice.answered.is_set():
                return False
            count = len(choice.names)
            picked = 1 <= number <= count
            record["refined"] = f"Number {number}"
            record["reply"] = f"Picked {choice.names[number - 1]}." if picked else f"Say a number from 1 to {count}."
            if picked:
                choice.number = number
                choice.answered.set()
        if not picked:  # numbers stay on screen until the wait runs out
            self._status(record["reply"], "working")
        self._finish(record, "done")
        return True

    def _end_choice(self) -> None:
        """Stop waiting for a number: whoever is waiting goes on without one."""
        with self._lock:
            choice = self._choice
        if choice is not None:
            choice.answered.set()

    def _interrupt_current(self, skip: dict | None = None) -> dict | None:
        """Stop the command being understood or edited. Returns it, or None if nothing was running."""
        with self._lock:
            current = self._current
        if current is None or current is skip:
            return None
        current["_stop"] = True
        self._end_choice()  # after _stop is set, so a command about to ask sees it
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
        reply = self._host_op({"cmd": kind}, run)
        if reply and reply.get("ok"):
            record["reply"] = "Undid the last change." if kind == "undo" else "Redid the change."
            self._done(record, "↶ Undid the last change" if kind == "undo" else "↷ Redid the change")
        elif reply and reply.get("error") == "busy":
            self._fail(record, f"{self.host_label} is busy. Finish what you're doing, then say {kind} again.")
        else:
            self._fail(record, f"{self.host_label} couldn't {kind}.")

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

    # --- gaze ---------------------------------------------------------------------------------------
    @staticmethod
    def _gaze_hint(record: dict) -> str:
        return f" · {record['_gaze_note']}" if record["_gaze_note"] else ""

    def _pick(self, points: list[list[float]]) -> list | None:
        """What is under each point of the viewport, or None when Blender couldn't say."""
        reply = self._bridge_call({"cmd": "gaze_pick", "points": points})
        hits = reply.get("hits") if reply and reply.get("ok") else None
        return hits if isinstance(hits, list) else None

    def _piece(self, candidate) -> dict | None:
        """Which part of the model the best guess is, or None when Blender couldn't say."""
        if len(candidate.point) != 3:
            return None
        reply = self._bridge_call({"cmd": "gaze_piece", "uid": candidate.uid, "point": candidate.point})
        if not reply or not reply.get("ok"):
            return None
        return {k: v for k, v in reply.items() if k != "ok"}

    def _hide_gaze(self) -> None:
        """Take everything gaze drew (the dot, the ring and the numbers) off the viewport."""
        self._bridge_call({"cmd": "gaze_show", "dot": None, "ring": None, "marks": []})

    def _ask_which(self, record: dict, grounding: Grounding, run: _Run) -> Candidate | None:
        """Number the first target's guesses in the viewport and wait for the person to say which one.

        Returns the guess they picked. Returns None when they didn't: nothing said within `choice_seconds`, "stop",
        voice turning off, or Blender unable to draw the numbers. The numbers always come down again.
        """
        candidates = grounding.targets[0].candidates[:MAX_CANDIDATES]
        marks = [{"n": n, "u": c.u, "v": c.v, "name": c.name} for n, c in enumerate(candidates, 1)]
        choice = _Choice([c.name for c in candidates])
        self._stage("refining", record)  # the command is still being understood
        with self._lock:
            self._choice = choice
        try:
            if record["_stop"] or run.stop.is_set():  # looked at after the choice is open, so a stop can't slip between
                return None
            shown = self._bridge_call({"cmd": "gaze_show", "dot": None, "ring": None, "marks": marks})
            if not (shown and shown.get("ok")):
                return None  # nobody can answer numbers they can't see
            self._status(WHICH_ONE, "working")
            choice.answered.wait(self.choice_seconds)
            if choice.number is None or record["_stop"] or run.stop.is_set():
                return None
            return candidates[choice.number - 1]
        finally:
            with self._lock:
                self._choice = None
            self._hide_gaze()

    def _settle(self, record: dict, grounding: Grounding, run: _Run) -> bool:
        """Ask which one for each target that isn't clear, in order. False if one went unanswered.

        The guess the person picked moves to the front of its target, with the part of the model looked at closer.
        """
        for target in grounding.targets:
            if is_clear(target):
                continue
            picked = self._ask_which(record, Grounding("clarify", [target], grounding.radius), run)
            if picked is None:
                return False
            if target.candidates[0] is not picked:  # the first guess has been looked at already
                target.candidates.remove(picked)
                target.candidates.insert(0, picked)
                target.piece = self._piece(picked)
        grounding.status = "resolved"
        return True

    @staticmethod
    def _matches_selection(gaze: dict, focus: dict | None) -> bool | None:
        """Whether the first pointing word's best guess is selected. None when there is nothing to compare."""
        selected = (focus or {}).get("selected") or []
        if not selected or not gaze["targets"]:
            return None
        return gaze["targets"][0]["candidates"][0]["name"] in selected

    def _viewport_changed(self, gaze) -> bool:
        """Whether Blender's viewport is a different size now from the one the camera was calibrated on.

        Asked again for each command, because the person can resize the viewport at any time. False when there is
        nothing to compare (the mouse), or when Blender can't say: ground() copes with that the way it always has.
        """
        saved = getattr(gaze, "view", None)
        if saved is None:
            return False
        reply = self._bridge_call({"cmd": "gaze_view"})
        live = viewport_size(reply) if reply and reply.get("ok") else None
        return live is not None and live != saved

    def _crop_result(self, deadline: float, run: _Run, cancelled: Callable[[], bool]) -> str | None:
        """The picture Blender made, as base64, once it is ready. None on any error, or at the deadline or a stop."""
        while not cancelled():
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            reply = self._bridge_call({"cmd": "gaze_crop_result"}, timeout=max(0.2, min(BRIDGE_REPLY_SECONDS, left)))
            if not (reply and reply.get("ok")):
                return None
            if not reply.get("pending"):
                png = reply.get("png")
                return png if isinstance(png, str) and png else None
            run.stop.wait(CROP_POLL_SECONDS)
        return None

    def _picture_of(
        self, target: Target, radius: tuple[float, float], pictures, deadline: float, run: _Run,
        cancelled: Callable[[], bool],
    ) -> str | None:
        """One sentence about what is inside the ring around where the person looked, or None.

        Blender crops its viewport to the ring, from memory, and hands the picture over once. It goes straight to the
        describer and is not kept anywhere here: not in the record, the target, or the bridge client.
        """
        ru, rv = float(radius[0]), float(radius[1])
        u, v = target.fixation.u, target.fixation.v
        if not all(math.isfinite(x) and x > 0 for x in (ru, rv)) or not all(math.isfinite(x) for x in (u, v)):
            return None
        ring = [min(1.0, ru), min(1.0, rv)]
        started = self._bridge_call({"cmd": "gaze_crop", "u": min(1.0, max(0.0, u)), "v": min(1.0, max(0.0, v)),
                                     "radius": ring, "size": PICTURE_SIZE})
        if not (started and started.get("ok") and started.get("pending")):
            return None  # an add-on from before pictures says "unknown command", which is no picture and no error
        png = self._crop_result(deadline, run, cancelled)
        left = deadline - time.monotonic()
        if png is None or left <= 0 or cancelled():
            return None
        return clean_description(pictures.describe(png, target.word, target.candidates[0].name, timeout=left,
                                                   cancelled=cancelled))

    def _look_closer(self, record: dict, grounding: Grounding, run: _Run) -> None:
        """For each settled target that is a big piece of mesh with no name, put a sentence from a picture in `picture`.

        All of it, the crops and the describers, shares one PICTURE_SECONDS. Nothing here can stop a command: pictures
        off, a check that hasn't passed for this Claude Code, an add-on that can't crop, an error, a describer that is
        slow or says "unclear", all leave `picture` None and the command goes on with the text it has. A stop, or voice
        turning off, ends the wait at once and kills the describer. Whether pictures may be used is only read, from
        what the picture part last found out (see Factories.pictures), so nothing about it can hold the command up.
        """
        pictures = self._pictures
        wanted = [t for t in grounding.targets if needs_picture(t)]
        if pictures is None or not wanted:
            return

        def cancelled() -> bool:
            return bool(record["_stop"] or run.stop.is_set())

        try:
            if cancelled() or not pictures.ready():
                return
        except Exception:  # noqa: BLE001 - not being able to tell is the same as not allowed
            print(NO_PICTURE, file=sys.stderr)
            return
        began = time.monotonic()
        deadline = began + PICTURE_SECONDS
        self._stage("refining", record)  # the command is still being understood
        self._status(LOOKING_CLOSER, "working")
        try:
            for target in wanted:
                if cancelled() or time.monotonic() >= deadline:
                    break
                target.picture = self._picture_of(target, grounding.radius, pictures, deadline, run, cancelled)
        except Exception:  # noqa: BLE001 - whatever went wrong, the person's command is not the place to say so
            print(NO_PICTURE, file=sys.stderr)
        finally:
            record["timings"]["picture"] = round(time.monotonic() - began, 2)

    def _ground(self, record: dict, run: _Run) -> bool:
        """Work out what the pointing words in a spoken command refer to, from where the person was looking.

        The answer goes in record["gaze"]. In "on" mode it also joins the focus the brains read, and guesses too close
        to call are put to the person first (see _ask_which). Nothing here can stop a command: if gaze is lost or
        breaks, or nobody says which one, the command runs on the selection. The one exception is that last case with
        nothing selected. There is nothing to run it on, so the command fails and this returns False. A camera that was
        calibrated on a differently sized viewport is treated like lost gaze, and the status line says to calibrate again.
        """
        gaze = self._gaze  # there is none when gaze is off, can't start, or has no camera part yet
        if gaze is None:
            return True
        if self._viewport_changed(gaze):  # for every command: the person can resize the viewport at any moment
            record["gaze"] = {"status": "none", "targets": [], "radius": [], "matched_selection": None, "asked": False,
                              "viewport_changed": True}
            record["_gaze_note"] = VIEW_CHANGED
            return True
        words, clip_start = record["words"], record["_t0"] - PREROLL  # the clip _transcribe cut starts here
        if not pointing_words(words, clip_start):
            return True
        asked = settled = False
        try:
            grounding = ground(words, clip_start, gaze.track, gaze.radius, self._pick, self._piece)
            if self.gaze_mode == "on" and grounding.status == "clarify":
                asked, settled = True, self._settle(record, grounding, run)
            if self.gaze_mode == "on" and grounding.status == "resolved":  # watching only never takes a picture
                self._look_closer(record, grounding, run)
            found = grounding.to_dict()
        except Exception as exc:  # noqa: BLE001 - a dead socket or odd data from the host must not stop voice
            print(f"GazeVoice: gaze failed: {exc!r}", file=sys.stderr)
            found = {"status": "none", "targets": [], "radius": [], "error": repr(exc)}
        matched = self._matches_selection(found, record["focus"])
        record["gaze"] = {**found, "matched_selection": matched, "asked": asked}
        if matched is not None:
            with self._lock:
                self._gaze_checked += 1
                self._gaze_matched += matched
        if self.gaze_mode != "on":
            return True
        if not found["targets"]:
            record["_gaze_note"] = GAZE_LOST
            return True
        if record["_stop"] or run.stop.is_set():  # "stop" was said while asking; _edit ends the command
            return True
        if asked and not settled:
            if not (record["focus"] or {}).get("selected"):
                self._fail(record, COULDNT_TELL)
                return False
            record["_gaze_note"] = NO_ANSWER
            return True
        # The brains read how long into the command each word was said, not the clock time.
        targets = [{**t, "at": t["at"] - clip_start} for t in found["targets"]]
        record["focus"] = {**(record["focus"] or {}), "gaze": {**found, "targets": targets}}
        return True

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
            if not self._ground(record, run):  # it asked which one, got no answer, and had no selection to fall back on
                return
            if record["_stop"] or run.stop.is_set():  # "stop" was said while Blender was being asked
                self._stopped(record, "Stopped.", run)
                return
            self._stage("refining", record)
            self._status(f"Understanding…{self._gaze_hint(record)}", "working")
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
                record["reply"] = f"That didn't sound like a {self.host_label} command, so nothing changed."
                self._done(record, record["reply"])
                return
        else:
            record["refined"] = record["prompt"] = record["heard"]
            record["kind"] = "command"
        if record["_stop"] or run.stop.is_set():
            self._stopped(record, "Stopped.", run)
            return
        self._stage("editing", record)
        self._status(f"Working: {record['refined']}{self._gaze_hint(record)}", "working")
        self._bridge_call({"cmd": "begin_edit"})  # from here, the host tracks changes for undo

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
            if self.host == "unity" or changes_scene(record["tools"]):
                self._push_undo(record, run, " (failed)")
            self._fail(record, str(exc))
            return
        record["timings"]["edit"] = round(time.monotonic() - began, 2)
        if turn.interrupted or (turn.is_error and record["_stop"]):
            self._stopped(record, "Stopped.", run)
            return
        record["reply"] = turn.text
        failed = turn.is_error or bool(_COULDNT.match(turn.text))
        if self.host == "unity" or changes_scene(turn.tools):
            self._push_undo(record, run, " (failed)" if failed else "")
        if failed:
            self._fail(record, turn.text or "The edit didn't finish.")
            return
        self._done(record, f"✓ {turn.text or 'Done'}")
