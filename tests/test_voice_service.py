import contextlib
import io
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice import gaze_worker
from gazevoice.components.base import CameraClaim
from gazevoice.events import EventBus
from gazevoice.voice.audio import MicError, RingBuffer
from gazevoice.voice.brains import BrainError, TurnResult
from gazevoice.paths import AppPaths
from gazevoice.runner import FakeRunner
from gazevoice.voice.gaze import GazeSample, GazeTrack
from gazevoice.voice.gaze_tracker import GazeTracker
from gazevoice.voice.leftovers import make_noise_picture
from gazevoice.voice.picture import Describer, clear_leftover_images, write_picture_check
from gazevoice.voice.service import (
    CHOICE_LIMIT_SECONDS,
    CHOICE_SECONDS,
    Factories,
    PointerTracker,
    VoiceError,
    VoiceService,
)
from gazevoice.voice.stt import SpeechError


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


FAKE_CLAUDE = [sys.executable, str(Path(__file__).parent / "fixtures" / "fake_claude.py")]
EAGLE = {"uid": 116, "name": "Eagle", "faces": 5120, "point": [0.41, 0.12, 0.73]}
PERCH = {"uid": 117, "name": "Perch", "faces": 40, "point": [0.5, 0.1, 0.2]}
WING_PIECE = {"face": 3120, "part": "Wing.L", "piece_faces": 480, "object_faces": 5120}
BODY_PIECE = {"face": 3120, "part": None, "piece_faces": 4000, "object_faces": 5120}  # big, and nothing names it


class FakeBridge:
    """Speaks the bridge protocol the way the add-on does (see blender_addon/gazevoice_bridge/protocol.py)."""

    def __init__(self):
        self.statuses, self.pushed, self.calls = [], [], []
        self.undos = self.redos = 0
        self.down = False  # Blender is closed: connections are refused
        self.frozen = False  # Blender is busy (a heavy edit): requests time out
        self.busy = 0  # undo operations answer "busy" this many more times
        self.changed = True  # whether the scene changed since begin_edit
        self.poll_error: Exception | None = None  # one poll raises this
        self.key = "⌥ Space"
        self.boot = "boot-1"
        self.selected = ["Chair"]  # what Blender says is selected
        self.selection_unreadable = False  # Blender can't report a selection
        self.pick_reply = lambda points: [EAGLE] * len(points)  # what is under each gaze ray; None = no 3D viewport
        self.piece_reply = WING_PIECE  # what gaze_piece says about the top guess; None = no such mesh object
        self.gaze_error: Exception | None = None  # every gaze request raises this (a dead socket)
        self.view_reply = {"ok": True, "width": 1840, "height": 1100}  # what gaze_view says the viewport is
        self.picks, self.pieces, self.shows = [], [], []
        # gaze_crop: what it answers (an add-on from before pictures knows no such command), the picture the next
        # crop will make (None: it fails), and how many more times gaze_crop_result says "pending" first.
        self.crop_start = {"ok": False, "error": "unknown command"}
        self.crop_png = None
        self.crop_pending = 0
        self.crops, self.crop_reads, self._crop = [], 0, None
        self.lock = threading.Lock()
        self._events: list[dict] = []
        self._seq = 0

    def add(self, event_type, **fields):
        with self.lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": event_type, **fields})

    def request(self, req, timeout=None):
        if self.down:
            raise ConnectionRefusedError("Blender is closed")
        if self.frozen:
            raise TimeoutError("timed out")
        cmd = req["cmd"]
        self.calls.append(cmd)
        if cmd == "ping":
            return {"ok": True, "bridge": "0.2.0", "blender": "5.2.2", "key": self.key}
        if cmd == "poll":
            if self.poll_error is not None:
                error, self.poll_error = self.poll_error, None
                raise error
            with self.lock:
                ack = req.get("ack")
                if ack is not None and req.get("boot") not in (None, self.boot):
                    ack = 0
                if ack is None:
                    events, self._events = self._events, []
                else:
                    self._events = [e for e in self._events if e["seq"] > ack]
                    events = [dict(e) for e in self._events]
            return {"ok": True, "boot": self.boot, "events": events}
        if cmd == "selection":
            if self.selection_unreadable:
                return {"ok": False}
            active = self.selected[0] if self.selected else None
            return {"ok": True, "active": active, "selected": list(self.selected), "mode": "OBJECT"}
        if cmd == "scene_names":
            return {"ok": True, "names": ["Chair", "Table"]}
        if cmd == "status":
            self.statuses.append((req["text"], req.get("state")))
            return {"ok": True}
        if cmd == "begin_edit":
            return {"ok": True}
        if cmd.startswith("gaze_"):
            return self.gaze(cmd, req)
        if cmd in ("undo_push", "undo", "redo") and self.busy:
            self.busy -= 1
            return {"ok": False, "error": "busy"}
        if cmd == "undo_push":
            if req.get("only_if_changed") and not self.changed:
                return {"ok": True, "pushed": False}
            self.pushed.append(req["message"])
            return {"ok": True, "pushed": True}
        if cmd in ("undo", "redo"):
            setattr(self, cmd + "s", getattr(self, cmd + "s") + 1)
            return {"ok": True}
        return {"ok": False}

    def gaze(self, cmd, req):
        if self.gaze_error is not None:
            raise self.gaze_error
        if cmd == "gaze_view":
            return self.view_reply
        if cmd == "gaze_pick":
            self.picks.append(req["points"])
            if self.pick_reply is None:
                return {"ok": False, "error": "no 3D viewport"}
            return {"ok": True, "hits": self.pick_reply(req["points"])}
        if cmd == "gaze_piece":
            self.pieces.append(req)
            if self.piece_reply is None:
                return {"ok": False, "error": "no such mesh object"}
            return {"ok": True, **self.piece_reply}
        if cmd == "gaze_show":
            self.shows.append(req)
            return {"ok": True}
        if cmd == "gaze_crop":
            self.crops.append(req)
            self._crop = self.crop_png if self.crop_start.get("ok") else None
            return self.crop_start
        if cmd == "gaze_crop_result":
            self.crop_reads += 1
            if self.crop_pending > 0:
                self.crop_pending -= 1
                return {"ok": True, "pending": True}
            if self._crop is None:
                return {"ok": False, "error": "no picture is waiting"}
            png, self._crop = self._crop, None  # handed over once
            return {"ok": True, "png": png}
        return {"ok": False, "error": "unknown command"}

    def close(self):
        pass


class FakeMic:
    def __init__(self, fail=False, silent=False):
        self.fail = fail
        self.silent = silent
        self.started = False
        self.starts = 0
        self.buffer = RingBuffer()

    @property
    def alive(self):
        return self.started

    def start(self):
        if self.fail:
            raise MicError("The microphone didn't start. Check System Settings › Privacy & Security › Microphone.")
        self.started = True
        self.starts += 1
        tone = b"".join(struct.pack("<h", 0 if self.silent else int(8000 * math.sin(i / 3))) for i in range(1600))
        for i in range(100):  # 10 s of audio, t = 1000.1 … 1010.0
            self.buffer.append(tone, 1000.1 + i * 0.1)

    def heard_only_silence(self, min_seconds=1.0):
        return self.silent

    def stop(self):
        self.started = False


class FakeStt:
    def __init__(self, texts, block=None, words=None):
        self.texts = list(texts)
        self.block = block  # start() waits for this (a slow model load)
        self.words = words or []  # word timings, seconds into the clip, given with every transcript
        self.calls = []
        self.alive = True
        self.stopped = False

    def start(self):
        if self.block is not None:
            self.block.wait(5)
            if self.stopped:
                raise SpeechError("Speech-to-text stopped (exit -15)")
        return 0.1

    def transcribe(self, audio, prompt=None):
        self.calls.append((audio, prompt))
        return {"text": self.texts.pop(0) if self.texts else "", "words": list(self.words), "seconds": 0.1}

    def stop(self):
        self.alive = False
        self.stopped = True
        if self.block is not None:
            self.block.set()


class FakeSession:
    def __init__(self, replies=(), block=None, tools_first=(), die_on_interrupt=False, stay_alive=False):
        self.stay_alive = stay_alive  # an error leaves the process running (say, a timed-out turn)
        self.replies = list(replies)
        self.block = block
        self.tools_first = list(tools_first)
        self.die_on_interrupt = die_on_interrupt
        self.sent = []
        self.timeouts = []
        self.interrupted = False
        self.closed = False
        self._alive = True

    @property
    def alive(self):
        return self._alive

    def start(self, **_):
        pass

    def send(self, text, on_tool=None, timeout=180):
        self.sent.append(text)
        self.timeouts.append(timeout)
        if self.block is not None:
            for tool in self.tools_first:
                if on_tool:
                    on_tool(tool)
            self.block.wait(5)
            if self.interrupted:
                if self.die_on_interrupt:
                    self._alive = False
                    raise BrainError("The brain stopped mid-command (exit 0): no output")
                return TurnResult("", True, list(self.tools_first), interrupted=True)
        reply = self.replies.pop(0) if self.replies else TurnResult("ok", False)
        if isinstance(reply, Exception):
            self._alive = self.stay_alive
            raise reply
        for tool in reply.tools:
            if on_tool:
                on_tool(tool)
        return reply

    def interrupt(self):
        self.interrupted = True
        if self.block is not None:
            self.block.set()

    def close(self):
        self.closed = True


class FakeGaze:
    """A gaze part (a camera, or the mouse). Tests drop samples into its track with look()."""

    def __init__(self, fail=False, radius=(0.10, 0.15), view=None):
        self.fail = fail
        self.radius = radius
        self.view = view  # the viewport size it was calibrated on; None for the mouse, which needs no calibration
        self.alive = False
        self.starts = self.stops = 0
        self.track_reads = 0
        self._track = GazeTrack()

    @property
    def track(self):
        self.track_reads += 1  # the service looked at the gaze
        return self._track

    def start(self):
        if self.fail:
            raise RuntimeError("the camera is busy")
        self.alive = True
        self.starts += 1

    def stop(self):
        self.alive = False
        self.stops += 1

    def look(self, u, v, start=1002.5, count=8, confidence=0.9):
        for i in range(count):
            self._track.add(GazeSample(start + i * 0.05, u, v, confidence))


