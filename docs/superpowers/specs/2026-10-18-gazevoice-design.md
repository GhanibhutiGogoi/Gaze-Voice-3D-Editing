# GazeVoice: Design

**Date:** 2026-10-18
**Status:** Agreed in conversation. The user skipped a formal design review and asked for the build to start with the control center.

## Intent

The user wants to edit 3D models in Blender by talking now, and by looking later. Blender stays the only thing on screen; there is no AI chat window. LLM 3D editing today works only from the prompt. This system also tells the model what the user is focused on, so it edits only that part. Today that signal is the selection; later it will be eye gaze. The idea comes from SketchGPT, which used a sketch as a second channel to the LLM. Here the second channel is gaze.

## Goals (this stage: voice + MCP)

- The user holds a key and speaks a command, and Blender changes within a few seconds.
- Messy speech is cleaned into one clear instruction before the editing model sees it.
- Edits are scoped to what the user is focused on. Selection stands in for gaze.
- A control center (dashboard) shows every component and installs each one when asked. It connects them and shows when the workflow is ready. It should feel like a polished plugin tool, not bare scripts.

## Non-goals (this stage)

- Eye tracking. We only keep slots for it: the focus field, word timestamps, and the bridge add-on.
- Hands-free or wake-word activation.
- Distribution to other people. This is for personal use on this Mac.

## Anti-goals

- Any chat window to type or talk into.
- Asking the user for an API key.
- Installing anything, or spending model usage, without the user clicking for it.
- A refiner that adds intent the user did not say.

## Architecture

```
hold key → mic → 1 Speech-to-text (local) → 2 Refiner (fast LLM, no tools)
       → 3 Editor (Claude Code or Codex, headless, Blender tools only) → MCP → Blender
Bridge add-on (ours) ⇄ app: selection, status line in the viewport, gaze later
```

| Component | Decision |
|---|---|
| Blender | 5.2 LTS, the newest as of 2026-09-15. The official MCP needs 5.1 or newer. Install with `brew install --cask blender`. Launch with `--online-mode`, because the official add-on only auto-starts when online access is on. |
| Blender MCP add-on | Blender Lab "MCP" extension v1.0.3, id `mcp`, with `blender_version_min` 5.1.0. It comes as the release asset `mcp-1.0.3.zip` and installs with `Blender --command extension install-file -r user_default -e <zip>`. It listens on `localhost:9876`. Messages are null-byte-delimited JSON of the form `{"type":"execute","code":…,"strict_json":bool}`. |
| Blender MCP server | Blender Lab `blender-mcp`: package in `mcp/`, entry point `blender-mcp = blmcp:main`, Python ≥ 3.10. It comes as the release asset `blender-1.0.3.mcpb`, a zip of `mcp/` that keeps its `pyproject.toml`. It is extracted into the app folder and run with `uv run --directory <dir> blender-mcp`, with env `BLENDER_PATH`. |
| Bridge add-on (ours) | Blender extension `gazevoice_bridge` on `127.0.0.1:9877`, using newline-delimited JSON. Commands: `ping`, `selection`, `status` (text in the viewport). Gaze will be added later. |
| Speech-to-text | Local MLX engines in a venv the app manages (Python 3.12 via uv). The default is Parakeet: `parakeet-mlx` with `mlx-community/parakeet-tdt-0.6b-v2`. The alternative is Whisper: `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`. Both get compared on the user's own voice later. |
| Microphone | ffmpeg avfoundation (already installed) lists devices and makes test recordings. |
| Brain | The local `claude` CLI is tried first, then `codex`. Each uses whatever login it already has, subscription or key. The app never prompts for a key. Detection uses `claude auth status` (JSON; exit 0 means logged in; `authMethod`) and `codex login status`. |
| Headless Claude | `-p`, `--tools ""`, `--strict-mcp-config` plus `--mcp-config`, `--allowedTools mcp__blender`, our own system prompt, `--setting-sources project`, and `--disable-slash-commands`. Never `--bare`, which ignores the subscription login. |
| Refiner | Uses the same brain CLI's fastest model in its own session, with no tools. It drops fillers and false starts and applies self-corrections. It fixes Blender terms and object names from the scene's vocabulary. It never adds intent. It keeps "this/that/here" and their timestamps. "Undo" and "stop" bypass the editor. |
| Control center | A local web dashboard served by a Python server that uses only the standard library, on `127.0.0.1:8977`. It has a card per component (status, details, Install/Connect/Test), a workflow strip, live job logs, and settings. |

## Constraints

- macOS on Apple Silicon (M4, 16 GB). Our code needs Python 3.12 or newer. The control center uses only the standard library, so it needs nothing installed to run.
- Novo's Claude Code guard rejects payloads containing LLM-vendor SDK imports or installs, API-key variable names, or API hostnames. Our code reaches models only through the `claude` and `codex` commands.
- Installs, downloads and model calls run only when the user clicks for them.
- Anthropic does not allow third-party products to offer claude.ai login without approval, so this stays for personal use only.

## Phasing

1. **Control center** (plan `docs/superpowers/plans/2026-10-18-control-center.md`): dashboard, detection, installers, connection tests, a full workflow check, and the bridge add-on (ping, selection, status line).
2. **Voice loop:** push-to-talk, speech-to-text, refiner, editor session, viewport statuses, undo and stop by voice, per-command logs, and Start/Stop in the dashboard.
3. **Codex as editor, scoping check, comparison:** Codex as the editor backend, a scene-diff check that only the focused object changed, and the speech-to-text comparison.
4. **Gaze.**
