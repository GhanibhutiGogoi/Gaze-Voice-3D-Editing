"""Wire protocol for the GazeVoice Bridge add-on.

Newline-delimited JSON over a local TCP socket. This module never imports bpy,
so it can be tested outside Blender.

    {"cmd": "ping"}                -> {"ok": true, "bridge": "0.3.0", "blender": "5.2.2", "key": "⌥ Space"}
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

Gaze. The target is the largest 3D viewport in the first window. "u, v" are 0 to 1 across it, from the
top left. Nothing here edits the scene.
    {"cmd": "gaze_view"}           -> {"ok": true, "width": 1840, "height": 1100}
                                      or {"ok": false, "error": "no 3D viewport"}
    {"cmd": "gaze_pick",           -> {"ok": true, "hits": [{"uid": 116, "name": "Eagle", "faces": 5120,
     "points": [[u, v], …]}                                   "point": [x, y, z]} or null, …]}
        One hit per point, in order; null is a miss. "uid" identifies the object for as long as Blender
        runs (names are only for people), "faces" counts the original mesh's faces, "point" is in world
        space. At most MAX_POINTS points, each inside the viewport.
    {"cmd": "gaze_piece", "uid": 116, "point": [x, y, z]}
                                   -> {"ok": true, "face": 3120, "part": "Wing.L" or null,
                                       "piece_faces": 480 or null, "object_faces": 5120}
        What part of the model the world-space point on object "uid" is. "face" is the ORIGINAL
        mesh's face (modifiers renumber faces, and edits change the original); it is null only when
        the original mesh has no faces, say because Geometry Nodes makes them all. "part" is read off the
        face you SEE, so a mirrored wing reads "Wing.R": the vertex group with the most weight, else the
        material name when the object has two or more, else null. "piece_faces" is how many faces
        connect to that face through shared vertices, null above MAX_PIECE_FACES.
    {"cmd": "gaze_show", "dot": [u, v] or null, "ring": [u, v, ru, rv] or null,
     "marks": [{"n": 1, "u": 0.4, "v": 0.3, "name": "Chair"}]}      -> {"ok": true}
        Draws in the target viewport (a null/empty field draws nothing). "ru, rv" are the ring's radii
        as shares of the viewport's width and height.
    {"cmd": "gaze_crop", "u": 0.4, "v": 0.3,                        -> {"ok": true, "pending": true}
     "radius": [0.1, 0.15], "size": 512}                              or {"ok": false, "error": "…"}
    {"cmd": "gaze_crop_result"}    -> {"ok": true, "pending": true}   (keep asking)
                                      or {"ok": true, "png": "<base64>"}
                                      or {"ok": false, "error": "…"}
        A picture of the part of the viewport around a target that has no name, for the app to show a
        model. "u, v" and "radius" are as in gaze_show's ring. The picture is the box 2.5 radii each way
        from the point, cut to the viewport, scaled so its long side is "size" pixels (at most
        MAX_CROP_SIZE), with the ring drawn on it and nothing else. It is a PNG in the reply, as base64 on
        one line (replies may be long; only requests are limited by MAX_LINE). The viewport only draws
        when Blender repaints it, so gaze_crop returns at once and the picture follows a moment later;
        gaze_crop_result answers "pending" until it is ready, then hands it over exactly once: Blender keeps
        no copy and writes no file. A second gaze_crop while one is pending is refused. If the viewport
        hasn't drawn within CROP_SECONDS (say the window is hidden), the result is an error.
    While the push-to-talk key is held, "poll" also carries the mouse as a stand-in for gaze, at most
    MAX_POINTER_RATE a second, whether it moves or rests where it is:
        {"seq": 14, "type": "pointer", "u": 0.4, "v": 0.3, "t": …}
"""

from __future__ import annotations

import base64
import json
import math
import secrets
import struct
import time
import zlib
from typing import Callable, Protocol

