"""Wire protocol for the GazeVoice Bridge add-on.

Newline-delimited JSON over a local TCP socket. This module never imports bpy,
so it can be tested outside Blender.

    {"cmd": "ping"}                -> {"ok": true, "bridge": "0.2.0", "blender": "5.2.2", "key": "⌥ Space"}
    {"cmd": "selection"}           -> {"ok": true, "active": "Cube", "selected": ["Cube"], "mode": "OBJECT"}
    {"cmd": "status", "text": "…", -> {"ok": true}   (shown in the 3D viewport and sidebar)
     "state": "listening"}
    {"cmd": "poll", "ack": 12, "boot": "9f3a…"}
                                   -> {"ok": true, "boot": "9f3a…", "events": [{"seq": 13, "type": "ptt",
                                                                               "state": "down", "t": …}]}
        Events stay until a later poll acknowledges them, so a reply lost to a timeout loses
        nothing. "boot" changes when Blender restarts, and an ack sent with an earlier boot
        acknowledges nothing. Without "ack", a poll drains everything.
    {"cmd": "scene_names"}         -> {"ok": true, "names": ["Cube", …]}
    {"cmd": "begin_edit"}          -> {"ok": true}   (undo_push can then tell whether the scene changed)
    {"cmd": "undo_push", "message": "…", "only_if_changed": true} -> {"ok": true, "pushed": true}
    {"cmd": "undo"} / {"cmd": "redo"} -> {"ok": true}
        Undo operations answer {"ok": false, "error": "busy"} while another modal operator
        (say, a G/R/S transform) runs; the app tries again a moment later.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Callable, Protocol

BRIDGE_PORT = 9877
BRIDGE_VERSION = "0.2.0"
MAX_STATUS = 200
MAX_NAMES = 300
MAX_LINE = 64 * 1024
MAX_EVENTS = 100
MAX_UNDO_NAME = 63  # Blender keeps 64 bytes of an undo step's name, the closing NUL included
LIVE_SECONDS = 3.0  # polled this recently means the app's voice loop is on
STATUS_STATES = ("idle", "listening", "working", "done", "error")
INVALID = {"cmd": "__invalid__"}
_KEY_NAMES = {"SPACE": "Space", "RET": "Return", "ESC": "Esc", "TAB": "Tab", "BACK_SPACE": "Delete"}


class SceneAccess(Protocol):
    def version(self) -> str: ...

    def active_name(self) -> str | None: ...

    def selected_names(self) -> list[str]: ...

    def mode(self) -> str: ...

    def set_status(self, text: str, state: str = "idle") -> None: ...

    def object_names(self) -> list[str]: ...

    def undo_push(self, message: str) -> bool: ...

    def undo(self) -> bool: ...

    def redo(self) -> bool: ...

    def key_label(self) -> str: ...

    def busy(self) -> bool: ...

    def changed(self) -> bool: ...

    def mark_unchanged(self) -> None: ...


class EventQueue:
    """Hold-to-talk events waiting for the app to poll them."""

    def __init__(self, limit: int = MAX_EVENTS, clock: Callable[[], float] = time.time):
        self.boot = secrets.token_hex(4)
        self.limit = limit
        self.clock = clock
        self.last_poll = 0.0
        self._seq = 0
        self._events: list[dict] = []

    def add(self, event_type: str, **fields) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "type": event_type, "t": self.clock(), **fields})
        del self._events[:-self.limit]

    def poll(self, ack: int | None) -> list[dict]:
        self.last_poll = self.clock()
        if ack is None:
            events, self._events = self._events, []
            return events
        self._events = [event for event in self._events if event["seq"] > ack]
        return list(self._events)

    def live(self, within: float = LIVE_SECONDS) -> bool:
        """Whether the app is polling, i.e. its voice loop is on."""
        return self.clock() - self.last_poll < within


def undo_name(message: str, limit: int = MAX_UNDO_NAME) -> str:
    """The message cut to fit Blender's undo step names (counted in bytes), ending in … when cut."""
    data = message.encode()
    if len(data) <= limit:
        return message
    return data[: limit - len("…".encode())].decode(errors="ignore").rstrip() + "…"


def key_label(key_type: str, ctrl: bool = False, alt: bool = False, shift: bool = False, oskey: bool = False) -> str:
    """A key binding the way a Mac writes it: "⌥ Space", "⌃⇧ Numpad 0"."""
    mods = "".join(sym for on, sym in ((ctrl, "⌃"), (alt, "⌥"), (shift, "⇧"), (oskey, "⌘")) if on)
    name = _KEY_NAMES.get(key_type) or key_type.replace("_", " ").title()
    return f"{mods} {name}" if mods else name


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


def handle(request: dict, scene: SceneAccess, events: EventQueue) -> dict:
    command = request.get("cmd")
    if command == "ping":
        return {"ok": True, "bridge": BRIDGE_VERSION, "blender": scene.version(), "key": scene.key_label()}
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
        state = request.get("state", "idle")
        scene.set_status(text[:MAX_STATUS], state if state in STATUS_STATES else "idle")
        return {"ok": True}
    if command == "poll":
        ack = request.get("ack")
        ack = ack if isinstance(ack, int) and not isinstance(ack, bool) else None
        if ack is not None and request.get("boot") not in (None, events.boot):
            ack = 0  # counted against an earlier Blender session: nothing here has been seen yet
        return {"ok": True, "boot": events.boot, "events": events.poll(ack)}
    if command == "scene_names":
        return {"ok": True, "names": scene.object_names()[:MAX_NAMES]}
    if command == "begin_edit":
        scene.mark_unchanged()
        return {"ok": True}
    if command == "undo_push":
        message = request.get("message")
        if not isinstance(message, str):
            return {"ok": False, "error": "message must be a string"}
        if scene.busy():
            return {"ok": False, "error": "busy"}
        if request.get("only_if_changed") and not scene.changed():
            return {"ok": True, "pushed": False}
        pushed = bool(scene.undo_push(undo_name(message)))
        if pushed:
            scene.mark_unchanged()
        return {"ok": pushed, "pushed": pushed}
    if command in ("undo", "redo"):
        if scene.busy():
            return {"ok": False, "error": "busy"}
        return {"ok": bool(scene.undo() if command == "undo" else scene.redo())}
    return {"ok": False, "error": "unknown command"}
