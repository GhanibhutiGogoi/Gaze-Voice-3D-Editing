"""Stands in for `codex exec --json [...] [resume <id>] <prompt>`.

Prompt words: "fail" fails the turn, "slow" runs for 5 s. FAKE_CODEX_ARGV_OUT records argv.
"""

import json
import os
import sys
import time

argv = sys.argv[1:]
if os.environ.get("FAKE_CODEX_ARGV_OUT"):
    with open(os.environ["FAKE_CODEX_ARGV_OUT"], "a") as fh:
        fh.write(json.dumps(argv) + "\n")

prompt = argv[-1]
thread = argv[argv.index("resume") + 1] if "resume" in argv else "thr-123"


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
if "slow" in prompt:
    time.sleep(5)
if "fail" in prompt:
    emit({"type": "turn.failed", "error": {"message": "model refused"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "i1", "type": "mcp_tool_call", "server": "blender",
                                         "tool": "execute_blender_code", "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Made the chair taller."}})
emit({"type": "turn.completed", "usage": {}})
