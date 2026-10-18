"""Talk to the sockets Blender add-ons open on localhost."""

from __future__ import annotations

import json
import socket

BRIDGE_PORT = 9877  # matches blender_addon/gazevoice_bridge/protocol.py


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bridge_request(request: dict, host: str = "127.0.0.1", port: int = BRIDGE_PORT, timeout: float = 2.0) -> dict:
    """Send one request to the GazeVoice Bridge add-on and return its reply."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("The bridge closed the connection")
            buffer += chunk
    reply = json.loads(buffer.split(b"\n", 1)[0])
    if not isinstance(reply, dict):
        raise ValueError("The bridge sent something unexpected")
    return reply