BRIDGE_PORT = 9877
BRIDGE_VERSION = "0.3.0"
MAX_STATUS = 200
MAX_NAMES = 300
MAX_LINE = 64 * 1024
MAX_EVENTS = 100
MAX_UNDO_NAME = 63  # Blender keeps 64 bytes of an undo step's name, the closing NUL included
LIVE_SECONDS = 3.0  # polled this recently means the app's voice loop is on
MAX_POINTS = 64  # rays in one gaze_pick
MAX_PIECE_FACES = 200000  # above this many faces, counting a piece is skipped so a huge mesh can't freeze Blender
MAX_MARKS = 20
MAX_MARK_NAME = 40
MAX_POINTER_RATE = 20  # pointer events a second, at most
MAX_CROP_SIZE = 1024  # the long side of a gaze_crop picture, in pixels
CROP_SECONDS = 3.0  # a crop whose viewport hasn't drawn by then is given up
CROP_MARGIN = 2.5  # a crop reaches this many ring radii out from the target, each way
RING_COLOR = (90, 190, 255)  # the ring drawn on a crop: one bright colour...
RING_EDGE_COLOR = (12, 12, 16)  # ...with a dark edge, so it shows on any background
RING_HALF_WIDTH = 1.5  # pixels either side of the ring's line, bright
RING_EDGE = 1.5  # more pixels beyond that on each side, dark
MAX_RING_STEPS = 16384  # points along the ring that get a dab of colour
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

    def gaze_view(self) -> tuple[int, int] | None:
        """The target viewport's (width, height) in pixels, or None when there is no 3D viewport."""

    def gaze_pick(self, points: list[tuple[float, float]]) -> list[dict | None] | None:
        """One hit (uid, name, faces, point) or None for each (u, v); None when there is no 3D viewport."""

    def gaze_piece(self, uid: int, point: tuple[float, float, float]) -> dict | None:
        """face, part, piece_faces and object_faces, or None when no mesh object has this uid."""

    def gaze_show(self, dot: list | None, ring: list | None, marks: list[dict]) -> None: ...

    def gaze_crop(self, u: float, v: float, radius: list[float], size: int) -> str | None:
        """Start taking the picture. None once it has started, else why it can't: no 3D viewport, or one is pending."""

    def gaze_crop_result(self) -> dict:
        """{"pending": True}, {"png": bytes} or {"error": str}. Handing over a picture or an error clears it."""


class EventQueue:
    """Hold-to-talk events waiting for the app to poll them."""

    def __init__(self, limit: int = MAX_EVENTS, clock: Callable[[], float] = time.time):
        self.boot = secrets.token_hex(4)
        self.limit = limit
        self.clock = clock
        self.last_poll = 0.0
        self._seq = 0
        self._last_pointer = -math.inf
        self._events: list[dict] = []

    def add(self, event_type: str, **fields) -> None:
        self._seq += 1
        self._events.append({"seq": self._seq, "type": event_type, "t": self.clock(), **fields})
        while len(self._events) > self.limit:
            # Mouse positions are a stream: shed the oldest of those before a hold's down or up.
            oldest_pointer = next((i for i, event in enumerate(self._events) if event["type"] == "pointer"), 0)
            del self._events[oldest_pointer]

    def add_pointer(self, u: float, v: float) -> bool:
        """Queue where the mouse is, unless one was queued less than 1/MAX_POINTER_RATE seconds ago."""
        now = self.clock()
        if now - self._last_pointer < 1 / MAX_POINTER_RATE:
            return False
        self._last_pointer = now
        self.add("pointer", u=u, v=v)
        return True

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


