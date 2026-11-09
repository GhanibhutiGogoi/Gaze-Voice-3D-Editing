# Gaze: accuracy on a real face, a head guide and a live marker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task.

**Goal:** Make a real calibration pass, guide the person's head into place before the dots, and show a live marker in Blender's viewport wherever gaze says they are looking.

**Architecture:** The gaze worker keeps the camera and sends only numbers. The calibration page (`/calibrate`) already shows dots full screen and saves a calibration against the screen. This plan changes what the worker learns from those dots, adds a head-position step before them, and adds a marker that the voice loop feeds to the add-on on its existing poll.

**Tech Stack:** Python 3.12, standard library only in the control center and `gaze_model.py`; MediaPipe 1.2.2 in the gaze venv; ffmpeg avfoundation; plain HTML, CSS and JS with a strict CSP; Blender 5.2 add-on (`gpu`, `blf`).

**Spec:** The user's request, 2026-11-08: "It should have a human body position outline before doing the calibration, asking me to position my head. And also, to know that if the gaze is working in the Blender or not, we should always have that liquid thing on the screen in the viewport of Blender if the gaze is on, showing where we are concentrating. Basically, the marker showing where our gaze is." Plus their real calibration result, below.

## What the real run showed

The user's calibration at 01:05 on 2026-11-08, on a MacBook Air M4 (13-inch, screen 1710 by 1112 points):

- "That calibration was typically off by 32% of the screen."
- "The camera sent 1793 frames while the dots were up: no face in 0, half-read in 0, eyes counted as shut in 1 (typical blink score 0.30, limit 0.5), errors in 0. Average brightness 128 of 255."
- "Readings kept: 1093 to learn from, 699 to check."

What that means:

- The face is found well. Light and blinks aren't the problem now.
- **The model learned almost nothing.** Always guessing the middle of the screen would miss the five check dots by a median of 28%, so 32% is worse than no model at all. It fits 90 numbers from only 9 places on the screen. Rows at one dot are near copies of each other, so 1093 rows are really about 9 pieces of evidence, and `alpha=1.0` does nothing against that.
- **Frames are doubled.** The macOS log shows the camera sent a steady 30 frames per second during this run, but the worker got about 64 per second (14 dots at 2 s each should be about 840 frames, not 1793). ffmpeg's raw video output defaults to constant frame rate and is padding with copies.
- Center Stage was off (the macOS log says so), so it isn't the cause.
- An earlier review simulated this model: about 6% at rest, and 26 to 48% after a small head move. The check dots come last, so any drift lands on them.

## Global Constraints

- **Consent.** Agents never open the real camera, and never run Calibrate or Test against the real camera. Nothing records unless the user clicks. Use `--fake`, `--source-argv` or ffmpeg's lavfi sources.
- **Camera and pictures.** The camera stays with the gaze worker. The browser never gets a picture and never calls `getUserMedia`. Only numbers leave the worker. No picture is saved.
- **Control center.** Standard-library Python, bound to 127.0.0.1. `gazevoice/gaze_model.py` stays standard library only.
- **Blender.** Agents never touch the user's open Blender, its files or its settings. Real checks use a throwaway GUI Blender: `--factory-startup`, `--no-window-focus`, a small `--window-geometry`, `BLENDER_USER_CONFIG`, `BLENDER_USER_SCRIPTS`, `BLENDER_USER_DATAFILES` and `BLENDER_USER_EXTENSIONS` pointing at scratch folders, `TMPDIR` set to a scratch folder, and end it with `os._exit(0)` from a timer so that no `quit.blend` is written. Don't bind port 9877 from a scratch Blender.
- **Git.** No commits and no pushes. The user commits only when they ask. Don't run `git stash`, `git reset`, `git checkout` on files or anything else that changes other people's uncommitted work: the working tree holds uncommitted work that is meant to stay.
- **Writing.** What the person reads is plain language: short sentences, no jargon.
- **Tests.** `python3 -m unittest discover -s tests -t .` (the `-t .` matters). Real worker tests use `--fake`.
- **Temporary files.** Use the scratch folder named in your dispatch, never `/tmp`.

## Review Focus

