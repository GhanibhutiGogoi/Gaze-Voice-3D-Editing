"""The GazeVoice package inside Unity, and the MCP server that reaches Unity through it."""

from __future__ import annotations

import re
from pathlib import Path

from ..blender_socket import bridge_request, port_open
from ..jobs import Job, JobError
from ..mcp_client import McpError, StdioMcpClient
from ..unity_install import PACKAGE_NAME, add_bridge, bridge_current, bridge_installed, remove_bridge
from ..unity_mcp import TOOLS, server_command
from .base import ActionSpec, Component, Health
from .unity import is_running, project_problem

BRIDGE_PORT = 9878  # matches Port in unity_package/Editor/GazeVoiceBridge.cs
EDITOR_LOG = Path.home() / "Library" / "Logs" / "Unity" / "Editor.log"
LOG_TAIL_BYTES = 512 * 1024
CSHARP_CHECK = "return Application.unityVersion;"
_OUR_COMPILE_ERROR = re.compile(rf"^.*{re.escape(PACKAGE_NAME)}.*error CS\d+.*$", re.MULTILINE)


def package_compile_error(log: Path) -> str | None:
    """The first error Unity reported compiling our package, from the end of its log."""
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            text = handle.read().decode(errors="replace")
    except OSError:
        return None
    match = _OUR_COMPILE_ERROR.search(text)
    return match.group(0).strip() if match else None


class UnityBridge(Component):
    id = "unity_bridge"
    name = "GazeVoice Unity Bridge"
    role = "Our Unity package · selection and talk window"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.port = BRIDGE_PORT
        self.editor_log = EDITOR_LOG

    @property
    def project(self) -> Path | None:
        return Path(self.settings.unity_project) if self.settings.unity_project else None

    def _add_action(self, project: Path, label: str = "Add to project") -> ActionSpec:
        return ActionSpec("install", label, confirm=(
            f"Copy the GazeVoice package into {project.name}, at Packages/{PACKAGE_NAME}? "
            "Nothing else in the project changes."))

    def _remove_action(self, project: Path) -> ActionSpec:
        return ActionSpec("uninstall", "Remove from project",
                          confirm=f"Remove the GazeVoice package from {project.name}?")

    def check(self):
        project = self.project
        if project is None or project_problem(project):
            return self.state(Health.MISSING, "Needs a project first. Set up Unity on the Unity unit makes one.")
        details = [("Port", self.port), ("Project", project.name)]
        if not bridge_installed(project):
            return self.state(Health.MISSING, f"Not in {project.name} yet.", details, [self._add_action(project)])
        if not bridge_current(project):
            return self.state(Health.INSTALLED, "The project has an older copy of the package.", details,
                              [self._add_action(project, "Update"), self._remove_action(project)])
        remove = [self._remove_action(project)]
        if not port_open("127.0.0.1", self.port):
            if is_running(self.runner, self.ctx.probes):
                problem = (package_compile_error(project / "Logs" / "Editor.log")  # newer Unity logs here
                           or package_compile_error(self.editor_log))
                if problem:
                    return self.state(Health.ERROR, f"Unity couldn't compile the package: {problem}", details, remove)
                return self.state(Health.INSTALLED, "Added. Switch to Unity so it can load the package.",
                                  details, remove)
            return self.state(Health.INSTALLED, "Added. Open the project in Unity to connect.", details, remove)
        try:
            ping = bridge_request({"cmd": "ping"}, port=self.port, timeout=1.0)
        except (OSError, ValueError):
            ping = {}
        if not ping.get("ok"):
            return self.state(Health.ERROR, f"Something else is using port {self.port}.", details, remove)
        details.append(("Package", ping.get("bridge") or "unknown"))
        return self.state(Health.READY, f"Connected to Unity {ping.get('unity', '')}. Turn voice on, then hold "
                          f"{ping.get('key') or '⌥ Space'} in Unity to talk, or use Window › GazeVoice.",
                          details, [ActionSpec("test", "Test"), *remove])

    def action_install(self, job: Job) -> dict:
        if self.project is None:
            raise JobError("Pick a Unity project first.")
        add_bridge(self.project)
        job.log(f"Added {PACKAGE_NAME} to {self.project.name}. Unity loads it the next time it's in front.")
        return {"added": True}

    def action_uninstall(self, job: Job) -> dict:
        if self.project is None:
            raise JobError("Pick a Unity project first.")
        remove_bridge(self.project)
        job.log(f"Removed {PACKAGE_NAME} from {self.project.name}.")
        return {"removed": True}

    def action_test(self, job: Job) -> dict:
        try:
            job.log("Showing “GazeVoice connected” in Unity's GazeVoice window…")
            bridge_request({"cmd": "status", "text": "GazeVoice connected", "state": "done"}, port=self.port)
            selection = bridge_request({"cmd": "selection"}, port=self.port)
            names = bridge_request({"cmd": "scene_names"}, port=self.port).get("names") or []
            ping = bridge_request({"cmd": "ping"}, port=self.port)
            job.log("Running a line of C# inside Unity…")
            csharp = bridge_request({"cmd": "tool", "name": "execute_csharp", "arguments": {"code": CSHARP_CHECK}},
                                    port=self.port, timeout=120.0)
        except (OSError, ValueError) as exc:
            raise JobError(f"The bridge didn't answer: {exc}") from exc
        if not csharp.get("ok"):
            raise JobError(f"Unity answered, but it can't run C#: {csharp.get('details') or csharp.get('error')}")
        job.log(f"C# works: it reported Unity {csharp.get('result')}")
        selected = selection.get("selected") or []
        job.log(f"Selected: {', '.join(selected) if selected else 'nothing'}")
        job.log(f"The scene has {len(names)} objects.")
        return {"selection": selection, "objects": len(names), "unity": ping.get("unity")}


class UnityMcp(Component):
    id = "unity_mcp"
    name = "Unity MCP server"
    role = "Speaks MCP for Claude Code / Codex"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.port = BRIDGE_PORT

    def check(self):
        test = [ActionSpec("test", "Test")]
        custom = self.settings.unity_mcp_command
        connected = port_open("127.0.0.1", self.port)
        health = Health.READY if connected else Health.INSTALLED
        if custom:
            return self.state(health, f"Using your own server: {custom}", actions=test)
        details = [("Tools", len(TOOLS))]
        if connected:
            return self.state(health, "Built into GazeVoice · Unity is connected", details, test)
        return self.state(health, "Built into GazeVoice, so there's nothing to install. Waiting for Unity.",
                          details, test)

    def action_test(self, job: Job) -> dict:
        command = server_command(self.settings)
        job.log("Starting the Unity MCP server and asking for its tools…")
        try:
            with StdioMcpClient([command["command"], *command["args"]], env=command.get("env"), timeout=15) as client:
                client.initialize()
                tools = client.list_tools()
                names = [t.get("name", "?") for t in tools]
                job.log(f"{len(tools)} tools: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}")
                unity = "not running"
                if not self.settings.unity_mcp_command and port_open("127.0.0.1", self.port):
                    job.log("Asking Unity for the objects in the open scene…")
                    result = client.call_tool("get_scene_objects", {})
                    unity = "error" if result.get("isError") else "connected"
        except (McpError, OSError) as exc:
            raise JobError(f"The Unity MCP server failed: {exc}") from exc
        return {"tools": len(tools), "unity": unity}
