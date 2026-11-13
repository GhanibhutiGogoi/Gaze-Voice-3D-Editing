"""Local stdio MCP server that drives the Unity Editor through the GazeVoice package.

Fixed tools cover the everyday edits. execute_csharp and write_script run whatever the
model writes, the way Blender's MCP server runs Python, so nothing in the Editor is out
of reach. All Unity API calls run on the Editor main thread through the Unity bridge.

The ML-Agents tools (LOCAL_TOOLS) are different: Unity never sees them. They are answered
here by asking the GazeVoice dashboard, which runs the training on this Mac.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .blender_socket import bridge_request
from .mcp_client import PROTOCOL_VERSION

PORT = 9878
RELOAD_SECONDS = 180.0  # a script compile and reload, or a package install, can take this long
READY_POLL = 0.25
TOOL_SECONDS = {"execute_csharp": 300.0, "write_script": 60.0, "capture_scene_view": 30.0, "add_package": 30.0}
RELOADING_TOOLS = ("write_script", "add_package")  # Unity recompiles after these, so they wait for it
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
    {"name": "execute_csharp",
     "description": "Run C# in the Unity Editor right away, with no recompile: any UnityEngine or UnityEditor API, and any "
                    "script or package in the project. `code` is the body of a method returning object, so write statements, "
                    "and `return` a value to see it. Already imported: System, System.Collections, System.Collections.Generic, "
                    "System.IO, System.Linq, UnityEngine, UnityEditor, UnityEngine.SceneManagement, UnityEditor.SceneManagement. "
                    "Replies with the returned value, anything sent to Debug.Log, and compile or runtime errors. Runs on the "
                    "Editor's main thread: an endless loop hangs Unity.",
     "inputSchema": {**OBJECT, "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "write_script",
     "description": "Write a C# file under Assets/, then wait while Unity compiles and reloads. Replies with any compile "
                    "errors. Use it for MonoBehaviours and anything else that has to stay in the project.",
     "inputSchema": {**OBJECT, "properties": {"path": {"type": "string", "description": "Such as Assets/GazeVoice/Scripts/Spin.cs"},
                                              "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "get_compile_status", "description": "Whether Unity is compiling, and the errors from the last compile.",
     "inputSchema": {**OBJECT, "properties": {}}},
    {"name": "read_console", "description": "The latest Unity Console messages, newest last.",
     "inputSchema": {**OBJECT, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 300},
                                              "errors_only": {"type": "boolean"}}}},
    {"name": "capture_scene_view", "description": "A picture of the Scene view, to check how the result looks.",
     "inputSchema": {**OBJECT, "properties": {}}},
    {"name": "add_package",
     "description": "Add a Unity package to the project, then wait while Unity imports it: a name such as "
                    "com.unity.probuilder, name@version, or a git URL.",
     "inputSchema": {**OBJECT, "properties": {"package": {"type": "string"}}, "required": ["package"]}},
]
# Answered here over HTTP by the GazeVoice dashboard, not by the Unity bridge.
LOCAL_TOOLS = [
    {"name": "mlagents_status",
     "description": "Whether ML-Agents' Python side is installed, and how the training run is going: its state "
                    "(idle, waiting, training, stopping, stopped, finished or failed), run id, whether it is listening "
                    "for Unity yet (listening), each behavior's step and mean reward, saved models, and a plain "
                    "message. runs lists earlier runs that have a model, newest first. Changes nothing.",
     "inputSchema": {**OBJECT, "properties": {}}},
    {"name": "mlagents_train",
     "description": "Start training the project's agents on this Mac with mlagents-learn, in the Unity project picked "
                    "in the dashboard. It installs nothing: if ML-Agents isn't installed it refuses, and the person "
                    "presses Install on the dashboard's ML-Agents unit. Use a new run_id each time (an old run's "
                    "folder is kept). This waits up to 45 seconds for ML-Agents to start listening for Unity. Training "
                    "waits for Unity to enter Play mode and gives up after 2 minutes, but it only hears a Play that "
                    "starts after it is listening. So when the reply says listening is true, tell the user to press Play in "
                    "Unity; press Play yourself with execute_csharp (EditorApplication.isPlaying = true;) only if the "
                    "command asked you to. If listening is false, tell them ML-Agents is still starting; if you were "
                    "asked to press Play, call mlagents_status until it is true first. Unity must be out of Play "
                    "mode before this is called.",
     "inputSchema": {**OBJECT, "properties": {
         "run_id": {"type": "string", "description": "Letters, digits, . _ - only; up to 64; starts with a letter or digit. "
                                                     "Such as Mover-20261111-1530"},
         "config_yaml": {"type": "string", "description": "The training settings (trainer_type, max_steps, and so on) "
                                                          "for each Behavior Name. Leave out to use ML-Agents' defaults."},
         "resume": {"type": "boolean", "description": "Carry on an earlier run with this run_id"},
         "overwrite": {"type": "boolean", "description": "Replace an earlier run with this run_id"}},
         "required": ["run_id"]}},
    {"name": "mlagents_stop",
     "description": "Stop the training run. It saves the model first, and Unity then leaves Play mode by itself. "
                    "This waits up to 40 seconds: if the reply's state is still \"stopping\", the model is still being "
                    "saved, so call mlagents_status again before using it.",
     "inputSchema": {**OBJECT, "properties": {}}},
    {"name": "mlagents_import_model",
     "description": "Copy a trained model (.onnx) from a run into the Unity project under Assets/GazeVoice/MLAgents/<run_id>/. "
                    "Replies with its asset_path and behavior; then load it with AssetDatabase and assign it to the "
                    "agent's BehaviorParameters.Model.",
     "inputSchema": {**OBJECT, "properties": {
         "run_id": {"type": "string"},
         "behavior": {"type": "string", "description": "Which behavior's model, when the run saved several"}},
         "required": ["run_id"]}},
]
TOOLS += LOCAL_TOOLS
NAMES = {tool["name"] for tool in TOOLS}
LOCAL_NAMES = {tool["name"] for tool in LOCAL_TOOLS}
DASHBOARD_ROUTES = {  # tool → (method, path) on the dashboard
    "mlagents_status": ("GET", "/api/mlagents"),
    "mlagents_train": ("POST", "/api/mlagents/train"),
    "mlagents_stop": ("POST", "/api/mlagents/stop"),
    "mlagents_import_model": ("POST", "/api/mlagents/import"),
}
# Stopping saves the model. The dashboard answers within 40 s, even if the save goes on (Codex gives a tool 60 s),
# so this waits a little longer than that. Starting waits up to 45 s for the trainer to listen (see mlagents.LISTEN_SECONDS).
DASHBOARD_SECONDS = {"mlagents_stop": 50.0, "mlagents_train": 55.0}
DASHBOARD_DEFAULT_SECONDS = 30.0
DASHBOARD_UNREACHABLE = ("The GazeVoice dashboard isn't reachable, so ML-Agents tools can't run. "
                         "If you restarted the dashboard, turn voice off and on.")
DASHBOARD_SLOW = "The GazeVoice dashboard didn't answer in time. Check the ML-Agents unit there."


def server_command(settings, control_url: str | None = None) -> dict:
    """How an MCP client starts the server: the one set in Routing, or this built-in one.

    The built-in one is told where the dashboard is, for the ML-Agents tools. Your own server is left as it is.
    """
    if settings.unity_mcp_command:
        return {"command": settings.unity_mcp_command, "args": list(settings.unity_mcp_args)}
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    if control_url:
        env["GAZEVOICE_URL"] = control_url
    return {"command": sys.executable, "args": ["-m", "gazevoice.unity_mcp"], "env": env}


def wait_until_ready() -> bool:
    """Wait out a script compile and reload. It takes two clean answers in a row, because
    Unity can look idle for a moment between finishing a compile and starting the reload."""
    deadline = time.monotonic() + RELOAD_SECONDS
    clean = 0
    while time.monotonic() < deadline:
        try:
            reply = bridge_request({"cmd": "ping"}, port=PORT, timeout=2.0)
            clean = clean + 1 if reply.get("ok") and not reply.get("compiling") else 0
        except (OSError, ValueError):  # the socket is down while Unity reloads
            clean = 0
        if clean >= 2:
            return True
        time.sleep(READY_POLL)
    return False


def finish_reload(name: str, reply: dict) -> dict:
    """The reply to a tool that made Unity recompile, once Unity has finished."""
    if not wait_until_ready():
        return {**reply, "ok": False, "error": "Unity is still compiling. Check get_compile_status in a moment."}
    status = bridge_request({"cmd": "tool", "name": "get_compile_status", "arguments": {}}, port=PORT, timeout=10.0)
    errors = status.get("errors") or []
    problem = status.get("package_error") if name == "add_package" else None
    finished = {**reply, "ok": not errors and not problem, "compiled": not errors, "errors": errors}
    return {**finished, "error": problem} if problem else finished


def call_unity(name: str, arguments: dict) -> dict:
    request = {"cmd": "tool", "name": name, "arguments": arguments}
    timeout = TOOL_SECONDS.get(name, 10.0)
    try:
        reply = bridge_request(request, port=PORT, timeout=timeout)
    except ConnectionRefusedError:  # Unity is reloading, so nothing reached it and it's safe to send again
        if not wait_until_ready():
            raise
        reply = bridge_request(request, port=PORT, timeout=timeout)
    except ConnectionError:  # cut off partway: it may have run, so sending it again could run it twice
        if not wait_until_ready():
            raise
        return {"ok": False, "error": "Unity reloaded during this call, so it may not have finished. "
                                      "Check the scene before you try it again."}
    if name in RELOADING_TOOLS and reply.get("ok") and reply.get("changed", True):
        return finish_reload(name, reply)
    return reply


def dashboard_address() -> str | None:
    """Where the dashboard is, if this machine's own address was given."""
    url = os.environ.get("GAZEVOICE_URL", "").strip()
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that can't be one
        return url.rstrip("/") if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") else None
    except ValueError:
        return None


