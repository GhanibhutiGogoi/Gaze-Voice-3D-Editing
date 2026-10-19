# GazeVoice Voice Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
1. Hold a key in Blender and say a command.
2. GazeVoice transcribes it on the Mac and cleans it up.
3. Claude Code (or Codex) makes the edit through Blender's MCP, scoped to what's selected.
4. One undo step is added, and the status shows in the viewport.
5. Everything is started, watched and tested from the dashboard.

**Architecture:**
- A `VoiceService` runs inside the control-center process. It has two worker threads and a queue between them.
- **Thread A:** capture → speech-to-text, plus the meta commands undo/redo/stop.
- **Thread B:** refine → edit → undo step.
- **Long-lived processes:**
  - ffmpeg streams the mic into an in-memory ring buffer.
  - The speech worker keeps the model loaded.
  - Claude Code runs two headless stream-json sessions: the refiner (fast model, no tools) and the editor (Blender MCP only).
- **Bridge add-on 0.2.0:** adds hold-to-talk (a modal operator on ⌥Space), an event queue the app polls, scene names, undo push/undo/redo, and a sidebar panel.

**Tech Stack:**
- Python 3.12+ stdlib (subprocess, threading, wave, json)
- ffmpeg avfoundation
- `claude -p --input-format stream-json --output-format stream-json`
- `codex exec --json` (+ `resume`)
- Blender 5.x bpy (modal operator, keymap, timers, `temp_override`)

**Spec:** `docs/superpowers/specs/2026-10-18-gazevoice-design.md` (phase 2), extending `docs/superpowers/plans/2026-10-18-control-center.md`.

## Global Constraints

- All of plan 1's Global Constraints apply: stdlib only, argv lists, the Novo guard, no installs or recordings without a click, 127.0.0.1 only.
- **Mic consent:**
  - The mic is open only while Voice is On, and the dashboard and Blender say so.
  - Audio is kept in memory and saved to disk only for a command you spoke.
- **Hold-to-talk:**
  - The default key is ⌥ Space (`type="SPACE", alt=True`) in the "Window" keymap, and it can be rebound in Blender's Keymap preferences.
  - Escape cancels.
  - A press shorter than 0.3 s is ignored.
- **Audio:**
  - The ring buffer holds 16 kHz mono s16le and keeps 60 s.
  - Capture covers `[down − 0.35 s, up + 0.15 s]`.
- **Brains:**
  - Editor = the brain picked in Routing. With Claude it runs `--model <editor_model>` (`default` means no flag).
  - Refiner = the same CLI. With Claude it runs `--model <refiner_model>` (default `haiku`).
  - Editor flags: `CLAUDE_ISOLATION` plus `--input-format stream-json --output-format stream-json --verbose --mcp-config <cfg> --allowedTools mcp__blender --permission-prompts none --system-prompt-file <editor.md>`.
  - Refiner flags: the same without MCP, using `--system-prompt-file <refiner.md>`.
- **Undo:**
  - After a successful edit, the app pushes one undo step through the bridge: `"GazeVoice: <command>"`.
  - Voice "undo" and "redo" go through the bridge, not the LLM.
- **Per command logs:** `logs/commands/<YYYYmmdd-HHMMSS>-<n>/` holds `audio.wav` and `record.json` (heard, words, refined, focus, tool calls, reply, timings, error).
- **Bridge versions:**
  - The app expects bridge `0.2.0`.
  - An older bridge shows "installed · update needed" with an `install` action labeled "Update add-on".

## Review Focus

1. **Speaking while a command runs:** the new audio is transcribed and queued. "stop" interrupts the running edit (SIGINT, restarting the session if it exits) and clears the queue.
2. **A brain session dies mid-command** (crash, logout): the command fails with a readable error, the next command starts a fresh session, and the status line says so.
3. **Blender closes while Voice is On:** polling fails quietly, the dashboard shows "Blender isn't connected", and voice resumes by itself when Blender comes back.
4. **Unparseable refiner output:** fall back to the cleaned transcript as the command and log a warning. Never drop the command silently.
5. **Mic device missing or permission denied when Voice starts:** Start fails with the same Privacy & Security guidance the Microphone test gives. Nothing half-starts.

---

### Task 1: Bridge add-on 0.2.0 (hold-to-talk, events, undo, scene names)
- **Files:**
  - `blender_addon/gazevoice_bridge/{protocol.py,__init__.py,blender_manifest.toml}`
  - `gazevoice/components/bridge.py`
  - `gazevoice/blender_socket.py` (adds `BridgeClient`: a persistent connection with `request()`)
  - tests