class FakePictures:
    """The service's picture part, without a process: ready() says whether pictures may be used, describe() answers."""

    def __init__(self, reply="the left wing tip", ready=True):
        self.reply = reply  # a sentence, None, an Exception to raise, or a function of the call
        self.is_ready = ready
        self.ready_calls = self.refreshes = 0
        self.calls = []

    def refresh(self):
        self.refreshes += 1

    def ready(self):
        self.ready_calls += 1
        return self.is_ready

    def describe(self, png_base64, word, object_name, *, timeout, cancelled=None):
        self.calls.append({"png": png_base64, "word": word, "object": object_name, "timeout": timeout,
                           "cancelled": cancelled})
        reply = self.reply(self.calls[-1]) if callable(self.reply) else self.reply
        if isinstance(reply, list):
            reply = reply[len(self.calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


REFINED = json.dumps({"kind": "command", "text": "Make this twice as tall."})


class ReloadingBridge(FakeBridge):
    """Turns away the first undo_push attempts, the way Unity does while it reloads scripts."""

    def __init__(self):
        super().__init__()
        self.refusals = 3

    def request(self, req, timeout=None):
        if req["cmd"] == "undo_push" and self.refusals:
            self.refusals -= 1
            raise ConnectionRefusedError("reloading")
        return super().request(req, timeout)


class VoiceTestBase(unittest.TestCase):
    """A service wired to fakes; tests swap in different fakes before build()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = FakeBridge()
        self.mic = FakeMic()
        self.stt = FakeStt(["uh make this like twice as tall"])
        self.refiner = FakeSession([TurnResult(REFINED, False)])
        self.editor = FakeSession([TurnResult("Made Chair twice as tall.", False, ["execute_blender_code"])])
        self.editors_made = 0
        self.engine = "parakeet"
        self.gaze = None  # set a gaze part before build() to give the service one
        self.gazes_made = 0
        self.pictures = None  # set a picture part before build() to give the service one
        self.pictures_made = 0

    def tearDown(self):
        if hasattr(self, "service"):
            self.service.stop()
        self.tmp.cleanup()

    def make_editor(self):
        self.editors_made += 1
        return self.editor if self.editors_made == 1 else self.next_editor

    def make_gaze(self):
        self.gazes_made += 1
        return self.gaze

    def make_pictures(self):
        self.pictures_made += 1
        return self.pictures

    def build(self, **kwargs):
        factories = Factories(
            bridge=lambda: self.bridge, mic=lambda: self.mic, stt=lambda: self.stt,
            refiner=lambda: self.refiner, editor=self.make_editor,
            gaze=self.make_gaze if self.gaze is not None else None,
            pictures=self.make_pictures if self.pictures is not None else None,
        )
        # The fake mic's audio is stamped 1000.1–1010 and speak() presses at 1002, so the service's
        # clock starts there too; on the real clock every hold would look decades long.
        began = time.monotonic()
        kwargs.setdefault("clock", lambda: 1001.0 + (time.monotonic() - began))
        self.service = VoiceService(Path(self.tmp.name), EventBus(), factories, engine=self.engine, **kwargs)
        return self.service

    def last_record(self):
        return self.service.recent[-1]

    def speak(self, down=1002.0, up=1003.0):
        self.service.ptt("down", down)
        self.service.ptt("up", up)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] in ("done", "failed", "stopped"))
        return self.last_record()

    def record(self, public):
        return next(r for r in self.service.recent if r["id"] == public["id"])


class UnityReloadTest(VoiceTestBase):
    def test_unity_reloading_scripts_still_leaves_one_undo_step(self):
        self.bridge = ReloadingBridge()
        self.build(host="unity").start()
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertTrue(record["undo_step"])
        self.assertEqual(len(self.bridge.pushed), 1)

    def test_blender_going_away_doesnt_make_it_wait(self):
        self.bridge = ReloadingBridge()
        self.build().start()
        record = self.speak()
        self.assertFalse(record["undo_step"])
        self.assertEqual(self.bridge.refusals, 2)

    def test_unity_gets_longer_to_finish_compiling(self):
        self.assertGreater(self.build(host="unity").busy_wait, self.build().busy_wait)


class VoiceServiceTest(VoiceTestBase):
    def test_happy_path(self):
        self.build().start()
        self.assertEqual(self.service.state, "ready")
        record = self.speak()
        self.assertEqual(record["stage"], "done", record.get("error"))
        self.assertEqual(record["heard"], "uh make this like twice as tall")
        self.assertEqual(record["refined"], "Make this twice as tall.")
        self.assertEqual(record["focus"]["selected"], ["Chair"])
        self.assertEqual(record["tools"], ["execute_blender_code"])
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make this twice as tall."])
        self.assertTrue(record["undo_step"])
        self.assertIn("Command: Make this twice as tall.", self.editor.sent[0])
        self.assertIn('"heard": "uh make this like twice as tall"', self.refiner.sent[0])
        self.assertIsNone(self.stt.calls[0][1])  # Parakeet takes no vocabulary prompt
        self.assertTrue({"stt", "refine", "edit", "total"} <= set(record["timings"]))
        folder = Path(self.tmp.name) / "commands" / record["id"]
        self.assertTrue((folder / "audio.wav").is_file())
        self.assertEqual(json.loads((folder / "record.json").read_text())["reply"], "Made Chair twice as tall.")
        texts = [text for text, _ in self.bridge.statuses]
        self.assertIn("Listening… release the key to send", texts)
        self.assertIn("✓ Made Chair twice as tall.", texts)

    def test_the_record_says_which_model_and_thinking_did_the_edit(self):
        self.editor.init = {"model": "claude-opus-5-5"}
        self.editor.effort = "max"
        self.build().start()
        record = self.speak()
        self.assertEqual((record["model"], record["effort"], record["provider"]), ("claude-opus-5-5", "max", None))

    def test_the_record_names_the_api_provider(self):
        self.editor.init = {"model": "deepseek-v4-pro", "provider": "DeepSeek"}
        self.build().start()
        self.assertEqual(self.speak()["provider"], "DeepSeek")

    def test_a_build_request_sends_the_detailed_prompt(self):
        brief = "Model a bird with realistic proportions, layered wing feathers, a fanned tail and smooth shading."
        self.refiner = FakeSession([TurnResult(json.dumps({"kind": "command", "title": "Make a bird model.", "text": brief}), False)])
        self.build().start()
        record = self.speak()
        self.assertEqual((record["refined"], record["prompt"]), ("Make a bird model.", brief))
        self.assertIn(f"Command: {brief}", self.editor.sent[0])
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make a bird model."])
        self.assertIn(("Working: Make a bird model.", "working"), self.bridge.statuses)

    def test_typed_commands_go_as_typed(self):
        self.build().start()
        record = self.service.say("add a cube")
        wait_for(lambda: self.record(record)["stage"] == "done")
        self.assertEqual(self.record(record)["prompt"], "add a cube")

    def test_an_edit_gets_ten_minutes(self):
        self.build().start()
        self.speak()
        self.assertEqual(self.editor.timeouts, [600])

    def test_events_from_blender_drive_hold_to_talk(self):
        self.build().start()
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make this twice as tall."])

    def test_spoken_undo_skips_the_brains(self):
        self.stt.texts = ["Undo that."]
        self.build().start()
        record = self.speak()
        self.assertEqual((record["kind"], record["stage"]), ("undo", "done"))
        self.assertEqual(self.bridge.undos, 1)
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))

    def test_stop_interrupts_the_edit_and_clears_the_queue(self):
        self.editor = FakeSession(block=threading.Event())
        self.build().start()
        running = self.service.say("make it red")
        wait_for(lambda: len(self.editor.sent) == 1)
        queued = self.service.say("make it blue")
        stop = self.service.say("stop")
        wait_for(lambda: self.record(running)["stage"] == "stopped")
        self.assertTrue(self.editor.interrupted)
        self.assertIsNone(self.record(running)["error"])
        self.assertEqual(self.record(running)["reply"], "Stopped.")
        self.assertEqual((self.record(queued)["stage"], self.record(queued)["reply"]),
                         ("stopped", "Stopped before it started."))
        self.assertEqual(self.record(stop)["stage"], "done")
        self.assertEqual(self.bridge.pushed, [])  # nothing changed, so no undo step
        self.assertEqual(len(self.editor.sent), 1)
        wait_for(lambda: self.service.state == "ready")

    def test_stop_after_tools_ran_leaves_one_undo_step(self):
        self.editor = FakeSession(block=threading.Event(), tools_first=["execute_blender_code"])
        self.build().start()
        running = self.service.say("make it red")
        wait_for(lambda: self.record(running)["tools"] == ["execute_blender_code"])
        self.service.say("stop")
        wait_for(lambda: self.record(running)["stage"] == "stopped")
        self.assertEqual(self.bridge.pushed, ["GazeVoice: make it red (stopped)"])
        self.assertTrue(self.record(running)["undo_step"])
        self.assertIn("Undo", self.record(running)["reply"])

    def test_stop_while_understanding_never_reaches_the_editor(self):
        self.refiner = FakeSession(block=threading.Event())
        self.build().start()
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        wait_for(lambda: len(self.refiner.sent) == 1)
        spoken = self.last_record()
        self.service.say("stop")
        wait_for(lambda: self.record(spoken)["stage"] == "stopped")
        self.assertTrue(self.refiner.interrupted)
        self.assertEqual(self.editor.sent, [])

    def test_brain_that_had_to_be_killed_by_stop_counts_as_stopped(self):
        self.editor = FakeSession(block=threading.Event(), die_on_interrupt=True)
        self.next_editor = FakeSession([TurnResult("Added a cube.", False, ["execute_blender_code"])])
        self.build().start()
        running = self.service.say("make it red")
        wait_for(lambda: len(self.editor.sent) == 1)
        self.service.say("stop")
        wait_for(lambda: self.record(running)["stage"] == "stopped")
        self.assertIsNone(self.record(running)["error"])
        after = self.service.say("add a cube")
        wait_for(lambda: self.record(after)["stage"] == "done")
        self.assertEqual(self.editors_made, 2)

    def test_stop_with_nothing_running_says_so(self):
        self.build().start()
        stop = self.service.say("stop")
        self.assertEqual(self.record(stop)["reply"], "Nothing was running.")
        self.assertIn(("Nothing was running.", "done"), self.bridge.statuses)

    def test_editor_error_is_reported_and_voice_stays_ready(self):
        self.editor = FakeSession([TurnResult("Couldn't find a chair.", True)])
        self.build().start()
        record = self.speak()
        self.assertEqual((record["stage"], record["error"]), ("failed", "Couldn't find a chair."))
        self.assertEqual(self.bridge.pushed, [])
        wait_for(lambda: self.service.state == "ready")

    def test_lookup_only_command_adds_no_undo_step(self):
        self.editor = FakeSession([TurnResult("Chair has 8 vertices.", False, ["get_object_detail_summary"])])
        self.build().start()
        record = self.speak()
        self.assertEqual((record["stage"], record["reply"]), ("done", "Chair has 8 vertices."))
        self.assertEqual(self.bridge.pushed, [])
        self.assertFalse(record["undo_step"])

    def test_editor_that_couldnt_do_it_reports_a_failure(self):
        self.editor = FakeSession([TurnResult("Couldn't find a lamp in this scene.", False, ["get_objects_summary"])])
        self.build().start()
        record = self.speak()
        self.assertEqual((record["stage"], record["error"]), ("failed", "Couldn't find a lamp in this scene."))
        self.assertEqual(self.bridge.pushed, [])
        self.assertIn(("✗ Couldn't find a lamp in this scene.", "error"), self.bridge.statuses)

    def test_curly_apostrophe_still_counts_as_a_failure(self):
        self.editor = FakeSession([TurnResult("Can’t bevel an empty, so nothing changed.", False)])
        self.build().start()
        self.assertEqual(self.speak()["stage"], "failed")

    def test_stop_after_only_lookups_adds_no_undo_step(self):
        self.editor = FakeSession(block=threading.Event(), tools_first=["get_objects_summary"])
        self.build().start()
        running = self.service.say("make it red")
        wait_for(lambda: self.record(running)["tools"] == ["get_objects_summary"])
        self.service.say("stop")
        wait_for(lambda: self.record(running)["stage"] == "stopped")
        self.assertEqual(self.bridge.pushed, [])
        self.assertEqual(self.record(running)["reply"], "Stopped.")

    def test_dead_brain_is_replaced_on_the_next_command(self):
        self.editor = FakeSession([BrainError("exit 3")])
        self.next_editor = FakeSession([TurnResult("Added a cube.", False, ["execute_blender_code"])])
        self.build().start()
        first = self.service.say("add a cube")
        wait_for(lambda: [r for r in self.service.recent if r["id"] == first["id"]][0]["stage"] == "failed")
        second = self.service.say("add a cube")
        wait_for(lambda: [r for r in self.service.recent if r["id"] == second["id"]][0]["stage"] == "done")
        self.assertEqual(self.editors_made, 2)

    def test_start_fails_cleanly_without_blender(self):
        self.bridge.down = True
        with self.assertRaises(VoiceError) as caught:
            self.build().start()
        self.assertIn("Blender isn't connected", str(caught.exception))
        self.assertEqual(self.service.state, "error")
        self.assertFalse(self.mic.started)

    def test_mic_failure_explains_permission_and_starts_nothing_else(self):
        self.mic = FakeMic(fail=True)
        with self.assertRaises(VoiceError) as caught:
            self.build().start()
        self.assertIn("Privacy & Security", str(caught.exception))
        self.assertEqual(self.stt.calls, [])

    def test_too_short_press_is_ignored(self):
        self.build().start()
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1002.1)
        time.sleep(0.2)
        self.assertEqual(list(self.service.recent), [])
        self.assertIn(("Too short: hold the key while you speak", "error"), self.bridge.statuses)

    def test_empty_transcript(self):
        self.stt.texts = [""]
        self.build().start()
        record = self.speak()
        self.assertEqual(record["stage"], "failed")
        self.assertIn("Didn't catch that", record["error"])

    def test_unusable_refiner_reply_falls_back_to_the_transcript(self):
        self.refiner = FakeSession([TurnResult("I think you want it taller.", False)])
        self.build().start()
        record = self.speak()
        self.assertTrue(record["fallback"])
        self.assertEqual(record["refiner_reply"], "I think you want it taller.")
        self.assertIn("Command: uh make this like twice as tall", self.editor.sent[0])

    def test_typed_command_skips_speech_and_refiner(self):
        self.build().start()
        record = self.service.say("add a cube")
        wait_for(lambda: [r for r in self.service.recent if r["id"] == record["id"]][0]["stage"] == "done")
        self.assertEqual((self.stt.calls, self.refiner.sent), ([], []))
        self.assertIn("Command: add a cube", self.editor.sent[0])

    def test_blender_disconnect_and_return(self):
        self.build().start()
        self.bridge.down = True
        wait_for(lambda: "isn't connected" in self.service.snapshot()["reason"])
        self.bridge.down = False
        wait_for(lambda: self.service.snapshot()["reason"] == "", timeout=4)
        self.assertTrue(self.service.snapshot()["blender_connected"])

    def test_whisper_gets_the_scene_vocabulary(self):
        self.engine = "whisper"
        self.build().start()
        self.speak()
        self.assertIn("Chair", self.stt.calls[0][1])

    def test_say_requires_voice_on(self):
        with self.assertRaises(VoiceError):
            self.build().say("add a cube")


class CameraSharingTest(VoiceTestBase):
    """Voice and gaze calibration both need the camera, so they take turns."""

    def setUp(self):
        super().setUp()
        self.camera = CameraClaim()
        self.next_editor = FakeSession()  # for a restart

    def test_voice_wont_start_while_gaze_is_using_the_camera(self):
        self.camera.take("gaze")
        with self.assertRaises(VoiceError) as caught:
            self.build(camera=self.camera).start()
        self.assertIn("calibrat", str(caught.exception))
        self.assertIn("camera", str(caught.exception))
        self.assertEqual(self.service.state, "off")  # nothing was started, and it can be tried again
        self.assertFalse(self.mic.started)
        self.assertEqual(self.camera.holder, "gaze")  # still gaze's
        self.camera.release("gaze")
        self.service.start()
        self.assertEqual(self.service.state, "ready")

    def test_voice_holds_the_camera_while_it_is_on_and_gives_it_back_when_it_stops(self):
        service = self.build(camera=self.camera)
        service.start()
        self.assertEqual(self.camera.holder, "voice")
        self.assertEqual(self.camera.take("gaze"), "voice")  # so calibrating is refused meanwhile
        service.stop()
        self.assertIsNone(self.camera.holder)
        service.start()
        self.assertEqual(self.camera.holder, "voice")

    def test_a_second_start_while_on_does_not_disturb_the_first(self):
        service = self.build(camera=self.camera)
        service.start()
        with self.assertRaises(VoiceError) as caught:
            service.start()
        self.assertIn("already on", str(caught.exception))
        self.assertEqual(self.camera.holder, "voice")

    def test_a_start_that_fails_gives_the_camera_back(self):
        self.mic = FakeMic(fail=True)
        with self.assertRaises(VoiceError):
            self.build(camera=self.camera).start()
        self.assertEqual(self.service.state, "error")
        self.assertIsNone(self.camera.holder)

    def test_stopping_while_starting_gives_the_camera_back(self):
        self.stt = FakeStt([], block=threading.Event())
        self.build(camera=self.camera)
        starter = threading.Thread(target=lambda: self.start_quietly())
        starter.start()
        wait_for(lambda: self.service.reason.startswith("Loading speech"))
        self.assertEqual(self.camera.holder, "voice")
        self.service.stop()
        starter.join(5)
        self.assertEqual(self.service.state, "off")
        self.assertIsNone(self.camera.holder)

    def start_quietly(self):
        try:
            self.service.start()
        except VoiceError:
            pass

    def test_a_gaze_that_cant_start_leaves_the_camera_free_to_calibrate(self):
        self.gaze = FakeGaze(fail=True)
        self.build(gaze_mode="on", gaze_source="camera", camera=self.camera).start()
        self.assertEqual(self.service.state, "ready")
        self.assertIsNone(self.camera.holder)  # or Calibrate would say voice is using a camera nothing has open
        self.assertIsNone(self.camera.take("gaze"))
        self.service.stop()
        self.assertEqual(self.camera.holder, "gaze")  # stopping voice leaves a Calibrate that started since alone

    def test_a_gaze_part_that_cant_be_built_leaves_the_camera_free_too(self):
        def no_camera():
            raise OSError("That camera isn't connected.")

        self.gaze = FakeGaze()
        self.make_gaze = no_camera
        self.build(gaze_mode="on", gaze_source="camera", camera=self.camera).start()
        self.assertEqual(self.service.state, "ready")
        self.assertIsNone(self.camera.holder)

    def test_stopping_after_a_failed_start_gives_nothing_back_twice(self):
        self.mic = FakeMic(fail=True)
        with self.assertRaises(VoiceError):
            self.build(camera=self.camera).start()
        self.camera.take("gaze")  # calibrating started once voice had failed
        self.service.stop()  # clearing the error must not take the camera from gaze
        self.assertEqual(self.camera.holder, "gaze")

    def test_without_a_claim_voice_starts_as_it_always_did(self):
        self.build().start()
        self.assertEqual(self.service.state, "ready")


class StopInTheGapTest(VoiceTestBase):
    """A part is known before it has started, so a stop can reach it while it has nothing to stop yet."""

    def stop_while_starting(self, in_the_gap, go_on, gone_from_the_service):
        """Start voice, stop it inside the gap, then let the part carry on starting."""
        errors = []

        def start():
            try:
                self.service.start()
            except VoiceError as exc:
                errors.append(str(exc))

        starter = threading.Thread(target=start)
        starter.start()
        self.assertTrue(in_the_gap.wait(5))
        stopper = threading.Thread(target=self.service.stop)
        stopper.start()
        wait_for(gone_from_the_service)  # the stop has been, and found nothing to stop yet
        go_on.set()
        starter.join(10)
        stopper.join(10)
        self.assertFalse(starter.is_alive() or stopper.is_alive())
        self.assertEqual(errors, ["Voice was turned off before it finished starting."])
        self.assertEqual(self.service.state, "off")

    def test_a_stop_before_the_gaze_worker_exists_leaves_no_worker(self):
        real_popen = subprocess.Popen
        in_the_gap, go_on = threading.Event(), threading.Event()

        def slow_popen(*args, **kwargs):
            in_the_gap.set()
            go_on.wait(5)
            return real_popen(*args, **kwargs)

        camera = CameraClaim()
        self.gaze = GazeTracker(Path(sys.executable), Path(gaze_worker.__file__), "0", extra_args=["--fake"])
        self.addCleanup(self.gaze.stop)  # a failing run mustn't leave the worker behind
        self.build(gaze_mode="on", gaze_source="camera", camera=camera)
        with patch("gazevoice.voice.gaze_tracker.subprocess.Popen", slow_popen):
            self.stop_while_starting(in_the_gap, go_on, lambda: self.service._gaze is None)
        self.assertIsNotNone(self.gaze.proc)  # it really did start, after the stop
        self.assertFalse(self.gaze.alive)
        self.assertRaises(ProcessLookupError, os.kill, self.gaze.proc.pid, 0)
        self.assertIsNone(camera.holder)

    def late_part(self, base):
        """A part whose start() goes on after a stop that came too early for it to notice."""
        in_the_gap, go_on = threading.Event(), threading.Event()

        class Late(base):
            def start(self):
                in_the_gap.set()
                go_on.wait(5)
                self.running = True
                return super().start()

            def stop(self):
                self.running = False
                super().stop()

        return Late, in_the_gap, go_on

    def test_a_stop_before_the_mic_has_started_leaves_no_mic_open(self):
        Late, in_the_gap, go_on = self.late_part(FakeMic)
        self.mic = Late()
        self.build()
        self.stop_while_starting(in_the_gap, go_on, lambda: self.service._mic is None)
        self.assertFalse(self.mic.running)

    def test_a_stop_before_the_speech_worker_has_started_leaves_no_worker(self):
        Late, in_the_gap, go_on = self.late_part(FakeStt)
        self.stt = Late([])
        self.build()
        self.stop_while_starting(in_the_gap, go_on, lambda: self.service._stt is None)
        self.assertFalse(self.stt.running)

    def test_every_part_is_ended_when_a_start_fails_and_ending_twice_does_no_harm(self):
        self.mic = FakeMic(silent=True)  # fails after the mic, the speech worker and both brains are up
        stops = []
        real_stop = self.stt.stop
        self.stt.stop = lambda: (stops.append("stt"), real_stop())
        with self.assertRaises(VoiceError):
            self.build().start()
        self.assertEqual(stops, ["stt", "stt"])  # by the teardown, and again from the run's own list
        self.assertFalse(self.mic.started)
        self.assertTrue(self.refiner.closed and self.editor.closed)
        self.assertEqual(self.service.state, "error")


class DeafSession(FakeSession):
    """Ignores interrupt(); its slow turn only ends when close() kills the process (a stuck Claude turn)."""

    def __init__(self):
        super().__init__()
        self.killed = threading.Event()
        self.threads = set()

    def send(self, text, on_tool=None, timeout=180):
        self.threads.add(threading.get_ident())
        self.sent.append(text)
        if "slow" in text:
            self.killed.wait(10)
            raise BrainError("The brain stopped mid-command (exit -9)")
        return TurnResult("Done.", False, ["execute_blender_code"])

    def interrupt(self):
        self.interrupted = True

    def close(self):
        self.closed = True
        self.killed.set()


class VoiceRobustnessTest(VoiceTestBase):
    """What the review of the voice loop found, kept fixed."""

    def start_quietly(self):
        try:
            self.service.start()
        except VoiceError as exc:
            self.start_error = str(exc)

    # --- a brain that failed or overran ------------------------------------------------------
    def test_a_brain_that_failed_mid_turn_is_replaced_even_if_still_running(self):
        self.editor = FakeSession([BrainError("The brain took too long to answer.")], stay_alive=True)
        self.next_editor = FakeSession([TurnResult("Added a cube.", False, ["execute_blender_code"])])
        self.build().start()
        first = self.service.say("add a cube")
        wait_for(lambda: self.record(first)["stage"] == "failed")
        self.assertEqual(self.record(first)["error"], "The brain took too long to answer.")
        self.assertTrue(self.editor.closed)
        second = self.service.say("add a cube")
        wait_for(lambda: self.record(second)["stage"] == "done")
        self.assertEqual((self.editors_made, self.record(second)["reply"]), (2, "Added a cube."))

    def test_a_change_made_before_a_failure_is_its_own_undo_step(self):
        self.editor = FakeSession([TurnResult("Couldn't finish the bevel.", False, ["execute_blender_code"])])
        self.build().start()
        record = self.speak()
        self.assertEqual(record["stage"], "failed")
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make this twice as tall. (failed)"])

    # --- starting and stopping -------------------------------------------------------------------
    def test_stop_while_starting_turns_voice_off_cleanly(self):
        self.stt = FakeStt([], block=threading.Event())
        self.build()
        starter = threading.Thread(target=self.start_quietly)
        starter.start()
        wait_for(lambda: self.service.reason.startswith("Loading speech"))
        self.service.stop()
        starter.join(5)
        self.assertEqual(self.service.state, "off")
        self.assertEqual(self.start_error, "Voice was turned off before it finished starting.")
        self.assertFalse(self.mic.started)
        self.assertTrue(self.stt.stopped)
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))
        self.stt = FakeStt(["uh make this like twice as tall"])
        self.service.start()
        self.assertEqual(self.speak()["stage"], "done")

    def test_stop_waits_for_the_loops_so_a_restart_has_one_editing_thread(self):
        editors = []

        def make():
            editors.append(DeafSession())
            return editors[-1]

        self.make_editor = make
        self.build().start()
        old_edit_thread = next(t for t in self.service._run.threads if t.name == "voice-edit")
        running = self.service.say("slow edit")
        wait_for(lambda: editors[0].sent)
        began = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - began, 5)
        self.assertFalse(old_edit_thread.is_alive())
        self.assertEqual(self.record(running)["stage"], "stopped")
        self.service.start()
        for i in range(3):
            self.service.say(f"edit {i}")
        wait_for(lambda: len(editors[-1].sent) == 3)
        self.assertEqual(len(editors[-1].threads), 1)

    def test_start_failure_leaves_nothing_running(self):
        class FailingStt(FakeStt):
            def start(self):
                raise SpeechError("Speech-to-text didn't load: out of memory")

        self.stt = FailingStt([])
        closed = []
        self.bridge.close = lambda: closed.append(True)
        with self.assertRaises(VoiceError):
            self.build().start()
        self.assertEqual(self.service.state, "error")
        self.assertTrue(self.stt.stopped)
        self.assertFalse(self.mic.started)
        self.assertEqual(closed, [True])

    def test_an_unexpected_start_error_is_reported_and_cleaned_up(self):
        class OddMic(FakeMic):
            def start(self):
                raise RuntimeError("device vanished")

        self.mic = OddMic()
        with self.assertRaises(VoiceError) as caught:
            self.build().start()
        self.assertIn("device vanished", str(caught.exception))
        self.assertEqual(self.service.state, "error")

    def test_a_mic_sending_pure_silence_stops_start_with_the_permission_help(self):
        self.mic = FakeMic(silent=True)
        with self.assertRaises(VoiceError) as caught:
            self.build().start()
        self.assertIn("pure silence", str(caught.exception))
        self.assertIn("Privacy & Security", str(caught.exception))
        self.assertFalse(self.mic.started)

    def test_a_silent_recording_explains_the_permission(self):
        self.build().start()
        silent = RingBuffer()
        for i in range(100):
            silent.append(b"\x00" * 3200, 1000.1 + i * 0.1)
        self.mic.buffer = silent
        record = self.speak()
        self.assertEqual(record["stage"], "failed")
        self.assertIn("pure silence", record["error"])
        self.assertEqual(self.stt.calls, [])

    # --- key events from Blender -------------------------------------------------------------------
    def test_key_presses_from_before_voice_was_on_are_dropped(self):
        self.bridge.add("ptt", state="down", t=900.0)
        self.bridge.add("ptt", state="up", t=902.0)
        self.bridge.add("toggle", t=950.0)
        self.bridge.add("toggle", t=953.0)
        self.build().start()
        time.sleep(0.4)
        self.assertEqual(list(self.service.recent), [])
        self.assertFalse([text for text, state in self.bridge.statuses if state == "error"])

    def test_events_are_acknowledged_and_handled_once(self):
        self.build().start()
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")
        time.sleep(0.3)
        self.assertEqual(len(self.service.recent), 1)
        wait_for(lambda: self.bridge._events == [])

    def test_a_busy_blender_is_not_disconnected_and_loses_no_key_press(self):
        self.build().start()
        self.bridge.frozen = True
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        time.sleep(0.3)
        self.assertTrue(self.service.snapshot()["blender_connected"])
        self.bridge.frozen = False
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")

    def test_a_blender_restart_resets_the_event_count(self):
        self.stt.texts.append("add a cube")
        self.build().start()
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")
        with self.bridge.lock:
            self.bridge.boot, self.bridge._seq, self.bridge._events = "boot-2", 0, []
        self.bridge.add("ptt", state="down", t=1004.0)
        self.bridge.add("ptt", state="up", t=1005.0)
        wait_for(lambda: len(self.service.recent) == 2 and self.last_record()["stage"] in ("done", "failed"))
        self.assertEqual(self.last_record()["heard"], "add a cube")

    def test_the_poll_loop_survives_a_surprise(self):
        self.build().start()
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            self.bridge.poll_error = RuntimeError("surprise")
            wait_for(lambda: "surprise" in logged.getvalue())
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")

    def test_holding_for_over_a_minute_cancels(self):
        now = [1002.0]
        self.build(clock=lambda: now[0]).start()
        self.service.ptt("down", 1002.0)
        now[0] += 61
        wait_for(lambda: not self.service.snapshot()["listening"])
        time.sleep(0.1)
        self.assertEqual(list(self.service.recent), [])
        self.assertIn(("Held for over a minute, so that was dropped.", "error"), self.bridge.statuses)

    def test_the_talk_button_says_click_again(self):
        self.build().start()
        self.bridge.add("toggle", t=1002.0)
        wait_for(lambda: ("Listening… click Talk again to send", "listening") in self.bridge.statuses)

    def test_the_key_blender_reports_is_the_one_in_the_hints(self):
        self.bridge.key = "F5"
        self.build().start()
        self.assertIn(("Ready · hold F5 to talk", "idle"), self.bridge.statuses)
        self.assertEqual(self.service.snapshot()["key"], "F5")

    def test_reconnecting_mid_edit_keeps_the_working_status(self):
        gate = threading.Event()
        self.editor = FakeSession([TurnResult("Done.", False, ["execute_blender_code"])], block=gate)
        self.build().start()
        self.service.say("make it red")
        wait_for(lambda: len(self.editor.sent) == 1)
        self.bridge.down = True
        wait_for(lambda: not self.service.snapshot()["blender_connected"])
        self.bridge.down = False
        wait_for(lambda: self.service.snapshot()["blender_connected"], timeout=4)
        time.sleep(0.1)
        self.assertNotIn("Ready", self.bridge.statuses[-1][0])
        gate.set()

    # --- undo ---------------------------------------------------------------------------------------
    def test_undo_waits_for_the_edit_in_progress(self):
        gate = threading.Event()
        self.editor = FakeSession([TurnResult("Made it red.", False, ["execute_blender_code"])], block=gate)
        self.build().start()
        self.service.say("make it red")
        wait_for(lambda: len(self.editor.sent) == 1)
        undo = self.service.say("undo that")
        time.sleep(0.2)
        self.assertEqual((self.bridge.undos, self.record(undo)["stage"]), (0, "queued"))
        gate.set()
        wait_for(lambda: self.record(undo)["stage"] == "done")
        self.assertGreater(self.bridge.calls.index("undo"), self.bridge.calls.index("undo_push"))

    def test_undo_tries_again_while_blender_is_busy(self):
        self.stt.texts = ["Undo that."]
        self.bridge.busy = 2
        self.build().start()
        record = self.speak()
        self.assertEqual((record["stage"], self.bridge.undos), ("done", 1))

    def test_the_undo_step_waits_while_blender_is_busy(self):
        self.bridge.busy = 2
        self.build().start()
        record = self.speak()
        self.assertTrue(record["undo_step"])
        self.assertEqual(self.bridge.pushed, ["GazeVoice: Make this twice as tall."])

    def test_no_undo_step_when_the_scene_did_not_change(self):
        self.bridge.changed = False
        self.build().start()
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertFalse(record["undo_step"])
        self.assertEqual(self.bridge.pushed, [])
        self.assertLess(self.bridge.calls.index("begin_edit"), self.bridge.calls.index("undo_push"))

    def test_long_commands_keep_their_suffix_in_the_undo_name(self):
        self.editor = FakeSession(block=threading.Event(), tools_first=["execute_blender_code"])
        self.build().start()
        running = self.service.say("make every leg of the dining chair a little thinner and slightly taller")
        wait_for(lambda: self.record(running)["tools"])
        self.service.say("stop")
        wait_for(lambda: self.record(running)["stage"] == "stopped")
        name = self.bridge.pushed[0]
        self.assertLessEqual(len(name.encode()), 63)
        self.assertTrue(name.endswith("… (stopped)"), name)

    # --- conversation and resilience ---------------------------------------------------------------
    def test_new_conversation_waits_for_the_running_edit(self):
        gate = threading.Event()
        self.editor = FakeSession([TurnResult("Made it red.", False, ["execute_blender_code"])], block=gate)
        self.next_editor = FakeSession([TurnResult("Added a cube.", False, ["execute_blender_code"])])
        self.build().start()
        first = self.service.say("make it red")
        wait_for(lambda: len(self.editor.sent) == 1)
        self.service.reset_conversation()
        self.assertFalse(self.editor.closed)
        gate.set()
        wait_for(lambda: self.record(first)["stage"] == "done")
        second = self.service.say("add a cube")
        wait_for(lambda: self.record(second)["stage"] == "done")
        self.assertTrue(self.editor.closed)
        self.assertEqual(self.editors_made, 2)

    def test_new_conversation_forgets_earlier_commands(self):
        self.stt.texts = ["make it red", "now make it blue"]
        self.refiner = FakeSession([TurnResult(json.dumps({"kind": "command", "text": "Make it red."}), False),
                                    TurnResult(json.dumps({"kind": "command", "text": "Make it blue."}), False)])
        self.next_editor = FakeSession([TurnResult("Made it blue.", False, ["execute_blender_code"])])
        self.build().start()
        self.speak(1002.0, 1003.0)
        self.service.reset_conversation()
        self.speak(1004.0, 1005.0)
        self.assertEqual(json.loads(self.refiner.sent[1])["previous_commands"], [])

    def test_a_dead_speech_worker_is_restarted(self):
        self.build().start()
        self.stt.alive = False
        self.stt = FakeStt(["add a cube"])
        record = self.speak()
        self.assertEqual(record["heard"], "add a cube")

    def test_a_dead_mic_is_restarted_when_you_press_the_key(self):
        self.build().start()
        self.mic.started = False  # ffmpeg exited (the device went away)
        record = self.speak()
        self.assertEqual(record["stage"], "done")
        self.assertEqual(self.mic.starts, 2)

    def test_records_are_complete_from_the_start_and_shared_as_copies(self):
        self.build().start()
        public = self.service.say("add a cube")
        internal = self.record(public)
        for key in ("_t0", "_t1", "_stop", "audio", "refine_error", "refiner_reply"):
            self.assertIn(key, internal)
        public["tools"].append("x")
        public["timings"]["x"] = 1
        self.assertNotIn("x", internal["tools"])
        self.assertNotIn("x", internal["timings"])

    def test_a_record_that_cant_be_saved_doesnt_stop_voice(self):
        (Path(self.tmp.name) / "commands").write_text("not a folder")
        self.build().start()
        for _ in range(2):
            record = self.service.say("add a cube")
            wait_for(lambda: self.record(record)["stage"] in ("done", "failed"))


# "add blue color there": the clip starts 0.35 s before the key goes down at 1002.0, so "there" is said at 1002.65.
THERE_WORDS = [
    {"text": "add", "start": 0.1, "end": 0.3},
    {"text": "blue", "start": 0.4, "end": 0.6},
    {"text": "color", "start": 0.6, "end": 0.9},
    {"text": "there", "start": 1.0, "end": 1.2},
]
THERE_REFINED = json.dumps({"kind": "command", "text": "Add blue color there."})


class GazeVoiceTest(VoiceTestBase):
    """Where the person looks, or points with the mouse, reaches the editor as part of the focus."""

    def setUp(self):
        super().setUp()
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
        self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])])
        self.next_editor = FakeSession()  # for a restart
        self.gaze = FakeGaze()
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [EAGLE] * 19 + [PERCH] * 4 + [None]

    def run_gaze(self, mode="on", look=True, source="mouse"):
        self.build(gaze_mode=mode, gaze_source=source).start()
        if look:
            self.gaze.look(0.5, 0.5)
        return self.speak()

    def asked_gaze(self):
        return [c for c in self.bridge.calls if c in ("gaze_pick", "gaze_piece")]

    # --- the modes -----------------------------------------------------------------------------
    def test_on_mode_puts_the_gaze_target_in_the_editors_message(self):
        record = self.run_gaze("on")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.editor.sent[0], "\n".join([
            "Command: Add blue color there.",
            'Heard: "add blue color there"',
            'Focus (gaze): "there" at 1.0 s → Eagle (79%), Perch (17%)',
            "  part: Wing.L · point (0.41, 0.12, 0.73) on face 3120 · connected piece of 480 of 5120 faces",
            "  gaze is accurate to about 10% of the viewport · selected: nothing · mode OBJECT",
        ]))
        self.assertEqual(record["gaze"]["status"], "resolved")
        self.assertEqual(self.bridge.pieces[0]["uid"], 116)  # the top guess was looked at closer
        self.assertEqual(self.bridge.pieces[0]["point"], EAGLE["point"])

    def test_shadow_mode_records_but_doesnt_change_the_focus(self):
        self.bridge.selected = ["Chair"]
        record = self.run_gaze("shadow")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.editor.sent[0], "\n".join([
            "Command: Add blue color there.",
            'Heard: "add blue color there"',
            "Focus: selected Chair (active: Chair) · mode OBJECT · source: selection",
        ]))
        self.assertNotIn("gaze", record["focus"])
        self.assertNotIn("gaze", json.loads(self.refiner.sent[0]))
        gaze = record["gaze"]
        self.assertEqual(gaze["targets"][0]["candidates"][0]["name"], "Eagle")
        self.assertEqual(gaze["targets"][0]["piece"], WING_PIECE)  # what Blender said about the part, without "ok"
        self.assertIs(gaze["matched_selection"], False)  # it looked at the Eagle, with the Chair selected
        saved = json.loads((Path(self.tmp.name) / "commands" / record["id"] / "record.json").read_text())
        self.assertEqual(saved["gaze"], gaze)

    def test_off_mode_never_starts_gaze(self):
        record = self.run_gaze("off", look=False)
        self.assertEqual((self.gazes_made, self.gaze.starts, self.gaze.track_reads), (0, 0, 0))
        self.assertEqual(self.asked_gaze(), [])
        self.assertIsNone(record["gaze"])
        self.assertIn("Focus: nothing selected", self.editor.sent[0])
        self.assertEqual(self.service.snapshot()["gaze"],
                         {"mode": "off", "source": "mouse", "live": False, "problem": "", "checked": 0, "matched": 0})

    def test_on_mode_without_a_gaze_part_runs_on_the_selection(self):
        self.gaze = None  # the camera isn't built yet, so there is nothing to start
        self.bridge.selected = ["Chair"]
        record = self.run_gaze("on", look=False, source="camera")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertEqual(self.asked_gaze(), [])
        self.assertFalse(self.service.snapshot()["gaze"]["live"])

    def test_the_refiner_sees_the_gaze_target_too(self):
        self.run_gaze("on")
        message = json.loads(self.refiner.sent[0])
        self.assertIn("Eagle (79%)", message["gaze"])
        self.assertIn("Wing.L", message["gaze"])
        self.assertEqual(message["heard"], "add blue color there")
        self.assertNotIn("gaze", message["focus"])

    def test_a_command_with_no_pointing_word_skips_gaze(self):
        self.stt = FakeStt(["add a cube"], words=[{"text": "add", "start": 0.1, "end": 0.3},
                                                  {"text": "cube", "start": 0.4, "end": 0.7}])
        record = self.run_gaze("on")
        self.assertEqual(self.asked_gaze(), [])
        self.assertIsNone(record["gaze"])
        self.assertNotIn("(gaze)", self.editor.sent[0])

    def test_word_times_count_from_the_start_of_the_recorded_clip(self):
        # "there" is the first thing said, 0.1 s into a clip that began 0.35 s before the key went down.
        self.stt = FakeStt(["there"], words=[{"text": "there", "start": 0.1, "end": 0.3}])
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5, start=1001.2)  # the eyes settled before the key was pressed
        self.speak()
        self.assertEqual(len(self.bridge.picks), 1)

    def test_the_gaze_reaches_the_editor_even_when_the_selection_cant_be_read(self):
        self.bridge.selection_unreadable = True
        record = self.run_gaze("on")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertTrue(self.editor.sent[0].splitlines()[2].startswith('Focus (gaze): "there"'))
        self.assertNotIn("selected:", self.editor.sent[0])  # it doesn't know, so it doesn't say "nothing"
        self.assertEqual(list(record["focus"]), ["gaze"])
        self.assertIn("Eagle", json.loads(self.refiner.sent[0])["gaze"])

    # --- losing track ----------------------------------------------------------------------------
    def test_lost_tracking_falls_back_to_the_selection(self):
        self.bridge.selected = ["Chair"]
        record = self.run_gaze("on", look=False)
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertNotIn("(gaze)", self.editor.sent[0])
        self.assertEqual(record["gaze"]["status"], "none")
        self.assertIsNone(record["gaze"]["matched_selection"])
        self.assertEqual(self.asked_gaze(), [])  # no samples, so nobody asked Blender anything
        texts = [text for text, _ in self.bridge.statuses]
        self.assertTrue(any(t.startswith("Understanding") and "Gaze lost" in t and "your selection" in t
                            for t in texts), texts)
        self.assertTrue(any("Gaze lost" in t and t.startswith("Working:") for t in texts), texts)

    def test_lost_gaze_is_still_on_the_status_line_when_the_edit_is_done(self):
        self.bridge.selected = ["Chair"]
        self.editor = FakeSession([TurnResult("Made Chair blue.", False, ["execute_blender_code"])])
        self.run_gaze("on", look=False)
        self.assertEqual(self.bridge.statuses[-1], ("✓ Made Chair blue. · Gaze lost, using your selection", "done"))

    def test_a_long_reply_doesnt_push_the_note_off_the_status_line(self):
        self.bridge.selected = ["Chair"]
        self.editor = FakeSession([TurnResult("Made " + "very " * 60 + "blue.", False, ["execute_blender_code"])])
        self.run_gaze("on", look=False)
        text = self.bridge.statuses[-1][0]
        self.assertLessEqual(len(text), 200)
        self.assertTrue(text.endswith(" · Gaze lost, using your selection"), text)

    def test_the_done_line_is_as_it_was_when_gaze_was_fine(self):
        self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])])
        self.run_gaze("on")
        self.assertEqual(self.bridge.statuses[-1], ("✓ Made the wing blue.", "done"))

    def test_lost_tracking_in_shadow_mode_says_nothing(self):
        record = self.run_gaze("shadow", look=False)
        self.assertEqual(record["gaze"]["status"], "none")
        self.assertFalse([text for text, _ in self.bridge.statuses if "Gaze" in text])

    # --- a command that has to wait its turn ----------------------------------------------------------
    def queue_one_behind_a_long_edit(self, mode="on"):
        """Two commands with a pointing word: the first is held in the editor, the second queued behind it."""
        self.stt = FakeStt(["add blue color there", "add red color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False), TurnResult(THERE_REFINED, False)])
        self.release_edit = threading.Event()
        self.editor = FakeSession(block=self.release_edit)
        self.build(gaze_mode=mode, gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5, start=1002.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        first = self.last_record()
        wait_for(lambda: self.editor.sent)
        self.gaze.look(0.8, 0.2, start=1005.5)
        self.service.ptt("down", 1005.0)
        self.service.ptt("up", 1006.0)
        second = self.last_record()
        wait_for(lambda: self.service.snapshot()["queued"] == 1)  # heard, and waiting its turn
        return first, second

    def time_passes(self):
        """The edit runs on for minutes: everything the track held from when the command was said is gone."""
        track = self.gaze._track
        track.seconds = 1.0
        track.add(GazeSample(5000.0, 0.1, 0.1, 0.9))
        self.assertEqual(track.between(1000.0, 1010.0), [])

    def test_a_command_queued_behind_a_long_edit_keeps_its_gaze(self):
        first, second = self.queue_one_behind_a_long_edit()
        self.time_passes()
        self.release_edit.set()
        wait_for(lambda: second["stage"] == "done")
        self.assertEqual(second["gaze"]["status"], "resolved")
        self.assertEqual(self.bridge.picks[-1][0], [0.8, 0.2])  # where they looked for this one, not the first
        self.assertIn('Focus (gaze): "there"', self.editor.sent[1])
        self.assertEqual(second["_gaze_note"], "")  # it wasn't lost

    def test_watching_only_keeps_it_too(self):
        _, second = self.queue_one_behind_a_long_edit("shadow")
        self.time_passes()
        self.release_edit.set()
        wait_for(lambda: second["stage"] == "done")
        self.assertEqual(second["gaze"]["status"], "resolved")

    def test_what_a_command_keeps_never_reaches_its_record(self):
        _, second = self.queue_one_behind_a_long_edit()
        self.assertIsNotNone(second["_look"])  # it does hold it, while it waits
        self.assertNotIn("_look", self.service._public(second))
        self.assertNotIn("_look", self.service._copy(second))
        self.release_edit.set()
        wait_for(lambda: second["stage"] == "done")
        text = saved_record_text(Path(self.tmp.name), second)
        self.assertNotIn("_look", text)
        self.assertNotIn("confidence", text)  # nor any sample, under any name

    def test_a_command_lets_go_of_it_once_it_has_finished(self):
        _, second = self.queue_one_behind_a_long_edit()
        self.release_edit.set()
        wait_for(lambda: second["stage"] == "done")
        self.assertIsNone(second["_look"])

    def test_a_queued_command_that_is_stopped_lets_go_of_it(self):
        _, second = self.queue_one_behind_a_long_edit()
        self.service.say("stop")
        wait_for(lambda: second["stage"] == "stopped")
        self.assertIsNone(second["_look"])

    def test_turning_voice_off_lets_go_of_it_even_for_a_command_that_never_ran(self):
        _, second = self.queue_one_behind_a_long_edit()
        self.service.stop()
        self.assertIsNone(second["_look"])

    def test_a_track_that_breaks_doesnt_stop_the_command(self):
        class BrokenTrack(FakeGaze):
            @property
            def track(self):
                raise RuntimeError("no track")

        self.gaze = BrokenTrack()
        with contextlib.redirect_stderr(io.StringIO()):
            record = self.run_gaze("on", look=False)
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(record["_gaze_note"], "Gaze lost, using your selection")

    def test_a_dead_socket_during_gaze_doesnt_stop_voice(self):
        self.bridge.gaze_error = ConnectionResetError("Blender went away")
        record = self.run_gaze("on")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: nothing selected", self.editor.sent[0])
        self.assertTrue(any("Gaze lost" in text for text, _ in self.bridge.statuses))

    def test_no_3d_viewport_means_no_gaze_but_voice_goes_on(self):
        self.bridge.pick_reply = None
        record = self.run_gaze("on")
        self.assertEqual((record["stage"], record["gaze"]["status"]), ("done", "none"))

    def test_nonsense_from_blender_doesnt_stop_voice(self):
        self.bridge.pick_reply = lambda points: ["oops"] * len(points)
        logged = io.StringIO()
        with contextlib.redirect_stderr(logged):
            record = self.run_gaze("on")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: nothing selected", self.editor.sent[0])
        self.assertIn("gaze", logged.getvalue().lower())
        self.assertTrue(any("Gaze" in text for text, _ in self.bridge.statuses))

    def test_a_failed_piece_lookup_still_gives_the_object(self):
        self.bridge.piece_reply = None
        record = self.run_gaze("on")
        self.assertIn('→ Eagle (79%), Perch (17%)\n  point (0.41, 0.12, 0.73)\n', self.editor.sent[0])
        self.assertNotIn("part:", self.editor.sent[0])
        self.assertIsNone(record["gaze"]["targets"][0]["piece"])

    # --- starting and stopping --------------------------------------------------------------------
    def test_gaze_failing_to_start_doesnt_stop_voice(self):
        self.gaze = FakeGaze(fail=True)
        self.bridge.selected = ["Chair"]
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.assertEqual(self.service.state, "ready")
        self.assertFalse(self.gaze.alive)
        self.assertGreaterEqual(self.gaze.stops, 1)  # whatever half-started was closed
        self.assertFalse(self.service.snapshot()["gaze"]["live"])
        self.assertTrue(any("Gaze couldn't start" in text and "the camera is busy" in text and "selection" in text
                            for text, _ in self.bridge.statuses), self.bridge.statuses)
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertEqual(self.asked_gaze(), [])

    def test_a_gaze_part_that_cant_even_be_built_doesnt_stop_voice(self):
        def broken():
            raise OSError("no camera")

        self.make_gaze = broken
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.assertEqual(self.service.state, "ready")
        self.assertEqual(self.speak()["stage"], "done")

    def test_the_snapshot_says_why_gaze_didnt_start(self):
        self.gaze = FakeGaze(fail=True)
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.assertEqual(self.service.snapshot()["gaze"]["problem"],
                         "Gaze couldn't start (the camera is busy), so voice uses your selection.")
        self.service.stop()
        self.gaze.fail = False
        self.service.start()
        self.assertEqual(self.service.snapshot()["gaze"]["problem"], "")  # a start that works clears it

    def test_the_snapshot_has_no_problem_to_report_when_gaze_starts(self):
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.assertEqual(self.service.snapshot()["gaze"]["problem"], "")

    def idle_lines(self):
        return [text for text, state in self.bridge.statuses if state == "idle"]

    def test_the_status_line_says_while_the_camera_is_on(self):
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.assertEqual(self.idle_lines()[-1], "Ready · hold ⌥ Space to talk · gaze on")

    def test_watching_only_opens_the_camera_too_so_it_says_so_too(self):
        self.build(gaze_mode="shadow", gaze_source="camera").start()
        self.assertEqual(self.idle_lines()[-1], "Ready · hold ⌥ Space to talk · gaze on")

    def test_the_status_line_stops_saying_so_once_the_camera_is_off(self):
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.gaze.alive = False  # the worker died
        self.service.ptt("down", 1002.0)
        self.service.ptt("cancel", 1002.5)
        self.assertEqual(self.idle_lines()[-1], "Ready · hold ⌥ Space to talk")

    def test_the_status_line_says_nothing_about_a_camera_nothing_opened(self):
        for name, kwargs in {"the mouse": dict(gaze_mode="on", gaze_source="mouse"),
                             "gaze off": dict(gaze_mode="off", gaze_source="camera")}.items():
            with self.subTest(name):
                self.bridge.statuses.clear()
                self.build(**kwargs).start()
                self.assertEqual(self.idle_lines()[-1], "Ready · hold ⌥ Space to talk")
                self.service.stop()

    def test_gaze_that_failed_to_start_doesnt_claim_to_be_on(self):
        self.gaze = FakeGaze(fail=True)
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.assertEqual(self.idle_lines()[-1], "Ready · hold ⌥ Space to talk")

    def test_gaze_runs_while_voice_is_on_and_stops_with_it(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.assertTrue(self.gaze.alive)
        self.assertTrue(self.service.snapshot()["gaze"]["live"])
        self.service.stop()
        self.assertFalse(self.gaze.alive)
        self.assertFalse(self.service.snapshot()["gaze"]["live"])
        self.service.start()
        self.assertTrue(self.gaze.alive)  # a fresh start opens it again

    def test_stopping_while_gaze_is_starting_turns_voice_off_cleanly(self):
        started = threading.Event()
        release = threading.Event()

        class SlowGaze(FakeGaze):
            def start(self):
                started.set()
                release.wait(5)
                if self.stops:
                    raise RuntimeError("stopped while starting")
                super().start()

        self.gaze = SlowGaze()
        self.build(gaze_mode="on", gaze_source="mouse")
        starter = threading.Thread(target=self.start_quietly)
        starter.start()
        self.assertTrue(started.wait(5))
        stopper = threading.Thread(target=self.service.stop)
        stopper.start()
        time.sleep(0.2)
        release.set()
        starter.join(5)
        stopper.join(5)
        self.assertEqual(self.service.state, "off")
        self.assertFalse(self.gaze.alive)
        self.assertEqual(self.start_error, "Voice was turned off before it finished starting.")
        self.assertFalse(self.mic.started)

    def start_quietly(self):
        try:
            self.service.start()
        except VoiceError as exc:
            self.start_error = str(exc)

    def test_starting_voice_clears_any_drawing_left_in_the_viewport(self):
        for mode in ("off", "on"):
            self.bridge.shows.clear()
            self.build(gaze_mode=mode, gaze_source="mouse").start()
            self.assertEqual(self.bridge.shows, [{"cmd": "gaze_show", "dot": None, "ring": None, "marks": []}], mode)
            self.service.stop()

    def test_an_old_bridge_that_cant_clear_the_drawing_doesnt_stop_voice(self):
        self.bridge.gaze_error = ConnectionResetError("reset")
        self.build().start()
        self.assertEqual(self.service.state, "ready")

    def test_unity_runs_exactly_as_before(self):
        self.build(host="unity", gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual((self.gazes_made, self.gaze.starts), (0, 0))
        self.assertFalse([c for c in self.bridge.calls if c.startswith("gaze_")])
        self.assertIn("Focus: nothing selected", self.editor.sent[0])

    # --- a resized viewport ------------------------------------------------------------------------
    def saved_for(self, width=1840, height=1100):
        self.gaze = FakeGaze(view=(width, height))

    def test_a_resized_viewport_means_no_gaze_for_that_command_and_says_to_calibrate_again(self):
        self.saved_for(1500, 1100)  # calibrated on a narrower viewport than Blender has now
        self.bridge.selected = ["Chair"]
        record = self.run_gaze("on", source="camera")
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.asked_gaze(), [])  # no ray was cast with a mapping that no longer fits
        self.assertIn("Focus: selected Chair", self.editor.sent[0])  # it ran on the selection
        self.assertNotIn("(gaze)", self.editor.sent[0])
        self.assertEqual(record["gaze"]["status"], "none")
        self.assertIs(record["gaze"]["viewport_changed"], True)
        self.assertIsNone(record["gaze"]["matched_selection"])
        texts = [text for text, _ in self.bridge.statuses]
        self.assertTrue(any(t.startswith("Understanding") and "Viewport changed" in t and "calibrate gaze again" in t
                            for t in texts), texts)
        self.assertTrue(any(t.startswith("Working:") and "Viewport changed" in t for t in texts), texts)

    def test_the_same_size_viewport_uses_gaze_as_usual(self):
        self.saved_for(1840, 1100)
        record = self.run_gaze("on", source="camera")
        self.assertEqual(record["gaze"]["status"], "resolved")
        self.assertNotIn("viewport_changed", record["gaze"])
        self.assertTrue(self.asked_gaze())
        self.assertIn("(gaze)", self.editor.sent[0])

    def test_the_viewport_is_checked_for_every_command_not_once_when_voice_starts(self):
        self.stt = FakeStt(["add blue color there", "and there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)] * 2)
        self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])] * 2)
        self.saved_for(1840, 1100)
        self.build(gaze_mode="on", gaze_source="camera").start()
        self.gaze.look(0.5, 0.5)
        first = self.speak()
        self.assertEqual(first["gaze"]["status"], "resolved")
        self.bridge.view_reply = {"ok": True, "width": 1840, "height": 900}  # the person resized it between commands
        self.gaze.look(0.5, 0.5, start=1004.5)
        second = self.speak(down=1004.0, up=1005.0)
        self.assertEqual(second["stage"], "done", second["error"])
        self.assertIs(second["gaze"]["viewport_changed"], True)
        self.assertEqual(self.bridge.calls.count("gaze_view"), 2)

    def test_watch_only_records_nothing_from_a_resized_viewport_and_says_why(self):
        self.saved_for(1500, 1100)
        self.bridge.selected = ["Chair"]
        record = self.run_gaze("shadow", source="camera")
        self.assertEqual(self.asked_gaze(), [])
        self.assertIs(record["gaze"]["viewport_changed"], True)
        self.assertEqual(self.service.snapshot()["gaze"]["checked"], 0)  # it can't count towards agreement with the selection
        self.assertTrue(any("Viewport changed" in text for text, _ in self.bridge.statuses))

    def test_the_mouse_has_no_calibration_so_the_viewport_is_never_checked(self):
        self.gaze = PointerTracker()
        self.bridge.view_reply = {"ok": True, "width": 10, "height": 10}
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.feed(0.5, 0.5, 1002.5)
        self.gaze.feed(0.5, 0.5, 1002.6)
        self.assertIsNone(self.gaze.view)
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertNotIn("gaze_view", self.bridge.calls)
        self.assertNotIn("viewport_changed", record["gaze"])

    def test_if_blender_cant_say_the_size_gaze_goes_ahead_and_copes_as_before(self):
        self.saved_for(1500, 1100)
        for reply in ({"ok": False, "error": "unknown command"}, {"ok": False, "error": "no 3D viewport"}, {}, None):
            self.bridge.view_reply = reply
            self.bridge.picks.clear()
            self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
            self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
            self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])])
            record = self.run_gaze("on", source="camera")
            self.assertNotIn("viewport_changed", record["gaze"], reply)
            self.assertTrue(self.bridge.picks, reply)
            self.service.stop()

    def test_a_command_with_no_pointing_word_is_checked_too_and_says_so_when_the_viewport_changed(self):
        self.stt = FakeStt(["add a cube"], words=[{"text": "add", "start": 0.1, "end": 0.3},
                                                  {"text": "cube", "start": 0.4, "end": 0.7}])
        self.saved_for(1500, 1100)
        record = self.run_gaze("on", source="camera")
        self.assertIs(record["gaze"]["viewport_changed"], True)
        self.assertEqual(self.asked_gaze(), [])
        self.assertTrue(any("Viewport changed" in text for text, _ in self.bridge.statuses))

    def test_a_command_with_no_pointing_word_leaves_no_gaze_record_when_the_viewport_is_the_same(self):
        self.stt = FakeStt(["add a cube"], words=[{"text": "add", "start": 0.1, "end": 0.3},
                                                  {"text": "cube", "start": 0.4, "end": 0.7}])
        self.saved_for(1840, 1100)
        record = self.run_gaze("on", source="camera")
        self.assertIsNone(record["gaze"])
        self.assertEqual(self.bridge.calls.count("gaze_view"), 1)
        self.assertEqual(self.asked_gaze(), [])

    # --- commands that never use it -----------------------------------------------------------------
    def test_stop_and_undo_never_touch_gaze(self):
        self.stt = FakeStt(["undo that", "stop that"], words=[{"text": "that", "start": 0.5, "end": 0.7}])
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5, start=1002.5)
        self.gaze.look(0.5, 0.5, start=1004.5)
        undone = self.speak(1002.0, 1003.0)
        stopped = self.speak(1004.0, 1005.0)
        self.assertEqual((undone["kind"], stopped["kind"]), ("undo", "stop"))
        self.assertEqual(self.bridge.undos, 1)
        self.assertEqual(self.asked_gaze(), [])
        self.assertEqual(self.gaze.track_reads, 0)
        self.assertEqual((undone["gaze"], stopped["gaze"]), (None, None))

    def test_typed_commands_dont_use_gaze(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        typed = self.service.say("add blue color there")
        wait_for(lambda: self.record(typed)["stage"] == "done")
        self.assertEqual(self.asked_gaze(), [])
        self.assertEqual(self.gaze.track_reads, 0)
        self.assertIsNone(self.record(typed)["gaze"])
        self.assertIn("Focus: nothing selected", self.editor.sent[0])

    def test_stop_while_blender_is_being_asked_ends_the_command_before_the_brains(self):
        asking, release = threading.Event(), threading.Event()

        def slow_pick(points):
            asking.set()
            release.wait(5)
            return [EAGLE] * len(points)

        self.bridge.pick_reply = slow_pick
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        spoken = self.last_record()
        self.assertTrue(asking.wait(5))
        self.service.say("stop")
        release.set()
        wait_for(lambda: self.record(spoken)["stage"] == "stopped")
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))

    # --- how often it agrees with the selection -----------------------------------------------------
    def test_agreement_with_the_selection_is_counted(self):
        self.bridge.selected = ["Eagle", "Perch"]
        self.build(gaze_mode="shadow", gaze_source="mouse").start()
        self.assertEqual(self.service.snapshot()["gaze"],
                         {"mode": "shadow", "source": "mouse", "live": True, "problem": "", "checked": 0, "matched": 0})
        self.gaze.look(0.5, 0.5)
        record = self.speak()
        self.assertIs(record["gaze"]["matched_selection"], True)
        self.assertEqual(self.service.snapshot()["gaze"]["checked"], 1)
        self.assertEqual(self.service.snapshot()["gaze"]["matched"], 1)

    def test_a_miss_counts_as_checked_but_not_matched(self):
        self.bridge.selected = ["Chair"]
        self.run_gaze("shadow")
        self.assertEqual(self.service.snapshot()["gaze"]["checked"], 1)
        self.assertEqual(self.service.snapshot()["gaze"]["matched"], 0)

    def test_nothing_selected_or_no_gaze_means_nothing_to_compare(self):
        record = self.run_gaze("shadow")  # looked at the Eagle with nothing selected
        self.assertIsNone(record["gaze"]["matched_selection"])
        self.assertEqual(self.service.snapshot()["gaze"]["checked"], 0)

    def test_the_counts_start_over_with_each_voice_session(self):
        self.bridge.selected = ["Eagle"]
        self.run_gaze("shadow")
        self.assertEqual(self.service.snapshot()["gaze"]["matched"], 1)
        self.service.stop()
        self.service.start()
        self.assertEqual(self.service.snapshot()["gaze"]["checked"], 0)

    # --- the mouse as a stand-in ----------------------------------------------------------------------
    def test_the_mouse_stands_in_for_gaze(self):
        self.gaze = PointerTracker()
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.bridge.add("ptt", state="down", t=1002.0)
        for i in range(8):
            self.bridge.add("pointer", u=0.4, v=0.3, t=1002.5 + i * 0.05)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] in ("done", "failed"))
        self.assertEqual(self.last_record()["stage"], "done", self.last_record()["error"])
        samples = self.gaze.track.between(1002.0, 1003.0)
        self.assertEqual([(s.t, s.u, s.v, s.confidence) for s in samples],
                         [(1002.5 + i * 0.05, 0.4, 0.3, 1.0) for i in range(8)])
        self.assertEqual(self.bridge.picks[0][0], [0.4, 0.3])  # the first ray goes where the pointer was
        self.assertTrue(all(abs(u - 0.4) <= 0.02 + 1e-9 and abs(v - 0.3) <= 0.03 + 1e-9
                            for u, v in self.bridge.picks[0]))  # and the rays stay within the mouse's small patch
        self.assertIn('Focus (gaze): "there"', self.editor.sent[0])
        self.assertIn("accurate to about 2% of the viewport", self.editor.sent[0])

    def test_a_pointer_tracker_is_a_gaze_part(self):
        tracker = PointerTracker()
        self.assertFalse(tracker.alive)
        tracker.start()
        self.assertTrue(tracker.alive)
        tracker.stop()
        self.assertFalse(tracker.alive)
        self.assertEqual(tracker.radius, (0.02, 0.03))
        self.assertIsNone(tracker.track.latest_time)

    def test_a_malformed_pointer_event_is_ignored_and_polling_goes_on(self):
        self.gaze = PointerTracker()
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.bridge.add("pointer", u="left", v=None, t=1002.5)
        self.bridge.add("pointer", u=0.4, v=0.3)  # no time
        self.bridge.add("pointer", u=float("nan"), v=0.3, t=1002.6)
        self.bridge.add("pointer", u=0.4, v=0.3, t=float("inf"))
        self.bridge.add("pointer", u=10 ** 400, v=0.3, t=1002.6)  # too big for a float
        self.bridge.add("pointer", u=0.4, v=0.3, t=1002.7)
        wait_for(lambda: self.gaze.track.latest_time == 1002.7)
        self.assertEqual(len(self.gaze.track.between(0, 2000)), 1)
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")

    def test_pointer_events_are_dropped_when_nothing_listens_to_them(self):
        self.build(gaze_mode="off", gaze_source="mouse").start()
        self.bridge.add("pointer", u=0.4, v=0.3, t=1002.5)
        self.bridge.add("ptt", state="down", t=1002.0)
        self.bridge.add("ptt", state="up", t=1003.0)
        wait_for(lambda: self.service.recent and self.last_record()["stage"] == "done")


CLEARED = {"cmd": "gaze_show", "dot": None, "ring": None, "marks": []}
WHICH = ("Which one? Say the number.", "working")
COULDNT_TELL = "Couldn't tell what you meant. Select it or say its name."
# 11 rays on the Eagle and 10 on the Perch: 46% against 42%, too close to call.
CLOSE = lambda points: [EAGLE] * 11 + [PERCH] * 10 + [None] * 3  # noqa: E731
THIS_AND_THERE = [
    {"text": "move", "start": 0.1, "end": 0.3},
    {"text": "this", "start": 0.5, "end": 0.7},
    {"text": "there", "start": 1.0, "end": 1.2},
]


class NoLabelsBridge(FakeBridge):
    """A Blender that can't draw the numbers (an old add-on, or no 3D viewport)."""

    def gaze(self, cmd, req):
        if cmd == "gaze_show" and req["marks"]:
            self.shows.append(req)
            return {"ok": False, "error": "no 3D viewport"}
        return super().gaze(cmd, req)


class GazeChoiceTest(VoiceTestBase):
    """When two guesses are close, numbers appear in the viewport and the person says which one."""

    def setUp(self):
        super().setUp()
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
        self.editor = FakeSession([TurnResult("Made the perch blue.", False, ["execute_blender_code"])])
        self.gaze = FakeGaze()
        self.bridge.selected = []
        self.bridge.pick_reply = CLOSE

    def start(self, mode="on", seconds=5.0, look=True):
        service = self.build(gaze_mode=mode, gaze_source="mouse")
        service.choice_seconds = seconds
        service.start()
        if look:
            self.gaze.look(0.5, 0.5, start=1001.6, count=30)

    def hold(self, down=1002.0, up=1003.0):
        """Speak without waiting for the command to finish."""
        self.service.ptt("down", down)
        self.service.ptt("up", up)
        return self.last_record()

    def labelled(self):
        return [show for show in self.bridge.shows if show["marks"]]

    def wait_for_labels(self, count=1):
        wait_for(lambda: len(self.labelled()) >= count)

    def statuses(self):
        return [text for text, _ in self.bridge.statuses]

    # --- asking ------------------------------------------------------------------------------------
    def test_close_guesses_show_numbered_labels_and_wait(self):
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        wait_for(lambda: WHICH in self.bridge.statuses)
        shown = self.labelled()[0]
        self.assertEqual((shown["cmd"], shown["dot"], shown["ring"]), ("gaze_show", None, None))
        self.assertEqual([(m["n"], m["name"]) for m in shown["marks"]], [(1, "Eagle"), (2, "Perch")])
        self.assertTrue(all(0 < m["u"] < 1 and 0 < m["v"] < 1 for m in shown["marks"]))
        self.assertNotEqual((shown["marks"][0]["u"], shown["marks"][0]["v"]),
                            (shown["marks"][1]["u"], shown["marks"][1]["v"]))  # each sits where its own rays hit
        time.sleep(0.2)  # it is waiting, not working: the brains haven't heard a thing
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))
        self.assertNotIn(spoken["stage"], ("done", "failed", "stopped"))
        self.assertEqual(self.service.snapshot()["state"], "refining")

    def test_saying_a_number_picks_that_one(self):
        self.stt = FakeStt(["add blue color there", "Two."], words=THERE_WORDS)
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        answer = self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "done" and answer["stage"] == "done")
        self.assertEqual(answer["reply"], "Picked Perch.")
        self.assertEqual(self.bridge.pieces[-1]["uid"], 117)  # the Perch was looked at closer, as the Eagle had been
        self.assertEqual(self.bridge.pieces[-1]["point"], PERCH["point"])
        lines = self.editor.sent[0].splitlines()
        self.assertEqual(lines[2], 'Focus (gaze): "there" at 1.0 s → Perch (42%), Eagle (46%)')
        self.assertIn("point (0.50, 0.10, 0.20)", lines[3])
        self.assertEqual(spoken["gaze"]["status"], "resolved")
        self.assertTrue(spoken["gaze"]["asked"])
        self.assertEqual(spoken["gaze"]["targets"][0]["candidates"][0]["name"], "Perch")
        self.assertEqual(spoken["gaze"]["targets"][0]["piece"], WING_PIECE)
        self.assertEqual(spoken["kind"], "command")  # the answer was never a command of its own
        self.assertEqual(answer["kind"], "")
        self.assertEqual(len(self.refiner.sent), 1)

    def test_the_picture_is_of_the_one_the_person_picked(self):
        self.stt = FakeStt(["add blue color there", "Two."], words=THERE_WORDS)
        self.bridge.piece_reply = BODY_PIECE
        self.bridge.crop_start = {"ok": True, "pending": True}
        self.bridge.crop_png = make_noise_picture(64).base64
        self.pictures = FakePictures("the perch's top")
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        self.assertEqual(self.pictures.calls, [])  # not before they have chosen
        self.assertEqual(self.bridge.crops, [])
        self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "done")
        self.assertEqual([c["object"] for c in self.pictures.calls], ["Perch"])
        self.assertEqual(spoken["gaze"]["targets"][0]["picture"], "the perchs top")  # quote marks are dropped
        self.assertIn('picture (only a description of what the ring shows): "the perchs top"', self.editor.sent[0])

    def test_picking_the_first_one_keeps_the_guess_it_already_looked_at(self):
        self.stt = FakeStt(["add blue color there", "number 1"], words=THERE_WORDS)
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "done")
        self.assertEqual([p["uid"] for p in self.bridge.pieces], [116])
        self.assertEqual(self.editor.sent[0].splitlines()[2],
                         'Focus (gaze): "there" at 1.0 s → Eagle (46%), Perch (42%)')

    # --- the wait running out while the answer is on its way ------------------------------------------------
    def asked(self, seconds=0.3):
        """Numbers are up, on a wait short enough to run out during the test, with nothing selected to fall back on."""
        self.stt = FakeStt(["add blue color there", "two"], words=THERE_WORDS)
        self.start(seconds=seconds)
        self.spoken = self.hold()
        self.wait_for_labels()

    def perch_was_picked(self, answer):
        wait_for(lambda: self.spoken["stage"] == "done")
        self.assertEqual(answer["reply"], "Picked Perch.")
        self.assertEqual(self.spoken["gaze"]["targets"][0]["candidates"][0]["name"], "Perch")
        self.assertNotIn('"heard": "two"', " ".join(self.refiner.sent))  # the answer, not a new command

    def test_a_key_held_as_the_wait_runs_out_is_an_answer_coming(self):
        self.asked()
        self.service.ptt("down", 1004.0)
        time.sleep(0.5)  # well past the wait, and still speaking
        self.assertNotIn(self.spoken["stage"], ("done", "failed"))
        self.assertTrue(self.labelled())
        self.assertNotEqual(self.bridge.shows[-1], CLEARED)
        self.service.ptt("up", 1005.0)
        wait_for(lambda: self.last_record()["reply"] == "Picked Perch.")
        self.perch_was_picked(self.last_record())

    def test_an_answer_being_written_down_as_the_wait_runs_out_still_counts(self):
        self.asked()
        heard, release = threading.Event(), threading.Event()
        real = self.stt.transcribe

        def slow(audio, prompt=None):
            heard.set()
            release.wait(5)
            return real(audio, prompt)

        self.stt.transcribe = slow
        answer = self.hold(1004.0, 1005.0)
        self.assertTrue(heard.wait(5))
        time.sleep(0.5)
        release.set()
        self.perch_was_picked(answer)

    def test_an_answer_queued_behind_a_transcript_still_counts(self):
        self.asked()
        heard, release = threading.Event(), threading.Event()
        real = self.stt.transcribe

        def slow(audio, prompt=None):
            heard.set()
            release.wait(5)
            return real(audio, prompt)

        self.stt.texts.insert(0, "add red color")
        self.stt.transcribe = slow
        self.hold(1004.0, 1005.0)  # says something else
        self.assertTrue(heard.wait(5))
        answer = self.hold(1006.0, 1007.0)  # then the number, which waits for the speech worker
        time.sleep(0.5)
        self.stt.transcribe = real
        release.set()
        self.perch_was_picked(answer)

    def test_a_key_let_go_as_the_wait_runs_out_is_not_lost_while_blender_is_asked_about_the_scene(self):
        self.asked()
        reading, release = threading.Event(), threading.Event()
        real = self.bridge.request

        def slow_selection(req, timeout=None):
            if req["cmd"] == "selection":
                reading.set()
                release.wait(5)
            return real(req, timeout)

        self.bridge.request = slow_selection
        self.service.ptt("down", 1004.0)
        letting_go = threading.Thread(target=lambda: self.service.ptt("up", 1005.0))
        letting_go.start()  # not listening any more, and nothing queued yet: the clip is on its way
        self.assertTrue(reading.wait(5))
        time.sleep(0.5)
        self.assertNotIn(self.spoken["stage"], ("done", "failed"))
        release.set()
        letting_go.join(5)
        answer = self.last_record()
        self.perch_was_picked(answer)

    def test_nothing_on_its_way_means_the_wait_ends_on_time(self):
        self.asked()
        began = time.monotonic()
        wait_for(lambda: self.spoken["stage"] == "failed")
        self.assertLess(time.monotonic() - began, 1.5)

    def test_a_too_short_press_is_not_an_answer_coming(self):
        self.asked(seconds=0.5)
        self.service.ptt("down", 1004.0)
        self.service.ptt("up", 1004.1)  # dropped, so nothing is coming
        began = time.monotonic()
        wait_for(lambda: self.spoken["stage"] == "failed")
        self.assertLess(time.monotonic() - began, 1.5)
        self.service.ptt("down", 1005.0)  # and the wait isn't held open by it next time
        self.service.ptt("cancel", 1005.5)

    def test_the_wait_has_a_limit_however_long_the_key_is_held(self):
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        service = self.build(gaze_mode="on", gaze_source="mouse")
        service.choice_seconds, service.choice_limit = 0.2, 0.6
        service.start()
        self.gaze.look(0.5, 0.5, start=1001.6, count=30)
        spoken = self.hold()
        self.wait_for_labels()
        began = time.monotonic()
        service.ptt("down", 1004.0)
        wait_for(lambda: spoken["stage"] == "failed")  # with the key still down
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertEqual(self.bridge.shows[-1], CLEARED)
        service.ptt("cancel", 1005.0)

    def test_the_limit_is_a_named_number_past_the_wait(self):
        self.assertGreater(CHOICE_LIMIT_SECONDS, CHOICE_SECONDS)
        self.assertEqual(self.build().choice_limit, CHOICE_LIMIT_SECONDS)

    def test_stop_ends_a_wait_held_open_by_an_answer(self):
        self.asked()
        self.service.ptt("down", 1004.0)
        time.sleep(0.5)
        self.service.say("stop")
        wait_for(lambda: self.spoken["stage"] == "stopped")
        self.service.ptt("cancel", 1005.0)

    def test_a_number_that_isnt_on_screen_keeps_waiting(self):
        self.stt = FakeStt(["add blue color there", "five", "one"], words=THERE_WORDS)
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        wrong = self.hold(1004.0, 1005.0)
        wait_for(lambda: "Say a number from 1 to 2." in self.statuses())
        wait_for(lambda: wrong["stage"] == "done")
        self.assertEqual(wrong["reply"], "Say a number from 1 to 2.")
        self.assertEqual((self.refiner.sent, spoken["stage"] == "done"), ([], False))
        self.hold(1006.0, 1007.0)
        wait_for(lambda: spoken["stage"] == "done")
        self.assertEqual(spoken["gaze"]["targets"][0]["candidates"][0]["name"], "Eagle")

    def test_a_word_that_sounds_like_a_number_is_an_answer_while_numbers_are_up(self):
        for heard, picked in (("to", "Perch"), ("Too.", "Perch"), ("won", "Eagle")):
            with self.subTest(heard):
                self.stt = FakeStt(["add blue color there", heard], words=THERE_WORDS)
                self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
                self.editor = FakeSession([TurnResult("Made it blue.", False, ["execute_blender_code"])])
                self.editors_made = 0
                self.start()
                spoken = self.hold()
                self.wait_for_labels(1)
                answer = self.hold(1004.0, 1005.0)
                wait_for(lambda: spoken["stage"] == "done")
                self.assertEqual(answer["reply"], f"Picked {picked}.")
                self.assertEqual(len(self.refiner.sent), 1)
                self.service.stop()
                self.bridge.shows.clear()

    def test_for_is_a_number_too_and_is_told_it_isnt_on_screen(self):
        self.stt = FakeStt(["add blue color there", "for"], words=THERE_WORDS)
        self.start(seconds=0.5)
        self.hold()
        self.wait_for_labels()
        answer = self.hold(1004.0, 1005.0)
        wait_for(lambda: answer["stage"] == "done")
        self.assertEqual(answer["reply"], "Say a number from 1 to 2.")

    def test_a_word_that_sounds_like_a_number_with_nothing_waiting_is_an_ordinary_command(self):
        self.stt = FakeStt(["to"])
        self.start()
        record = self.speak()
        self.assertEqual(record["kind"], "command")
        self.assertEqual(len(self.refiner.sent), 1)

    def test_a_number_with_nothing_waiting_is_an_ordinary_command(self):
        self.stt = FakeStt(["two"], words=[])
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(record["kind"], "command")
        self.assertEqual(json.loads(self.refiner.sent[0])["heard"], "two")
        self.assertEqual(self.labelled(), [])

    def test_undo_and_stop_are_never_taken_for_a_number(self):
        self.stt = FakeStt(["add blue color there", "two"], words=THERE_WORDS)
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        undone = self.service.say("undo")  # it takes its turn behind the command that is waiting
        time.sleep(0.2)
        self.assertEqual((self.bridge.undos, spoken["stage"] == "done"), (0, False))
        self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "done" and self.record(undone)["stage"] == "done")
        self.assertEqual(self.bridge.undos, 1)
        self.assertEqual(spoken["gaze"]["targets"][0]["candidates"][0]["name"], "Perch")

    def looking_at_empty_space_for_there(self):
        self.stt = FakeStt(["move this over there"], words=THIS_AND_THERE)
        self.refiner = FakeSession([TurnResult(json.dumps({"kind": "command", "text": "Move this over there."}), False)])
        self.bridge.pick_reply = lambda points: [EAGLE] * len(points) if points[0][0] < 0.5 else [None] * len(points)
        self.start(look=False)
        self.gaze.look(0.2, 0.3, start=1001.7, count=7)  # for "this", which is said 0.5 s in
        self.gaze.look(0.8, 0.7, start=1002.6, count=8)  # for "there", 1 s in

    def test_a_word_that_looked_at_empty_space_is_noted_for_the_editor(self):
        self.looking_at_empty_space_for_there()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.labelled(), [])  # nothing to ask about
        lines = self.editor.sent[0].splitlines()
        self.assertEqual(lines[2], 'Focus (gaze): "this" at 0.5 s → Eagle (100%)')
        self.assertIn('  "there" at 1.0 s → nothing under the gaze', lines)
        self.assertIn('"there" at 1.0 s → nothing under the gaze', json.loads(self.refiner.sent[0])["gaze"])
        self.assertEqual([m["word"] for m in record["gaze"]["misses"]], ["there"])
        self.assertEqual(len(self.bridge.pieces), 1)  # the Eagle's: empty space has no piece, and no 3D point

    def test_each_unclear_word_is_asked_in_order(self):
        self.stt = FakeStt(["move this there", "two", "one"], words=THIS_AND_THERE)
        self.start()
        spoken = self.hold()
        self.wait_for_labels(1)
        self.hold(1004.0, 1005.0)
        self.wait_for_labels(2)
        self.assertEqual(len(self.labelled()), 2)  # the second question came only after the first answer
        self.hold(1006.0, 1007.0)
        wait_for(lambda: spoken["stage"] == "done")
        picked = [t["candidates"][0]["name"] for t in spoken["gaze"]["targets"]]
        self.assertEqual(picked, ["Perch", "Eagle"])
        self.assertEqual(spoken["gaze"]["status"], "resolved")

    def test_a_clear_word_isnt_asked_about(self):
        self.bridge.pick_reply = lambda points: [EAGLE] * 19 + [PERCH] * 4 + [None]
        self.start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.labelled(), [])
        self.assertFalse(record["gaze"]["asked"])

    # --- no answer -----------------------------------------------------------------------------------
    def test_no_answer_falls_back_to_the_selection(self):
        self.bridge.selected = ["Chair"]
        self.start(seconds=0.3)
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: selected Chair (active: Chair)", self.editor.sent[0])
        self.assertNotIn("(gaze)", self.editor.sent[0])
        self.assertEqual(record["gaze"]["status"], "clarify")
        self.assertTrue(record["gaze"]["asked"])
        self.assertNotIn("gaze", record["focus"])
        self.assertTrue(any(t.startswith("Understanding") and "using your selection" in t for t in self.statuses()))
        self.assertEqual(self.bridge.shows[-1], CLEARED)

    def test_no_answer_and_no_selection_says_so(self):
        self.start(seconds=0.3)
        record = self.speak()
        self.assertEqual((record["stage"], record["error"]), ("failed", COULDNT_TELL))
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))
        self.assertIn(f"✗ {COULDNT_TELL}", self.statuses())
        self.assertEqual(self.bridge.shows[-1], CLEARED)
        wait_for(lambda: self.service.snapshot()["state"] == "ready")

    def test_labels_that_cant_be_drawn_mean_no_question(self):
        self.bridge = NoLabelsBridge()
        self.bridge.pick_reply, self.bridge.selected = CLOSE, ["Chair"]
        self.start(seconds=30)
        record = self.speak()  # it doesn't sit out the 30 seconds
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertEqual(self.bridge.shows[-1], CLEARED)

    # --- stop, and the other ways out ------------------------------------------------------------------
    def test_stop_ends_the_wait_for_a_number(self):
        self.stt = FakeStt(["add blue color there", "Stop!"], words=THERE_WORDS)
        self.start(seconds=30)
        spoken = self.hold()
        self.wait_for_labels()
        stopper = self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "stopped")  # at once, not after 30 seconds
        wait_for(lambda: stopper["stage"] == "done")
        self.assertEqual(stopper["reply"], "Stopped.")
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))
        self.assertEqual(self.bridge.shows[-1], CLEARED)
        wait_for(lambda: self.service.snapshot()["state"] == "ready")

    def test_stop_just_before_the_question_still_stops_the_command(self):
        asking, release = threading.Event(), threading.Event()

        def slow_pick(points):
            asking.set()
            release.wait(5)
            return CLOSE(points)

        self.bridge.pick_reply = slow_pick
        self.start(seconds=30)
        spoken = self.hold()
        self.assertTrue(asking.wait(5))
        self.service.say("stop")
        release.set()
        wait_for(lambda: spoken["stage"] == "stopped")
        self.assertEqual(self.labelled(), [])

    def test_turning_voice_off_ends_the_wait(self):
        self.start(seconds=30)
        spoken = self.hold()
        self.wait_for_labels()
        began = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(spoken["stage"], "stopped")
        self.assertEqual(self.bridge.shows[-1], CLEARED)

    def test_the_labels_are_cleared_afterward(self):
        self.stt = FakeStt(["add blue color there", "two"], words=THERE_WORDS)
        self.start()
        spoken = self.hold()
        self.wait_for_labels()
        self.assertEqual(self.bridge.shows[-1]["marks"][0]["n"], 1)
        self.hold(1004.0, 1005.0)
        wait_for(lambda: spoken["stage"] == "done")
        self.assertEqual(self.bridge.shows[-1], CLEARED)
        self.assertEqual(len(self.labelled()), 1)

    # --- beside a Calibrate or a Test, which voice on the mouse doesn't have to wait for ---------------------
    def beside_calibration(self, seconds=30.0):
        self.claim = CameraClaim()
        self.claim.take("gaze")  # a Calibrate or a Test is running, and drawing in the viewport
        self.bridge.selected = ["Chair"]
        service = self.build(gaze_mode="on", gaze_source="mouse", overlay=self.claim)
        service.choice_seconds = seconds
        service.start()
        self.gaze.look(0.5, 0.5, start=1001.6, count=30)

    def test_numbers_are_not_drawn_over_a_calibrate_or_a_test(self):
        self.beside_calibration()
        record = self.speak()  # it doesn't sit out the 30 seconds either: nobody could answer
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.bridge.shows, [])  # its dot was neither covered, nor cleared
        self.assertIn("Focus: selected Chair", self.editor.sent[0])

    def test_voice_starting_during_a_calibrate_leaves_its_dot_on_screen(self):
        self.beside_calibration()
        self.assertEqual(self.bridge.shows, [])

    def test_numbers_are_back_once_the_calibrate_is_over(self):
        self.beside_calibration()
        self.claim.release("gaze")
        self.hold()
        self.wait_for_labels()

    def test_camera_gaze_holding_the_camera_itself_still_gets_its_numbers(self):
        claim = CameraClaim()
        service = self.build(gaze_mode="on", gaze_source="camera", camera=claim, overlay=claim)
        service.start()
        self.assertEqual(claim.holder, "voice")
        self.gaze.look(0.5, 0.5, start=1001.6, count=30)
        self.hold()
        self.wait_for_labels()

    # --- watching only -----------------------------------------------------------------------------------
    def test_shadow_mode_never_asks(self):
        self.bridge.selected = ["Chair"]
        self.start("shadow", seconds=30)
        record = self.speak()  # it doesn't wait for a number
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.labelled(), [])
        self.assertNotIn(WHICH[0], self.statuses())
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertEqual((record["gaze"]["status"], record["gaze"]["asked"]), ("clarify", False))