1. A tab closed or hidden during the head guide: the camera turns off within `PAGE_SECONDS`, the same as during the dots.
2. A guide that can't be satisfied on a real face, because its limits were never measured on one: the person is never stuck, and can always go on with *Start anyway*.
3. The bigger fit: with 1800 rows it finishes well inside `tracker.fit`'s 30 s, so the page isn't left waiting.
4. Blender closed, minimised or on another screen while voice runs with gaze on: the marker goes away, and the poll loop (which carries the talk key) never stalls.
5. An older add-on that doesn't know the marker: voice works exactly as before.

---

### Task 1: Make the calibration learn something real

**Files:**
- Modify: `gazevoice/gaze_model.py`, `gazevoice/gaze_worker.py`, `gazevoice/components/gaze.py`, `gazevoice/paths.py`, `README.md`
- Test: `tests/test_gaze_model.py`, `tests/test_gaze_tracker.py`, `tests/test_gaze_unit.py`

**Interfaces:**
- Consumes: `gaze_model.fit`, `predict`, `check`, `validate`, `features`; `Session._fit` in the worker; `Gaze._report`, `Gaze._accept`, `Gaze._make_tracker`.
- Produces:
  - `gaze_model.fit(rows, targets, alpha=1.0, columns=None) -> dict`. `columns` is a list of indices into `FEATURE_NAMES`, or None for all. The model dict gains `"columns"` and keeps `"features": FEATURE_COUNT` (the length of a full row). `mean`, `scale` and each `weights` list have one entry per column in `columns`.
  - `gaze_model.predict(model, row)` still takes a full row of `FEATURE_COUNT` values.
  - `gaze_model.validate(model)` checks `columns` and the lengths that follow from it.
  - `gaze_model.choose(rows, targets) -> Choice`, a NamedTuple `(name: str, columns: list[int], alpha_share: float, score: float)`.
  - `gaze_model.FEATURE_SETS: dict[str, list[int]]`.
  - The worker's fit reply also holds `"model"` (the chosen set's name), `"guess"` (the median miss on the check dots if it always said the middle of the learning dots) and `"moved"` (`{"cm": float, "degrees": float}`, how far the head went between the first dot and the last).

- [ ] **Step 1: Stop the doubled frames**

Add `"-fps_mode", "passthrough"` to `camera_argv` in `gazevoice/gaze_worker.py`, as an output option before `-f rawvideo`. Show with real ffmpeg and a lavfi source whose timestamps have gaps that the old command makes extra frames and the new one doesn't. Pin the argument in a test.

- [ ] **Step 2: Give the eyes more pixels**

Set `CAMERA_SIZE = (1280, 720)`. The aspect stays 16:9, so `CAMERA_ASPECT` stays right. Frames were being scaled down to 960 by 540, which left very few pixels for each eye.

Reverted to 960x540 after a real run lost the face, because the face finder scores bigger frames lower; see `tests/test_face_refind.py`.

- [ ] **Step 3: Write the failing model tests**

In `tests/test_gaze_model.py`, build a synthetic run with `random.Random` and a fixed seed, shaped like the real one: nine learning dots and five check dots at the positions in `components/gaze.py`, about 60 rows per dot that are near copies of each other, iris values that follow the dot in a straight line, noise, and a small steady head drift over the run so the check dots are seen from a slightly moved head. Make full rows with the same arithmetic as `features` (expose that step as `gaze_model.expand(base)` and have `features` use it).

Tests:
- `test_fitting_every_feature_to_nine_places_fails_on_new_places`: the old way (`fit(rows, targets)` on all columns, alpha 1) misses the check dots by more than 0.25.
- `test_a_chosen_model_lands_on_places_it_never_saw`: fitting with `choose`'s pick misses them by less than 0.15.
- `test_choosing_leaves_out_a_whole_dot_at_a_time`: a feature that gives away the dot's identity in the learning rows doesn't win.
- `test_a_fit_on_some_columns_predicts_from_a_full_row`.
- `test_validate_refuses_columns_that_dont_match_the_weights`.
- `test_choosing_1800_rows_takes_under_ten_seconds` (time it on this Mac and leave a generous margin).

- [ ] **Step 4: Implement the choice**