- **Protocol additions (pure):**
  - `poll` → `{"ok": True, "events": [...]}`, which drains the queue.
  - `scene_names` → `{"ok": True, "names": [...<=300]}`.
  - `undo_push` with `message` → `{"ok": bool}`.
  - `undo` / `redo` → `{"ok": bool}`.
  - `status` gains an optional `state ∈ {idle, listening, working, done, error}`.
  - `SceneAccess` gains `object_names()`, `undo_push(msg)`, `undo()`, `redo()`, `drain_events()`.
- **Events:** `{"type": "ptt", "state": "down" | "up" | "cancel", "t": time.time()}` and `{"type": "toggle", "t": ...}` (from the sidebar button).
- **bpy side:**
  - The `GAZEVOICE_OT_push_to_talk` modal operator queues `down` in invoke. It ends on SPACE RELEASE (`up`) or ESC (`cancel`), passes other events through, and caps at 60 s.
  - The keymap is registered in `keyconfigs.addon` "Window".
  - The `GAZEVOICE_PT_panel` sidebar tab "GazeVoice" shows the status, the last command, a Talk toggle and the key hint.
  - Undo ops run through `context.temp_override(window, area, region)` on the first window with a VIEW_3D area.
- **Component:** if ping reports a bridge version other than `0.2.0`, the unit shows `installed` "Update needed (0.1.0 → 0.2.0)" with `install` labeled "Update add-on". The `test` action also reports `scene_names` count.
- **Tests:** every new protocol command with FakeScene; drain semantics; outdated-version state; `BridgeClient` reuses one socket for 3 requests.

### Task 2: Microphone stream (ring buffer)
- **Files:** `gazevoice/voice/__init__.py`, `gazevoice/voice/audio.py`, `tests/test_audio.py`
- **Interfaces:**
  - `RingBuffer(rate=16000, seconds=60)` with `.append(pcm: bytes, end_time: float)`, `.capture(t0, t1) -> bytes`, `.latest_time`.
  - `write_wav(path, pcm, rate=16000)`
  - `MicStream(runner_argv: list[str])` with `.start()`, `.stop()`, `.alive`, `.error`, `.buffer`, built by `mic_argv(device_index)` = `["ffmpeg","-hide_banner","-loglevel","error","-f","avfoundation","-i",f":{i}","-ac","1","-ar","16000","-f","s16le","-"]`.
- **Tests:**
  - Capture slices at exact sample boundaries.
  - Old audio is trimmed.
  - Capture before any audio returns `b""`.
  - Integration: a real ffmpeg `-f lavfi -i sine=frequency=440:sample_rate=16000` stream feeds the buffer, and a 0.5 s capture is about −3 dBFS (skipped if ffmpeg is missing).

### Task 3: Speech service (model stays loaded)
- **Files:** `gazevoice/speech_worker.py` (adds `--serve`), `gazevoice/voice/stt.py`, tests
- **Worker `--serve`:**
  - Prints `{"ready": true, "load_seconds": x}`.
  - Then for each stdin line `{"id", "audio", "prompt"?}` it prints `{"id", "text", "seconds", "words": [{"text","start","end"}]}` or `{"id", "error"}`.
  - Words come from Parakeet `result.sentences[].tokens[]` and from Whisper `segments[].words[]` (`word_timestamps=True`, `initial_prompt` = prompt).
  - `GAZEVOICE_FAKE_ENGINE=1` returns a canned transcript, so tests can drive the real protocol without MLX.
- **`SpeechService(python, worker, engine, model)`:** `.start(timeout=180)`, `.transcribe(path, prompt=None, timeout=60) -> dict`, `.stop()`.
- **Tests:** a real subprocess in fake mode covering ready, two requests and a bad audio path; the service raises `SpeechError` when the worker dies.

### Task 4: Brain sessions
- **Files:** `gazevoice/voice/brains.py`, `tests/fixtures/fake_claude.py`, `tests/fixtures/fake_codex.py`, tests
- **`TurnResult(text: str, is_error: bool, tools: list[str], seconds: float, raw_error: str | None)`**
- **`ClaudeSession(argv: list[str], cwd)`:**
  - `.start(timeout=60)` waits for `system/init`; when MCP is configured it checks that `blender` is `connected` in `mcp_servers`.
  - `.send(text, on_tool=None, timeout=180) -> TurnResult` writes `{"type":"user","message":{"role":"user","content":text}}`, reads events until `result`, and collects `tool_use` names from `assistant` content.
  - `.interrupt()` sends SIGINT. `.close()`.
  - Helper argv builders: `claude_session_argv(system_prompt_file, model, mcp_config=None)`.
- **`CodexSession(cwd, mcp_cmd=None)`:**
  - `.send()` runs `codex exec --json --skip-git-repo-check --sandbox read-only [-c mcp...] [resume <thread_id>] <prompt>`, keeps `thread_id` from `thread.started`, and collects `mcp_tool_call` items.
  - The system prompt is prepended to the first message.
  - It is experimental.
