"""A brain on an API provider (DeepSeek, Qwen, custom), behaving like a Claude Code session.

Claude Code and Codex bring their own agent loop. A plain chat API doesn't, so for
editing GazeVoice runs it: send the command with the Blender MCP tools, run the
tools the model asks for, send the results back, and repeat until it answers.
Cleanup is a plain chat.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Callable

from ..mcp_client import McpError, StdioMcpClient
from ..providers import ProviderError, thinking_fields
from .brains import BrainError, TurnResult

TOOL_RESULT_CHARS = 20_000
_KEEP = ("role", "content", "tool_calls", "reasoning_content")  # what goes back to the provider


class _Interrupted(Exception):
    pass


def mcp_tools(tools: list[dict]) -> list[dict]:
    """Blender MCP tools as OpenAI-style function tools."""
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description") or "",
            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
        }}
        for t in tools if t.get("name")
    ]


def tool_text(result: dict) -> str:
    parts = []
    for part in result.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        else:
            parts.append(f"[{part.get('type', 'other') if isinstance(part, dict) else 'other'} omitted]")
    text = "\n".join(parts) or json.dumps(result.get("structuredContent") or {})
    if result.get("isError"):
        text = f"Error: {text}"
    return text[:TOOL_RESULT_CHARS]


class _McpHandle:
    """A started StdioMcpClient with a plain close()."""

    def __init__(self, server_cmd: dict, timeout: float):
        self._client = StdioMcpClient([server_cmd["command"], *server_cmd["args"]], env=server_cmd.get("env"),
                                      timeout=timeout)
        self._client.__enter__()

    def initialize(self) -> dict:
        return self._client.initialize()

    def list_tools(self) -> list[dict]:
        return self._client.list_tools()

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._client.call_tool(name, arguments)

    def close(self) -> None:
        self._client.__exit__(None, None, None)


def blender_mcp(server_cmd: dict, timeout: float = 180.0) -> Callable[[], Any]:
    """A factory for ApiSession's mcp_factory: the official Blender MCP server, started on demand."""
    return lambda: _McpHandle(server_cmd, timeout)