```python
_EYES = ["right iris along", "right iris across", "left iris along", "left iris across"]
_HEAD = ["head yaw", "head pitch", "head x", "head y"]
FEATURE_SETS = {
    "eyes": _EYES,
    "eyes and head": _EYES + _HEAD,
    "eyes, curved": _EYES + every pair of _EYES (a value with itself included),
    "eyes and head, curved": _EYES + _HEAD + every pair of _EYES + each of _EYES × each of ("head yaw", "head pitch"),
    "everything": all of FEATURE_NAMES,
}   # stored as lists of indices into FEATURE_NAMES
ALPHA_SHARES = (0.001, 0.01, 0.1, 1.0)  # alpha is this share of the rows being fitted
KEEP_PER_DOT = 40  # evenly spaced rows per dot while choosing, because the rest are near copies
```

`choose` goes through every set and every share. Rows are grouped by their target, since each dot is one target. For each dot, it fits on the thinned rows of all the other dots and scores the miss on that dot's rows. A candidate's score is the median across the dots. The lowest wins, and within 0.005 of the best, the candidate with the fewest columns wins, then the larger share.

- [ ] **Step 5: Use it in the worker**

`Session._fit`: call `choose` on the learning rows, fit that choice on them (thinned as in `choose`), and mark it on the check rows, which gives the honest figure as now. Then, when there are check rows, refit the same choice on learning and check rows together, because 14 places teach more than 9, and that is the model that is saved. The saved `check` stays the honest figure from before the refit. Compute `guess` and `moved`. For `moved`, compare the median base values of the first dot's rows with the last dot's: `cm` from head x, head y and head distance, `degrees` from head yaw and pitch.

- [ ] **Step 6: Keep the numbers of the last run**

Add `AppPaths.gaze_readings` → `state / "gaze-last-run.json"`. The worker takes `--readings-file PATH`. At each fit, whether it worked or not, if it has any rows it writes the file whole or not at all: the 12 base values per row (rounded to 5 places), each row's target, which rows were held back, and the counts from `DotsSeen`. `Gaze._make_tracker` passes the flag only when calibrating (when `calibration` is None), never for voice. This holds numbers only, no picture, and each run replaces the last. It lets a failed real run be studied without asking the person to run it again.

- [ ] **Step 7: Say it plainly**

`Gaze._report` logs, and the page shows, for example: `Readings kept: 420 to learn from, 240 to check. Typically off by 12% of the screen; always guessing the middle would be 28%. Model: eyes and head.` and a second line starting `Your head moved about 1.2 cm and turned about 2 degrees during the run.` Add `"Your head moved"` to `MEASURED` in `gazevoice/server/static/calibrate.js`. Keep working when the reply has none of these keys.

- [ ] **Step 8: README**

In the "Use gaze" section, say what the last-run file holds and that each run replaces it, and add it to the table of files.

- [ ] **Step 9: Verify**

Run the full suite. Run the real worker with real MediaPipe on a lavfi source to prove the new camera size and the fit path run end to end, and that a calibration file made by the new code loads through `load_calibration`.

---

### Task 2: A head-and-shoulders guide before the dots

**Files:**
- Modify: `gazevoice/gaze_worker.py`, `gazevoice/voice/gaze_tracker.py`, `gazevoice/components/gaze.py`, `gazevoice/server/app.py`, `gazevoice/server/static/calibrate.html`, `calibrate.css`, `calibrate.js`, `README.md`
- Test: `tests/test_gaze_tracker.py`, `tests/test_gaze_unit.py`, `tests/test_server.py`, `tests/test_dashboard_js.py`

**Interfaces:**
- Consumes: `DotPage`, the `gaze.dot` event, `/api/gaze/page`, the stages in `calibrate.js`.
- Produces:
  - Worker command `{"cmd": "guide", "on": true|false}`. While on, frames are looked at and it sends at most `GUIDE_RATE` times a second `{"guide": {"found": bool, "x": share, "y": share, "distance": cm}}`. `x` and `y` are the middle of the eyes as shares of the picture, not mirrored, from landmarks 33 and 263. `distance` is the base value "head distance". With no face, only `found: false`.
  - `Observation` gains `place: tuple[float, float, float] | None`. `FakeDetector` reports a steady place that fits the outline, so `--fake` runs pass the guide.
  - `GazeTracker.guide(on: bool)` and `GazeTracker.place` (the latest guide reading and when it arrived, safe to read from another thread).
  - Event `gaze.guide`: `{"job_id", "found", "x", "y", "scale", "fits", "advice"}`. `x` and `y` are mirrored so the page acts like a mirror. `scale` is how big the head should look compared with the outline (target distance over distance).
  - `/api/gaze/page` also takes `{"job": id, "alive": true}` and `{"job": id, "skip": true}`.

