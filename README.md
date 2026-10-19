# Gaze-Voice-3D-Editing

GazeVoice lets you edit Blender scenes by talking now, and by looking later, without an AI chat window.

- **Voice:** your speech is transcribed on your Mac and cleaned up into one clear instruction. Claude Code (or Codex) then edits Blender through Blender's official MCP server.
- **Focus:** each command carries what you're focused on, so edits stay scoped to it. For now that's your selection; later it will be your gaze.

**Status:** two stages are built:

- **The control center:** a local dashboard that installs each piece, connects them and checks that the whole workflow is ready.
- **The voice loop:** hold a key in Blender, say what you want, let go, and Blender changes. The chain runs hold-to-talk → speech-to-text → cleanup → editor → one undo step.

Gaze comes later. See [the design](docs/superpowers/specs/2026-10-18-gazevoice-design.md), [the control center plan](docs/superpowers/plans/2026-10-18-control-center.md) and [the voice loop plan](docs/superpowers/plans/2026-10-19-voice-loop.md).

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
4. **GazeVoice Bridge:** *Install add-on* adds our small Blender add-on (v0.2.0). It gives Blender the hold-to-talk key and a GazeVoice tab in the sidebar, shares your selection, and shows a status line in the viewport. If you installed an older version, the unit says *Update add-on*. Restart Blender after installing or updating.
5. **Speech-to-text:** *Install* sets up Parakeet (or Whisper, chosen in Routing) in its own Python environment and downloads the model. *Test* transcribes a clip spoken by macOS.
6. **Microphone:** *Test* records 3 seconds. If it hears silence, allow the app you started GazeVoice from in System Settings › Privacy & Security › Microphone.
7. **Claude Code / Codex:** detected automatically, using whatever login they already have. GazeVoice never asks for an API key. *Test* and *Connect to Blender* each spend a little of your plan.
8. **Check everything** runs every test from left to right and lights up the signal path.

## Talk to Blender

1. Get every unit ready. Until then, the **Voice** panel lists exactly what's missing.
2. Press **Start voice**. This:
   - connects to Blender;
   - opens the microphone;
   - loads the speech model;
   - starts two Claude Code sessions, a quick one that cleans up what you said and one that edits Blender.

   The first start takes a while because the model loads. Warming up the brain uses a little of your plan.
3. In Blender, select what you mean. Then hold **⌥ Space** (Alt+Space), say it, and let go. For example:
   - "make this twice as tall"
   - "bevel the edges a little"
   - "add an array modifier with five copies"
4. Watch the status line at the bottom of the viewport, or the **GazeVoice** tab in the sidebar (press N). It goes Listening → Transcribing → Understanding → Working → ✓. The dashboard shows each command with:
   - what was heard;
   - what it was cleaned up to;
   - what changed;
   - how long each step took.
5. Every command that changes the scene becomes **one undo step**, named "GazeVoice: …", so Ctrl+Z works as usual. Commands that only look something up don't add one.

**Saying "stop", "undo" or "redo"** skips the AI:

- **"Stop"** is instant. "Cancel", "wait", "never mind" and "no, stop it" work too. It ends the running edit and drops anything queued. If the edit already changed something, that change is its own undo step.
- **"Undo"** and **"redo"** wait until the edit in progress finishes, so they never land in the middle of one.
- While you're holding the key, Esc cancels. So does switching away from Blender.
- While voice is off, the key does nothing except say so in Blender's status bar.

**More ways to talk:**

- *Talk* in the sidebar starts listening on the first click and sends on the second.
- The dashboard has *Hold to talk* and a box to **type a command**. Typing is handy for testing without a mic.
- To use a key other than ⌥ Space, change "GazeVoice: Hold to Talk" in Blender's Preferences › Keymap. The dashboard and Blender's hints follow your choice.
- If ⌥ Space does nothing, another app (Alfred, Raycast, a chat app's quick launcher) probably owns that shortcut system-wide. Change one of the two.
- Use the key over the 3D viewport. In Blender's Text Editor or Python Console, ⌥ Space types instead.

**Settings:**

- **New conversation** makes the editor forget earlier commands, so "it" starts fresh.
- Routing picks the **Editing model** (Default is your Claude Code default) and the **Cleanup model** (Haiku is quickest). Stop and start voice to apply changes.
- **Codex** works as the brain too, but it's experimental and slower, about 10 seconds or more per command.

**Privacy:**

- The mic is open only while voice is on.
- Audio stays in memory. Only the commands you speak are saved, in GazeVoice's own logs folder.
- Claude Code runs with session saving off, so your commands don't appear in its history.
- Codex keeps its own history of the commands it handled.

### What to expect

On an M4 MacBook Air, measured outside Blender:

| Step | Time |
|---|---|
| Speech-to-text, once loaded | well under a second |
| Cleanup (Haiku) | about 2–5 s |
| Editor start (warm-up and connecting to the Blender MCP server) | about 6 s |
| Editor reply for a lookup | a few seconds |

Real edits take longer with bigger models.

## Where things live

| What | Where |
|---|---|
| Settings, downloads, the MCP server, the speech environment | `~/Library/Application Support/GazeVoice/` (override with `GAZEVOICE_HOME`) |
| The last good microphone test | `…/GazeVoice/state/microphone.json` (delete it to make the mic show "Installed" again) |
| Speech models | `~/.cache/huggingface/` (or `$HF_HOME` if you set it) |
| Spoken commands (audio.wav + record.json each) | `…/GazeVoice/logs/commands/<time>-<n>/` |
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
| `gazevoice/voice/` | The voice loop: mic ring buffer, speech service, brain sessions, prompts, orchestration |
| `gazevoice/server/static/` | The dashboard: plain HTML, CSS and JS |
| `blender_addon/gazevoice_bridge/` | Our Blender add-on |
| `DESIGN.md` | The dashboard's visual system |
| `PRODUCT.md` | What the product is for |
