"""GazeVoice Bridge: talk to Blender through the GazeVoice app.

- Hold ⌥ Space (rebindable in Preferences › Keymap) anywhere in Blender to talk.
  The add-on only queues "down"/"up" events; the app does the listening. While
  the app isn't polling (voice is off), presses are refused instead of queued.
- Shares your selection and the scene's object names with the app.
- Shows GazeVoice's status at the bottom-left of every 3D viewport and in the
  sidebar's "GazeVoice" tab.
- Lets the app add one undo step per voice command, and undo or redo by voice.
- Answers what is under a point in the largest 3D viewport and what part of the model
  that is, draws the app's gaze dot, ring and numbered marks there, and while the key is
  held reports the mouse as a stand-in for gaze. None of that edits the scene.

Listens on 127.0.0.1 only. A bpy timer polls a non-blocking socket, so all
scene access happens on Blender's main thread.
"""

from __future__ import annotations

import math
import socket
import time

import blf
import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from . import protocol

HOST = "127.0.0.1"
POLL_SECONDS = 0.1
MAX_HOLD_SECONDS = 60.0
FRESH_STATUS_SECONDS = 6.0  # a status set this recently shows even while voice is off (the Test key's)
DEFAULT_KEY = "⌥ Space"
VOICE_OFF = "Voice is off. Turn it on in the GazeVoice dashboard."
# Modal operators that edit data; undoing under one of them can crash Blender. Navigation and
# add-ons that keep a modal running all the time don't count.
EDITING_OPERATORS = ("transform", "mesh", "object", "sculpt", "paint", "curve", "curves", "armature", "pose", "uv",
                     "grease_pencil", "gpencil", "node", "anim", "graph", "action", "lattice", "font", "particle")
STATE_COLORS = {
    "idle": (0.75, 0.73, 0.68, 1.0),
    "listening": (1.0, 0.45, 0.35, 1.0),
    "working": (0.40, 0.72, 1.0, 1.0),
    "done": (0.35, 0.86, 0.55, 1.0),
    "error": (1.0, 0.40, 0.35, 1.0),
}
STATE_ICONS = {"idle": "INFO", "listening": "REC", "working": "TIME", "done": "CHECKMARK", "error": "ERROR"}
GAZE_COLOR = (0.40, 0.72, 1.0, 0.95)  # the dot and the ring
MARK_COLOR = (1.0, 0.80, 0.25, 1.0)  # a numbered mark's disc
MARK_TEXT_COLOR = (0.08, 0.08, 0.08, 1.0)
DOT_RADIUS = 7  # pixels
MARK_RADIUS = 12
RING_WIDTH = 2.5
CIRCLE_STEPS = 48

_server: socket.socket | None = None
_clients: dict[socket.socket, bytearray] = {}
_events = protocol.EventQueue()
_status = {"text": "", "state": "idle", "last": "", "at": 0.0}
_changed = {"since_mark": True}
_shown = {"status": False, "live": False}  # what the viewport and sidebar last showed
_gaze = {"dot": None, "ring": None, "marks": []}  # what the app asked to have drawn
_draw_handle = None
_addon_keymaps: list = []


def _queue(event_type: str, **fields) -> None:
    _events.add(event_type, **fields)


def _status_visible() -> bool:
    """Show the status while voice is on, or briefly after the app set it; never a stale line."""
    return bool(_status["text"]) and (_events.live() or time.time() - _status["at"] < FRESH_STATUS_SECONDS)


@bpy.app.handlers.persistent
def _on_depsgraph_update(*_args) -> None:
    _changed["since_mark"] = True


def _talk_keymap_item():
    """The binding in effect for hold-to-talk: the user's (if they rebound it), else ours."""
    window_manager = bpy.context.window_manager
    for keyconfig in (window_manager.keyconfigs.user, window_manager.keyconfigs.addon):
        keymap = keyconfig.keymaps.get("Window") if keyconfig else None
        for item in keymap.keymap_items if keymap else ():
            if item.idname == GAZEVOICE_OT_push_to_talk.bl_idname and item.active:
                return item
    return None


