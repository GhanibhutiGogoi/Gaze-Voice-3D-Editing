# Gaze bubble and the sent picture on the dashboard

**Goal:** Gaze shows as a see-through bubble, in Blender's viewport and on the picture sent to the AI, and the dashboard shows each picture that was sent.

**What the user asked for (2026-11-09):**
- "Marker look: Yes": the live marker becomes a bigger, see-through bubble like the ones in eye-tracker videos, and the same bubble replaces the ring on the picture.
- "Only the sent picture, sent picture should have the marker indicating where the AI should focus on."

**Rules that apply to everything here:**
- Nothing installs, downloads, records or calls a model unless the user clicks for it.
- The control center stays standard-library Python and binds only to 127.0.0.1.
- The picture is never written to disk. It may be held in the control center's memory so the dashboard can show it, and it is gone when the dashboard stops.
- Plain language in code comments, messages and docs.
- No commits unless the user asks.

**Rulings made while planning:**
- In the viewport the bubble is a fixed size, about 90 points across. On the picture it covers the area gaze may mean (the area the ring covered), because a smaller bubble would tell the AI to look more precisely than gaze can know. If this is wrong, it is one size setting.
- The dashboard keeps the last 20 sent pictures. If this is wrong, it is one number.

---

## Task 1: The bubble, in Blender and on the picture

**Files:** `blender_addon/gazevoice_bridge/__init__.py`, `blender_addon/gazevoice_bridge/protocol.py`, `blender_addon/gazevoice_bridge/blender_manifest.toml`, `gazevoice/blender_socket.py` (Bridge version), `gazevoice/voice/prompts/picture.md`, `gazevoice/voice/prompts/picture_rules.md`, tests in `tests/test_bridge_protocol.py` and wherever the prompts and Bridge version are pinned.

1. **Live marker** (`_draw_marker`, `MARKER_*`): a see-through aqua bubble about 90 points across.
   - It has a soft edge and a faint rim, and no solid core.
   - The model must stay clearly visible through every part of it.
   - Keep the gliding, the stretching while it moves, and the fading in and out.
   - It still never appears in the picture.
2. **The picture** (`_crop_png`, `protocol.ring_in_crop`, `protocol.draw_ring`): draw the bubble in place of the ring, over the same area.
   - It is a light tint with a clear rim, so the AI can tell inside from outside.
   - The tint must not hide what is under it.
   - Use a pure function in `protocol.py`, so it can be tested outside Blender.
3. **The describer's wording:** the prompts say "bubble" where they said "ring".
   - The question is still: name the part inside it, or say "unclear".
4. **Bridge version** goes to 0.3.3, everywhere it is pinned.
   - The README says to press *Update add-on* and restart Blender.
5. **Check it in a throwaway Blender**, as the marker was checked before.
   - Use `--factory-startup` with a scratch user config, and leave the user's Blender untouched.
   - Take screenshots of the viewport bubble at rest and moving, and a crop PNG with its bubble.
   - Look at all of them. They stay in the scratchpad and are deleted when done.

## Task 2: The sent picture on the dashboard

**Files:** `gazevoice/voice/service.py` (`_picture_of`, `_look_closer`), `gazevoice/voice/picture.py` (docstring), `gazevoice/server/app.py`, `gazevoice/server/static/app.js` (and its CSS), `README.md`, and tests in `tests/test_voice_service.py`, `tests/test_server.py` and `tests/test_dashboard_js.py`.

1. **Keep the picture in memory.** After a picture is sent, the voice service keeps its PNG bytes in memory, tied to the command and the pointing word.
   - It keeps at most 20 pictures; the oldest goes first.
   - Nothing about the image goes into `record.json`, apart from a small note that a picture was shown, which the dashboard can use.
   - Clearing the history, or stopping the dashboard, drops the pictures.
2. **Serve it.** `GET /api/voice/pictures/<command id>/<n>` returns `image/png` from memory, or 404 once it is gone.
   - It is served only on 127.0.0.1, with no caching headers that would make the browser keep it on disk (`Cache-Control: no-store`).
   - The page's CSP has to allow it.
3. **Show it.** The command's line in the dashboard shows a small picture next to the sentence the AI gave about it.
   - Clicking it shows the picture full size; Esc or a click closes it.
   - If the picture is gone (the dashboard restarted, or it is older than the last 20), the line says so in plain words.
   - Commands with no picture show nothing new.
4. **Docs:** fix the README's "What stays on this Mac" and pictures text, and the docstrings in `picture.py` and `service.py`, which say the picture is never kept.
   - Say plainly that it stays in the dashboard's memory for the last 20 commands so you can see what the AI saw, is never saved to disk, and is gone when the dashboard stops.

5. **Leftovers from Task 1's review.** These are wording and tests only; nothing changes what the user sees in Blender.
   - Where text still says the picture has a "ring", make it say "bubble":
     - `gazevoice/voice/commands.py` ~173: the editor line about the picture. It is pinned in `tests/test_voice_commands.py` and `tests/test_voice_service.py`.
     - `gazevoice/voice/prompts/refiner.md` ~6.
     - `gazevoice/voice/prompts/editor.md` ~11.
     - The `picture.py` and `service.py` docstrings.
     - The stale test docstring "A soft marker" in `tests/test_voice_service.py`.
   - Leave alone the blue Test ring, the calibration guide ring and the "dot, ring and numbers" text, which are about other things.
   - `tests/test_picture.py` ~221: check for "ring" as a whole word (`\bring\b`), not as a substring.
   - Add a test that compares `protocol.draw_bubble` with a plain per-pixel version of the same formula, for circles and ovals up to 3:1, including bubbles that run off the edge.
   - Note in `draw_bubble`'s docstring that very thin ovals (5:1 or more) lose a little of the outer dark edge. Gaze's radius is about round, so this doesn't happen today.

## Done when
- Both tasks pass their own review.
- A final review of both together finds nothing important.
- The full test suite passes, run by the main session.