- **Tests:** the fake CLIs speak the real formats. Cover:
  - two turns over one process
  - tool names reported
  - an `is_error` result
  - a process death mid-turn, which raises `BrainError`
  - interrupt
  - codex resume uses the captured thread id

### Task 5: Prompts, refiner parsing, meta commands
- **Files:** `gazevoice/voice/prompts/editor.md`, `gazevoice/voice/prompts/refiner.md`, `gazevoice/voice/commands.py`, tests
- **Interfaces:**
  - `meta_command(text) -> "undo" | "redo" | "stop" | None`, which accepts "undo", "undo that", "take that back", "redo", "stop", "cancel", "never mind" with optional punctuation and fillers.
  - `refiner_message(heard, focus, names, previous) -> str`, a JSON payload.
  - `parse_refined(reply, heard) -> Refined(kind, text, fallback: bool)`, which pulls the first JSON object; on failure `Refined("command", heard, fallback=True)`.
  - `editor_message(refined, heard, focus) -> str`
  - `vocabulary_prompt(names) -> str`: Blender terms plus scene names, for Whisper.
- **Tests:**
  - meta variants
  - JSON inside a code fence or with leading text
  - fallback
  - an `ignore` kind
  - the focus text with nothing selected

### Task 6: VoiceService orchestration
- **Files:** `gazevoice/voice/service.py`, `tests/test_voice_service.py`
- **`VoiceService(ctx, bus, factories)`:**
  - `factories` builds mic, stt, refiner, editor and bridge, so tests inject fakes.
  - Methods: `.start() -> None`, which raises `VoiceError` with a readable reason; `.stop()`; `.ptt(state, t=None)`; `.say(text)`, a typed command that skips speech-to-text; `.undo()`; `.reset_conversation()`; `.snapshot() -> dict` (state, reason, brain, recent[≤20], queue length).
- **States:** `off`, `starting`, `ready`, `listening`, `transcribing`, `refining`, `editing`, `error`.
- **Bus events:** `voice.state` and `voice.command`, with each command's record updated as it moves through the stages.
- **Threads:**
  - a poll thread that drains bridge events every 50 ms and reconnects with backoff
  - thread A (stt)
  - thread B (edit)
- **Blender status:** `status` is pushed at each stage: "Listening…", "Heard: …", "Working: <tool>…", "✓ <reply>" or "✗ <error>", then "Ready · hold ⌥Space to talk".
- **Tests (all fakes):**
  - the happy path pushes an undo step with the refined command
  - meta undo skips the brains
  - "stop" interrupts the editor and clears the queue
  - an editor error reports and stays ready
  - start fails cleanly when the bridge is missing
  - a short press is ignored
  - an empty transcript gives "Didn't catch that"
  - the records are written to the log dir

### Task 7: API, settings, readiness
- **Files:** `gazevoice/server/app.py`, `gazevoice/settings.py`, `gazevoice/__main__.py`, tests
- **Routes:**
  - `GET /api/voice` returns the snapshot.
  - `POST /api/voice/start|stop|undo|reset`
  - `POST /api/voice/say {"text"}`
  - `POST /api/voice/ptt {"state": "down"|"up"|"cancel"}`
  - All routes use the same guards (header, Host, Origin).
  - `start` returns 409 with the reason if not ready.
- **Settings:**
  - `editor_model ∈ {default, opus, sonnet, haiku}`, default `default`.
  - `refiner_model ∈ {haiku, sonnet, default}`, default `haiku`.
- **Snapshot:** `/api/state` gains `voice`.

### Task 8: Dashboard Voice unit
- **Files:** `gazevoice/server/static/{index.html,app.css,app.js}`, `DESIGN.md`
- **Layout:** the "Voice" transport unit sits at the top of the side column. It has:
  - a state LED and readout
  - Start/Stop (amber)
  - Hold to talk (pointer and Space hold, keyboard accessible)
  - a "Type a command" field
  - Undo last
  - New conversation
  - the recent commands: heard → understood → result, with stage timings
- **Not-ready state:** lists exactly which units aren't ready.
- **Routing additions:** editor and refiner model selects.
- **SSE:** `voice.state` and `voice.command` events.

### Task 9: Docs, real-environment checks, review
- **Docs:** a README "Talk to Blender" section.
- **Real-environment checks** (no system installs):
  - (a) a real Claude Code stream-json round trip with no tools
  - (b) the real Blender MCP server installed into a temp `GAZEVOICE_HOME`, whose tools are listed through `ClaudeSession`-independent `StdioMcpClient`
  - (c) a real ffmpeg lavfi stream into the ring buffer
  - (d) `--help` checks for every new CLI flag
- **Final review:** a fresh reviewer goes over the whole change, and its fixes get tests.
