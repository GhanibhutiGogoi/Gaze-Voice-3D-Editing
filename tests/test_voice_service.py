import contextlib
import io
import json
import math
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gazevoice.events import EventBus
from gazevoice.voice.audio import MicError, RingBuffer
from gazevoice.voice.brains import BrainError, TurnResult
from gazevoice.voice.service import Factories, VoiceError, VoiceService
from gazevoice.voice.stt import SpeechError


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


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
            return {"ok": True, "active": "Chair", "selected": ["Chair"], "mode": "OBJECT"}
        if cmd == "scene_names":
            return {"ok": True, "names": ["Chair", "Table"]}
        if cmd == "status":
            self.statuses.append((req["text"], req.get("state")))
            return {"ok": True}
        if cmd == "begin_edit":
            return {"ok": True}
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
    def __init__(self, texts, block=None):
        self.texts = list(texts)
        self.block = block  # start() waits for this (a slow model load)
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
        return {"text": self.texts.pop(0) if self.texts else "", "words": [], "seconds": 0.1}

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


REFINED = json.dumps({"kind": "command", "text": "Make this twice as tall."})


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

    def tearDown(self):
        if hasattr(self, "service"):
            self.service.stop()
        self.tmp.cleanup()

    def make_editor(self):
        self.editors_made += 1
        return self.editor if self.editors_made == 1 else self.next_editor

    def build(self, **kwargs):
        factories = Factories(
            bridge=lambda: self.bridge, mic=lambda: self.mic, stt=lambda: self.stt,
            refiner=lambda: self.refiner, editor=self.make_editor,
        )
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


if __name__ == "__main__":
    unittest.main()