def _editing_operator(operator) -> bool:
    name = str(getattr(operator, "bl_idname", "")).lower()
    category = name.split("_ot_", 1)[0] if "_ot_" in name else name.split(".", 1)[0]
    return category in EDITING_OPERATORS


def _redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _ui_context() -> dict:
    """A window/area/region to run operators from inside a timer, which has no UI context."""
    windows = bpy.context.window_manager.windows
    for window in windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                found = {"window": window, "area": area, "region": region}
                return {key: value for key, value in found.items() if value is not None}
    return {"window": windows[0]} if windows else {}


def _run_op(op, **kwargs) -> bool:
    try:
        with bpy.context.temp_override(**_ui_context()):
            result = op(**kwargs)
    except (RuntimeError, TypeError) as exc:
        print(f"GazeVoice Bridge: {exc}")
        return False
    return "FINISHED" in result


def _target():
    """(window, area, main region) of the 3D viewport gaze looks at, or None.

    It is the largest VIEW_3D area in the first window, and the first of equals, so it is the same one every time.
    """
    windows = bpy.context.window_manager.windows
    best = None
    for area in windows[0].screen.areas if windows else ():
        region = next((r for r in area.regions if r.type == "WINDOW"), None) if area.type == "VIEW_3D" else None
        if region is None or region.width <= 0 or region.height <= 0:
            continue
        if best is None or area.width * area.height > best[1].width * best[1].height:
            best = (windows[0], area, region)
    return best


