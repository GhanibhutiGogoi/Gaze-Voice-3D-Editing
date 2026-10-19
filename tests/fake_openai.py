"""A local stand-in for an OpenAI-compatible chat API: it records requests and answers from a script."""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAI:
    def __init__(self):
        self.requests: list[dict] = []
        self._replies: queue.Queue = queue.Queue()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fake.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                })
                try:
                    status, body, headers, delay = fake._replies.get_nowait()
                except queue.Empty:
                    status, body, headers, delay = 500, {"error": {"message": "no scripted reply"}}, {}, 0
                time.sleep(delay)
                if isinstance(body, dict) and "__sse__" in body:
                    data = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in body["__sse__"])
                    data += b"data: [DONE]\n\n"
                    self.send_response(status)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, body: dict, status: int = 200, headers: dict | None = None, delay: float = 0.0) -> None:
        self._replies.put((status, body, headers or {}, delay))

    def stream(self, chunks: list[dict]) -> None:
        """Script one streamed (server-sent events) chat reply."""
        self.reply({"__sse__": chunks})

    def message(self, content: str = "", tool_calls: list | None = None, **extra) -> None:
        """Script one chat completion reply."""
        msg = {"role": "assistant", "content": content, **extra}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"
        self.reply({"choices": [{"index": 0, "message": msg, "finish_reason": finish}]})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