- [ ] **Step 1: Write failing tests** for each behaviour below before building it, in the files above. Use `FakeBrowser` in `tests/test_gaze_unit.py` for the job and real `--fake` workers for the pipe.

- [ ] **Step 2: Flow**

The person presses Start, the page goes full screen, they click the circle (which measures the page, as now), the camera opens, the guide shows, and the dots follow by themselves once the head has been in place for `HOLD_SECONDS`. So `action_calibrate` gains a step after `tracker.start()` and before `_look_at_the_dots`.

- [ ] **Step 3: Limits and words**

In `components/gaze.py`:

```python
GUIDE_RATE = 10            # updates a second
GUIDE_SECONDS = 60.0       # no head in place by now: stop, and the camera goes off
HOLD_SECONDS = 1.0         # the head has to stay in place this long
GUIDE_X = (0.40, 0.60)     # where the middle of the eyes belongs, as shares of the mirrored picture
GUIDE_Y = (0.30, 0.60)
GUIDE_DISTANCE = (40.0, 80.0)  # centimetres from the camera
GUIDE_TARGET = (0.5, 0.45, 60.0)  # the middle of those: where the outline sits, and the size it stands for
```

One piece of advice at a time, largest miss first: "Move left", "Move right", "Move up", "Move down", "Come closer", "Move back". "Hold still" while it fits. "I can't see your face. Check the light and look at the screen." when there is no face. Mirrored `x` too large means the head appears too far right, so the advice is "Move left".

- [ ] **Step 4: Never stuck, never left on**

- `DotPage` gains `alive(job_id)` and `skip(job_id)`. The page sends `alive` at least every 0.5 s during the guide. If nothing is heard for `self.timeout`, the job raises `PAGE_GONE`, so a closed tab turns the camera off during the guide as it does during the dots. `left` works as now.
- The page has a *Start anyway* key throughout the guide. Pressing it sends `skip`, and the dots start at once. The limits above have never been measured on a real face, so they must never trap anyone.
- Past `GUIDE_SECONDS` the job ends with a `JobError` that names what to try, and the camera goes off.
- The guide stage turns off the worker's guide command before the first dot.

- [ ] **Step 5: The page**

A new stage `guide`, bright like the dots. Inline SVG (so the CSP needs no change) draws a head-and-shoulders outline in the middle of the screen, sized to about 70% of the height, stroked in the dots' ink and drawn with CSS classes, never a `style` attribute. Over it, a ring shows where the person's head is now, moved by `x` and `y` and sized by `scale`, gliding (a CSS transition around 120 ms, off under `prefers-reduced-motion`). Above the outline, the advice in large text. When `fits` is true the outline and ring turn green. Below it, the *Start anyway* key. Esc works as in other stages. Keep the cursor visible in this stage. Update the text on the ready card: first you line your head up with an outline, then you follow the dots, and the camera turns on when you click the circle.

Pull out `guideRing(data, width, height)` as a pure function and test it with node as `pageArea` is tested.

- [ ] **Step 6: Real browser check**

`server.py`, `drive.mjs` and `run.py` in the scratch folder given in your dispatch run the real dashboard from a scratch folder with `--fake`, and drive real headless Chrome with real clicks. Adapt copies of them to cover: the guide showing and passing by itself, *Start anyway*, a closed tab during the guide, and take a screenshot of the guide stage, which you open and look at. Report what you saw.

- [ ] **Step 7: README**: update step 3 of "Use gaze". Run the full suite.

---

### Task 3: A live gaze marker in Blender's viewport

