"""Our own Blender add-on: tells the app what's selected, shows a status line."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Sequence

from ..blender_socket import BRIDGE_PORT, bridge_request, port_open
from ..jobs import Job, JobError
from .base import ActionSpec, Component, ComponentState, Health
from .blender import MIN_BLENDER, blender_version, extensions_dir, find_blender, install_extension, is_running

BRIDGE_ID = "gazevoice_bridge"
BRIDGE_VERSION = "0.1.0"
BRIDGE_SRC = Path(__file__).resolve().parents[2] / "blender_addon" / BRIDGE_ID


def build_bridge_zip(src_dir: Path, dest_zip: Path) -> Path:
    """Zip the add-on with its files at the archive root, as Blender extensions expect."""
    if not (src_dir / "blender_manifest.toml").is_file():
        raise JobError(f"The bridge add-on's files aren't at {src_dir}. Is this a complete GazeVoice checkout?")
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                zf.write(path, path.relative_to(src_dir).as_posix())
    return dest_zip


class Bridge(Component):
    id = "bridge"
    name = "GazeVoice Bridge"
    role = "Our Blender add-on · selection + status line"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.candidates: Sequence[Path] | None = None
        self.home: Path | None = None
        self.port = BRIDGE_PORT

    def _blender(self):
        binary = find_blender(self.runner, self.candidates, self.ctx.probes)
        return binary, blender_version(self.runner, binary, self.ctx.probes) if binary else None

    def check(self) -> ComponentState:
        binary, version = self._blender()
        if binary is None:
            return self.state(
                Health.MISSING, "Install Blender first. Then this adds our add-on that shares your selection."
            )
        if version is None or version[:2] < MIN_BLENDER:
            return self.state(Health.MISSING, "Needs Blender 5.1 or newer.")
        details = [("Port", self.port)]
        if not (extensions_dir(version, self.home) / BRIDGE_ID).is_dir():
            return self.state(
                Health.MISSING,
                "Not in Blender yet. Install adds it as a Blender extension.",
                details,
                [ActionSpec("install", "Install add-on")],
            )
        if not port_open("127.0.0.1", self.port):
            if is_running(self.runner, self.ctx.probes):
                return self.state(Health.INSTALLED, "Installed. Restart Blender so the bridge starts listening.", details)
            return self.state(Health.INSTALLED, "Installed. Start Blender to connect.", details)
        try:
            ping = bridge_request({"cmd": "ping"}, port=self.port, timeout=1.0)
        except (OSError, ValueError):
            ping = {}
        if not ping.get("ok"):
            return self.state(Health.ERROR, f"Something else is using port {self.port}.", details)
        return self.state(
            Health.READY,
            f"Connected to Blender {ping.get('blender', '')}".strip(),
            details,
            [ActionSpec("test", "Test")],
        )

    def action_install(self, job: Job) -> dict:
        binary, version = self._blender()
        if binary is None:
            raise JobError("Install Blender first.")
        zip_path = build_bridge_zip(BRIDGE_SRC, self.paths.downloads / f"{BRIDGE_ID}-{BRIDGE_VERSION}.zip")
        job.log(f"Built {zip_path.name}")
        install_extension(self.runner, binary, zip_path, job)
        job.log("Done. If Blender is open, restart it so the bridge starts listening.")
        return {"installed": True}

    def action_test(self, job: Job) -> dict:
        try:
            job.log("Showing “GazeVoice connected” in the Blender viewport…")
            bridge_request({"cmd": "status", "text": "GazeVoice connected"}, port=self.port)
            selection = bridge_request({"cmd": "selection"}, port=self.port)
            ping = bridge_request({"cmd": "ping"}, port=self.port)
        except (OSError, ValueError) as exc:
            raise JobError(f"The bridge didn't answer: {exc}") from exc
        selected = selection.get("selected") or []
        job.log(f"Selected: {', '.join(selected) if selected else 'nothing'} (mode {selection.get('mode')})")
        return {"selection": selection, "blender": ping.get("blender")}
