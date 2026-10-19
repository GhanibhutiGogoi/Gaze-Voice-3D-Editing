"""The two conversations behind a voice command: the refiner and the editor.

Claude Code runs as one long-lived headless process per role, fed one JSON line
per command (`--input-format stream-json`), so there's no startup cost per
command and the editor remembers what "it" was. Codex has no such mode, so each
command is one `codex exec` run that resumes the previous thread.
"""

from __future__ import annotations

import collections
import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..components.brains import CLAUDE_ISOLATION, codex_mcp_args


INTERRUPT_GRACE = 8.0  # seconds a Claude turn gets to end after interrupt() before its process is stopped
_TURN_EVENTS = ("system", "assistant", "user", "stream_event")  # signs that Claude Code has begun a turn


class BrainError(Exception):
    """The brain process failed, stopped or timed out; the message says which."""


@dataclass
class TurnResult:
    text: str
    is_error: bool
    tools: list[str] = field(default_factory=list)
    seconds: float = 0.0
    interrupted: bool = False  # it ended early because interrupt() was called


def _end_process(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """SIGINT the process group, then SIGKILL it if it's still running `grace` seconds later."""
    for sig in (signal.SIGINT, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def short_tool(name: str) -> str:
    """`mcp__blender__execute_blender_code` → `execute_blender_code`."""
    return name.split("__")[-1] if name.startswith("mcp__") else name


def claude_session_argv(
    system_prompt_file: Path, model: str | None, mcp_config: Path | None, max_turns: int = 12,
    effort: str | None = None,
) -> list[str]:
    argv = [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        *CLAUDE_ISOLATION,
        "--no-session-persistence",  # spoken commands stay out of Claude Code's saved sessions
        "--permission-prompts", "none",
        "--max-turns", str(max_turns),
        "--system-prompt-file", str(system_prompt_file),
    ]
    if model and model != "default":
        argv += ["--model", model]
    if effort and effort != "default":
        argv += ["--effort", effort]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config), "--allowedTools", "mcp__blender"]
    return argv


class ClaudeSession:
    def __init__(self, argv: Sequence[str], cwd: str | os.PathLike, env: Mapping[str, str] | None = None):
        self.argv = [str(a) for a in argv]
        self.cwd = cwd
        self.env = dict(os.environ, **(env or {}))
        self.proc: subprocess.Popen | None = None
        self.init: dict | None = None
        self.turns = 0
        self._events: queue.Queue[dict] = queue.Queue()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)
        self.interrupt_grace = INTERRUPT_GRACE
        self._lock = threading.Lock()  # one turn at a time
        self._write_lock = threading.Lock()  # turns and interrupts both write to stdin
        self._in_turn = False
        self._turn_begun = False
        self._interrupted = False
        self._interrupt_sent = False
        self._interrupt_lock = threading.Lock()
        self._requests = itertools.count(1)
        self._threads: list[threading.Thread] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, message: dict) -> None:
        assert self.proc and self.proc.stdin
        with self._write_lock:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()

    def _tail(self) -> str:
        return " | ".join(line for line in list(self._stderr)[-5:] if line) or "no output"

    def start(self, warmup: str | None = None, require_mcp: str | None = None, timeout: float = 90.0) -> None:
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1, cwd=self.cwd, env=self.env, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise BrainError(f"{self.argv[0]} isn't installed.") from exc
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True, name="brain-out"),
            threading.Thread(target=self._read_stderr, daemon=True, name="brain-err"),
        ]
        for thread in self._threads:
            thread.start()
        if warmup:
            turn = self.send(warmup, timeout=timeout)
            if turn.is_error:
                raise BrainError(f"The brain didn't answer its first question: {turn.text}")
        if require_mcp:
            servers = (self.init or {}).get("mcp_servers") or []
            status = next((s.get("status") for s in servers if s.get("name") == require_mcp), "missing")
            if status != "connected":
                raise BrainError(f"Claude Code couldn't reach the {require_mcp} MCP server ({status}).")

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    self._events.put(json.loads(line))
                except ValueError:
                    continue

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _next_event(self, deadline: float) -> dict:
        while True:
            try:
                return self._events.get(timeout=0.1)
            except queue.Empty:
                if not self.alive:
                    self._threads[0].join(timeout=0.5)  # let the reader deliver what's left
                    if self._events.empty():
                        code = self.proc.returncode if self.proc else "?"
                        raise BrainError(f"The brain stopped mid-command (exit {code}): {self._tail()}") from None
                elif time.monotonic() > deadline:
                    if self.proc is not None:  # its late answer would become the next command's answer
                        _end_process(self.proc)
                    raise BrainError("The brain took too long to answer.") from None

    def send(self, text: str, on_tool: Callable[[str], None] | None = None, timeout: float = 180.0) -> TurnResult:
        with self._lock:
            if not self.alive:
                raise BrainError(f"The brain isn't running: {self._tail()}")
            self._interrupted = self._interrupt_sent = self._turn_begun = False
            self._in_turn = True
            try:
                try:
                    self._write({"type": "user", "message": {"role": "user", "content": text}})
                except (OSError, ValueError) as exc:
                    raise BrainError(f"The brain stopped: {self._tail()}") from exc
                return self._read_turn(on_tool, timeout)
            finally:
                self._in_turn = False

    def _read_turn(self, on_tool: Callable[[str], None] | None, timeout: float) -> TurnResult:
        began = time.monotonic()
        deadline = began + timeout
        tools: list[str] = []
        texts: list[str] = []
        while True:
            event = self._next_event(deadline)
            kind = event.get("type")
            if kind in _TURN_EVENTS and not self._turn_begun:
                self._turn_begun = True
                if self._interrupted:
                    self._request_interrupt()
            if kind == "system" and event.get("subtype") == "init":
                self.init = event
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        name = short_tool(str(block.get("name", "?")))
                        tools.append(name)
                        if on_tool:
                            on_tool(name)
                    elif block.get("type") == "text":
                        texts.append(block.get("text", ""))
            elif kind == "result":
                self.turns += 1
                result = event.get("result")
                final = result if isinstance(result, str) else " ".join(texts)
                failed = bool(event.get("is_error")) or event.get("subtype") not in (None, "success")
                seconds = round(time.monotonic() - began, 2)
                return TurnResult(final.strip(), failed, tools, seconds, interrupted=failed and self._interrupted)

    def interrupt(self) -> None:
        """End the running turn, like pressing Esc: the session, and what it remembers, stay.

        Claude Code takes this as a stream-json control request. One that arrives before the
        turn has begun is dropped, so until then it waits for the turn's first event. SIGINT
        would end the whole process, so that's only the fallback for a turn still running
        `interrupt_grace` seconds later.
        """
        if not (self._in_turn and self.alive):
            return
        self._interrupted = True
        if self._turn_begun:
            self._request_interrupt()
        timer = threading.Timer(self.interrupt_grace, self._end_stuck_turn, args=(self.turns,))
        timer.daemon = True
        timer.start()

    def _request_interrupt(self) -> None:
        with self._interrupt_lock:
            if self._interrupt_sent:
                return
            self._interrupt_sent = True
        request = {"type": "control_request", "request_id": f"gazevoice-{next(self._requests)}",
                   "request": {"subtype": "interrupt"}}
        try:
            self._write(request)
        except (OSError, ValueError):
            pass

    def _end_stuck_turn(self, turn: int) -> None:
        proc = self.proc
        if proc is not None and self._in_turn and self.turns == turn:
            _end_process(proc)

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=3)
        for thread in self._threads:
            thread.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


