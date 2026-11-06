# Gaze in Blender Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While building a model in Blender, you look at a part and say "add blue color there". GazeVoice works out through the webcam which part you were looking at and tells the editor in words, so the edit lands on that part without selecting it first.

**Architecture:**
- A **gaze worker** in its own Python environment reads the camera in memory, finds your face with MediaPipe, and streams an estimated viewport position.
- `VoiceService` keeps a short in-memory track of those positions. It uses the speech word timings to find where you looked when you said "this", then asks Blender what's in that patch of the viewport.
- The **Bridge add-on** casts rays and names what they hit: the object, its part, and how sure we are. It also gives the 3D point and the connected piece of mesh, so the editor knows where to paint on a single joined mesh.
- The result goes into the `Focus` line of the command as **text**. Text is the main format.
- A picture is the exception. When the target has no name, a crop goes from Blender's GPU memory to a short-lived process that describes it in words and exits. **No editing session ever sees an image.**

**Tech Stack:**
- Control center: Python 3.12 standard library.
- Worker: its own uv venv with `mediapipe` 1.2.2 (Apache 2.0) and the 3.76 MB `face_landmarker.task` model.
- Camera: ffmpeg avfoundation piped into the worker, the same tool the microphone uses.
- Blender 5.2: `Scene.ray_cast`, `bpy_extras.view3d_utils`, `gpu.types.GPUOffScreen`.
- Picture step: `claude -p` with stream-json and `--no-session-persistence`.

**Spec:** there's no separate spec. You asked for the plan directly on 2026-11-06. The design comes from this plan, `docs/gaze/Gaze_Recommended_Solution.md` and `docs/gaze/Gaze_Implementation_Research.md`. Where they differ, this plan wins, and each difference is listed under "Changes from the proposal".

## Global Constraints

- **Your image rule:** temporary image files are allowed, but every one gets cleaned up.
  - Capture in memory where that works. A temp file is allowed when a step needs one.
  - Temp images are named `gazevoice-gaze-*.png`, go in `paths.work / "gaze-images"`, and are deleted as soon as the call using them ends, even if it fails.
  - Leftovers from a crash are deleted when voice starts.
  - Images never go in logs, `record.json` or the Blender file.
  - Camera frames are never saved.
  - The editor may keep using Blender's MCP screenshot tools, which clean up their own temp files.
  - `codex exec` can't be used for images: it keeps its own copy in its session logs, which GazeVoice can't clean up.
- **What "deleted" can and can't mean:** Python can drop an image but can't wipe its memory, and a sent image is on the provider's servers under their terms. Anthropic keeps inputs for 30 days, or 5 years with the model-improvement setting on. The Routing note says this in plain words.
- **Consent:**
  - Nothing installs, downloads or opens the camera until you click.
  - The camera is open only while gaze is on. macOS's green light and Blender's status line both show it.
  - Pictures are off by default. Picture calls are labeled as using your plan.
- **Gaze never edits anything.** It only supplies focus. Edits, logs and one undo step per command stay as they are.
- **Gaze never gets in the way of voice.** If tracking fails or is off, voice works as it does today, using your selection.
- **Blender and Unity:** Blender only for now. Unity must keep working unchanged. Bridge commands are named `gaze_*` so Unity can follow later.
- **Models and keys:** same rules as before. No new vendor SDK, key or API host in the control center. The control center stays standard-library Python on 127.0.0.1. MediaPipe lives only in the worker's venv.
- **Numbers are starting points:** every threshold below is a first guess, to be reset from shadow-mode data (Task 7).
- **Git:** commit once per task, once you've said to start building. Never push unless asked.

## Review Focus

1. **Tracking drops out mid-sentence** (you blink, look down, or leave the frame). The command still runs on your selection, and the status line says gaze was lost. *(Task 3 `test_lost_tracking_falls_back_to_the_selection`)*
2. **The Blender window or viewport changes size after calibrating.** Gaze stops being trusted and the unit says to recalibrate, because every position is now wrong. *(Task 7 `test_a_resized_viewport_needs_recalibrating`)*
3. **Your phone is camera 0.** This Mac lists "Nokia Unbreakable V Camera" before the MacBook camera, so the app must pick by name and never open a phone or screen by accident. *(Task 5 `test_auto_prefers_the_built_in_camera`)*
4. **An image gets left behind.** A test scans the whole app folder for PNG data after full runs with pictures, including one that fails partway. *(Task 9 `test_no_picture_is_left_behind`)*
5. **"Stop" while it's waiting for you to pick a number.** Stop ends the wait at once. *(Task 4 `test_stop_ends_the_wait_for_a_number`)*

## Changes from the proposal

- **Shared clock:** the proposal asks for a new one. Not needed, because the key press, the microphone and the camera are all on this Mac and all use `time.time()`.
- **Calibration:** done inside Blender's viewport instead of a full-screen window, so positions are viewport-relative from the start. That avoids converting between screen, window and Retina pixels.
- **Gaze model:** this plan picks one (MediaPipe landmarks plus your own calibration).
- **Filtering:** One Euro and I-DT are left out at first. A median over the window around the word is simpler, and it's what the data is used for.
- **Hit testing:** the proposal casts one ray. This plan casts 24 across the uncertainty patch.
- **Parts:** the proposal stops at objects. This plan also reports the part, the 3D point and the connected piece of mesh, because recoloring part of a model is the main use.
- **Pictures:** the proposal doesn't send any to the model. This plan adds them for targets with no name.

