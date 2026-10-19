"""Talk to the sockets Blender add-ons open on localhost."""

from __future__ import annotations

import json
import socket
import threading

BRIDGE_PORT = 9877  # matches blender_addon/gazevoice_bridge/protocol.py
BRIDGE_VERSION = "0.2.0"


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


class BridgeClient:
    """One kept-open connection to the bridge, shared safely between threads.

    Voice polls the bridge 20 times a second; reconnecting each time would be wasteful.
    Any socket error closes the connection, and the next request reconnects.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = BRIDGE_PORT, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()

    def request(self, request: dict, timeout: float | None = None) -> dict:
        """Send one request and wait for its reply (`timeout` overrides the default for this one)."""
        with self._lock:
            try:
                if self._sock is None:
                    self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                    self._buffer = b""
                self._sock.settimeout(timeout or self.timeout)
                self._sock.sendall((json.dumps(request) + "\n").encode())
                while b"\n" not in self._buffer:
                    chunk = self._sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("The bridge closed the connection")
                    self._buffer += chunk
                line, self._buffer = self._buffer.split(b"\n", 1)
                reply = json.loads(line)
            except (OSError, ValueError):
                self._close_locked()
                raise
        if not isinstance(reply, dict):
            raise ValueError("The bridge sent something unexpected")
        return reply

    def _close_locked(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""

    def close(self) -> None:
        with self._lock:
            self._close_locked()