def _mirrored_points(obj, local: Vector) -> list[Vector]:
    """`local` (a point in obj's space) and where it lands reflected across the planes of obj's Mirror modifiers,
    in every combination: a mirrored copy of the mesh is where the evaluated surface is, not the original's."""
    points = [local]
    for modifier in obj.modifiers:
        if modifier.type != "MIRROR" or not modifier.show_viewport:
            continue
        # the planes pass through the origin of the mirror object, else of obj itself
        into = Matrix.Identity(4)
        if modifier.mirror_object is not None:
            into = modifier.mirror_object.matrix_world.inverted() @ obj.matrix_world
        back = into.inverted()
        for axis in range(3):
            if not modifier.use_axis[axis]:
                continue
            for point in list(points):
                flipped = into @ point
                flipped[axis] = -flipped[axis]
                points.append(back @ flipped)
    return points


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

    def set_status(self, text: str, state: str = "idle") -> None:
        _status["text"] = text
        _status["state"] = state
        _status["at"] = time.time()
        if state in ("done", "error"):
            _status["last"] = text
        _redraw()

    def object_names(self) -> list[str]:
        scene = bpy.context.scene
        return [obj.name for obj in scene.objects] if scene else []

    def undo_push(self, message: str) -> bool:
        return _run_op(bpy.ops.ed.undo_push, message=message)

    def undo(self) -> bool:
        return _run_op(bpy.ops.ed.undo)

    def redo(self) -> bool:
        return _run_op(bpy.ops.ed.redo)

    def key_label(self) -> str:
        item = _talk_keymap_item()
        if item is None:
            return DEFAULT_KEY
        on = (lambda value: value == 1)  # modifiers are 1 (held), 0 or -1 (either)
        return protocol.key_label(item.type, ctrl=on(item.ctrl), alt=on(item.alt), shift=on(item.shift),
                                  oskey=on(item.oskey))

    def busy(self) -> bool:
        """A data-editing modal operator (a G/R/S transform, a knife cut…) is running in some window."""
        for window in bpy.context.window_manager.windows:
            if any(_editing_operator(op) for op in getattr(window, "modal_operators", ())):
                return True
        return False

    def changed(self) -> bool:
        return _changed["since_mark"]

    def mark_unchanged(self) -> None:
        _changed["since_mark"] = False

    def gaze_view(self) -> tuple[int, int] | None:
        target = _target()
        return (target[2].width, target[2].height) if target else None

    def gaze_pick(self, points: list[tuple[float, float]]) -> list[dict | None] | None:
        target = _target()
        if target is None:
            return None
        _window, area, region = target
        region_3d = area.spaces.active.region_3d
        scene = bpy.context.scene
        depsgraph = bpy.context.evaluated_depsgraph_get()
        hits = []
        for u, v in points:
            coord = (u * region.width, (1.0 - v) * region.height)
            origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, coord)
            direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, coord)
            hit, location, _normal, _face, obj, _matrix = scene.ray_cast(depsgraph, origin, direction)
            if not hit or obj is None:
                hits.append(None)
                continue
            original = obj.original
            hits.append({
                "uid": original.session_uid,
                "name": original.name,
                "faces": len(original.data.polygons) if original.type == "MESH" else 0,
                "point": [round(c, 6) for c in location],
            })
        return hits

    def gaze_piece(self, uid: int, point: tuple[float, float, float]) -> dict | None:
        obj = next((o for o in bpy.data.objects if o.session_uid == uid), None)
        if obj is None or obj.type != "MESH":
            return None
        depsgraph = bpy.context.evaluated_depsgraph_get()
        world = Vector(point)
        mesh = obj.data
        material_count = sum(1 for slot in obj.material_slots if slot.material)

        # The face you see: nearest point on the evaluated mesh, so a mirrored wing reads Wing.R.
        evaluated = obj.evaluated_get(depsgraph)
        _found, _location, _normal, seen = evaluated.closest_point_on_mesh(
            evaluated.matrix_world.inverted() @ world, depsgraph=depsgraph)
        weights: dict[str, float] = {}
        material = None
        seen_mesh = evaluated.data
        if 0 <= seen < len(seen_mesh.polygons):
            polygon = seen_mesh.polygons[seen]
            for index in polygon.vertices:
                for element in seen_mesh.vertices[index].groups:
                    name = evaluated.vertex_groups[element.group].name
                    weights[name] = weights.get(name, 0.0) + element.weight
            if polygon.material_index < len(obj.material_slots):
                slot_material = obj.material_slots[polygon.material_index].material
                material = slot_material.name if slot_material else None
        part = protocol.face_part(weights, material, material_count)

        # The original face, which the modifiers renumber: nearest point on the cage.
        faces = [tuple(polygon.vertices) for polygon in mesh.polygons]
        face = None
        if faces:
            cage = BVHTree.FromPolygons([vertex.co.copy() for vertex in mesh.vertices], faces)
            nearest = None
            for candidate in _mirrored_points(obj, obj.matrix_world.inverted() @ world):
                found = cage.find_nearest(candidate)
                if found[2] is not None and (nearest is None or found[3] < nearest[3]):
                    nearest = found
            face = nearest[2] if nearest else None
        piece_faces = None
        if face is not None and len(faces) <= protocol.MAX_PIECE_FACES:
            piece_faces = protocol.linked_face_count(faces, face)
        return {"face": face, "part": part, "piece_faces": piece_faces, "object_faces": len(faces)}

    def gaze_show(self, dot: list | None, ring: list | None, marks: list[dict]) -> None:
        _gaze.update(dot=dot, ring=ring, marks=marks)
        _redraw()


# --- socket server ----------------------------------------------------------------

def _drop(conn: socket.socket) -> None:
    _clients.pop(conn, None)
    try:
        conn.close()
    except OSError:
        pass


def _poll() -> float | None:
    if _server is None:
        return None  # unregisters the timer
    now = {"status": _status_visible(), "live": _events.live()}
    if now != _shown:  # voice went on or off, or a status expired: repaint the viewport and sidebar
        _shown.update(now)
        _redraw()
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
                response = protocol.handle(request, scene, _events)
            except Exception as exc:  # noqa: BLE001 - never kill the timer
                response = {"ok": False, "error": str(exc)}
            try:
                conn.settimeout(1.0)  # a reply never blocks Blender's UI for long
                conn.sendall(protocol.encode(response))
                conn.setblocking(False)
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