## What to expect from accuracy

Plan on about **2–4 cm of error**. The best large real-world result is 2.39 cm on laptops right after a 9-point calibration, drifting to 3.2–3.8 cm (Chiasm preprint, 2026). On your 30 cm wide screen, that's about a tenth of the width. So gaze can tell a chair from a table when both are reasonably large on screen. It can't reliably pick a single leg or wing tip, which is why results are ranked guesses, and why the plan has numbered labels and pictures. Zooming in helps, because gaze error is measured on the screen: a part that fills more of the screen is easier to hit. No published figure exists for this camera, so it gets measured on the real thing.

## File map

| File | Job |
|---|---|
| `gazevoice/voice/gaze.py` (new) | Pure logic: gaze track, pointing words, patches, scoring, deciding |
| `gazevoice/gaze_model.py` (new) | Pure maths: features, ridge fit, prediction. Standard library only, so it's testable anywhere |
| `gazevoice/gaze_worker.py` (new) | Runs in the gaze venv: camera, MediaPipe, sample stream |
| `gazevoice/voice/gaze_tracker.py` (new) | Runs the worker from the app, like `SpeechService` |
| `gazevoice/voice/picture.py` (new) | The short-lived describer, image cleanup and the leftover check |
| `gazevoice/voice/prompts/picture.md` (new) | The describer's instructions |
| `gazevoice/components/gaze.py` (new) | The Gaze unit: install, calibrate, test |
| `blender_addon/gazevoice_bridge/protocol.py`, `__init__.py` | New `gaze_*` commands, drawing, in-memory capture. Version 0.3.0 |
| `gazevoice/voice/service.py`, `commands.py`, `wiring.py`, `prompts/editor.md`, `prompts/refiner.md` | Gaze enters the voice loop |
| `gazevoice/settings.py`, `server/app.py`, `server/static/app.js` | Settings, API, dashboard |

---

## Phase 0: Spike (by hand; you approve it before anything is built)

### Task 0: Prove the three risky parts on this Mac

**Status:** S3, S4 and S5 are done, and G3 and G4 passed. See `docs/superpowers/specs/2026-11-06-gaze-spike.md`. S1 and S2 need a person at the camera, so they run in the hand session at the end, using the product's own Calibrate key.

**Who:** I run the scripts and you look at the dots. Throwaway code goes in the scratch folder and nothing goes in the repo.

**You approve first:**
- installing `mediapipe` into a scratch venv and downloading the model;
- opening the MacBook camera for about 5 minutes;
- about three short Claude Code calls on your plan.

**Output:** `docs/superpowers/specs/<date>-gaze-spike.md` with the results and any plan changes.

- [ ] **S1 Camera and speed.** Stream the MacBook camera through ffmpeg into MediaPipe for 60 s, with Blender open. Record the frames per second, the worker's CPU and memory, and which frame size works. Check whether Center Stage shifts the frames while you move, because that would break calibration.
- [ ] **S2 Accuracy.** Show 9 calibration dots and 5 test dots in a Blender viewport. Fit the model in `gazevoice/gaze_model.py`'s planned form. Record the median and 90th-percentile error as a share of viewport width, then again after you shift in your seat and after 10 minutes.
- [ ] **S3 Picture from memory.** In a running Blender 5.2, capture a viewport into a Python buffer with `GPUOffScreen.draw_view3d` inside a one-off draw handler.
- [ ] **S4 Parts on modified meshes.** On a mesh with a Subdivision modifier and vertex groups, check that `obj.closest_point_on_mesh` on the original object finds the right original face from a ray's hit point, so "Wing.L" comes back correctly.
- [ ] **S5 Leftover check.** Send a noise image through `claude -p` with `--no-session-persistence`, then search `~/.claude` and the temp folders for it. This confirms what was only read from the binary.

**Gates:**
- **G1 Speed:** at least 20 samples a second, using less than one CPU core.
- **G2 Accuracy:** median error at or under 15% of viewport width. Below that, gaze can still separate large objects, and it's worth continuing.
- **G3 Memory capture:** S3 works without a file.
- **G4 Nothing left behind:** S5 finds nothing after the process exits.

**If a gate fails:**
- G1 or G2: repeat S1 and S2 with GazeFollower 1.1.0. It's non-commercial, which is fine for personal use.
- G3: Task 8 captures to a temp file with `bpy.ops.screen.screenshot_area` instead, deleted right after use.
- G4: drop Phase 3. Gaze stays text-only, with nothing else lost.

---

## Phase 1: Everything except the camera

The mouse stands in for your eyes, so the whole path can be tried for real before any camera work.

### Task 1: The gaze logic

**Files:**
- Create: `gazevoice/voice/gaze.py`
- Test: `tests/test_gaze.py`

