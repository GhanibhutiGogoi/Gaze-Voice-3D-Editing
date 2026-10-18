"""A tiny MCP server over stdio for tests. Flags: --crash, --silent."""

import json
import sys

if "--crash" in sys.argv:
    sys.stderr.write("boom: missing module blmcp\n")
    sys.exit(3)

TOOLS = [
    {"name": "get_objects_summary", "inputSchema": {"type": "object"}},
    {"name": "execute_blender_code", "inputSchema": {"type": "object"}},
    {"name": "get_blendfile_summary_datablocks", "inputSchema": {"type": "object"}},
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    if "--silent" in sys.argv:
        continue
    method, rid, params = request.get("method"), request.get("id"), request.get("params") or {}
    if rid is None:
        continue  # a notification
    # Servers may log before answering; clients must ignore it.
    send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": method}})
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-blender", "version": "1.0.3"}}
    elif method == "tools/list":
        result = {"tools": TOOLS[:2], "nextCursor": "page2"} if not params.get("cursor") else {"tools": TOOLS[2:]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": json.dumps(params.get("arguments"))}], "isError": False}
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}})
        continue
    send({"jsonrpc": "2.0", "id": rid, "result": result})
