"""Local stdio MCP server exposing a fixed Unity Editor scene-editing API.

All Unity API calls run on the Editor main thread through the Unity bridge.
No model-generated C# or Python is executed in Unity.
"""

from __future__ import annotations

import json
import sys

from .blender_socket import bridge_request
from .mcp_client import PROTOCOL_VERSION

OBJECT = {"type": "object", "additionalProperties": False}
TARGET = {"target": {"type": "integer", "description": "Unity instance ID from get_scene_objects; omit for active selection"}}
VECTOR = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
TOOLS = [
    {"name": "get_scene_objects", "description": "List scene GameObjects with stable-in-session instance IDs, hierarchy and selection.",
     "inputSchema": {**OBJECT, "properties": {}}},
    {"name": "get_object_details", "description": "Inspect one GameObject and its Transform, components and material color.",
     "inputSchema": {**OBJECT, "properties": TARGET}},
    {"name": "select_object", "description": "Select a GameObject in the Unity Editor.",
     "inputSchema": {**OBJECT, "properties": TARGET, "required": ["target"]}},
    {"name": "set_color", "description": "Set the target renderer's material color using #RRGGBB or #RRGGBBAA.",
     "inputSchema": {**OBJECT, "properties": {**TARGET, "color": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"}}, "required": ["color"]}},
    {"name": "transform_object", "description": "Set local position, Euler rotation or local scale. Omitted vectors stay unchanged.",
     "inputSchema": {**OBJECT, "properties": {**TARGET, "position": VECTOR, "rotation": VECTOR, "scale": VECTOR}}},
    {"name": "duplicate_object", "description": "Duplicate the target GameObject, optionally naming the copy.",
     "inputSchema": {**OBJECT, "properties": {**TARGET, "name": {"type": "string"}}}},
    {"name": "delete_object", "description": "Delete the target GameObject with Unity Undo support.",
     "inputSchema": {**OBJECT, "properties": TARGET, "required": ["target"]}},
    {"name": "create_primitive", "description": "Create a cube, sphere, capsule, cylinder, plane or quad in the active scene.",
     "inputSchema": {**OBJECT, "properties": {"shape": {"type": "string", "enum": ["cube", "sphere", "capsule", "cylinder", "plane", "quad"]}, "name": {"type": "string"}, "position": VECTOR}, "required": ["shape"]}},
]
NAMES = {tool["name"] for tool in TOOLS}


def handle(request: dict) -> dict | None:
    method, rid = request.get("method"), request.get("id")
    if rid is None:
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "gazevoice-unity", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params") or {}
            name, arguments = params.get("name"), params.get("arguments") or {}
            if name not in NAMES or not isinstance(arguments, dict):
                raise ValueError("Unknown Unity tool or invalid arguments")
            reply = bridge_request({"cmd": "tool", "name": name, "arguments": arguments}, port=9878, timeout=10.0)
            result = {"content": [{"type": "text", "text": json.dumps(reply)}],
                      "isError": not reply.get("ok", False)}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except (OSError, ValueError) as exc:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = handle(request) if isinstance(request, dict) else None
            if response is not None:
                print(json.dumps(response), flush=True)
        except ValueError:
            continue


if __name__ == "__main__":
    main()
