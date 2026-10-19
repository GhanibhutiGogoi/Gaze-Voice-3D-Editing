"""The brain: the local Claude Code or Codex CLI, on whatever login it already has.

GazeVoice never asks for an API key. It only drives the `claude` and `codex`
commands, so a subscription login or a key the CLI already holds both work.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Mapping, Sequence

from ..blender_socket import port_open
from ..jobs import Job, JobError
from ..paths import AppPaths
from ..runner import CommandResult
from .base import ActionSpec, Component, ComponentState, Health
from .blender import find_blender
from .blender_mcp import ADDON_PORT, server_command, server_installed

TEST_PROMPT = "Reply with exactly the word: ready"
CONNECT_PROMPT = "Use the blender tools to count the objects in the open Blender scene. Reply with only the number."
BRAIN_ORDER = ("claude", "codex")

# A headless Claude Code session that sees none of the user's global setup:
# no built-in tools, no plugins/hooks/skills from ~/.claude, no other MCP servers.
# Never `--bare`: it ignores the subscription login and wants an API key.
CLAUDE_ISOLATION = [
    "--tools", "",
    "--setting-sources", "project",
    "--disable-slash-commands",
    "--strict-mcp-config",
]

_CLAUDE_LOGINS = {
    "claude.ai": "Claude subscription",
    "oauth_token": "Claude subscription (token)",
    "api_key": "API key",
    "api_key_helper": "API key helper",
    "third_party": "cloud provider",
}
_CODEX_LOGIN = re.compile(r"^Logged in using (?P<method>.+?)(?:\s+-\s+.*)?$", re.MULTILINE)


def claude_headless_argv(prompt: str, extra: Sequence[str] = ()) -> list[str]:
    return ["claude", "-p", "--output-format", "json", *CLAUDE_ISOLATION, *extra, prompt]


def codex_exec_argv(prompt: str, extra: Sequence[str] = ()) -> list[str]:
    return ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json", *extra, prompt]


def parse_codex_jsonl(text: str) -> str | None:
    """The last agent message in `codex exec --json` output."""
    reply = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                reply = item["text"]
    return reply


def write_mcp_config(paths: AppPaths, server_cmd: dict) -> Path:
    """The --mcp-config file that gives a headless brain the Blender MCP server, and nothing else."""
    path = paths.root / "mcp-blender.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"blender": server_cmd}}, indent=2))
    return path


def _toml(value) -> str:
    """A TOML literal for `codex -c key=value` (JSON strings and arrays are valid TOML)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def codex_mcp_args(cmd: dict) -> list[str]:
    """`-c` overrides that give one `codex exec` run the Blender MCP server."""
    args = [
        "-c", f"mcp_servers.blender.command={_toml(cmd['command'])}",
        "-c", f"mcp_servers.blender.args={_toml(cmd['args'])}",
    ]
    if cmd.get("env"):
        args += ["-c", f"mcp_servers.blender.env={_toml(cmd['env'])}"]
    return args


def pick_brain(states: Mapping[str, ComponentState], preference: str) -> str | None:
    """The first ready brain in preference order. A preferred brain that's still being
    checked blocks the fallback, so a slow Claude Code never flips the choice to Codex."""
    order = BRAIN_ORDER if preference == "auto" else (preference,)
    for brain in order:
        state = states.get(brain)
        if state is None:
            continue
        if state.health is Health.READY:
            return brain
        if state.health is Health.CHECKING:
            return None
    return None


def brain_pending(states: Mapping[str, ComponentState], preference: str) -> bool:
    """True when no brain is picked only because a preferred one hasn't answered yet."""
    order = BRAIN_ORDER if preference == "auto" else (preference,)
    for brain in order:
        state = states.get(brain)
        if state is None:
            continue
        if state.health is Health.READY:
            return False
        if state.health is Health.CHECKING:
            return True
    return False


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[-limit:] if text else "no output"


def _first_token(text: str) -> str | None:
    parts = text.split()
    return parts[0] if parts else None


def claude_reply(result: CommandResult) -> str:
    """The text of a `claude -p --output-format json` run, or a JobError."""
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise JobError(f"Claude Code didn't answer: {_tail(result.stderr or result.stdout)}")
    if payload.get("is_error"):
        raise JobError(f"Claude Code reported an error: {payload.get('result') or 'unknown error'}")
    reply = payload.get("result")
    if not isinstance(reply, str):
        raise JobError("Claude Code returned no text")
    return reply.strip()


