# Gaze spike: what the risky parts did on this Mac

Date: 2026-11-06. Machine: Apple silicon Mac, Blender 5.2.2 LTS (Metal), Claude Code 2.1.303. Everything throwaway ran from a scratch folder, with a scratch Blender config (`BLENDER_USER_RESOURCES`, `--factory-startup`). No product code changed, no camera or screen device opened.

## Gates

| Gate | Result |
|---|---|
| G1 Speed (20 samples/s, under one core) | Deferred to the hand session. Not measured. |
| G2 Accuracy (median error 15% of viewport width) | Deferred to the hand session. Not measured. |
| G3 Memory capture, no file | PASS |
| G4 Nothing left behind | PASS |

S1 and S2 need a person at the camera, so they were not run. The MediaPipe check below is camera-free and does not replace S1.

## MediaPipe, no camera (stands in for the install half of S1)

- Install: `uv venv --python 3.12`, then `uv pip install mediapipe==1.2.2` took 34 s. The venv is 338 MB. It pulls in 19 packages, including numpy 2.5.4, opencv-contrib-python 5.0.0.93 (the contrib build, 138 MB), matplotlib 3.11.2, sounddevice 0.5.6, absl-py and flatbuffers. Keep this in the separate worker, never in Blender's Python.
- Model `face_landmarker.task` (float16): 3,758,596 bytes, SHA-256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`.
- `mp.solutions` is gone. Working Tasks API (the native library loads through ctypes):
```python
import numpy as np, mediapipe as mp
from mediapipe.tasks import python as mp_tasks          # BaseOptions lives here
from mediapipe.tasks.python import vision               # FaceLandmarker, FaceLandmarkerOptions, RunningMode
opts = vision.FaceLandmarkerOptions(
    base_options=mp_tasks.BaseOptions(model_asset_path=MODEL),
    running_mode=vision.RunningMode.VIDEO, num_faces=1,
    output_face_blendshapes=True, output_facial_transformation_matrixes=True)   # note the spelling "matrixes"
lm = vision.FaceLandmarker.create_from_options(opts)
img = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_uint8_HxWx3)        # RGB, not BGR
r = lm.detect_for_video(img, timestamp_ms)               # ints, strictly increasing
# r.face_landmarks[0][i].x/.y/.z, r.face_blendshapes[0], r.facial_transformation_matrixes[0]; lm.close()
```
- Blank 960x540 frames, 200 timed calls: 1.14 ms mean, 1.12 median, 1.23 p95, 1.84 max. Model load 2.4 s, first call 2.7 ms. Peak memory 159 MB. The log says it uses the XNNPACK CPU delegate.
- Important: a blank frame finds no face, so only the cheap detector ran. Real per-frame time with a face (landmarks, blendshapes, matrix) will be higher. Do not read 1.1 ms as the real speed. That is what S1 measures.

## S3: picture from memory (G3 PASS)

Ran a real GUI Blender (default cube scene, largest 3D view 2096x1388 px), driven from `bpy.app.timers`, with a 60 s watchdog. It quit by itself after about 6 s.

Working code, one-off draw handler (names exact for 5.2):
```python
def grab():                                              # runs inside the handler
    c = bpy.context; region, rv3d = c.region, c.region_data
    w = min(960, region.width); h = round(region.height * w / region.width)   # keep the aspect ratio
    off = gpu.types.GPUOffScreen(w, h)
    try:
        off.draw_view3d(c.scene, c.view_layer, c.space_data, region,
                        rv3d.view_matrix, rv3d.window_matrix, do_color_management=True, draw_background=True)
        with off.bind():
            buf = gpu.state.active_framebuffer_get().read_color(0, 0, w, h, 4, 0, 'UBYTE')
        return bytes(buf), w, h                          # RGBA, bottom row first, 636x960x4 here
    finally:
        off.free()