**Interfaces:**
- Produces:
  - Constants: `POINTING_WORDS = ("this", "that", "these", "those", "it", "them", "here", "there")`, `BEFORE_WORD = 0.6`, `AFTER_WORD = 0.2`, `MIN_SAMPLES = 5`, `MIN_CONFIDENCE = 0.5`, `RAYS = 24`, `CONFIDENT_SCORE = 0.6`, `CONFIDENT_GAP = 0.25`, `DEFAULT_RADIUS = (0.10, 0.15)`, `MAX_CANDIDATES = 3`.
  - `GazeSample(t: float, u: float, v: float, confidence: float)`, frozen. `u, v` are 0–1 in the viewport, from the top left.
  - `GazeTrack(seconds: float = 30.0)` with `.add(sample)`, `.between(t0, t1) -> list[GazeSample]`, `.latest_time`. Thread-safe, memory only.
  - `PointingWord(text: str, start: float, end: float)`, in absolute `time.time()` seconds.
  - `pointing_words(words: list[dict], clip_start: float) -> list[PointingWord]`
  - `Fixation(u: float, v: float, samples: int)`
  - `fixate(samples: list[GazeSample]) -> Fixation | None`: the median of samples with enough confidence, or None if too few.
  - `ray_points(fixation: Fixation, radius: tuple[float, float], rays: int = RAYS) -> list[list[float]]`: a repeatable spread across the patch, clamped to 0–1.
  - `Candidate(uid: int, name: str, score: float, faces: int, u: float, v: float, point: list[float])`
  - `score_hits(points: list[list[float]], hits: list[dict | None]) -> list[Candidate]`: each ray is one vote and a miss votes for nothing. Best first, at most `MAX_CANDIDATES`. `u, v` is the average of the points that hit that object, for placing its label. `point` is the 3D hit nearest the centre of the patch, which is the first ray that hit it.
  - `Target(word: str, at: float, candidates: list[Candidate], fixation: Fixation, piece: dict | None = None, picture: str | None = None)`. `piece` is the `gaze_piece` reply for the top candidate.
  - `Grounding(status: str, targets: list[Target], radius: tuple[float, float])`, where `status` is `"resolved"`, `"clarify"` or `"none"`, with `.to_dict() -> dict`.
  - `ground(words, clip_start, track, radius, pick, piece) -> Grounding`, where `pick(points) -> list[dict | None] | None` and `piece(candidate: Candidate) -> dict | None` ask the host.

- [ ] **Step 1: Write the failing tests:**
  - `test_pointing_words_become_clock_times`: `{"text": "this", "start": 0.4, "end": 0.6}` with `clip_start=100.0` gives `start=100.4, end=100.6`.
  - `test_punctuation_and_case_dont_hide_a_pointing_word`: `"This,"` counts.
  - `test_the_track_forgets_old_samples`
  - `test_fixate_ignores_blinks`: low-confidence samples don't count.
  - `test_fixate_needs_enough_samples`: 4 samples give None.
  - `test_a_median_shrugs_off_one_wild_sample`
  - `test_ray_points_stay_on_the_viewport`: a fixation at `(0.01, 0.99)` stays within 0–1.
  - `test_score_counts_votes_and_misses`: 12 of 24 rays on Chair, 6 on Table and 6 misses give `0.5` and `0.25`.
  - `test_the_point_comes_from_the_ray_nearest_the_centre`
  - `test_the_top_guess_gets_its_piece`: `piece` is called once per target, for the top candidate only.
  - `test_a_failed_piece_lookup_still_gives_the_object`
  - `test_one_clear_winner_is_resolved`: scores 0.82 and 0.14.
  - `test_two_close_guesses_need_clarifying`: scores 0.45 and 0.40.
  - `test_each_pointing_word_gets_its_own_target`: "move this over there" with two fixations gives two targets.
  - `test_no_pointing_word_means_no_gaze`: status `"none"`.
  - `test_nothing_hit_means_no_gaze`
  - `test_a_failed_pick_means_no_gaze`: `pick` returns None.
- [ ] **Step 2: Run them and see them fail.** Run `python3 -m unittest tests.test_gaze -v`. Expected: ImportError.
- [ ] **Step 3: Implement.** The window for each word is `[start - BEFORE_WORD, end + AFTER_WORD]`. `resolved` means every target has a top score of at least `CONFIDENT_SCORE` and leads the next guess by at least `CONFIDENT_GAP`.
- [ ] **Step 4: Run them.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Work out what a pointing word refers to from gaze samples".

### Task 2: Bridge add-on 0.3.0: rays, drawing, the mouse stand-in

**Files:**
- Modify: `blender_addon/gazevoice_bridge/protocol.py`, `__init__.py`, `blender_manifest.toml`; `gazevoice/blender_socket.py`
- Test: `tests/test_bridge_protocol.py`, including the version literals at lines 88 and 106

