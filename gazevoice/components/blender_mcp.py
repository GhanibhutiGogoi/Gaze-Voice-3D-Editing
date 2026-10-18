"""Blender Lab's official MCP add-on (inside Blender) and MCP server (what the brain talks to)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Sequence

from ..blender_socket import port_open
from ..download import download
from ..jobs import Job, JobError
from ..mcp_client import McpError, StdioMcpClient
from ..paths import AppPaths, tilde
from .base import ActionSpec, Component, ComponentState, Health
from .blender import MIN_BLENDER, blender_version, extensions_dir, find_blender, install_extension

RELEASE = "1.0.3"
_RELEASES = "https://projects.blender.org/lab/blender_mcp/releases/download"
ADDON_ZIP_URL = f"{_RELEASES}/v{RELEASE}/mcp-{RELEASE}.zip"
SERVER_MCPB_URL = f"{_RELEASES}/v{RELEASE}/blender-{RELEASE}.mcpb"
ADDON_ID = "mcp"
ADDON_PORT = 9876
UV_PYTHON = "3.12"


def server_installed(paths: AppPaths) -> bool:
    """Unpacked *and* synced. An unpacked bundle whose `uv sync` failed must still offer Install."""
    folder = paths.mcp_server_dir
    return (folder / "pyproject.toml").is_file() and (folder / ".venv" / "bin" / "python").exists()


def server_command(paths: AppPaths, blender_binary: Path | None) -> dict:
    """How an MCP client (the brain, or our test) launches the official server."""
    env = {"BLENDER_PATH": str(blender_binary)} if blender_binary else {}
    return {"command": "uv", "args": ["run", "--directory", str(paths.mcp_server_dir), "blender-mcp"], "env": env}


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    root = target.resolve()
    for member in archive.infolist():
        destination = (target / member.filename).resolve()
        if destination != root and root not in destination.parents:
            raise JobError(f"Refusing to unpack {member.filename!r}: it points outside the install folder")
    archive.extractall(target)


class McpAddon(Component):
    id = "mcp_addon"
    name = "Blender MCP add-on"
    role = "Lets AI tools act inside Blender"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.candidates: Sequence[Path] | None = None
        self.home: Path | None = None
        self.port = ADDON_PORT
        self.download = download

    def _blender(self):
        binary = find_blender(self.runner, self.candidates, self.ctx.probes)
        return binary, blender_version(self.runner, binary, self.ctx.probes) if binary else None

    def check(self) -> ComponentState:
        binary, version = self._blender()
        if binary is None:
            return self.state(
                Health.MISSING, f"Install Blender first. Then this adds Blender Lab's official MCP add-on (v{RELEASE})."
            )
        if version is None or version[:2] < MIN_BLENDER:
            return self.state(Health.MISSING, "Needs Blender 5.1 or newer.")
        details = [("Release", f"{RELEASE} · Blender Lab"), ("Port", self.port)]
        if not (extensions_dir(version, self.home) / ADDON_ID).is_dir():
            return self.state(
                Health.MISSING,
                f"Not in Blender yet. Install downloads Blender Lab's official add-on (v{RELEASE}).",
                details,
                [ActionSpec("install", "Install add-on")],
            )
        if port_open("127.0.0.1", self.port):
            return self.state(Health.READY, f"Listening on localhost:{self.port}", details)
        return self.state(
            Health.INSTALLED,
            "Installed. It switches on when Blender starts with online access: use Launch on the Blender unit.",
            details,
        )

    def action_install(self, job: Job) -> dict:
        binary, _ = self._blender()
        if binary is None:
            raise JobError("Install Blender first.")
        zip_path = self.download(ADDON_ZIP_URL, self.paths.downloads / f"mcp-{RELEASE}.zip", job)
        install_extension(self.runner, binary, zip_path, job)
        job.log("Done. If Blender is open, quit it and use Launch so the add-on starts listening.")
        return {"installed": True}


class McpServer(Component):
    id = "mcp_server"
    name = "Blender MCP server"
    role = "Speaks MCP for Claude Code / Codex"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.candidates: Sequence[Path] | None = None
        self.addon_port = ADDON_PORT
        self.download = download
        self.command_override: dict | None = None  # tests run a fake server instead

    @property
    def folder(self) -> Path:
        return self.paths.mcp_server_dir

    def installed(self) -> bool:
        return server_installed(self.paths)

    def check(self) -> ComponentState:
        if not self.runner.which("uv"):
            return self.state(Health.ERROR, "uv is needed to run the MCP server. Install uv (astral.sh/uv), then refresh.")
        details = [("Release", f"{RELEASE} · Blender Lab"), ("Location", tilde(self.folder))]
        if not self.installed():
            return self.state(
                Health.MISSING,
                f"Not installed. Install downloads Blender Lab's official server (v{RELEASE}) and its Python packages.",
                details,
                [ActionSpec(
                    "install",
                    "Install server",
                    confirm=f"Download Blender Lab's MCP server v{RELEASE} and install its Python packages with uv?",
                )],
            )
        test = [ActionSpec("test", "Test")]
        if port_open("127.0.0.1", self.addon_port):
            return self.state(Health.READY, "Installed · Blender's MCP add-on is listening", details, test)
        return self.state(Health.INSTALLED, "Installed. Waiting for Blender's MCP add-on to start listening.", details, test)

    def action_install(self, job: Job) -> dict:
        if not self.runner.which("uv"):
            raise JobError("uv isn't installed. Install it from astral.sh/uv, then try again.")
        bundle = self.download(SERVER_MCPB_URL, self.paths.downloads / f"blender-{RELEASE}.mcpb", job)
        staging = self.folder.with_name(self.folder.name + ".new")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            with zipfile.ZipFile(bundle) as archive:
                _safe_extract(archive, staging)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise JobError(f"The downloaded bundle isn't a valid archive: {exc}") from exc
        except JobError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        root = staging
        entries = [p for p in staging.iterdir()]
        if not (root / "pyproject.toml").is_file() and len(entries) == 1 and entries[0].is_dir():
            root = entries[0]  # a bundle wrapped in one top-level folder
        if not (root / "pyproject.toml").is_file():
            shutil.rmtree(staging, ignore_errors=True)
            raise JobError("Unexpected bundle layout: there's no pyproject.toml in the .mcpb")
        shutil.rmtree(self.folder, ignore_errors=True)
        root.rename(self.folder)
        shutil.rmtree(staging, ignore_errors=True)
        job.log(f"Unpacked to {self.folder}")
        job.log("Installing the server's Python packages with uv…")
        code = self.runner.stream(
            ["uv", "sync", "--python", UV_PYTHON, "--directory", str(self.folder)], job.log, timeout=900
        )
        if code != 0:
            raise JobError(f"uv couldn't install the server's packages (exit {code}). See the log above.")
        return {"installed": True}

    def action_test(self, job: Job) -> dict:
        command = self.command_override or server_command(self.paths, find_blender(self.runner, self.candidates))
        job.log("Starting the MCP server and asking for its tools…")
        try:
            with StdioMcpClient([command["command"], *command["args"]], env=command["env"], timeout=120) as client:
                client.initialize()
                tools = client.list_tools()
                names = [t.get("name", "?") for t in tools]
                job.log(f"{len(tools)} tools: {', '.join(names[:8])}{'…' if len(names) > 8 else ''}")
                blender = "not running"
                if port_open("127.0.0.1", self.addon_port):
                    job.log("Asking Blender for a summary of the open file…")
                    result = client.call_tool("get_blendfile_summary_datablocks", {})
                    blender = "error" if result.get("isError") else "connected"
                else:
                    job.log("Blender's MCP add-on isn't listening, so the scene wasn't checked.")
        except McpError as exc:
            raise JobError(f"The MCP server failed: {exc}") from exc
        return {"tools": len(tools), "blender": blender}
