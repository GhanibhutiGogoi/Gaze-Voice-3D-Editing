# Gaze-Voice-3D-Editing

GazeVoice lets you edit Blender scenes by talking now, and by looking later, without an AI chat window.

- **Voice:** your speech is transcribed on your Mac and cleaned up into one clear instruction. Claude Code (or Codex) then edits Blender through Blender's official MCP server.
- **Focus:** each command carries what you're focused on, so edits stay scoped to it. For now that's your selection; later it will be your gaze.

**Status:** stage 1, the **control center**, is built. It's a local dashboard that installs each piece, connects them, and checks that the whole workflow is ready. The voice loop itself (push-to-talk → speech-to-text → refiner → editor) comes next. See [the design](docs/superpowers/specs/2026-10-18-gazevoice-design.md) and [the plan](docs/superpowers/plans/2026-10-18-control-center.md).

## Start the control center

```bash
python3 -m gazevoice            # opens http://127.0.0.1:8977
python3 -m gazevoice --port 9000 --no-browser
```

It needs only macOS on Apple silicon and Python 3.12 or newer. Nothing gets installed just to open it.

## Setting up, unit by unit

Every unit shows its real state on this Mac. Nothing installs, records audio or uses your Claude/Codex plan until you press its key. Keys that need consent ask first, right on the unit.

1. **Blender:** *Install Blender* (Homebrew), then *Launch*. Launch turns on online access, which the MCP add-on needs to start.
2. **Blender MCP add-on:** *Install add-on* downloads Blender Lab's official add-on (v1.0.3). After installing, quit Blender and *Launch* it again.
3. **Blender MCP server:** *Install server* downloads Blender Lab's server and installs its packages with `uv`. *Test* lists its tools and, if Blender is open, reads the scene.
4. **GazeVoice Bridge:** *Install add-on* adds our small Blender add-on. It shares your selection and shows a status line in the viewport.
5. **Speech-to-text:** *Install* sets up Parakeet (or Whisper, chosen in Routing) in its own Python environment and downloads the model. *Test* transcribes a clip spoken by macOS.
6. **Microphone:** *Test* records 3 seconds. If it hears silence, allow the app you started GazeVoice from in System Settings › Privacy & Security › Microphone.
7. **Claude Code / Codex:** detected automatically, using whatever login they already have. GazeVoice never asks for an API key. *Test* and *Connect to Blender* each spend a little of your plan.
8. **Check everything** runs every test from left to right and lights up the signal path.

## Where things live

| What | Where |
|---|---|
| Settings, downloads, the MCP server, the speech environment | `~/Library/Application Support/GazeVoice/` (override with `GAZEVOICE_HOME`) |
| The last good microphone test | `…/GazeVoice/state/microphone.json` (delete it to make the mic show "Installed" again) |
| Speech models | `~/.cache/huggingface/` (or `$HF_HOME` if you set it) |
| Blender add-ons | Blender's `extensions/user_default/` (`mcp`, `gazevoice_bridge`) |

## Good to know

- **Local only:** the dashboard listens on 127.0.0.1 only. Requests from other sites are refused.
- **Save often:** Blender's MCP server runs AI-written Python inside Blender without guards.
- **Personal use:** running on your own Claude login is fine for you. Anthropic doesn't allow products for other people to use claude.ai logins without approval.

## Develop

```bash
python3 -m unittest discover -s tests -t . -v
```

| Path | What's there |
|---|---|
| `gazevoice/` | The control center (Python standard library only) |
| `gazevoice/components/` | One file per unit |
| `gazevoice/server/static/` | The dashboard: plain HTML, CSS and JS |
| `blender_addon/gazevoice_bridge/` | Our Blender add-on |
| `DESIGN.md` | The dashboard's visual system |
| `PRODUCT.md` | What the product is for |