**Interfaces:**
- Produces, in the protocol:
  ```
  {"cmd": "gaze_view"}  -> {"ok": true, "width": 1840, "height": 1100} or {"ok": false, "error": "no 3D viewport"}
  {"cmd": "gaze_pick", "points": [[u, v], …]}
                        -> {"ok": true, "hits": [{"uid": 116, "name": "Eagle", "faces": 5120, "point": [x, y, z]} or null, …]}
  {"cmd": "gaze_piece", "uid": 116, "point": [x, y, z]}
                        -> {"ok": true, "face": 3120, "part": "Wing.L" or null, "piece_faces": 480 or null, "object_faces": 5120}
  {"cmd": "gaze_show", "dot": [u, v] or null, "ring": [u, v, ru, rv] or null, "marks": [{"n": 1, "u": 0.4, "v": 0.3, "name": "Chair"}]}
                        -> {"ok": true}
  ```
  - Pointer events: `{"seq": n, "type": "pointer", "u": 0.4, "v": 0.3, "t": …}`, at most 20 a second, only during a hold.
  - `SceneAccess` gains `gaze_view()`, `gaze_pick(points)`, `gaze_piece(uid, point)` and `gaze_show(dot, ring, marks)`.
  - `linked_face_count(faces: list[list[int]], start: int) -> int`, pure: how many faces connect to `start` through shared vertices.
  - `MAX_PIECE_FACES = 200000`: above this, `piece_faces` is null, so a huge mesh can't freeze Blender.
  - `MAX_POINTS = 64`. `BRIDGE_VERSION = "0.3.0"` in all three places, which makes the dashboard offer **Update add-on**.

- [ ] **Step 1: Write the failing tests** with `FakeScene`:
  - `test_gaze_pick_returns_one_hit_per_point`
  - `test_gaze_pick_refuses_too_many_points`
  - `test_gaze_pick_refuses_points_off_the_viewport`
  - `test_gaze_piece_reports_the_face_part_and_piece`
  - `test_gaze_piece_needs_a_uid_and_a_point`
  - `test_linked_faces_stop_at_a_gap`: two separate islands in one mesh count separately.
  - `test_gaze_view_says_when_there_is_no_viewport`
  - `test_gaze_show_passes_the_drawing_on`
  - `test_pointer_events_queue_like_the_others`
- [ ] **Step 2: Run them and see them fail.** Run `python3 -m unittest tests.test_bridge_protocol -v`.
- [ ] **Step 3: Implement the protocol, then the bpy side:**
  - The target is the largest `VIEW_3D` area in the first window, chosen the same way every time.
  - A pick converts `u, v` to `x = u * region.width`, `y = (1 - v) * region.height`, uses `view3d_utils.region_2d_to_origin_3d` and `region_2d_to_vector_3d`, then `scene.ray_cast(depsgraph, origin, direction)`.
  - `uid` is `obj.original.session_uid`, and `point` is the world-space hit. Names are for people, IDs are for matching.
  - `gaze_piece` finds the object by `session_uid` and needs two faces, because modifiers renumber them. `docs/superpowers/specs/2026-11-06-gaze-spike.md` S4 has working code for both:
    - **The face you see**, for `part`: `closest_point_on_mesh` on the evaluated object, then read groups off the evaluated mesh. A mirrored wing correctly reads `Wing.R`.
    - **The original face**, for `face` and `piece_faces`: a `mathutils.bvhtree.BVHTree.FromPolygons` of the original mesh, `find_nearest` on the local point, and for each axis a Mirror modifier uses, also try the reflected point and keep the closer. The editor's code changes the original mesh, so this is the index it needs. Don't use `closest_point_on_mesh` for this: it searches evaluated geometry and was right on only 2 of 24 rays.
  - `part` is the vertex group with the largest summed weight on the evaluated face, else the material name if the object has two or more materials, else null.
  - Draw the dot, ring and numbered marks in the existing `_draw_status` handler, in the target area only.
  - In `GAZEVOICE_OT_push_to_talk.modal`, turn `MOUSEMOVE` into pointer events. Return `PASS_THROUGH` as now.
- [ ] **Step 4: Run the full suite.** Expected: OK. Then, in real Blender after **Update add-on**, run `gaze_pick` at a known point over the default cube and get `Cube` back.
- [ ] **Step 5: Commit** with the message "Bridge 0.3.0: name what's under a point in the viewport".

### Task 3: Gaze in the voice loop

**Files:**
- Modify: `gazevoice/voice/service.py`, `commands.py`, `wiring.py`, `prompts/editor.md`, `prompts/refiner.md`; `gazevoice/settings.py`; `gazevoice/server/app.py`; `gazevoice/server/static/app.js`
- Test: `tests/test_voice_service.py`, `tests/test_voice_commands.py`, `tests/test_settings.py`, `tests/test_voice_wiring.py`

**Interfaces:**
- Consumes: Task 1 and Task 2.
- Produces:
  - Settings: `gaze_mode = "off"`, one of `"off"`, `"shadow"`, `"on"`; `gaze_source = "camera"`, one of `"camera"`, `"mouse"`.
  - `Factories.gaze: Callable[[], Any] | None = None`. A gaze part has `.start()`, `.stop()`, `.alive`, `.track` and `.radius`. The default is None, so existing tests and Unity don't change.
  - `PointerTracker`: a gaze part fed by pointer events from the poll loop, with `radius = (0.02, 0.03)` because a mouse is nearly exact.
  - `VoiceService._ground(record, run) -> None`, called at the top of `_edit` for spoken commands, before refining.
  - `record["gaze"]`: `Grounding.to_dict()` plus `"matched_selection": bool | None`.
  - `describe_focus(focus)` gains a gaze form when `focus["gaze"]` is present.
  - The snapshot gains `"gaze": {"mode", "source", "live", "checked", "matched"}`.

