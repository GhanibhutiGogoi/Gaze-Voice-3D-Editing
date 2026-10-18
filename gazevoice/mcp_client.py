"""A minimal MCP client over stdio: newline-delimited JSON-RPC 2.0, standard library only.

Enough to prove a server works: initialize, list tools, call a tool.
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
from typing import Mapping, Sequence

from . import __version__

PROTOCOL_VERSION = "2025-06-18"


class McpError(Exception):
    """The server failed, exited, timed out, or returned a JSON-RPC error."""


class StdioMcpClient:
    def __init__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 60.0,
    ):
        self.argv = [str(a) for a in argv]
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self.proc: subprocess.Popen | None = None

    # --- lifecycle ------------------------------------------------------------
    def __enter__(self) -> "StdioMcpClient":
        env = dict(os.environ)
        env.update(self.env or {})
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                cwd=self.cwd,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise McpError(f"{self.argv[0]}: not found") from exc
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=2)
        for thread in (self._stdout_thread, self._stderr_thread):
            thread.join(timeout=1)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    # --- reader threads -----------------------------------------------------------
    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            rid = message.get("id")
            if rid is None or ("result" not in message and "error" not in message):
                continue  # notifications and server-initiated requests
            with self._lock:
                waiter = self._pending.get(rid)
            if waiter is not None:
                waiter.put(message)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def _stderr_tail(self) -> str:
        self._stderr_thread.join(timeout=0.5)
        return " | ".join(line for line in self._stderr if line) or "no output"

    # --- JSON-RPC -------------------------------------------------------------------
    def _send(self, message: dict) -> None:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"The server stopped: {self._stderr_tail()}") from exc

    def _notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _request(self, method: str, params: dict | None = None) -> dict:
        if self.proc is None:
            raise McpError("Not started")
        rid = next(self._ids)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = waiter
        message = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._send(message)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    reply = waiter.get(timeout=0.1)
                    break
                except queue.Empty:
                    if self.proc.poll() is not None:
                        raise McpError(
                            f"The server exited (code {self.proc.returncode}): {self._stderr_tail()}"
                        ) from None
                    if time.monotonic() > deadline:
                        raise McpError(f"{method} timed out after {self.timeout:g}s") from None
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if "error" in reply:
            error = reply["error"] or {}
            raise McpError(f"{method} failed: {error.get('message', error)}")
        return reply.get("result") or {}

    # --- MCP ----------------------------------------------------------------------
    def initialize(self) -> dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "gazevoice", "version": __version__},
            },
        )
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._request("tools/call", {"name": name, "arguments": arguments})