**Files:**
- Modify: `gazevoice/voice/service.py`, `gazevoice/voice/gaze.py`, `blender_addon/gazevoice_bridge/__init__.py`, `blender_addon/gazevoice_bridge/protocol.py`, `blender_addon/gazevoice_bridge/blender_manifest.toml`, `gazevoice/blender_socket.py`, `README.md`
- Test: `tests/test_voice_service.py`, `tests/test_bridge_protocol.py`, `tests/test_gaze.py`

**Interfaces:**
- Consumes: `gaze.track`, `gaze.screen`, `viewport_area`, `on_viewport`, `Factories.window_frame`, `_poll_loop`, `MIN_CONFIDENCE`, `LOOKING_AWAY_MARGIN`.
- Produces:
  - The `poll` request gains an optional `"marker"`: `[u, v]` to show the marker there, `null` to take it away, and no key to leave it as it is. An add-on that doesn't know the key ignores it.
  - `SceneAccess.gaze_marker(at: list | None) -> None`.
  - `gaze.marker_point(track, now, screen, view) -> tuple[float, float] | None`.
  - Bridge version `0.3.2` in `protocol.py`, `blender_manifest.toml`, `blender_socket.py` and `tests/test_bridge_protocol.py`.

- [ ] **Step 1: Write failing tests** for each behaviour below before building it.

- [ ] **Step 2: Where to put it**

`marker_point` takes the samples of the last `MARKER_WINDOW = 0.2` seconds at or above `MIN_CONFIDENCE`, and needs at least `MARKER_MIN_SAMPLES = 3`. It returns their median moved onto the viewport with `on_viewport`, or None if there are too few or if the point is outside the viewport by more than `LOOKING_AWAY_MARGIN`. A point within the margin is clamped to 0 to 1, because the protocol takes 0 to 1 only.

- [ ] **Step 3: The voice loop**

It sends the marker on the existing poll request in `_poll_loop`, not on a second thread or a second request, so the talk key is never delayed and the rate takes care of itself. It is sent only when the gaze part is the camera (`getattr(gaze, "screen", None)` is not None), the part is alive and gaze isn't off. The viewport's place is refreshed at most every `MARKER_PLACE_SECONDS = 1.0`. This asks Blender and macOS, and it must not hold up the poll: if asking fails, or Blender can't be placed, or `screen.holds(view)` is false, the marker is null until the next refresh. Send `null` once, not on every poll, when there is nothing to show. On stopping, send `null`. Nothing about this may be able to raise out of the poll loop.

- [ ] **Step 4: The add-on's marker**

A soft, fluid blob of light, not a hard ring. The person called it "that liquid thing". It must always be easy to tell apart from the numbered marks and the test ring.

- `_gaze["marker"]` holds the target. A `_marker_tick` timer at about 30 Hz runs only while the marker is showing or fading. It eases the drawn point towards the target (a set share of the gap per tick, so it glides and never jumps), fades in on the first point, fades out over about 0.3 s after `null`, and calls `tag_redraw` on the target viewport only, and only when something changed visibly.
- Draw 3 or 4 layered translucent discs, largest and faintest first, with a brighter core. The whole blob stretches a little along its direction of travel and relaxes when it stops, which gives it the liquid feel. Size it in points so it looks the same on any display: about 30 points across at its widest, using the same pixels-per-point sum as `gaze_place`.
- Like the dot and ring, it draws only while `_events.live()`, so if the app dies it goes away by itself. The timer unregisters itself when nothing is showing, and on add-on unregister.
- Keep redraws cheap: a scene that is expensive to draw must not slow down because of the marker. Use only `POST_PIXEL` drawing, as the dot does.

- [ ] **Step 5: Real Blender check**

In a throwaway GUI Blender (see Global Constraints), load the real add-on code without binding port 9877, feed the marker a moving path, and capture the window with `bpy.ops.screen.screenshot` to a scratch file. Open it and report what it looks like at rest and in motion. Measure the time spent in the draw handler. Confirm the timer stops when the marker is gone.

- [ ] **Step 6: README**: add the marker to "Use gaze": what it is, that it's on whenever voice runs with camera gaze set to Watch only or On, and that a marker in the wrong place means gaze needs calibrating. Say to press *Update add-on*. Run the full suite.