Exact text for a resolved target. Leave out `part` when it's null, and the connected piece when `piece_faces` is null:

```text
Focus (gaze): "there" at 1.2 s → Eagle (82%), Perch (14%)
  part: Wing.L · point (0.41, 0.12, 0.73) on face 3120 · connected piece of 480 of 5120 faces
  gaze is accurate to about 10% of the viewport · selected: nothing
```

- [ ] **Step 1: Write the failing tests:**
  - `test_shadow_mode_records_but_doesnt_change_the_focus`
  - `test_on_mode_puts_the_gaze_target_in_the_editors_message`
  - `test_off_mode_never_starts_gaze`
  - `test_the_refiner_sees_the_gaze_target_too`
  - `test_lost_tracking_falls_back_to_the_selection`: no samples, the command runs on the selection, and the status says gaze was lost.
  - `test_gaze_failing_to_start_doesnt_stop_voice`
  - `test_stop_and_undo_never_touch_gaze`
  - `test_typed_commands_dont_use_gaze`
  - `test_agreement_with_the_selection_is_counted`: top candidate equal to the selected object gives `matched_selection = True`.
  - `test_describe_focus_keeps_todays_text_without_gaze`
  - `test_describe_focus_lists_each_word_with_its_guesses`
  - `test_describe_focus_gives_the_point_face_and_piece`
  - `test_the_editor_prompt_covers_gaze_parts_and_own_materials`: `prompts/editor.md` contains "own material", "connected piece" and "vertex group".
  - `test_the_mouse_stands_in_for_gaze`: pointer events during a hold reach the track.
  - `test_settings_check_the_gaze_values`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement:**
  - Compute `clip_start` as `record["_t0"] - PREROLL`, matching `_transcribe`'s `mic.buffer.capture(t0 - PREROLL, …)`.
  - Add `gaze_mode` and `gaze_source` to `_voice_config_now`, so a change prompts a voice restart.
  - In `prompts/editor.md`, add: "Focus can come from gaze. Gaze is a rough estimate: it lists its best guesses with percentages. Prefer the first guess. If the command names something, the name wins. A picture line describes what's inside the gaze ring. To recolor part of a model, give that part its own material, so nothing that shares the old one changes. If the part is its own object, work on that object. If it's part of a larger mesh, use its vertex group, or else the connected piece containing the given face, and assign only those faces to a new material slot. Use the point when something should go 'there'."
  - In `prompts/refiner.md`, add the `gaze` field to the JSON description and keep pointing words as said.
  - In Routing, add rows **Gaze** (Off, Watch only, On) and **Gaze source** (Camera, Mouse).
- [ ] **Step 4: Run the full suite.** Then try it for real with Gaze source Mouse and mode On: hover over a wing, hold the key, and say "add blue color there". Only that part turns blue. This uses a little of your plan.
- [ ] **Step 5: Commit** with the message "Tell the editor what you were looking at".

### Task 4: Numbered labels when it isn't sure

**Files:**
- Modify: `gazevoice/voice/service.py`, `gazevoice/voice/commands.py`
- Test: `tests/test_voice_service.py`, `tests/test_voice_commands.py`

**Interfaces:**
- Produces:
  - `choice_number(text: str) -> int | None`: "two", "number two" and "2" give 2.
  - `CHOICE_SECONDS = 8.0`.
  - `VoiceService._ask_which(record, grounding, run) -> Candidate | None`.

- [ ] **Step 1: Write the failing tests:**
  - `test_close_guesses_show_numbered_labels_and_wait`: `gaze_show` gets marks and the status reads "Which one? Say the number."
  - `test_saying_a_number_picks_that_one`
  - `test_a_number_with_nothing_waiting_is_an_ordinary_command`
  - `test_no_answer_falls_back_to_the_selection`
  - `test_no_answer_and_no_selection_says_so`: fails with "Couldn't tell what you meant. Select it or say its name."
  - `test_stop_ends_the_wait_for_a_number`
  - `test_the_labels_are_cleared_afterward`
  - `test_shadow_mode_never_asks`
  - `test_choice_number_reads_words_and_digits`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** The edit thread waits on an event. `_route` hands a number to the waiting command before anything else, but stop still goes first through `meta_command`.
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Ask with numbered labels when gaze can't tell two things apart".

---

## Phase 2: The camera

### Task 5: The gaze worker

**Files:**
- Create: `gazevoice/gaze_model.py`, `gazevoice/gaze_worker.py`, `gazevoice/voice/gaze_tracker.py`
- Modify: `gazevoice/components/microphone.py` (adds video device parsing)
- Test: `tests/test_gaze_model.py`, `tests/test_gaze_tracker.py`