class ApiSession:
    def __init__(
        self,
        client,
        model: str,
        system_prompt: str,
        kind: str = "custom",
        thinking: str = "default",
        mcp_factory: Callable[[], Any] | None = None,
        max_steps: int = 12,
        keep_commands: int = 8,
    ):
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.kind = kind
        self.thinking = thinking
        self.mcp_factory = mcp_factory
        self.max_steps = max_steps
        self.keep_commands = keep_commands
        self.init = {"model": model}
        self.effort = thinking if thinking and thinking != "default" else None
        self.turns = 0
        self._mcp = None
        self._tools: list[dict] = []
        self._history: list[list[dict]] = []  # one message group per finished command
        self._started = False
        self._in_turn = False
        self._interrupted = False
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._started

    def _body(self, messages: list[dict], **extra) -> dict:
        body = {"model": self.model, "messages": messages, **thinking_fields(self.kind, self.thinking), **extra}
        if self._tools:
            body["tools"] = self._tools
        return body

    def start(self, timeout: float = 90.0, **_ignored) -> None:
        """Connect (for editing, to the Blender MCP server) and check the key and model with one tiny request."""
        self._history = []
        if self.mcp_factory is not None:
            try:
                self._mcp = self.mcp_factory()
                self._mcp.initialize()
                self._tools = mcp_tools(self._mcp.list_tools())
            except (McpError, OSError) as exc:
                self.close()
                raise BrainError(f"Couldn't start the Blender MCP server: {exc}") from exc
        try:
            self.client.chat(self._body([{"role": "user", "content": "Reply with exactly: ok"}], max_tokens=16),
                             timeout=timeout)
        except ProviderError as exc:
            self.close()
            raise BrainError(str(exc)) from exc
        self._started = True

    def _wait(self, work: Callable[[], Any], deadline: float, what: str) -> Any:
        """Run work in the background; give up at once if the turn is interrupted or runs out of time.

        Whatever it returns late is dropped.
        """
        box: queue.Queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                box.put(("ok", work()))
            except Exception as exc:  # noqa: BLE001 - handed to the waiting turn
                box.put(("error", exc))

        threading.Thread(target=run, daemon=True, name="provider-call").start()
        while True:
            if self._interrupted:
                raise _Interrupted
            try:
                kind, value = box.get(timeout=0.1)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise BrainError(f"{what} took too long to answer.") from None
                continue
            if kind == "error":
                raise value
            return value

    def _ask(self, body: dict, deadline: float) -> dict:
        try:
            return self._wait(lambda: self.client.chat(body, timeout=max(1.0, deadline - time.monotonic())),
                              deadline, "The provider")
        except ProviderError as exc:
            raise BrainError(str(exc)) from exc
        except (BrainError, _Interrupted):
            raise
        except Exception as exc:  # noqa: BLE001 - anything else the HTTP layer raised
            raise BrainError(f"The provider call failed: {exc}") from exc

    def _run_tool(self, call: dict, on_tool, deadline: float) -> dict:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                raise ValueError
        except ValueError:
            return {"role": "tool", "tool_call_id": call.get("id"),
                    "content": f"Error: the arguments for {name} were not valid JSON. Call it again with a JSON object."}
        if on_tool:
            on_tool(name)
        mcp = self._mcp
        if mcp is None:
            raise BrainError("The Blender MCP server isn't running.")
        try:
            text = tool_text(self._wait(lambda: mcp.call_tool(name, arguments), deadline, "Blender"))
        except (McpError, OSError) as exc:
            text = f"Error: {exc}"
        return {"role": "tool", "tool_call_id": call.get("id"), "content": text}

    def send(self, text: str, on_tool: Callable[[str], None] | None = None, timeout: float = 180.0) -> TurnResult:
        with self._lock:
            if not self._started:
                raise BrainError("The brain isn't running.")
            self._interrupted = False
            self._in_turn = True
            began = time.monotonic()
            deadline = began + timeout
            group = [{"role": "user", "content": text}]
            tools: list[str] = []
            try:
                for _ in range(self.max_steps):
                    earlier = [m for g in self._history[-self.keep_commands:] for m in g]
                    messages = [{"role": "system", "content": self.system_prompt}, *earlier, *group]
                    reply = self._ask(self._body(messages), deadline)
                    choice = (reply.get("choices") or [{}])[0]
                    message = {k: v for k, v in (choice.get("message") or {}).items() if k in _KEEP and v is not None}
                    message["role"] = "assistant"
                    group.append(message)
                    calls = message.get("tool_calls") or []
                    if not calls:
                        self._remember(group)
                        return TurnResult(str(message.get("content") or "").strip(), False, tools,
                                          round(time.monotonic() - began, 2))
                    for call in calls:
                        if self._interrupted:
                            raise _Interrupted
                        result = self._run_tool(call, lambda name: (tools.append(name), on_tool and on_tool(name)),
                                                deadline)
                        group.append(result)
                return TurnResult(f"Stopped after {self.max_steps} steps without finishing.", True, tools,
                                  round(time.monotonic() - began, 2))
            except _Interrupted:
                return TurnResult("", True, tools, round(time.monotonic() - began, 2), interrupted=True)
            finally:
                self._in_turn = False
                self.turns += 1

    def _remember(self, group: list[dict]) -> None:
        # Reasoning goes back only within its own turn; later turns don't need it (DeepSeek drops it too).
        self._history.append([{k: v for k, v in m.items() if k != "reasoning_content"} for m in group])
        del self._history[:-self.keep_commands]

    def interrupt(self) -> None:
        """End the running turn within a moment: the request in flight is abandoned and no more tools run."""
        if self._in_turn:
            self._interrupted = True

    def close(self) -> None:
        self._started = False
        self._history = []
        mcp, self._mcp = self._mcp, None
        if mcp is not None:
            try:
                mcp.close()
            except Exception:  # noqa: BLE001 - best effort while shutting down
                pass