def call_dashboard(name: str, arguments: dict) -> dict:
    """Ask the dashboard to do an ML-Agents tool. A refusal comes back as {"ok": False, "error": its sentence}."""
    address = dashboard_address()
    if address is None:
        return {"ok": False, "error": DASHBOARD_UNREACHABLE}
    method, path = DASHBOARD_ROUTES[name]
    request = urllib.request.Request(
        address + path, method=method, data=json.dumps(arguments).encode() if method == "POST" else None,
        headers={"X-GazeVoice": "1", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this Mac's own address never goes through a proxy
    try:
        with opener.open(request, timeout=DASHBOARD_SECONDS.get(name, DASHBOARD_DEFAULT_SECONDS)) as response:
            body, refused = response.read(), False
    except urllib.error.HTTPError as exc:  # it answered, and said no
        with exc:
            body, refused = exc.read(), True
    except TimeoutError:
        return {"ok": False, "error": DASHBOARD_SLOW}
    except (OSError, ValueError, http.client.HTTPException):  # e.g. an answer cut off halfway; this must not end the server
        return {"ok": False, "error": DASHBOARD_UNREACHABLE}
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        return {"ok": False, "error": "The GazeVoice dashboard gave an answer the ML-Agents tools can't read."}
    return {**reply, "ok": False} if refused else {"ok": True, **reply}


def tool_result(name: str, arguments: dict) -> dict:
    if name in LOCAL_NAMES:
        reply = call_dashboard(name, arguments)
        return {"content": [{"type": "text", "text": json.dumps(reply)}], "isError": not reply["ok"]}
    reply = call_unity(name, arguments)
    if name == "capture_scene_view" and reply.get("ok"):
        shot = Path(reply["path"])
        picture = base64.b64encode(shot.read_bytes()).decode()
        shot.unlink(missing_ok=True)
        return {"content": [{"type": "image", "data": picture, "mimeType": "image/png"}], "isError": False}
    return {"content": [{"type": "text", "text": json.dumps(reply)}], "isError": not reply.get("ok", False)}


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
            result = tool_result(name, arguments)
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