**Interfaces:**
- Produces:
  - `gaze_model.py` (standard library):
    - `features(landmarks: list[tuple[float, float, float]], transform: list[list[float]]) -> list[float]`
    - `fit(rows: list[list[float]], targets: list[tuple[float, float]], alpha: float = 1.0) -> dict`
    - `predict(model: dict, row: list[float]) -> tuple[float, float]`
    - `check(model, rows, targets) -> dict`, giving `{"median": …, "p90": …, "radius": [ru, rv]}`
  - Features: iris position within each eye, scaled by eye width; eye opening; head rotation; head position and distance; plus their pairwise products. EyeTrax (MIT) has a good starting point but ignores head position. Credit it if code is adapted. S2 may change this list.
  - Worker: `python gaze_worker.py --serve --camera N [--model-file P] [--fake]`, in stdin and stdout JSON lines:
    ```
    → {"ready": true, "load_seconds": 1.2}
    → {"t": 1794000000.1, "uv": [0.4, 0.3], "confidence": 0.9}          after a calibration is loaded
    ← {"cmd": "collect", "uv": [0.5, 0.5]}  /  {"cmd": "stop_collect"}
    ← {"cmd": "fit"}  → {"fit": {model}, "check": {…}}
    ← {"cmd": "load", "model": {…}}
    ```
  - `CAMERA_SIZE = (960, 540)` and `camera_argv(index: str, width: int, height: int, fps: int = 30) -> list[str]`, with `-framerate 30 -i "<index>:none" -vf scale=960:540 -f rawvideo -pix_fmt rgb24 pipe:1`. Fixing the size in ffmpeg means it works whatever modes the camera offers.
  - `parse_video_devices(listing: str) -> list[tuple[str, str]]` and `pick_camera(devices, setting: str) -> tuple[str, str] | None`.
  - `GazeTracker(python, worker, camera, calibration: dict | None, env=None)`, a gaze part like Task 3 describes, plus `.collect(uv)`, `.stop_collect()`, `.fit() -> dict`. It's built like `SpeechService`.

- [ ] **Step 1: Write the failing tests:**
  - `test_fit_learns_a_known_mapping`: with synthetic rows, prediction error is under 0.01.
  - `test_more_alpha_steadies_a_noisy_fit`
  - `test_check_reports_median_p90_and_radius`
  - `test_features_are_the_same_length_every_time`
  - `test_auto_prefers_the_built_in_camera`: the real device list from this Mac (Nokia at 0, MacBook Air at 1, Desk Views, Capture screen) gives index `"1"`.
  - `test_desk_view_and_screens_are_never_picked`
  - `test_a_chosen_camera_thats_gone_returns_none`
  - `test_the_tracker_fills_its_track_from_the_worker`: uses the worker's `--fake` mode.
  - `test_a_dead_worker_reads_as_not_alive`
  - `test_stop_ends_ffmpeg_too`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** The worker reads one frame at a time from ffmpeg and keeps none, and it stamps each with `time.time()`. Confidence is 0 for no face or a blink, read from MediaPipe's `eyeBlink` blendshapes. `mediapipe` is imported only when serving for real, so tests run without it. `mp.solutions` no longer exists: the spike doc has the working Tasks API code, including the spelling `output_facial_transformation_matrixes`.
- [ ] **Step 4: Run the tests.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the gaze worker: camera, face landmarks, calibration maths".

### Task 6: The Gaze unit

**Files:**
- Create: `gazevoice/components/gaze.py`
- Modify: `gazevoice/components/__init__.py`, `gazevoice/settings.py`, `gazevoice/voice/wiring.py`, `gazevoice/server/static/app.js`, `gazevoice/paths.py`
- Test: `tests/test_gaze_unit.py`, `tests/test_voice_wiring.py`

