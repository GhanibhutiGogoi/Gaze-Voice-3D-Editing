"""GazeVoice Bridge: lets the GazeVoice app see your selection and show its status.

Listens on 127.0.0.1 only. A bpy timer polls a non-blocking socket, so all
scene access happens on Blender's main thread. The status line is drawn at
the bottom-left of every 3D viewport. Later this is where gaze plugs in.
"""

from __future__ import annotations

import socket

import blf
import bpy

from . import protocol

HOST = "127.0.0.1"
POLL_SECONDS = 0.1

_server: socket.socket | None = None
_clients: dict[socket.socket, bytearray] = {}
_status = {"text": ""}
_draw_handle = None


class BpyScene:
    """protocol.SceneAccess backed by the live Blender session."""

    def version(self) -> str:
        return bpy.app.version_string

    def active_name(self) -> str | None:
        layer = bpy.context.view_layer
        active = layer.objects.active if layer else None
        return active.name if active else None

    def selected_names(self) -> list[str]:
        layer = bpy.context.view_layer
        return [obj.name for obj in layer.objects if obj.select_get()] if layer else []

    def mode(self) -> str:
        return bpy.context.mode

    def set_status(self, text: str) -> None:
        _status["text"] = text
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()


def _drop(conn: socket.socket) -> None:
    _clients.pop(conn, None)
    try:
        conn.close()
    except OSError:
        pass


def _poll() -> float | None:
    if _server is None:
        return None  # unregisters the timer
    try:
        while True:
            conn, _ = _server.accept()
            conn.setblocking(False)
            _clients[conn] = bytearray()
    except (BlockingIOError, OSError):
        pass

    scene = BpyScene()
    for conn, buffer in list(_clients.items()):
        try:
            data = conn.recv(65536)
        except BlockingIOError:
            continue
        except OSError:
            _drop(conn)
            continue
        if not data:
            _drop(conn)
            continue
        buffer.extend(data)
        for request in protocol.decode_lines(buffer):
            try:
                response = protocol.handle(request, scene)
            except Exception as exc:  # noqa: BLE001 - never kill the timer
                response = {"ok": False, "error": str(exc)}
            try:
                conn.sendall(protocol.encode(response))
            except OSError:
                _drop(conn)
                break
    return POLL_SECONDS


def _start_server() -> None:
    global _server
    if _server is not None:
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, protocol.BRIDGE_PORT))
    except OSError as exc:
        print(f"GazeVoice Bridge: port {protocol.BRIDGE_PORT} is busy ({exc}); not listening.")
        sock.close()
        return
    sock.listen(4)
    sock.setblocking(False)
    _server = sock
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=POLL_SECONDS, persistent=True)


def _stop_server() -> None:
    global _server
    if bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    for conn in list(_clients):
        _drop(conn)
    if _server is not None:
        _server.close()
        _server = None


def _draw_status() -> None:
    text = _status["text"]
    if not text:
        return
    font = 0
    blf.size(font, 14)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
    blf.color(font, 0.95, 0.75, 0.35, 1.0)
    blf.position(font, 20, 28, 0)
    blf.draw(font, f"GazeVoice · {text}")
    blf.disable(font, blf.SHADOW)


def register() -> None:
    global _draw_handle
    if bpy.app.background:
        return  # e.g. `blender --command extension install-file`: no UI, no listener
    _start_server()
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_status, (), "WINDOW", "POST_PIXEL")


def unregister() -> None:
    global _draw_handle
    _stop_server()
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
