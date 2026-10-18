"""Wire protocol for the GazeVoice Bridge add-on.

Newline-delimited JSON over a local TCP socket. This module never imports bpy,
so it can be tested outside Blender.

    {"cmd": "ping"}                 -> {"ok": true, "bridge": "0.1.0", "blender": "5.2.2"}
    {"cmd": "selection"}            -> {"ok": true, "active": "Cube", "selected": ["Cube"], "mode": "OBJECT"}
    {"cmd": "status", "text": "…"}  -> {"ok": true}   (shown in the 3D viewport)
"""

from __future__ import annotations

import json
from typing import Protocol

BRIDGE_PORT = 9877
BRIDGE_VERSION = "0.1.0"
MAX_STATUS = 200
MAX_LINE = 64 * 1024
INVALID = {"cmd": "__invalid__"}


class SceneAccess(Protocol):
    def version(self) -> str: ...

    def active_name(self) -> str | None: ...

    def selected_names(self) -> list[str]: ...

    def mode(self) -> str: ...

    def set_status(self, text: str) -> None: ...


def decode_lines(buffer: bytearray) -> list[dict]:
    """Pop every complete line from buffer and parse it; a partial line stays put."""
    messages = []
    while True:
        end = buffer.find(b"\n")
        if end < 0:
            if len(buffer) > MAX_LINE:
                buffer.clear()
                messages.append(dict(INVALID))
            return messages
        line = bytes(buffer[:end])
        del buffer[: end + 1]
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        messages.append(message if isinstance(message, dict) else dict(INVALID))


def encode(message: dict) -> bytes:
    return (json.dumps(message) + "\n").encode()


def handle(request: dict, scene: SceneAccess) -> dict:
    command = request.get("cmd")
    if command == "ping":
        return {"ok": True, "bridge": BRIDGE_VERSION, "blender": scene.version()}
    if command == "selection":
        return {
            "ok": True,
            "active": scene.active_name(),
            "selected": scene.selected_names(),
            "mode": scene.mode(),
        }
    if command == "status":
        text = request.get("text")
        if not isinstance(text, str):
            return {"ok": False, "error": "text must be a string"}
        scene.set_status(text[:MAX_STATUS])
        return {"ok": True}
    return {"ok": False, "error": "unknown command"}