class CropSlot:
    """The one viewport picture Blender is making or has made for the app, held in memory and handed over once.

    The picture is made inside a draw callback some time after the request, so there is a short wait in between:
    idle, pending (asked for, not drawn yet), then ready (a picture) or failed (an error). Reading a picture or an
    error clears it. A pending crop that isn't done within `timeout` seconds becomes a failure.
    """

    NOTHING_WAITING = "no picture is waiting"
    TIMED_OUT = "Blender's viewport didn't draw in time (is its window hidden?)"

    def __init__(self, clock: Callable[[], float] = time.monotonic, timeout: float = CROP_SECONDS):
        self._clock = clock
        self._timeout = timeout
        self._state = "idle"
        self._started = 0.0
        self._png: bytes | None = None
        self._error = ""

    @property
    def state(self) -> str:
        """"idle", "pending", "ready" or "failed"."""
        if self._state == "pending" and self._clock() - self._started > self._timeout:
            self._state, self._error = "failed", self.TIMED_OUT
        return self._state

    def begin(self) -> bool:
        """Start a new picture. False, changing nothing, while one is still pending. A result nobody read is dropped."""
        if self.state == "pending":
            return False
        self._state, self._started, self._png, self._error = "pending", self._clock(), None, ""
        return True

    def finish(self, png: bytes) -> None:
        """The picture is made. Ignored unless a crop is pending, so a late one (after giving up) is thrown away."""
        if self.state == "pending":
            self._state, self._png = "ready", png

    def fail(self, error: str) -> None:
        if self.state == "pending":
            self._state, self._error = "failed", error

    def read(self) -> dict:
        """{"pending": True}, or {"png": bytes} / {"error": str} once, after which nothing is kept."""
        state = self.state
        if state == "pending":
            return {"pending": True}
        if state == "ready":
            png, self._png, self._state = self._png, None, "idle"
            return {"png": png}
        if state == "failed":
            error, self._error, self._state = self._error, "", "idle"
            return {"error": error}
        return {"error": self.NOTHING_WAITING}

    def drop(self) -> None:
        """Forget everything, say when the add-on is switched off."""
        self._state, self._png, self._error = "idle", None, ""


def crop_box(u: float, v: float, radius: list[float], width: int, height: int,
             margin: float = CROP_MARGIN) -> tuple[int, int, int, int]:
    """The pixels (left, top, right, bottom) of a width x height image to crop for a ring at (u, v) with radii
    `radius` (shares of the width and height): `margin` radii out each way, cut off at the image's edges. The right and
    bottom are exclusive, and the box is never empty."""
    centre_x, centre_y = u * width, v * height
    reach_x, reach_y = radius[0] * margin * width, radius[1] * margin * height
    left = min(max(0, math.floor(centre_x - reach_x)), width - 1)
    top = min(max(0, math.floor(centre_y - reach_y)), height - 1)
    right = max(min(width, math.ceil(centre_x + reach_x)), left + 1)
    bottom = max(min(height, math.ceil(centre_y + reach_y)), top + 1)
    return left, top, right, bottom


def crop_size(box: tuple[int, int, int, int], size: int) -> tuple[int, int]:
    """(width, height) of the box scaled so that its long side is `size` pixels."""
    box_width, box_height = box[2] - box[0], box[3] - box[1]
    scale = size / max(box_width, box_height)
    return max(1, round(box_width * scale)), max(1, round(box_height * scale))


def ring_in_crop(u: float, v: float, radius: list[float], width: int, height: int,
                 box: tuple[int, int, int, int], out: tuple[int, int]) -> tuple[float, float, float, float]:
    """Where the ring goes on the cropped, scaled picture: (cx, cy, rx, ry) in its pixels. `width, height` is the
    image the box was cut from and `out` the picture's size."""
    scale_x, scale_y = out[0] / (box[2] - box[0]), out[1] / (box[3] - box[1])
    return (
        (u * width - box[0]) * scale_x,
        (v * height - box[1]) * scale_y,
        radius[0] * width * scale_x,
        radius[1] * height * scale_y,
    )