**Interfaces:**
- Produces:
  - `Gaze(Component)`: `id = "gaze"`, `name = "Gaze"`, `role = "Sees where you're looking, on this Mac"`. It joins the Input shelf. It isn't in `voice_readiness` or the strip, because gaze is optional.
  - Settings: `camera_device = ""`, where `""` means auto.
  - `paths.gaze_model_file` and `paths.gaze_calibration`, in `state/`. The calibration file holds numbers only.
  - Model: `https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task`, 3,758,596 bytes, SHA-256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`.
  - Action: `install`, with a confirm saying it downloads about 340 MB. Pin `mediapipe==1.2.2`.

- [ ] **Step 1: Write the failing tests:**
  - `test_not_installed_offers_install`
  - `test_installed_but_uncalibrated_says_to_calibrate`
  - `test_ready_shows_the_measured_accuracy`
  - `test_no_camera_says_so`
  - `test_install_uses_its_own_venv_and_checks_the_download`
  - `test_a_download_with_the_wrong_checksum_is_refused`
  - `test_build_voice_only_adds_gaze_when_its_on_and_calibrated`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement,** following `components/speech.py`, and add a **Camera** menu in Routing beside the microphone's.
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the Gaze unit to the rack".

### Task 7: Calibration and watch-only mode

**Files:**
- Modify: `gazevoice/components/gaze.py`, `gazevoice/voice/service.py`, `gazevoice/server/static/app.js`
- Test: `tests/test_gaze_unit.py`

**Interfaces:**
- Consumes: `gaze_show` and `gaze_view` (Task 2), `GazeTracker.collect` and `.fit` (Task 5).
- Produces:
  - Actions: `calibrate` (confirm: "Opens the camera for about 40 seconds. Look at each dot in Blender.") and `test` (shows a live ring for 10 s).
  - The calibration file holds `{"model", "check", "view": {"width", "height"}, "when"}`.
  - `DOT_SECONDS = 2.0`, `SETTLE_SECONDS = 0.5`, 9 calibration dots, then 5 check dots at new positions.
  - The check's `radius` becomes the patch size used for rays and the picture ring.

- [ ] **Step 1: Write the failing tests:**
  - `test_calibrate_shows_each_dot_and_collects_after_it_settles`
  - `test_calibration_needs_blender_open`
  - `test_a_poor_result_says_so_and_keeps_the_old_one`: a worse check than 25% of the viewport isn't saved.
  - `test_a_resized_viewport_needs_recalibrating`: `gaze_view` differs from the saved size, so the unit says so and voice runs without gaze.
  - `test_the_dots_are_cleared_even_if_it_fails`
  - `test_the_test_ring_follows_the_track`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Drive the dots from the job thread. Voice and calibration exclude each other because both use the camera.
- [ ] **Step 4: Run the full suite,** then calibrate for real.
- [ ] **Step 5: Commit** with the message "Calibrate gaze inside Blender's viewport".
- [ ] **Step 6: Gate, by hand.** Use Watch only for about 30 commands, selecting the thing you mean and saying "this". Because your selection is the right answer, the dashboard can show how often gaze matched it without any labeling. **Agree the pass mark before you start.** I suggest 80%. Then reset the thresholds in Task 1 from this data, and switch to On.

---

## Phase 3: Pictures, for things with no name

If G3 or G4 failed, skip this phase.

### Task 8: A crop from Blender's memory

**Files:**
- Modify: `blender_addon/gazevoice_bridge/protocol.py`, `__init__.py`
- Test: `tests/test_bridge_protocol.py`

**Interfaces:**
- Produces, in the protocol:
  ```
  {"cmd": "gaze_crop", "u": 0.4, "v": 0.3, "radius": [0.1, 0.15], "size": 512} -> {"ok": true, "pending": true}
  {"cmd": "gaze_crop_result"} -> {"ok": true, "pending": true}, {"ok": true, "png": "<base64>"} or {"ok": false, "error": "…"}
  ```
  - `crop_box(u, v, radius, width, height, margin=2.5) -> tuple[int, int, int, int]`, pure.
  - `draw_ring(pixels: bytearray, width, height, cx, cy, rx, ry) -> None`, pure.
  - `encode_png(width: int, height: int, rgb: bytes) -> bytes`, pure, with `zlib` only.
  - The result is cleared from the add-on the moment it's read.

- [ ] **Step 1: Write the failing tests:**
  - `test_encode_png_makes_a_readable_file`: a stdlib decoder round-trips it.
  - `test_crop_box_stays_inside_the_image`
  - `test_draw_ring_marks_only_the_outline`: the centre pixel is unchanged.
  - `test_a_crop_result_can_only_be_read_once`
  - `test_protocol_never_touches_the_file_system`: `open` is patched to fail during a crop.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Use a one-shot `POST_PIXEL` draw handler, `GPUOffScreen.draw_view3d` and `read_color`. The spike doc's S3 section has code that works on Blender 5.2. Follow it: the handler fires only if a timer calls `area.tag_redraw()`; pass `do_color_management=True` or the picture comes out too dark; pixels are RGBA with the bottom row first; never use `buf.to_list()`; allow up to 380 ms for the first grab; remove the handler afterward. Scale the crop so its long side is `size` with Blender's bundled numpy.
- [ ] **Step 4: Run the full suite,** then capture for real.
- [ ] **Step 5: Commit** with the message "Bridge: capture a viewport crop for gaze".

### Task 9: The short-lived describer

**Files:**
- Create: `gazevoice/voice/picture.py`, `gazevoice/voice/prompts/picture.md`
- Modify: `gazevoice/voice/service.py`, `gazevoice/voice/brains.py`, `gazevoice/components/gaze.py`, `gazevoice/settings.py`, `gazevoice/server/static/app.js`, `tests/fixtures/fake_claude.py`
- Test: `tests/test_picture.py`, `tests/test_voice_service.py`

**Interfaces:**
- Produces:
  - Settings: `gaze_pictures = False`.
  - Constants: `PICTURE_MIN_FACES = 500`, `PICTURE_SIZE = 512`, `PICTURE_SECONDS = 20.0`, `PICTURE_MODEL = "claude-haiku-5-5"`.
  - `needs_picture(target: Target) -> bool`: the piece has no `part`, and its connected piece has at least `PICTURE_MIN_FACES` faces. A smaller piece is already a part you can name by its face.
  - `ClaudeSession.send` accepts a list of content blocks as well as a string.
  - `describe_crop(png_base64: str, word: str, object_name: str, paths: AppPaths) -> str | None`: starts one `claude -p` process, sends one message, gets one line of text, and closes it. Any failure returns None.
  - `clear_leftover_images(paths: AppPaths) -> int`: deletes any `gazevoice-gaze-*.png` left in `paths.work / "gaze-images"`, called when voice starts.
  - `run_leftover_check(job, paths) -> dict` and `find_leftovers(marker: bytes, since: float, roots: list[Path]) -> list[Path]`.
  - `state/gaze-picture-check.json`: `{"claude": "2.1.303", "passed": true, "when": …}`.
  - Action: `check_pictures` ("Check pictures get cleaned up"), `uses_plan=True`.

The rules this task pins:
- The image goes only to this one process, which exits straight away. The editor, the refiner and every long session get only the sentence it returns, so **Codex, DeepSeek and Qwen all still work**, and the image is never resent on later turns.
- Pictures run only through Claude Code, because it's the one route confirmed to take images from memory. If Claude Code isn't logged in, gaze stays text-only.
- Pictures work only while the check has passed for the **installed Claude Code version**. If Claude Code updates, pictures switch off until you run the check again. This matters because the flag has regressed before.
- The image is released as soon as the process ends. Any temp file is deleted in a `finally`, so a failed call can't leave one. `record` never holds it.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_named_part_needs_no_picture`
  - `test_a_small_connected_piece_needs_no_picture`
  - `test_a_big_unnamed_piece_needs_a_picture`
  - `test_describe_sends_the_image_inline_and_closes_the_process`: uses `fake_claude.py`, with `--no-session-persistence` in the argv and base64 in the stdin block.
  - `test_a_slow_describer_is_given_up_on_and_the_edit_carries_on`
  - `test_the_editor_gets_the_sentence_and_never_the_image`
  - `test_no_picture_is_left_behind`: after a full fake run, and after one where the describer fails, scan the whole app folder for the PNG signature and the base64 prefix `iVBOR`. Expected: nothing, including `record.json`.
  - `test_a_temp_image_is_deleted_even_if_the_call_fails`
  - `test_leftover_images_are_cleared_when_voice_starts`
  - `test_records_never_hold_the_image`
  - `test_pictures_stay_off_until_the_check_has_passed`
  - `test_a_new_claude_version_turns_pictures_off`
  - `test_find_leftovers_finds_a_planted_copy`
  - `test_shadow_mode_sends_no_picture`
  - `test_pictures_off_means_no_crop_is_taken`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
  - Flow in `_ground`: resolved target, `needs_picture`, pictures allowed, then `gaze_crop`, `gaze_crop_result`, `describe_crop`, and set `target.picture`. Status reads "Looking closer…", and `record["timings"]["picture"]` records the time.
  - The describer's argv is `claude_session_argv(prompt, PICTURE_MODEL, None, max_turns=1)`, which already carries the isolation flags and `--no-session-persistence`.
  - `picture.md`: "Someone said '{word}' while looking inside the ring in this picture of {object}. In at most 12 words, name the part inside the ring. If you can't tell, reply: unclear."
  - The Routing note says: "Pictures are deleted from this Mac right after use. Anthropic keeps what's sent under your account's terms."