class _Brain(Component):
    """Shared by both CLIs: the Blender connection check is offered once MCP is reachable."""

    connect_label = "Connect to Blender"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.addon_port = ADDON_PORT

    def mcp_reachable(self) -> bool:
        return server_installed(self.paths) and port_open("127.0.0.1", self.addon_port)

    def brain_actions(self) -> list[ActionSpec]:
        actions = [ActionSpec("test", "Test", uses_plan=True)]
        if self.mcp_reachable():
            actions.append(ActionSpec("connect_blender", self.connect_label, uses_plan=True))
        return actions

    def server_cmd(self) -> dict:
        return server_command(self.paths, find_blender(self.runner))


class ClaudeCode(_Brain):
    id = "claude"
    name = "Claude Code"
    role = "Brain · first choice"

    def check(self) -> ComponentState:
        if not self.runner.which("claude"):
            return self.state(Health.MISSING, "Install Claude Code (claude.com/code), then log in once.")
        version = _first_token(self.probe(["claude", "--version"], ttl=600).stdout)
        details: list[tuple[str, str]] = [("Version", version or "unknown")]
        status = self.probe(["claude", "auth", "status"])
        try:
            method = json.loads(status.stdout).get("authMethod")
        except (ValueError, AttributeError):
            method = None
        if status.ok and method and method != "none":
            login = _CLAUDE_LOGINS.get(method, method)
            return self.state(Health.READY, f"Logged in · {login}", details, self.brain_actions())
        return self.state(Health.INSTALLED, "Not logged in. Run `claude auth login` in Terminal.", details)

    def _ask(self, job: Job, prompt: str, extra: Sequence[str] = (), timeout: float = 120) -> dict:
        started = time.monotonic()
        result = self.runner.run(claude_headless_argv(prompt, extra), timeout=timeout, cwd=self.paths.work)
        seconds = round(time.monotonic() - started, 2)
        reply = claude_reply(result)
        job.log(f"Claude Code replied {reply!r} in {seconds}s")
        return {"reply": reply, "seconds": seconds}

    def action_test(self, job: Job) -> dict:
        job.log('Asking Claude Code to reply "ready" (uses a little of your plan)…')
        return self._ask(job, TEST_PROMPT)

    def action_connect_blender(self, job: Job) -> dict:
        config = write_mcp_config(self.paths, self.server_cmd())
        job.log("Asking Claude Code to count the scene's objects through the Blender tools (uses a little of your plan)…")
        extra = ["--mcp-config", str(config), "--allowedTools", "mcp__blender", "--max-turns", "6"]
        return self._ask(job, CONNECT_PROMPT, extra, timeout=180)


class Codex(_Brain):
    id = "codex"
    name = "Codex"
    role = "Brain · fallback"
    connect_label = "Connect to Blender (experimental)"

    def check(self) -> ComponentState:
        if not self.runner.which("codex"):
            return self.state(Health.MISSING, "Install Codex (the CLI), then run `codex login` once.")
        version_text = self.probe(["codex", "--version"], ttl=600).stdout.split()
        details: list[tuple[str, str]] = [("Version", version_text[-1] if version_text else "unknown")]
        status = self.probe(["codex", "login", "status"])
        match = _CODEX_LOGIN.search(f"{status.stdout}\n{status.stderr}")
        if match:
            method = match.group("method")
            login = "ChatGPT plan" if "ChatGPT" in method else ("API key" if "API key" in method else method)
            return self.state(Health.READY, f"Logged in · {login}", details, self.brain_actions())
        return self.state(Health.INSTALLED, "Not logged in. Run `codex login` in Terminal.", details)

    def _ask(self, job: Job, prompt: str, extra: Sequence[str] = (), timeout: float = 180) -> dict:
        started = time.monotonic()
        result = self.runner.run(codex_exec_argv(prompt, extra), timeout=timeout, cwd=self.paths.work)
        seconds = round(time.monotonic() - started, 2)
        reply = parse_codex_jsonl(result.stdout)
        if reply is None:
            raise JobError(f"Codex didn't answer: {_tail(result.stderr or result.stdout)}")
        job.log(f"Codex replied {reply.strip()!r} in {seconds}s")
        return {"reply": reply.strip(), "seconds": seconds}

    def action_test(self, job: Job) -> dict:
        job.log('Asking Codex to reply "ready" (uses a little of your plan)…')
        return self._ask(job, TEST_PROMPT)

    def action_connect_blender(self, job: Job) -> dict:
        extra = codex_mcp_args(self.server_cmd())
        job.log("Asking Codex to count the scene's objects through the Blender tools (experimental)…")
        return self._ask(job, CONNECT_PROMPT, extra, timeout=240)