hnd = bpy.types.SpaceView3D.draw_handler_add(cb, (), 'WINDOW', 'POST_PIXEL')
area.tag_redraw()                                        # from a timer, see below
bpy.types.SpaceView3D.draw_handler_remove(hnd, 'WINDOW') # remove from a timer afterwards
```
- Result: `framebuffer.read_color` worked first time. The fallback `off.texture_color.read()` also works (RGBA8, same pixels), so no fallback is needed. Time per 960x636 grab after warm-up: 11 to 25 ms (draw 3 to 9, read 8 to 18). The first grab costs 60 to 380 ms. The texture route costs the same, about 20 ms, once measured on its own.
- Not blank: values 0 to 255, mean 75, 1,356 distinct colours. I also looked at one frame dumped in a separate throwaway run: it shows the cube with its orange outline, grid, camera and light.
- Use `do_color_management=True`, or the background comes out near black (12 instead of 63). `bytes(buf)` and `np.asarray(buf)` take under 1 ms. Never use `buf.to_list()`, it takes 300 ms.
- The handler fires only when the area is redrawn. With no `tag_redraw` it fired 0 times in 15 s. With a timer calling `area.tag_redraw()` every 0.2 s it fired 8 times in about 2 s. A hidden or occluded window was not tested.
- No file: a before/after listing of Blender's temp folder and the scratch config folder showed no new or changed file during the capture. Across the whole run only one file appeared: Blender's own exit-time `quit.blend` in `$TMPDIR` (session recovery, not the image).

## S4: parts on modified meshes (information, not a gate)

The brief's idea does not work as written. `obj.original.closest_point_on_mesh(...)` still searches the evaluated mesh (its own doc string says "nearest point on evaluated geometry"), and `scene.ray_cast` also returns an evaluated face index. With a Subdivision modifier a 96-face mesh gives indexes like 210 and 317. So the brief's call returned the right original face on only 2 of 24 rays (the 2 are Mirror rays on the real half, where the numbers coincide).

Ground truth: an int face attribute `orig_face` stamped on the original mesh, which the modifiers copy onto the evaluated faces. Two routes were then checked against it, on a Subdivision cube, a Mirror half cube and a Mirror plus Subdivision half cube (24 rays: top, side and edge-near positions, e.g. top rays at (0.30,0.20), (-0.55,0.25), (0.15,-0.60), (-0.25,-0.35), (0.50,0.50), (-0.70,-0.15), (0.05,0.05), and +X, -X and +Y side rays):

| Case | Brief call | Cage BVH | Cage BVH + reflect | Group read off evaluated mesh |
|---|---|---|---|---|
| Subdivision, 10 rays | 0/10 | 10/10 | n/a | 10/10 |
| Mirror, 7 rays | 2/7 | 4/7 | 7/7 | 7/7 |
| Mirror + Subdivision, 7 rays | 0/7 | 5/7 | 7/7 | 7/7 |

```python
# Route A (recommended for "which group did the user look at"): no original face needed
ok, loc, nrm, eidx, ob, _ = scene.ray_cast(depsgraph, origin, direction)     # eidx is an EVALUATED face
ev = ob.evaluated_get(depsgraph)
tot = {}
for vi in ev.data.polygons[eidx].vertices:
    for g in ev.data.vertices[vi].groups:
        n = ev.vertex_groups[g.group].name; tot[n] = tot.get(n, 0) + g.weight
group = max(tot, key=tot.get) if tot else None                                # biggest summed weight wins
# Route B (needs the original face): cage BVH, build once per mesh edit (3 ms for 2,646 faces)
cage = BVHTree.FromPolygons([v.co.copy() for v in me.vertices], [tuple(p.vertices) for p in me.polygons])
local = ob.original.matrix_world.inverted() @ loc
_, _, orig_face, dist = cage.find_nearest(local)       # with Mirror on X, also try (-x, y, z) and keep the closer one
```
- Mirror matters: on the mirrored half, Route A returns `Wing.R` for a face painted `Wing.L` (Blender flips the name), which is what the user sees. Route B returns the original face and its `Wing.L`. Pick on purpose.
- Speed on 2,646 original / 42,336 evaluated faces: `ray_cast` 0.1 to 0.2 ms, Route A lookup 0.01 ms, `find_nearest` 0.01 ms. Both are cheap enough for every gaze sample.
- Group per face is the group with the largest summed vertex weight, since faces on a seam touch both groups.

## S5: leftover check (G4 PASS)

- Ran the exact command, with `NOVO_LLM_COST_USD=0.01`, from an empty scratch folder. Image: 256x256 random-noise PNG (196,992 bytes) made in memory with `os.urandom`, zlib and struct, sent as one stream-json user message (image block plus the text "What colors do you see? One short sentence."), stdin closed afterwards.
- Reply: "It's a dense, random static-like mix of many colors, mainly pink, green, blue, yellow, red, purple, and black on a noisy background." Model `claude-haiku-5-5`, 1 turn, 4 to 5 s, cost $0.0004 to $0.0009. The init event listed no tools and no MCP servers. The process exited by itself with code 0 about 8 s after stdin closed. `--max-turns` is missing from `claude --help` but was accepted.
- Search for four fingerprints (whole PNG, first 64 base64 chars, 64 base64 chars from the middle, 256 literal noise bytes, the last one also catches a lossless re-encode): zero hits. Searched files modified since the run began under `~/.claude` and `$TMPDIR` (`CLAUDE_CODE_TMPDIR` is unset): run 1 changed one file, a `~/.claude/backups/.claude.json.backup.*` config copy; run 2 changed none. Also searched every file in `~/.claude` regardless of date (6,566 files, 795 MB). A second run with a new image also searched `~/.claude.json`, `~/.local`, `~/Library/Application Support`, `Caches`, `Logs`, `Preferences`, all of `/var/folders/dt/...`, `/tmp` and the scratch folder (66 modified files, 62 MB): zero hits.
- No new `images/` or `image-cache` folder anywhere searched. A control (a different noise image written to a file) was found by all four fingerprints, then deleted. My copy of the real image never existed on disk, only in the script's memory and the pipe. I confirmed no PNG or image-like file remains in the scratch folder, apart from the Blender viewport picture.
- Not done: the optional run without `--no-session-persistence`. A scratch `CLAUDE_CONFIG_DIR` is logged out (`claude auth status` says `loggedIn: false`), so it would need a new login. Phase 3 must keep `--no-session-persistence`: what a saved session would store is untested.

## Plan changes

1. Task 8 keeps the in-memory capture, no temp-file fallback needed. It must tag a redraw from a timer, use `do_color_management=True`, allow a slow first grab, and remove the handler afterwards.
2. Hit-to-group lookup uses Route A (and Route B only where the original face is required), not `closest_point_on_mesh`. The worker venv is about 340 MB.
3. G1 and G2 are still open and need the hand session.