- [ ] **Step 4: Run the full suite,** then run the leftover check for real. It uses a little of your plan.
- [ ] **Step 5: Commit** with the message "Describe unnamed targets from a picture that gets cleaned up".

---

## Phase 4: Finish

### Task 10: Docs, a real session, review

**Files:**
- Modify: `README.md`, `PRODUCT.md`, `docs/gaze/Gaze_Recommended_Solution.md` (status line)

- [ ] **Step 1: Write the "Use gaze" README section:** setup, what to expect (rough, not a mouse), privacy, and what stays on this Mac.
- [ ] **Step 2: Run the full suite.** Expected: OK.
- [ ] **Step 3: Run this checklist in real Blender:**
  1. Install, calibrate, and see an accuracy figure.
  2. In Watch only, a command changes nothing about how edits land, and the dashboard shows what gaze would have picked.
  3. In On, look at a wing and say "add blue color there". Only the wing turns blue, and one undo reverts it.
  4. Look between two close objects and say "make this taller". Numbers appear. Say "two".
  5. Say "move this over there" with two looks. Both targets are listed.
  6. Look away while talking. It falls back to your selection and says so.
  7. Resize the viewport. The unit asks you to recalibrate.
  8. Turn on pictures, run the check, look at an unnamed part and say "make this bigger". The record shows the sentence and no image.
  9. Afterward, search the app folder and `~/.claude` for images. None are left.
  10. Switch Gaze off, then to Unity. Everything works as before.
- [ ] **Step 4: Final review.** A fresh reviewer goes over the whole branch, with the image rule as its first check.
- [ ] **Step 5: Commit** with the message "Document gaze".

---

## Risks and unknowns

| Risk | How we find out, and what happens |
|---|---|
| Webcam accuracy on this Mac is unknown | S2 measures it. Below the gate, try GazeFollower; if that fails too, stop before Phase 2 |
| Center Stage moves the frames and breaks calibration | S1 checks it. If so, the unit tells you to turn it off in Control Center |
| Calibration drifts when you shift in your seat | S2 measures it. Head position is in the model, and the test ring shows when it's off |
| You look at the thing, then look away before saying "this" | The 0.6 s lead is a guess. Watch-only data resets it |
| Looking isn't always meaning | Only pointing words use gaze, and only during a hold |
| The view changes while you talk | The pick runs when the edit starts, so a big orbit mid-sentence can miss. It's accepted for now, and the numbers catch most cases |
| A later Claude Code keeps images it used to clear | The leftover check is tied to the installed version, so pictures turn off by themselves until it passes again |
| Pictures add a few seconds | They run only for big unnamed meshes, and the time is recorded per command |

## Questions for me

1. **The pass mark:** is 80% agreement with your selection the right bar for moving from Watch only to On?