# --- talking ------------------------------------------------------------------------

def _voice_ready(operator) -> bool:
    if _server is None:
        operator.report({"WARNING"}, "GazeVoice Bridge isn't listening. Restart Blender.")
        return False
    if not _events.live():
        operator.report({"INFO"}, f"GazeVoice: {VOICE_OFF}")
        return False
    return True


def _pointer_uv(context, event) -> tuple[float, float] | None:
    """Where the mouse is across the gaze viewport, 0 to 1 from the top left; None when it is anywhere else."""
    target = _target()
    if target is None or context.window != target[0]:
        return None
    region = target[2]
    u = (event.mouse_x - region.x) / region.width
    v = 1.0 - (event.mouse_y - region.y) / region.height
    return (u, v) if 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 else None


class GAZEVOICE_OT_push_to_talk(bpy.types.Operator):
    """Hold to talk to GazeVoice; release to send. Esc cancels"""

    bl_idname = "gazevoice.push_to_talk"
    bl_label = "GazeVoice: Hold to Talk"

    def invoke(self, context, event):
        if not _voice_ready(self):
            return {"CANCELLED"}
        self._key = event.type
        self._started = time.time()
        _queue("ptt", state="down")
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == self._key and event.value == "RELEASE":
            _queue("ptt", state="up")
            return {"FINISHED"}
        if (event.type == "ESC" and event.value == "PRESS") or event.type == "WINDOW_DEACTIVATE":
            _queue("ptt", state="cancel")  # Esc, or Blender lost focus (the release would never come)
            return {"CANCELLED"}
        if time.time() - self._started > MAX_HOLD_SECONDS:
            _queue("ptt", state="cancel")  # a key stuck down mustn't send a minute of room audio
            return {"CANCELLED"}
        if event.type == self._key:
            return {"RUNNING_MODAL"}  # swallow key repeats (Space would play the animation)
        if event.type == "MOUSEMOVE":
            position = _pointer_uv(context, event)
            if position is not None:
                _events.add_pointer(*position)  # the mouse stands in for gaze while the key is held
        return {"PASS_THROUGH"}


class GAZEVOICE_OT_talk_toggle(bpy.types.Operator):
    """Start talking to GazeVoice; press again to send"""

    bl_idname = "gazevoice.talk_toggle"
    bl_label = "Talk"

    def execute(self, context):
        if not _voice_ready(self):
            return {"CANCELLED"}
        _queue("toggle")
        return {"FINISHED"}