def _dab(pixels: bytearray, width: int, height: int, x: float, y: float, radius: float, color: bytes) -> None:
    """Colour every pixel of a width x height RGB buffer whose centre is within `radius` of (x, y)."""
    radius_squared = radius * radius
    for row in range(max(0, math.floor(y - radius)), min(height, math.ceil(y + radius))):
        dy = row + 0.5 - y
        for column in range(max(0, math.floor(x - radius)), min(width, math.ceil(x + radius))):
            dx = column + 0.5 - x
            if dx * dx + dy * dy <= radius_squared:
                start = (row * width + column) * 3
                pixels[start : start + 3] = color


def draw_ring(pixels: bytearray, width: int, height: int, cx: float, cy: float, rx: float, ry: float) -> None:
    """Draw an ellipse outline into a width x height RGB buffer (top row first), in place: RING_COLOR with a dark
    edge either side. (cx, cy) and the radii are in pixels, a pixel being one unit square. Only the outline is
    touched, and parts outside the picture are left out."""
    if not all(math.isfinite(n) for n in (cx, cy, rx, ry)) or rx <= 0 or ry <= 0:
        return
    steps = min(MAX_RING_STEPS, max(16, math.ceil(2 * math.pi * max(rx, ry))))
    angles = [2 * math.pi * i / steps for i in range(steps)]
    points = [(cx + rx * math.cos(a), cy + ry * math.sin(a)) for a in angles]
    for radius, color in ((RING_HALF_WIDTH + RING_EDGE, bytes(RING_EDGE_COLOR)), (RING_HALF_WIDTH, bytes(RING_COLOR))):
        for x, y in points:
            _dab(pixels, width, height, x, y, radius, color)


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """A PNG file (8-bit RGB) from width x height pixels, top row first, three bytes each."""
    if width < 1 or height < 1 or len(rgb) != width * height * 3:
        raise ValueError("rgb must hold exactly width x height pixels of three bytes")
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride : (y + 1) * stride]) for y in range(height))  # filter type 0 per row
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


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


def linked_face_count(faces: list[list[int]], start: int) -> int:
    """How many faces connect to face `start` through shared vertices, itself included. 0 if there is no such face."""
    if not 0 <= start < len(faces):
        return 0
    by_vertex: dict[int, list[int]] = {}
    for index, face in enumerate(faces):
        for vertex in face:
            by_vertex.setdefault(vertex, []).append(index)
    seen = {start}
    visited_vertices: set[int] = set()
    stack = [start]
    while stack:
        for vertex in faces[stack.pop()]:
            if vertex in visited_vertices:
                continue
            visited_vertices.add(vertex)
            for other in by_vertex[vertex]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
    return len(seen)


def face_part(weights: dict[str, float], material: str | None = None, material_count: int = 0) -> str | None:
    """The part of the model a face belongs to: the vertex group with the most weight on it (`weights` is the
    summed weight per group), else the face's material when the object has two or more, else None."""
    best, best_weight = None, 0.0
    for name, weight in weights.items():
        if weight > best_weight:
            best, best_weight = name, weight
    if best is not None:
        return best
    return material if material_count >= 2 else None


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
    if command == "gaze_view":
        view = scene.gaze_view()
        if view is None:
            return _no_viewport()
        return {"ok": True, "width": view[0], "height": view[1]}
    if command == "gaze_pick":
        return _gaze_pick(request.get("points"), scene)
    if command == "gaze_piece":
        return _gaze_piece(request.get("uid"), request.get("point"), scene)
    if command == "gaze_show":
        return _gaze_show(request, scene)
    if command == "gaze_crop":
        return _gaze_crop(request, scene)
    if command == "gaze_crop_result":
        return _gaze_crop_result(scene)
    return {"ok": False, "error": "unknown command"}


def _no_viewport() -> dict:
    return {"ok": False, "error": "no 3D viewport"}