LOGGED_IN = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})


class GazePictureTest(VoiceTestBase):
    """A big mesh with nothing to name its part: a picture of it turns into one sentence in the Focus line."""

    def setUp(self):
        super().setUp()
        self.arm()

    def arm(self):
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
        self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])])
        self.gaze = FakeGaze()
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [EAGLE] * 19 + [PERCH] * 4 + [None]
        self.bridge.piece_reply = BODY_PIECE
        self.picture = make_noise_picture(64)
        self.bridge.crop_start = {"ok": True, "pending": True}
        self.bridge.crop_png = self.picture.base64
        self.pictures = FakePictures()

    def run_it(self, mode="on", look=True, **looking):
        self.build(gaze_mode=mode, gaze_source="mouse").start()
        if look:
            self.gaze.look(0.5, 0.5, **looking)
        return self.speak()

    def status_texts(self):
        return [text for text, _ in self.bridge.statuses]

    # --- what it does ---------------------------------------------------------------------------------------
    def test_a_big_unnamed_piece_is_looked_at_closer(self):
        record = self.run_it()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.bridge.crops, [{"cmd": "gaze_crop", "u": 0.5, "v": 0.5, "radius": [0.1, 0.15], "size": 512}])
        self.assertEqual(self.bridge.crop_reads, 1)
        self.assertEqual(self.pictures_made, 1)
        call, = self.pictures.calls
        self.assertEqual((call["png"], call["word"], call["object"]), (self.picture.base64, "there", "Eagle"))
        self.assertTrue(15 < call["timeout"] <= 20.0, call["timeout"])  # what is left of the 20 seconds
        self.assertEqual(record["gaze"]["targets"][0]["picture"], "the left wing tip")
        self.assertIn("Looking closer…", self.status_texts())
        self.assertGreaterEqual(record["timings"]["picture"], 0)

    def test_the_editor_gets_the_sentence_and_never_the_image(self):
        record = self.run_it()
        self.assertEqual(self.editor.sent[0], "\n".join([
            "Command: Add blue color there.",
            'Heard: "add blue color there"',
            'Focus (gaze): "there" at 1.0 s → Eagle (79%), Perch (17%)',
            "  point (0.41, 0.12, 0.73) on face 3120 · connected piece of 4000 of 5120 faces",
            '  picture (only a description of what the ring shows): "the left wing tip"',
            "  gaze is accurate to about 10% of the viewport · selected: nothing · mode OBJECT",
        ]))
        self.assertIn("the left wing tip", json.loads(self.refiner.sent[0])["gaze"])
        for sent in (*self.editor.sent, *self.refiner.sent):
            self.assertNotIn("iVBOR", sent)
            self.assertNotIn(self.picture.base64[:60], sent)
        self.assertEqual(record["stage"], "done", record["error"])

    def test_records_never_hold_the_image(self):
        published = []
        self.build(gaze_mode="on", gaze_source="mouse").start()
        real = self.service.bus.publish
        self.service.bus.publish = lambda kind, data: (published.append(json.dumps([kind, data])), real(kind, data))[1]
        self.gaze.look(0.5, 0.5)
        record = self.speak()
        saved = saved_record_text(self.tmp.name, record)
        everything = "\n".join([json.dumps(self.service.snapshot()["recent"]), saved, *published,
                                 *self.status_texts()])
        self.assertIn("the left wing tip", saved)
        self.assertNotIn("iVBOR", everything)
        self.assertNotIn(self.picture.base64[:60], everything)
        self.assertNotIn(self.picture.base64, repr(record))

    def test_the_sentence_goes_to_the_target_it_describes(self):
        self.stt = FakeStt(["move this there"], words=THIS_AND_THERE)
        self.pictures.reply = ["the beak", "the tail feathers"]
        record = self.run_it(start=1001.6, count=30)  # eyes on the Eagle through both words
        self.assertEqual([t["picture"] for t in record["gaze"]["targets"]], ["the beak", "the tail feathers"])
        self.assertEqual([c["word"] for c in self.pictures.calls], ["this", "there"])
        self.assertEqual(len(self.bridge.crops), 2)
        self.assertEqual(self.editor.sent[0].count("picture (only a description"), 2)

    # --- when it doesn't happen -------------------------------------------------------------------------------
    def test_voice_starts_finding_out_whether_pictures_may_be_used_without_waiting_for_it(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.assertEqual((self.pictures.refreshes, self.pictures.ready_calls), (1, 0))
        self.gaze.look(0.5, 0.5)
        self.speak()
        self.assertEqual(self.pictures.refreshes, 2)  # the start, and the key press: a command itself only reads
        self.assertEqual(self.pictures.ready_calls, 1)

    def test_each_key_press_with_pictures_on_asks_again_whether_they_may_be_used(self):
        self.stt = FakeStt(["add blue color there"] * 3, words=THERE_WORDS)
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        for presses in (1, 2, 3):
            self.speak()
            self.assertEqual(self.pictures.refreshes, 1 + presses)

    def test_the_press_refreshes_even_if_it_ends_up_cancelled(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.service.ptt("down", 1002.0)
        self.service.ptt("cancel")
        self.assertEqual(self.pictures.refreshes, 2)  # the press itself, whatever came of it

    def test_watching_only_never_looks_up_whether_pictures_may_be_used_on_a_press(self):
        self.build(gaze_mode="shadow", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.speak()
        self.assertEqual(self.pictures.refreshes, 1)  # only the start

    def test_a_key_press_costs_nothing_when_pictures_are_off(self):
        self.pictures = None
        self.build(gaze_mode="on", gaze_source="mouse").start()
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.pictures_made, 0)

    def test_a_refresh_that_fails_or_is_slow_to_start_cant_stop_a_key_press(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)

        def broken():
            raise OSError("no threads")

        self.pictures.refresh = broken
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(len(self.editor.sent), 1)

    def test_a_picture_part_that_cant_start_looking_leaves_voice_working_without_pictures(self):
        def broken():
            raise OSError("no threads for you")

        self.pictures.refresh = broken
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            record = self.run_it()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual((self.bridge.crops, self.pictures.calls), ([], []))
        self.assertIn("no picture", errors.getvalue())

    def test_a_named_part_needs_no_picture(self):
        self.bridge.piece_reply = WING_PIECE
        record = self.run_it()
        self.assertEqual((self.bridge.crops, self.pictures.calls, self.pictures.ready_calls), ([], [], 0))
        self.assertNotIn("picture", record["timings"])
        self.assertNotIn("picture (", self.editor.sent[0])

    def test_a_small_connected_piece_needs_no_picture(self):
        self.bridge.piece_reply = {**BODY_PIECE, "piece_faces": 40}
        self.run_it()
        self.assertEqual((self.bridge.crops, self.pictures.calls), ([], []))

    def test_pictures_off_means_no_crop_is_taken(self):
        self.pictures = None  # the setting is off: the wiring builds no describer
        record = self.run_it()
        self.assertEqual(self.bridge.crops, [])
        self.assertEqual(self.bridge.crop_reads, 0)
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertNotIn("picture (", self.editor.sent[0])

    def test_a_factory_that_gives_nothing_means_pictures_off_too(self):
        self.pictures = FakePictures()
        self.make_pictures = lambda: None
        record = self.run_it()
        self.assertEqual(self.bridge.crops, [])
        self.assertEqual(record["stage"], "done", record["error"])

    def test_shadow_mode_sends_no_picture(self):
        self.bridge.selected = ["Chair"]
        record = self.run_it("shadow")
        self.assertEqual((self.bridge.crops, self.bridge.crop_reads, self.pictures.calls, self.pictures.ready_calls),
                         ([], 0, [], 0))
        self.assertIsNone(record["gaze"]["targets"][0]["picture"])
        self.assertNotIn("picture (", self.editor.sent[0])
        self.assertNotIn("picture", record["timings"])

    def test_pictures_stay_off_until_the_check_has_passed(self):
        self.pictures.is_ready = False
        record = self.run_it()
        self.assertGreaterEqual(self.pictures.ready_calls, 1)
        self.assertEqual((self.bridge.crops, self.pictures.calls), ([], []))
        self.assertNotIn("Looking closer…", self.status_texts())
        self.assertNotIn("picture", record["timings"])
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn('Focus (gaze): "there"', self.editor.sent[0])  # the text-only Focus line, as without pictures

    def test_a_picture_is_not_taken_for_a_guess_nobody_settled(self):
        self.bridge.pick_reply = lambda points: [EAGLE] * 11 + [PERCH] * 10 + [None] * 3  # too close to call
        self.bridge.selected = ["Chair"]
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.service.choice_seconds = 0.3  # nobody answers
        self.gaze.look(0.5, 0.5)
        record = self.speak()
        self.assertEqual((self.bridge.crops, self.pictures.calls), ([], []))
        self.assertIn("Focus: selected Chair", self.editor.sent[0])
        self.assertEqual(record["stage"], "done", record["error"])

    def test_unity_never_starts_the_picture_part(self):
        self.stt = FakeStt(["add a light there"], words=THERE_WORDS)
        self.build(host="unity", gaze_mode="on", gaze_source="mouse").start()
        self.speak()
        self.assertEqual((self.pictures_made, self.bridge.crops), (0, []))

    # --- when it goes wrong: the edit goes on as if pictures were off --------------------------------------------
    def carries_on(self, record):
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIsNone(record["gaze"]["targets"][0]["picture"])
        self.assertNotIn("picture (", self.editor.sent[0])
        self.assertIn('Focus (gaze): "there" at 1.0 s', self.editor.sent[0])
        self.assertEqual(len(self.editor.sent), 1)

    def test_an_add_on_from_before_pictures_carries_on_text_only(self):
        self.bridge.crop_start = {"ok": False, "error": "unknown command"}
        record = self.run_it()
        self.carries_on(record)
        self.assertEqual((len(self.bridge.crops), self.bridge.crop_reads, self.pictures.calls), (1, 0, []))

    def test_a_crop_that_cant_be_made_carries_on(self):
        self.bridge.crop_start = {"ok": False, "error": "no 3D viewport"}
        self.carries_on(self.run_it())
        self.assertEqual(self.pictures.calls, [])

    def test_a_crop_that_is_refused_because_one_is_still_pending_carries_on(self):
        self.bridge.crop_start = {"ok": False, "error": "a picture is already being taken"}
        self.carries_on(self.run_it())
        self.assertEqual((self.pictures.calls, self.bridge.crop_reads), ([], 0))

    def test_a_viewport_that_never_drew_carries_on(self):
        self.bridge.crop_png = None  # the add-on gave up after three seconds: its result is an error
        self.bridge.crop_pending = 2
        record = self.run_it()
        self.carries_on(record)
        self.assertEqual((self.bridge.crop_reads, self.pictures.calls), (3, []))

    def test_waiting_for_the_picture_polls_until_it_is_ready(self):
        self.bridge.crop_pending = 4
        record = self.run_it()
        self.assertEqual(self.bridge.crop_reads, 5)
        self.assertEqual(record["gaze"]["targets"][0]["picture"], "the left wing tip")

    def test_a_picture_that_never_comes_is_given_up_on_at_the_deadline(self):
        self.bridge.crop_pending = 10 ** 9
        with patch("gazevoice.voice.service.PICTURE_SECONDS", 0.5):
            began = time.monotonic()
            record = self.run_it()
        self.carries_on(record)
        self.assertLess(time.monotonic() - began, 5.0)
        self.assertGreaterEqual(record["timings"]["picture"], 0.5)

    def test_a_second_target_gets_no_crop_once_the_time_is_up(self):
        self.stt = FakeStt(["move this there"], words=THIS_AND_THERE)

        def slow_describer(call):
            time.sleep(0.6)  # takes all the time there is
            return "the beak"

        self.pictures.reply = slow_describer
        with patch("gazevoice.voice.service.PICTURE_SECONDS", 0.4):
            record = self.run_it(start=1001.6, count=30)
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(len(self.bridge.crops), 1)  # not even a picture is taken for the second one
        self.assertEqual([t["picture"] for t in record["gaze"]["targets"]], ["the beak", None])

    def test_a_describer_that_says_nothing_or_unclear_carries_on(self):
        self.pictures.reply = None
        self.carries_on(self.run_it())

    def test_a_describer_that_blows_up_carries_on_and_says_nothing_of_why(self):
        self.pictures.reply = RuntimeError("boom iVBORw0KGgoAAAANSUhEUgAAAQ")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            record = self.run_it()
        self.carries_on(record)
        self.assertNotIn("iVBOR", errors.getvalue())  # nothing a describer says is repeated: it may hold the picture
        self.assertNotIn("boom", errors.getvalue())
        self.assertIn("no picture", errors.getvalue())

    def test_a_describer_that_cant_even_say_if_it_is_ready_carries_on(self):
        self.pictures.ready = lambda: (_ for _ in ()).throw(OSError("claude vanished"))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            record = self.run_it()
        self.carries_on(record)
        self.assertNotIn("claude vanished", errors.getvalue())
        self.assertEqual(self.bridge.crops, [])

    def test_a_bridge_that_dies_while_the_picture_is_being_made_carries_on(self):
        self.bridge.gaze_error = None
        real = self.bridge.gaze

        def dying(cmd, req):
            if cmd == "gaze_crop_result":
                raise ConnectionResetError("Blender went away")
            return real(cmd, req)

        self.bridge.gaze = dying
        self.carries_on(self.run_it())

    def test_a_picture_that_is_not_a_string_is_not_sent(self):
        self.bridge.crop_png = {"nope": 1}
        self.carries_on(self.run_it())
        self.assertEqual(self.pictures.calls, [])

    # --- stopping ----------------------------------------------------------------------------------------------
    def wait_in_the_describer(self):
        """A describer that waits as long as it is allowed to, the way a slow process would, and reports why it left."""
        started, left = threading.Event(), []

        def describe(call):
            started.set()
            end = time.monotonic() + 8
            while time.monotonic() < end and not call["cancelled"]():
                time.sleep(0.02)
            left.append("cancelled" if call["cancelled"]() else "timed out")
            return None

        self.pictures.reply = describe
        return started, left

    def test_stop_during_the_picture_ends_the_describer_and_the_command(self):
        started, left = self.wait_in_the_describer()
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        spoken = self.last_record()
        self.assertTrue(started.wait(5))
        self.service.say("stop")
        wait_for(lambda: spoken["stage"] in ("stopped", "done", "failed"))
        self.assertEqual(spoken["stage"], "stopped")
        self.assertEqual(left, ["cancelled"])
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))

    def test_turning_voice_off_during_the_picture_ends_the_describer(self):
        started, left = self.wait_in_the_describer()
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - began, 5.0)
        self.assertEqual(left, ["cancelled"])
        self.assertEqual(self.service.snapshot()["state"], "off")
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))