class GAZEVOICE_PT_panel(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GazeVoice"
    bl_label = "GazeVoice"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        if _server is None:
            col.label(text="Not listening: restart Blender", icon="ERROR")
            return
        if not _events.live():
            if _status_visible():
                col.label(text=_status["text"], icon=STATE_ICONS.get(_status["state"], "INFO"))
            col.label(text="Voice is off.", icon="INFO")
            col.label(text="Turn it on in the GazeVoice dashboard.")
            return
        col.label(text=_status["text"] or "Ready", icon=STATE_ICONS.get(_status["state"], "INFO"))
        if _status["last"] and _status["last"] != _status["text"]:
            col.label(text=f"Last: {_status['last']}")
        layout.separator()
        layout.operator(GAZEVOICE_OT_talk_toggle.bl_idname, text="Talk (click to start / send)", icon="REC")
        layout.label(text=f"Or hold {BpyScene().key_label()} anywhere")


def _draw_shape(kind: str, points: list[tuple[float, float]], color: tuple, width: float = 0.0) -> None:
    if width:
        shader = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
        shader.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        shader.uniform_float("lineWidth", width)
    else:
        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.uniform_float("color", color)
    batch_for_shader(shader, kind, {"pos": points}).draw(shader)


def _ellipse(x: float, y: float, rx: float, ry: float) -> list[tuple[float, float]]:
    angles = (2.0 * math.pi * i / CIRCLE_STEPS for i in range(CIRCLE_STEPS))
    return [(x + rx * math.cos(a), y + ry * math.sin(a)) for a in angles]


def _disc(x: float, y: float, radius: float) -> list[tuple[float, float]]:
    """A filled circle as a triangle fan."""
    rim = _ellipse(x, y, radius, radius)
    return [(x, y)] + rim + [rim[0]]


def _draw_gaze() -> None:
    """The dot, ring and numbered marks, in the gaze viewport only and only while the app is there to clear them."""
    if not (_gaze["dot"] or _gaze["ring"] or _gaze["marks"]) or not _events.live():
        return
    target, area = _target(), bpy.context.area
    if target is None or area is None or target[1].as_pointer() != area.as_pointer():
        return
    width, height = bpy.context.region.width, bpy.context.region.height

    def at(u: float, v: float) -> tuple[float, float]:
        return u * width, (1.0 - v) * height  # pixels from the region's bottom left

    gpu.state.blend_set("ALPHA")
    if _gaze["ring"]:
        u, v, ru, rv = _gaze["ring"]
        _draw_shape("LINE_LOOP", _ellipse(*at(u, v), ru * width, rv * height), GAZE_COLOR, RING_WIDTH)
    if _gaze["dot"]:
        x, y = at(*_gaze["dot"])
        _draw_shape("TRI_FAN", _disc(x, y, DOT_RADIUS), GAZE_COLOR)
    font = 0
    blf.size(font, 14)
    for mark in _gaze["marks"]:
        x, y = at(mark["u"], mark["v"])
        _draw_shape("TRI_FAN", _disc(x, y, MARK_RADIUS), MARK_COLOR)
        number = str(mark["n"])
        text_width, text_height = blf.dimensions(font, number)
        blf.color(font, *MARK_TEXT_COLOR)
        blf.position(font, x - text_width / 2, y - text_height / 2, 0)
        blf.draw(font, number)
        if mark["name"]:
            blf.enable(font, blf.SHADOW)
            blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
            blf.color(font, *MARK_COLOR)
            blf.position(font, x + MARK_RADIUS + 6, y - text_height / 2, 0)
            blf.draw(font, mark["name"])
            blf.disable(font, blf.SHADOW)
    gpu.state.blend_set("NONE")


def _draw_status() -> None:
    _draw_gaze()
    if not _status_visible():
        return
    text = _status["text"]
    font = 0
    blf.size(font, 14)
    blf.enable(font, blf.SHADOW)
    blf.shadow(font, 3, 0.0, 0.0, 0.0, 0.8)
    blf.color(font, *STATE_COLORS.get(_status["state"], STATE_COLORS["idle"]))
    blf.position(font, 20, 28, 0)
    blf.draw(font, f"GazeVoice · {text}")
    blf.disable(font, blf.SHADOW)


def _register_keymap() -> None:
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is None:
        return
    keymap = keyconfig.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
    item = keymap.keymap_items.new(GAZEVOICE_OT_push_to_talk.bl_idname, type="SPACE", value="PRESS", alt=True)
    _addon_keymaps.append((keymap, item))


def _unregister_keymap() -> None:
    for keymap, item in _addon_keymaps:
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    _addon_keymaps.clear()


_CLASSES = (GAZEVOICE_OT_push_to_talk, GAZEVOICE_OT_talk_toggle, GAZEVOICE_PT_panel)


def register() -> None:
    global _draw_handle
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    if bpy.app.background:
        return  # e.g. `blender --command extension install-file`: no UI, no listener
    _start_server()
    _register_keymap()
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_status, (), "WINDOW", "POST_PIXEL")


def unregister() -> None:
    global _draw_handle
    _stop_server()
    _gaze.update(dot=None, ring=None, marks=[])
    _unregister_keymap()
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