class CodexSession:
    """One `codex exec --json` run per command, resuming the same thread (experimental)."""

    def __init__(
        self,
        cwd: str | os.PathLike,
        instructions: str,
        mcp_cmd: dict | None = None,
        command: Sequence[str] = ("codex",),
        env: Mapping[str, str] | None = None,
    ):
        self.cwd = cwd
        self.instructions = instructions
        self.mcp_cmd = mcp_cmd
        self.command = [str(c) for c in command]
        self.env = dict(os.environ, **(env or {}))
        self.thread_id: str | None = None
        self.turns = 0
        self.proc: subprocess.Popen | None = None
        self._interrupted = False
        self._timed_out = False

    @property
    def alive(self) -> bool:
        return True  # nothing stays running between commands

    def start(self, **_ignored) -> None:
        return None

    def send(self, text: str, on_tool: Callable[[str], None] | None = None, timeout: float = 240.0) -> TurnResult:
        prompt = text if self.thread_id or not self.instructions else f"{self.instructions}\n\n{text}"
        argv = [*self.command, "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only"]
        if self.mcp_cmd:
            argv += codex_mcp_args(self.mcp_cmd)
        if self.thread_id:
            argv += ["resume", self.thread_id]
        argv.append(prompt)
        began = time.monotonic()
        self._interrupted = self._timed_out = False
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", cwd=self.cwd, env=self.env, start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise BrainError("codex isn't installed.") from exc
        watchdog = threading.Timer(timeout, self._expire)
        watchdog.daemon = True
        watchdog.start()
        tools: list[str] = []
        texts: list[str] = []
        error = None
        try:
            assert self.proc.stdout
            for line in self.proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                if kind == "thread.started" and event.get("thread_id"):
                    self.thread_id = event["thread_id"]
                elif kind == "item.completed" and item.get("type") == "mcp_tool_call":
                    name = str(item.get("tool") or "tool")
                    tools.append(name)
                    if on_tool:
                        on_tool(name)
                elif kind == "item.completed" and item.get("type") == "agent_message":
                    texts.append(str(item.get("text", "")))
                elif kind in ("turn.failed", "error"):
                    detail = event.get("error") if kind == "turn.failed" else event
                    error = (detail or {}).get("message") or json.dumps(detail)
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            self.proc.wait(timeout=10)
        finally:
            watchdog.cancel()
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()
        self.turns += 1
        seconds = round(time.monotonic() - began, 2)
        if self._interrupted:
            return TurnResult((texts[-1] if texts else "").strip(), True, tools, seconds, interrupted=True)
        if self._timed_out:
            return TurnResult("Codex took too long to answer.", True, tools, seconds)
        if error or (self.proc.returncode != 0 and not texts):
            detail = error or " ".join(stderr.split())[-300:] or f"exit {self.proc.returncode}"
            return TurnResult(f"Codex failed: {detail}", True, tools, seconds)
        return TurnResult((texts[-1] if texts else "").strip(), False, tools, seconds)

    def interrupt(self) -> None:
        """End the running command; the next one still resumes the same thread."""
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._interrupted = True
            threading.Thread(target=_end_process, args=(proc,), daemon=True).start()

    def _expire(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            self._timed_out = True
            _end_process(proc)

    def close(self) -> None:
        self.interrupt()