class GazePictureRealRouteTest(VoiceTestBase):
    """The whole path with the real describer talking to fake_claude.py. The app folder is the service's own folder."""

    def setUp(self):
        super().setUp()
        self.out = tempfile.TemporaryDirectory()  # what fake_claude.py reports, kept out of the app folder
        self.pid_file = Path(self.out.name) / "pid"
        self.paths = AppPaths(Path(self.tmp.name))
        self.paths.ensure()
        write_picture_check(self.paths.gaze_picture_check, "2.1.303", 1.0)
        self.picture = make_noise_picture(256)
        self.arm()

    def arm(self):
        """Everything one voice run uses, new: a second run in the same test starts from here."""
        self.stt = FakeStt(["add blue color there"], words=THERE_WORDS)
        self.refiner = FakeSession([TurnResult(THERE_REFINED, False)])
        self.editor = FakeSession([TurnResult("Made the wing blue.", False, ["execute_blender_code"])])
        self.editors_made = self.pictures_made = 0
        self.gaze = FakeGaze()
        self.mic = FakeMic()
        self.bridge = FakeBridge()
        self.bridge.selected = []
        self.bridge.pick_reply = lambda points: [EAGLE] * 19 + [PERCH] * 4 + [None]
        self.bridge.piece_reply = BODY_PIECE
        self.bridge.crop_start = {"ok": True, "pending": True}
        self.bridge.crop_png = self.picture.base64
        self.pid_file.unlink(missing_ok=True)
        self.use_claude()

    def tearDown(self):
        super().tearDown()
        self.out.cleanup()

    def use_claude(self, reply=None, hung=0.0, clock=time.monotonic, **env):
        runner = FakeRunner(which={"claude": "/Users/me/.local/bin/claude"})
        runner.add(["claude", "--version"], stdout="2.1.303 (Claude Code)\n", delay=hung)
        runner.add(["claude", "auth", "status"], stdout=LOGGED_IN)
        environ = {"FAKE_CLAUDE_PID_OUT": str(self.pid_file)}
        if reply is not None:
            environ["FAKE_CLAUDE_PICTURE"] = reply
        self.runner = runner
        self.pictures = Describer(self.paths, runner, command=FAKE_CLAUDE, env={**environ, **env}, clock=clock)

    def make_pictures(self):
        clear_leftover_images(self.paths)
        return super().make_pictures()

    def run_it(self, mode="on", ready=True):
        self.build(gaze_mode=mode, gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        if ready:
            wait_for(self.pictures.ready)  # voice asked claude about itself as it started; that takes a moment
        return self.speak()

    def picture_data_in_the_app_folder(self):
        found = []
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                if b"\x89PNG" in data or b"iVBOR" in data:
                    found.append(path)
        return found

    def pid(self):
        return int(self.pid_file.read_text())

    def process_gone(self):
        try:
            os.kill(self.pid(), 0)
        except ProcessLookupError:
            return True
        return False

    def test_the_sentence_comes_from_a_process_that_has_exited(self):
        record = self.run_it()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertIn('  picture (only a description of what the ring shows): "the left wing tip"', self.editor.sent[0])
        self.assertTrue(wait_until_true(self.process_gone))  # its input was closed, and it leaves by itself

    def test_the_edit_isnt_held_up_while_the_describer_is_still_leaving(self):
        self.use_claude(FAKE_CLAUDE_EXIT_DELAY="3")
        began = time.monotonic()
        record = self.run_it()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertIn("picture (", self.editor.sent[0])
        self.assertFalse(self.process_gone())  # still tidying up, as the edit goes on
        self.assertTrue(wait_until_true(self.process_gone, 8.0))

    def speak_for_a_moment(self):
        """Press, hold while any lookup finishes (which is quicker than saying anything), release, wait for the command."""
        count = len(self.service.recent)
        self.service.ptt("down", 1002.0)
        wait_until_true(lambda: not [t for t in threading.enumerate() if t.name == "picture-ready"], 5.0)
        self.service.ptt("up", 1003.0)
        wait_for(lambda: len(self.service.recent) > count and self.last_record()["stage"] in ("done", "failed", "stopped"))
        return self.last_record()

    def test_a_command_after_a_long_quiet_spell_still_gets_its_picture(self):
        gaps = (30, 90, 170, 185, 200, 600)  # seconds between commands that need a picture; answers are stale after 180
        clock = FakeClock()
        self.use_claude(hung=0.3, clock=clock)  # a healthy claude that takes a moment to say its version
        self.stt = FakeStt(["add blue color there"] * (len(gaps) + 1), words=THERE_WORDS)
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        wait_for(self.pictures.ready)
        first = self.speak_for_a_moment()
        self.assertEqual(first["gaze"]["targets"][0]["picture"], "the left wing tip")
        for gap in gaps:
            # Minutes pass with nothing running: whatever lookup the last command set going has long since finished.
            wait_until_true(lambda: not [t for t in threading.enumerate() if t.name == "picture-ready"], 5.0)
            clock.advance(gap)
            record = self.speak_for_a_moment()
            self.assertEqual(record["stage"], "done", record["error"])
            self.assertEqual(record["gaze"]["targets"][0]["picture"], "the left wing tip", f"after {gap} s")
        self.assertEqual(len(self.bridge.crops), len(gaps) + 1)  # every one of them took a picture

    def test_ready_still_never_waits_after_a_long_quiet_spell(self):
        clock = FakeClock()
        self.use_claude(hung=0.6, clock=clock)
        self.build(gaze_mode="on", gaze_source="mouse").start()
        wait_for(self.pictures.ready)
        clock.advance(500)  # everything it knew is stale, and the next lookup takes 0.6 s
        began = time.monotonic()
        self.assertFalse(self.pictures.ready())  # starts that lookup, and answers from what it has: no
        self.assertLess(time.monotonic() - began, 0.2)

    def test_voice_asks_about_claude_as_it_starts_and_not_again_per_command(self):
        self.build(gaze_mode="on", gaze_source="mouse").start()
        wait_for(self.pictures.ready)
        asked = self.runner.calls.count(("claude", "--version"))
        self.assertEqual(asked, 1)
        self.gaze.look(0.5, 0.5)
        record = self.speak()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertEqual(self.runner.calls.count(("claude", "--version")), 1)

    def test_a_claude_that_hangs_on_its_version_costs_the_command_nothing(self):
        self.use_claude(hung=2.0)  # `claude --version` takes two seconds to answer
        began = time.monotonic()
        record = self.run_it(ready=False)
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertLess(time.monotonic() - began, 1.5)  # no waiting for it
        self.assertEqual(self.bridge.crops, [])  # no answer yet: this command went on as text only
        self.assertNotIn("picture", record["timings"])
        self.assertNotIn("Looking closer…", [text for text, _ in self.bridge.statuses])
        self.assertEqual(self.runner.calls.count(("claude", "--version")), 1)  # and the command didn't ask it again
        self.assertTrue(wait_until_true(self.pictures.ready, 6.0))  # when it does answer, the next one can have a picture

    def test_turning_voice_off_while_claude_is_being_asked_doesnt_wait_for_it(self):
        self.use_claude(hung=3.0)
        self.build(gaze_mode="on", gaze_source="mouse").start()
        began = time.monotonic()
        self.service.stop()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(self.service.snapshot()["state"], "off")
        lookers = [t for t in threading.enumerate() if t.name == "picture-ready"]
        self.assertTrue(all(t.daemon for t in lookers))  # it can't keep the app from quitting either
        self.assertTrue(wait_until_true(lambda: not [t for t in threading.enumerate() if t.name == "picture-ready"], 6.0))

    def test_a_stop_while_claude_is_being_asked_still_stops_the_command(self):
        self.use_claude(hung=3.0)
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        spoken = self.last_record()
        self.service.say("stop")
        wait_for(lambda: spoken["stage"] in ("stopped", "done", "failed"))
        self.assertIn(spoken["stage"], ("stopped", "done"))  # whichever got there first; neither waited for claude
        self.assertTrue(wait_until_true(lambda: not [t for t in threading.enumerate() if t.name == "picture-ready"], 6.0))

    def test_no_picture_is_left_behind(self):
        # The scan has to be able to see one: plant a copy and watch it get found.
        planted = Path(self.tmp.name) / "work" / "planted.txt"
        planted.write_bytes(b"iVBORw0KGgoAAAA")
        self.assertEqual(self.picture_data_in_the_app_folder(), [planted])
        planted.unlink()
        for reply in (None, "fail", "die"):  # a full run, then runs where the describer fails
            with self.subTest(reply=reply):
                self.arm()
                if reply:
                    self.use_claude(reply)
                record = self.run_it()
                self.assertEqual(record["stage"], "done", record["error"])
                saved_record_text(self.tmp.name, record)  # it is written just after the stage says "done"
                self.assertEqual(self.picture_data_in_the_app_folder(), [])
                self.assertTrue(wait_until_true(self.process_gone))
                self.service.stop()

    def test_a_slow_describer_is_given_up_on_and_the_edit_carries_on(self):
        self.use_claude("slow")
        with patch("gazevoice.voice.service.PICTURE_SECONDS", 0.8):
            began = time.monotonic()
            record = self.run_it()
        self.assertEqual(record["stage"], "done", record["error"])
        self.assertLess(time.monotonic() - began, 6.0)
        self.assertIsNone(record["gaze"]["targets"][0]["picture"])
        self.assertNotIn("picture (", self.editor.sent[0])
        self.assertEqual(len(self.editor.sent), 1)  # the edit still happened, on the text-only Focus line
        self.assertTrue(wait_until_true(self.process_gone))

    def test_stop_kills_the_describer_that_is_still_thinking(self):
        self.use_claude("slow")
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        spoken = self.last_record()
        wait_for(lambda: self.pid_file.exists())
        self.service.say("stop")
        wait_for(lambda: spoken["stage"] == "stopped")
        self.assertTrue(wait_until_true(self.process_gone))
        self.assertEqual((self.refiner.sent, self.editor.sent), ([], []))

    def test_turning_voice_off_kills_the_describer_that_is_still_thinking(self):
        self.use_claude("slow")
        self.build(gaze_mode="on", gaze_source="mouse").start()
        self.gaze.look(0.5, 0.5)
        self.service.ptt("down", 1002.0)
        self.service.ptt("up", 1003.0)
        wait_for(lambda: self.pid_file.exists())
        self.service.stop()
        self.assertTrue(wait_until_true(self.process_gone))

    def test_leftover_images_are_cleared_when_voice_starts(self):
        images = self.paths.gaze_images
        images.mkdir(parents=True)
        (images / "gazevoice-gaze-77.png").write_bytes(b"\x89PNG left behind by a crash")
        (images / "other.txt").write_text("not ours")
        for mode in ("off", "on"):  # whatever the gaze setting is
            self.arm()
            (images / "gazevoice-gaze-77.png").write_bytes(b"\x89PNG left behind by a crash")
            self.build(gaze_mode=mode, gaze_source="mouse").start()
            self.assertEqual(sorted(p.name for p in images.iterdir()), ["other.txt"], mode)
            self.service.stop()

    def test_records_never_hold_the_image(self):
        record = self.run_it()
        saved = saved_record_text(self.tmp.name, record)
        self.assertIn("the left wing tip", saved)
        self.assertNotIn("iVBOR", saved)
        self.assertNotIn("iVBOR", json.dumps(self.service.snapshot()["recent"]))


class FakeClock:
    """A clock that only moves when told to: time that passes between commands without the test waiting for it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def saved_record_text(root, record) -> str:
    """record.json, once it has been written: it appears a moment after the command's stage says it is done."""
    path = Path(root) / "commands" / record["id"] / "record.json"

    def written() -> bool:
        try:
            json.loads(path.read_text())  # whole, not just begun
        except (OSError, ValueError):
            return False
        return True

    wait_for(written)
    return path.read_text()


def wait_until_true(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


if __name__ == "__main__":
    unittest.main()
