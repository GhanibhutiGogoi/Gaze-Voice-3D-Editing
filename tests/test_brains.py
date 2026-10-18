import json
import socket
import unittest

from gazevoice.components.base import Health
from gazevoice.components.brains import (
    CONNECT_PROMPT,
    ClaudeCode,
    Codex,
    TEST_PROMPT,
    brain_pending,
    claude_headless_argv,
    codex_exec_argv,
    parse_codex_jsonl,
    pick_brain,
    write_mcp_config,
)
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut

from .helpers import TempCtx, make_job

CLAUDE = {"claude": "/Users/me/.local/bin/claude"}
CODEX = {"codex": "/opt/homebrew/bin/codex"}


class ClaudeCodeTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which=CLAUDE)
        self.t.runner.add(["claude", "--version"], stdout="2.1.260 (Claude Code)\n")

    def tearDown(self):
        self.t.cleanup()

    def test_missing(self):
        self.t.runner.set_which("claude", None)
        state = ClaudeCode(self.t.ctx).check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Install Claude Code", state.summary)
        self.assertEqual(state.actions, ())

    def test_logged_in_with_subscription(self):
        self.t.runner.add(["claude", "auth", "status"], stdout=json.dumps({"authMethod": "claude.ai"}))
        state = ClaudeCode(self.t.ctx).check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Logged in · Claude subscription")
        self.assertIn(("Version", "2.1.260"), state.details)
        test = next(a for a in state.actions if a.id == "test")
        self.assertTrue(test.uses_plan)

    def test_logged_in_with_api_key(self):
        self.t.runner.add(["claude", "auth", "status"], stdout=json.dumps({"authMethod": "api_key"}))
        self.assertEqual(ClaudeCode(self.t.ctx).check().summary, "Logged in · API key")

    def test_not_logged_in(self):
        self.t.runner.add(["claude", "auth", "status"], returncode=1, stdout=json.dumps({"authMethod": "none"}))
        state = ClaudeCode(self.t.ctx).check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("claude auth login", state.summary)

    def test_timed_out_login_check_is_not_reported_as_logged_out(self):
        self.t.runner.add(["claude", "auth", "status"], returncode=124, stderr="claude: timed out after 15s")
        with self.assertRaises(ProbeTimedOut):
            ClaudeCode(self.t.ctx).check()

    def test_test_action_returns_reply(self):
        self.t.runner.add(
            claude_headless_argv(TEST_PROMPT),
            stdout=json.dumps({"type": "result", "is_error": False, "result": "ready"}),
        )
        result = ClaudeCode(self.t.ctx).run_action("test", make_job("claude"))
        self.assertEqual(result["reply"], "ready")
        self.assertIn("seconds", result)

    def test_test_action_error_result_fails(self):
        self.t.runner.add(
            claude_headless_argv(TEST_PROMPT),
            stdout=json.dumps({"type": "result", "is_error": True, "result": "Invalid API key"}),
        )
        with self.assertRaises(JobError):
            ClaudeCode(self.t.ctx).run_action("test", make_job("claude"))

    def test_headless_argv_isolates_session(self):
        argv = claude_headless_argv("hi", ["--max-turns", "2"])
        self.assertEqual(argv[:4], ["claude", "-p", "--output-format", "json"])
        for flag in ("--tools", "--setting-sources", "--disable-slash-commands", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertNotIn("--bare", argv)
        self.assertEqual(argv[-3:], ["--max-turns", "2", "hi"])


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which=CODEX)
        self.t.runner.add(["codex", "--version"], stdout="codex-cli 0.153.4\n")

    def tearDown(self):
        self.t.cleanup()

    def test_missing(self):
        self.t.runner.set_which("codex", None)
        state = Codex(self.t.ctx).check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Install Codex", state.summary)

    def test_logged_in_with_api_key_hides_key(self):
        self.t.runner.add(["codex", "login", "status"], stdout="Logged in using an API key - 049d756c***3c302\n")
        state = Codex(self.t.ctx).check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Logged in · API key")
        self.assertIn(("Version", "0.153.4"), state.details)
        self.assertNotIn("049d", json.dumps(state.to_dict()))

    def test_logged_in_with_chatgpt(self):
        self.t.runner.add(["codex", "login", "status"], stderr="Logged in using ChatGPT\n")
        self.assertEqual(Codex(self.t.ctx).check().summary, "Logged in · ChatGPT plan")

    def test_timed_out_login_check_raises(self):
        self.t.runner.add(["codex", "login", "status"], returncode=124, stderr="codex: timed out after 15s")
        with self.assertRaises(ProbeTimedOut):
            Codex(self.t.ctx).check()

    def test_not_logged_in(self):
        self.t.runner.add(["codex", "login", "status"], returncode=1, stderr="Not logged in\n")
        state = Codex(self.t.ctx).check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("codex login", state.summary)

    def test_test_action_parses_jsonl(self):
        events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "ready"}},
            {"type": "turn.completed"},
        ]
        self.t.runner.add(codex_exec_argv(TEST_PROMPT), stdout="\n".join(json.dumps(e) for e in events))
        result = Codex(self.t.ctx).run_action("test", make_job("codex"))
        self.assertEqual(result["reply"], "ready")


class ConnectBlenderTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which={**CLAUDE, **CODEX})
        self.t.runner.add(["claude", "--version"], stdout="2.1.260 (Claude Code)\n")
        self.t.runner.add(["claude", "auth", "status"], stdout=json.dumps({"authMethod": "claude.ai"}))
        self.t.runner.add(["codex", "--version"], stdout="codex-cli 0.153.4\n")
        self.t.runner.add(["codex", "login", "status"], stdout="Logged in using an API key - x***\n")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)

    def tearDown(self):
        self.listener.close()
        self.t.cleanup()

    def install_server(self):
        venv_python = self.t.paths.mcp_server_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("")
        (self.t.paths.mcp_server_dir / "pyproject.toml").write_text("")

    def claude(self):
        component = ClaudeCode(self.t.ctx)
        component.addon_port = self.listener.getsockname()[1]
        return component

    def test_write_mcp_config(self):
        cmd = {"command": "uv", "args": ["run", "blender-mcp"], "env": {"BLENDER_PATH": "/B"}}
        path = write_mcp_config(self.t.paths, cmd)
        self.assertEqual(json.loads(path.read_text()), {"mcpServers": {"blender": cmd}})

    def test_not_offered_until_mcp_server_installed(self):
        self.assertFalse(self.claude().check().offers("connect_blender"))

    def test_not_offered_while_blender_addon_not_listening(self):
        self.install_server()
        component = self.claude()
        component.addon_port = 1  # nothing listens there
        self.assertFalse(component.check().offers("connect_blender"))

    def test_connect_blender_uses_only_the_blender_mcp(self):
        self.install_server()
        component = self.claude()
        state = component.check()
        self.assertTrue(state.offers("connect_blender"))
        self.assertTrue(next(a for a in state.actions if a.id == "connect_blender").uses_plan)
        cfg = str(self.t.paths.root / "mcp-blender.json")
        argv = claude_headless_argv(CONNECT_PROMPT, ["--mcp-config", cfg, "--allowedTools", "mcp__blender", "--max-turns", "6"])
        self.t.runner.add(argv, stdout=json.dumps({"type": "result", "is_error": False, "result": "3"}))
        result = component.run_action("connect_blender", make_job("claude", "connect_blender"))
        self.assertEqual(result["reply"], "3")
        self.assertIn(tuple(argv), self.t.runner.calls)
        self.assertIn("blender", json.loads((self.t.paths.root / "mcp-blender.json").read_text())["mcpServers"])

    def test_codex_connect_is_marked_experimental(self):
        self.install_server()
        component = Codex(self.t.ctx)
        component.addon_port = self.listener.getsockname()[1]
        spec = next(a for a in component.check().actions if a.id == "connect_blender")
        self.assertIn("experimental", spec.label.lower())


class ParseAndPickTest(unittest.TestCase):
    def test_parse_codex_jsonl_takes_last_agent_message(self):
        text = "\n".join(
            [
                "not json",
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ready"}}),
            ]
        )
        self.assertEqual(parse_codex_jsonl(text), "ready")
        self.assertIsNone(parse_codex_jsonl("{}"))

    def _states(self, claude, codex):
        t = TempCtx()
        try:
            make = lambda cls, health: cls(t.ctx).state(health, "")  # noqa: E731
            return {"claude": make(ClaudeCode, claude), "codex": make(Codex, codex)}
        finally:
            t.cleanup()

    def test_pick_brain(self):
        both = self._states(Health.READY, Health.READY)
        self.assertEqual(pick_brain(both, "auto"), "claude")
        only_codex = self._states(Health.INSTALLED, Health.READY)
        self.assertEqual(pick_brain(only_codex, "auto"), "codex")
        self.assertIsNone(pick_brain(self._states(Health.READY, Health.MISSING), "codex"))
        self.assertEqual(pick_brain(both, "claude"), "claude")
        self.assertIsNone(pick_brain(self._states(Health.MISSING, Health.MISSING), "auto"))

    def test_pick_brain_waits_for_a_preferred_brain_still_checking(self):
        # Don't flip to Codex just because Claude Code is slow to answer.
        self.assertIsNone(pick_brain(self._states(Health.CHECKING, Health.READY), "auto"))
        self.assertEqual(pick_brain(self._states(Health.READY, Health.CHECKING), "auto"), "claude")
        self.assertTrue(brain_pending(self._states(Health.CHECKING, Health.READY), "auto"))
        self.assertFalse(brain_pending(self._states(Health.READY, Health.CHECKING), "auto"))

    def test_status_probes_are_cached(self):
        t = TempCtx(which=CLAUDE)
        try:
            t.runner.add(["claude", "--version"], stdout="2.1.260 (Claude Code)\n")
            t.runner.add(["claude", "auth", "status"], stdout=json.dumps({"authMethod": "claude.ai"}))
            ClaudeCode(t.ctx).check()
            ClaudeCode(t.ctx).check()
            self.assertEqual(t.runner.calls.count(("claude", "auth", "status")), 1)
        finally:
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
