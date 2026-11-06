"""Stands in for `claude -p --input-format stream-json --output-format stream-json`.

Message content decides the behaviour: "use tool", "fail", "die", "slow" (runs until interrupted),
"deaf" (slow, and ignores interrupt requests); anything else is echoed. Like the real CLI, a
control_request interrupt ends the running turn and keeps the session, while SIGINT ends the process.
FAKE_CLAUDE_ARGV_OUT=<file> records argv. FAKE_MCP_STATUS sets the blender server's status.
FAKE_CLAUDE_STARTUP=<seconds> delays the first turn; an interrupt sent before a turn begins is lost.

A message whose content is a list of blocks may carry an image (the picture describer). Then the reply comes
from FAKE_CLAUDE_PICTURE (default "the left wing tip"), which may also be one of the words above to misbehave.
FAKE_CLAUDE_STDIN_OUT=<file> records the shape of that message (never the image itself): block types, the
image's media type, length and SHA-256, and the text. FAKE_CLAUDE_PID_OUT=<file> records the process id.
FAKE_CLAUDE_NEVER_READS=1 sleeps without ever reading stdin, like a process that hung before it started.
FAKE_CLAUDE_LEAK_BASE64=<dir> and FAKE_CLAUDE_LEAK_RAW=<dir> make it behave like a Claude Code that keeps a copy of
the picture there (as sent, or decoded), so the leftover check has something to find.
"""

import base64
import hashlib
import json
import os
import queue
import signal
import sys
import threading
import time

if os.environ.get("FAKE_CLAUDE_ARGV_OUT"):
    with open(os.environ["FAKE_CLAUDE_ARGV_OUT"], "w") as fh:
        json.dump(sys.argv[1:], fh)

signal.signal(signal.SIGINT, lambda *_: os._exit(0))

if os.environ.get("FAKE_CLAUDE_PID_OUT"):
    with open(os.environ["FAKE_CLAUDE_PID_OUT"], "w") as fh:
        fh.write(str(os.getpid()))
if os.environ.get("FAKE_CLAUDE_NEVER_READS"):
    time.sleep(60)

lock = threading.Lock()
messages: queue.Queue = queue.Queue()
interrupt = threading.Event()
deaf = threading.Event()


def emit(event):
    with lock:
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()


def read():
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("type") == "control_request":
            if message["request"].get("subtype") == "interrupt" and not deaf.is_set():
                emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"], "response": {"still_queued": []}}})
                interrupt.set()
            continue
        messages.put(message)
    messages.put(None)


def result(text, ok=True):
    emit({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
          "result": text, "session_id": "sess-1", "duration_ms": 5, "num_turns": 1})


threading.Thread(target=read, daemon=True).start()
first = True
while True:
    message = messages.get()
    if message is None:
        break
    if first:
        first = False
        time.sleep(float(os.environ.get("FAKE_CLAUDE_STARTUP", "0")))
    assert message["type"] == "user" and message["message"]["role"] == "user"
    content = message["message"]["content"]
    pictured = False
    if isinstance(content, list):  # content blocks: text, and maybe an image
        images = [b for b in content if b.get("type") == "image"]
        pictured = bool(images)
        if os.environ.get("FAKE_CLAUDE_STDIN_OUT"):
            image = images[0]["source"] if images else {}
            data = image.get("data", "")
            with open(os.environ["FAKE_CLAUDE_STDIN_OUT"], "w") as fh:
                json.dump({
                    "types": [b.get("type") for b in content],
                    "source_type": image.get("type"), "media_type": image.get("media_type"),
                    "length": len(data), "sha256": hashlib.sha256(data.encode()).hexdigest(),
                    "text": " ".join(b.get("text", "") for b in content if b.get("type") == "text"),
                    "cwd": os.getcwd(),
                }, fh)
        for variable, make in (("FAKE_CLAUDE_LEAK_BASE64", lambda d: d.encode()),
                               ("FAKE_CLAUDE_LEAK_RAW", lambda d: base64.b64decode(d))):
            if os.environ.get(variable) and images:
                leak = os.path.join(os.environ[variable], f"leak-{os.getpid()}.bin")
                with open(leak, "wb") as fh:
                    fh.write(b"cache: " + make(images[0]["source"]["data"]))
        content = os.environ.get("FAKE_CLAUDE_PICTURE", "the left wing tip") if images else \
            " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    interrupt.clear()
    servers = []
    if "--mcp-config" in sys.argv:
        servers = [{"name": "blender", "status": os.environ.get("FAKE_MCP_STATUS", "connected")}]
    emit({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "fake", "mcp_servers": servers})
    if "die" in content:
        sys.exit(3)
    if "use tool" in content:
        emit({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Making it taller."},
            {"type": "tool_use", "id": "t1", "name": "mcp__blender__execute_blender_code", "input": {"code": "x"}},
        ]}})
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
        result("Made the chair twice as tall.")
    elif "fail" in content:
        result("Something broke", ok=False)
    elif "slow" in content or "deaf" in content:
        if "deaf" in content:
            deaf.set()
        interrupt.wait(5)
        deaf.clear()
        if interrupt.is_set():
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user]"}]}})
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "sess-1"})
        else:
            result("Finished the slow job.")
    else:
        result(content if pictured else f"echo: {content}")
