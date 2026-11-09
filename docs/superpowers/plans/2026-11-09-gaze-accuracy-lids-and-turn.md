# Gaze accuracy: eyelids, head turn, shorter dots, closer seat

**Goal:** Bring the calibration's typical miss down from 0.23 of the screen toward 0.20, let the user sit closer, and record enough numbers that the next real run shows what is worth trying after that.

**Why (measured on the user's run of 2026-11-09 00:14; scripts and numbers in the session scratchpad `accuracy/`):**
- The model misses even its own calibration dots by about 0.22. It is coarse, not drifting.
- **Up and down:** the iris barely moves. The eyelids follow the gaze, and using how open the eyes are cuts the up-and-down error by about 40%.
- **Left and right:** turning the head moves where you look by about (distance ÷ screen width) × the turn. As a fixed geometric correction, not a learned one, this took the run from 0.228 to about 0.21. The learned head-turn sets did worse.
- **Dot length:** about 1 s of looking per dot did as well as 2 s, so more dots fit in the same time.
- **What did not help:** a bigger or cropped face picture, MediaPipe's eyeLook scores (made from the same points), and stronger or weaker ridge.
- **Confidence is low to moderate.** The gain is about the size of run-to-run differences.

**Rules:**
- Nothing installs, downloads, records or calls a model unless the user clicks for it.
- The control center stays standard-library Python on 127.0.0.1. The worker may use the gaze venv.
- No picture is ever saved; the readings file is numbers only.
- Plain language in comments, messages and docs.
- No commits unless asked.
- No feature set may read where the head sits (head x, y, distance) as a learned column. The head-shift test stays green.

**Rulings made while planning:**
- **Learned head-turn sets are dropped.** The fixed correction replaces them. If this is wrong, it is one set to add back.
- **Head nod (pitch) gets no correction.** It made this run worse.
- **No extra head-moving steps in the calibration yet,** such as turning, nodding or leaning. The user only asked for the three changes, so these wait for the next run's numbers.
- **If the screen's physical size can't be read,** the model fits with no turn correction, the same as today, and says so in the result.

---

## Task: one change, in four parts

**Files:**
- `gazevoice/gaze_model.py`, `gazevoice/gaze_worker.py`, `gazevoice/components/gaze.py`, `gazevoice/mac_windows.py` (or a small new stdlib module for the display size)
- `gazevoice/server/static/calibrate.js` and `calibrate.html`, only if the dot timing or the outline needs it
- `README.md`, and the tests for each

1. **Eyelids in the fit.**
   - The feature sets become "eyes", "eyes and lids", "eyes, curved" and "eyes and lids, curved".
   - "Lids" are the existing right and left eye-opening columns.
   - `choose()` picks among them as it does now.
2. **Fixed head-turn correction.**
   - The model learns `u + k·yaw` from the eyes and predicts `f(eyes) − k·yaw`, where `k = head distance ÷ screen width`. Head distance is per frame, from the row, in cm. Screen width is the calibrated area's width in cm.
   - Take the sign and size from the study (`accuracy/q2_app.py`): about −1.37 per radian at 39.6 cm on a 29.0 cm-wide area.
   - The screen width comes from the main display's physical size (CoreGraphics `CGDisplayScreenSize` against its size in points, through ctypes as `mac_windows.py` does), times the calibration area's width in points. The control center measures it and passes it to the worker's `fit`.
   - The model saves `k`'s inputs, and `validate` refuses models without them, so older calibrations ask to be made again.
   - Test it with a synthetic face that turns its head while looking at one point: the guess must stay put.
3. **More, shorter dots.**
   - 16 fit dots and 8 check dots.
   - The settle stays at 0.5 s; then each dot is looked at for about 1.0 s, so the whole run takes about as long as now.
   - The check dots are mixed in among the fit dots, and the order jumps around the screen.
   - Fit dots cover the screen edge to edge. Check dots sit where no fit dot is.
   - Make sure `choose()`, `thin` (`KEEP_PER_DOT`) and the minimum-readings checks still make sense with about 30 rows a dot.
4. **Sit closer, with the whole face in view.**
   - The user wants the face big in the picture, so the eyes are big and clear.
   - Change `GUIDE_DISTANCE` to (25.0, 45.0) and `GUIDE_TARGET` to (0.5, 0.45, 30.0). At 960x540 that makes the face about 230–280 px wide, around the face model's own 256 px working size.
   - The outline on the page must stand for the new target size.
   - Update the advice wording if it names distances.
   - **New guide check:** if any of the face's landmarks come within a small margin of the picture's edge, the face counts as not in place. The advice says "Move back a little, part of your face is out of view".
   - The worker's `guide` reading gains what this needs, as numbers only, for example the share of the picture the face's box reaches to on each side.
   - Test it with a face whose box touches an edge.
5. **Record more for the next run**, numbers only, in `gaze-last-run.json`. For each row:
   - ms since the run began;
   - the 14 eye blendshape scores (eyeLook ×8, blink, squint and wide, per eye);
   - the 4x4 head transform;
   - the face's width in pixels;
   - both eyes' contour and iris points, as picture shares.

   For the run: the camera's real frame size and rate, from the line ffmpeg prints when it opens the camera (`-loglevel info -nostats`, parsed once, without flooding the stderr tail used in error messages).

   Update the README paragraph on what the readings file holds, truthfully and in plain words.
6. **README:**
   - the new distance (about 30 cm, between 25 and 45, with the whole face in view);
   - that a calibration from before this change must be made again;
   - that sitting closer and keeping the head level helps.

## Done when
- The task passes its own review and a final review.
- The full suite passes, run by the main session.
- On the user's saved run, the study's own yardstick (fit on the 9 fit dots, marked on the 5 check dots, 14-dot refit) gives no worse than the study's 0.21 for the new model. Report the number; it is a check, not a test.