def _real(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unit(value) -> bool:
    """A number from 0 to 1. NaN and infinity are not."""
    return _real(value) and 0.0 <= value <= 1.0


def _gaze_pick(points, scene: SceneAccess) -> dict:
    if not isinstance(points, list) or not all(
        isinstance(p, (list, tuple)) and len(p) == 2 and all(_real(c) for c in p) for p in points
    ):
        return {"ok": False, "error": "points must be a list of [u, v] pairs"}
    if len(points) > MAX_POINTS:
        return {"ok": False, "error": f"at most {MAX_POINTS} points"}
    if not all(_unit(u) and _unit(v) for u, v in points):
        return {"ok": False, "error": "points must be inside the viewport (0 to 1)"}
    hits = scene.gaze_pick([(float(u), float(v)) for u, v in points])
    if hits is None:
        return _no_viewport()
    return {"ok": True, "hits": hits}


def _gaze_piece(uid, point, scene: SceneAccess) -> dict:
    if not isinstance(uid, int) or isinstance(uid, bool):
        return {"ok": False, "error": "uid must be an integer"}
    if not (isinstance(point, (list, tuple)) and len(point) == 3 and all(_real(c) and math.isfinite(c) for c in point)):
        return {"ok": False, "error": "point must be [x, y, z]"}
    piece = scene.gaze_piece(uid, (float(point[0]), float(point[1]), float(point[2])))
    if piece is None:
        return {"ok": False, "error": "no such mesh object"}
    return {"ok": True, **piece}


def _gaze_show(request: dict, scene: SceneAccess) -> dict:
    dot, ring, marks = request.get("dot"), request.get("ring"), request.get("marks", [])
    if dot is not None and not (isinstance(dot, (list, tuple)) and len(dot) == 2 and all(_unit(c) for c in dot)):
        return {"ok": False, "error": "dot must be [u, v] or null"}
    if ring is not None and not (isinstance(ring, (list, tuple)) and len(ring) == 4 and all(_unit(c) for c in ring)):
        return {"ok": False, "error": "ring must be [u, v, ru, rv] or null"}
    if marks is None:
        marks = []
    if not isinstance(marks, list) or not all(isinstance(m, dict) for m in marks):
        return {"ok": False, "error": "marks must be a list of {n, u, v, name}"}
    if len(marks) > MAX_MARKS:
        return {"ok": False, "error": f"at most {MAX_MARKS} marks"}
    clean = []
    for mark in marks:
        n, name = mark.get("n"), mark.get("name", "")
        if not (isinstance(n, int) and not isinstance(n, bool) and _unit(mark.get("u")) and _unit(mark.get("v"))
                and isinstance(name, str)):
            return {"ok": False, "error": "marks must be a list of {n, u, v, name}"}
        clean.append({"n": n, "u": float(mark["u"]), "v": float(mark["v"]), "name": name[:MAX_MARK_NAME]})
    scene.gaze_show(
        None if dot is None else [float(c) for c in dot],
        None if ring is None else [float(c) for c in ring],
        clean,
    )
    return {"ok": True}


def _gaze_crop(request: dict, scene: SceneAccess) -> dict:
    u, v, radius, size = request.get("u"), request.get("v"), request.get("radius"), request.get("size")
    if not (_unit(u) and _unit(v)):
        return {"ok": False, "error": "u and v must be 0 to 1"}
    if not (isinstance(radius, (list, tuple)) and len(radius) == 2 and all(_real(c) and 0 < c <= 1 for c in radius)):
        return {"ok": False, "error": "radius must be [ru, rv], each above 0 and at most 1"}
    if not (isinstance(size, int) and not isinstance(size, bool) and size >= 1):
        return {"ok": False, "error": "size must be a whole number of pixels, 1 or more"}
    refused = scene.gaze_crop(float(u), float(v), [float(radius[0]), float(radius[1])], min(size, MAX_CROP_SIZE))
    if refused:
        return {"ok": False, "error": refused}
    return {"ok": True, "pending": True}


def _gaze_crop_result(scene: SceneAccess) -> dict:
    result = scene.gaze_crop_result()
    if "png" in result:
        return {"ok": True, "png": base64.b64encode(result["png"]).decode("ascii")}
    if "error" in result:
        return {"ok": False, "error": result["error"]}
    return {"ok": True, "pending": True}
