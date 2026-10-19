import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gazevoice.voice.brains import BrainError, ClaudeSession, CodexSession, claude_session_argv

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLAUDE = [sys.executable, str(FIXTURES / "fake_claude.py")]
FAKE_CODEX = [sys.executable, str(FIXTURES / "fake_codex.py")]


class ArgvTest(unittest.TestCase):
    def test_editor_argv(self):
        argv = claude_session_argv(Path("/p/editor.md"), "sonnet", Path("/p/mcp.json"), max_turns=12)
        self.assertEqual(argv[:2], ["claude", "-p"])
        for flag in ("--input-format", "--output-format", "--verbose", "--permission-prompts", "--tools",
                     "--setting-sources", "--strict-mcp-config", "--disable-slash-commands",
                     "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/p/mcp.json")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__blender")
        self.assertEqual(argv[argv.index("--system-prompt-file") + 1], "/p/editor.md")
        self.assertNotIn("--bare", argv)

    def test_thinking_effort(self):
        argv = claude_session_argv(Path("/p/editor.md"), "claude-opus-5-5", None, effort="max")
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertNotIn("--effort", claude_session_argv(Path("/p/editor.md"), "claude-opus-5-5", None, effort="default"))
        self.assertNotIn("--effort", claude_session_argv(Path("/p/editor.md"), "claude-opus-5-5", None))

    def test_refiner_argv_has_no_tools_and_default_model_is_omitted(self):
        argv = claude_session_argv(Path("/p/refiner.md"), "default", None, max_turns=1)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)


class ClaudeSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, *extra, env=None):
        return ClaudeSession([*FAKE_CLAUDE, *extra], cwd=self.tmp.name, env=env)

    def test_two_turns_over_one_process(self):
        s = self.session()
        s.start()
        try:
            first = s.send("hello")
            second = s.send("again")
            self.assertEqual((first.text, second.text), ("echo: hello", "echo: again"))
            self.assertFalse(first.is_error)
            self.assertEqual(s.turns, 2)
        finally:
            s.close()

    def test_tools_are_reported_live_and_in_the_result(self):
        seen = []
        s = self.session()
        s.start()
        try:
            turn = s.send("please use tool", on_tool=lambda name: seen.append(name))
        finally:
            s.close()
        self.assertEqual(turn.tools, ["execute_blender_code"])
        self.assertEqual(seen, ["execute_blender_code"])
        self.assertEqual(turn.text, "Made the chair twice as tall.")

    def test_error_result(self):
        s = self.session()
        s.start()
        try:
            turn = s.send("this will fail")
        finally:
            s.close()
        self.assertTrue(turn.is_error)
        self.assertEqual(turn.text, "Something broke")

    def test_death_mid_turn_raises(self):
        s = self.session()
        s.start()
        with self.assertRaises(BrainError):
            s.send("die now", timeout=5)
        self.assertFalse(s.alive)
        s.close()

    def test_warmup_checks_blender_mcp(self):
        s = self.session("--mcp-config", "cfg.json", env={"FAKE_MCP_STATUS": "failed"})
        with self.assertRaises(BrainError) as caught:
            s.start(warmup="Reply ready", require_mcp="blender")
        self.assertIn("failed", str(caught.exception))
        s.close()
        ok = self.session("--mcp-config", "cfg.json")
        ok.start(warmup="Reply ready", require_mcp="blender")
        self.assertTrue(ok.alive)
        ok.close()

    def test_interrupt_ends_the_turn_and_keeps_the_session(self):
        s = self.session()
        s.start()
        try:
            threading.Timer(0.3, s.interrupt).start()
            started = time.monotonic()
            turn = s.send("slow job", timeout=10)
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue(turn.is_error)
            self.assertTrue(turn.interrupted)
            self.assertTrue(s.alive)
            after = s.send("still here")
            self.assertEqual(after.text, "echo: still here")
            self.assertFalse(after.interrupted)
        finally:
            s.close()

    def test_a_turn_that_times_out_ends_the_session(self):
        s = self.session()
        s.start()
        try:
            with self.assertRaises(BrainError) as caught:
                s.send("slow job", timeout=0.5)
            self.assertIn("too long", str(caught.exception))
            self.assertFalse(s.alive)  # so its late answer can't become the next command's answer
            with self.assertRaises(BrainError):
                s.send("make it red")
        finally:
            s.close()

    def test_interrupt_sent_before_the_turn_begins_still_lands(self):
        s = self.session(env={"FAKE_CLAUDE_STARTUP": "0.8"})
        s.interrupt_grace = 3
        s.start()
        try:
            threading.Timer(0.1, s.interrupt).start()
            started = time.monotonic()
            turn = s.send("slow job", timeout=10)
            self.assertLess(time.monotonic() - started, 2.5)  # neither the slow job nor the fallback
            self.assertTrue(turn.interrupted)
            self.assertTrue(s.alive)
        finally:
            s.close()

    def test_interrupt_between_turns_does_nothing(self):
        s = self.session()
        s.start()
        try:
            s.interrupt()
            turn = s.send("hello")
            self.assertEqual((turn.text, turn.interrupted), ("echo: hello", False))
        finally:
            s.close()

    def test_ignored_interrupt_stops_the_process(self):
        s = self.session()
        s.interrupt_grace = 0.3
        s.start()
        try:
            threading.Timer(0.3, s.interrupt).start()
            started = time.monotonic()
            with self.assertRaises(BrainError):
                s.send("deaf job", timeout=10)
            self.assertLess(time.monotonic() - started, 4)
            self.assertFalse(s.alive)
        finally:
            s.close()


class CodexSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.argv_log = Path(self.tmp.name) / "argv.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_turn_carries_instructions_then_resumes_thread(self):
        mcp = {"command": "uv", "args": ["run", "blender-mcp"], "env": {}}
        s = CodexSession(cwd=self.tmp.name, instructions="You edit Blender.", mcp_cmd=mcp,
                         command=FAKE_CODEX, env={"FAKE_CODEX_ARGV_OUT": str(self.argv_log)})
        seen = []
        first = s.send("make it taller", on_tool=seen.append)
        second = s.send("now wider")
        calls = [json.loads(line) for line in self.argv_log.read_text().splitlines()]
        self.assertEqual(first.text, "Made the chair taller.")
        self.assertEqual(first.tools, ["execute_blender_code"])
        self.assertEqual(seen, ["execute_blender_code"])
        self.assertIn("You edit Blender.", calls[0][-1])
        self.assertNotIn("resume", calls[0])
        self.assertEqual(calls[1][calls[1].index("resume") + 1], "thr-123")
        self.assertNotIn("You edit Blender.", calls[1][-1])
        self.assertIn("--json", calls[1])
        self.assertTrue(any(a.startswith("mcp_servers.blender.command=") for a in calls[0]))
        self.assertFalse(second.is_error)

    def test_failed_turn(self):
        s = CodexSession(cwd=self.tmp.name, instructions="", mcp_cmd=None, command=FAKE_CODEX)
        turn = s.send("this will fail")
        self.assertTrue(turn.is_error)
        self.assertIn("model refused", turn.text)

    def test_interrupt_marks_the_turn_interrupted(self):
        s = CodexSession(cwd=self.tmp.name, instructions="", mcp_cmd=None, command=FAKE_CODEX)
        threading.Timer(0.5, s.interrupt).start()
        started = time.monotonic()
        turn = s.send("slow job", timeout=10)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(turn.is_error)
        self.assertTrue(turn.interrupted)

    def test_timeout_is_a_failure_not_an_interrupt(self):
        s = CodexSession(cwd=self.tmp.name, instructions="", mcp_cmd=None, command=FAKE_CODEX)
        turn = s.send("slow job", timeout=0.5)
        self.assertTrue(turn.is_error)
        self.assertFalse(turn.interrupted)
        self.assertIn("too long", turn.text)


if __name__ == "__main__":
    unittest.main()
