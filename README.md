# Gaze-Voice-3D-Editing

GazeVoice lets you edit Blender or Unity scenes by talking now, and by looking later, without an AI chat window.

- **Voice:** your speech is transcribed on your Mac and cleaned up into one clear instruction. Claude Code, Codex or a configured API provider edits the selected host through MCP.
- **Focus:** each command carries what you're focused on, so edits stay scoped to it. For now that's your selection; later it will be your gaze.

**Status:** two stages are built:

- **The control center:** a local dashboard that installs each piece, connects them and checks that the whole workflow is ready.
- **The voice loop:** hold to talk in Blender or Unity, say what you want, and release to edit. The chain runs hold-to-talk → speech-to-text → cleanup → editor → one undo step.

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
4. **GazeVoice Bridge:** *Install add-on* adds our small Blender add-on (v0.3.0). It gives Blender the hold-to-talk key and a GazeVoice tab in the sidebar, shares your selection, and shows a status line in the viewport. If you installed an older version, the unit says *Update add-on*. Restart Blender after installing or updating.
5. **Speech-to-text:** *Install* sets up Parakeet (or Whisper, chosen in Routing) in its own Python environment and downloads the model. *Test* transcribes a clip spoken by macOS.
6. **Microphone:** *Test* records 3 seconds. If it hears silence, allow the app you started GazeVoice from in System Settings › Privacy & Security › Microphone.
7. **Claude Code / Codex:** detected automatically, using whatever login they already have. GazeVoice never asks for a key for these two. *Test* and *Connect to Blender* each spend a little of your plan.
8. **DeepSeek, Qwen or a custom API** (optional): see [Use DeepSeek, Qwen or another API](#use-deepseek-qwen-or-another-api).
9. **Check everything** runs every test from left to right and lights up the signal path.

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
5. **What you say gets written up properly.** Ask for something new, like "make a bird model", and the cleanup step writes the editor a detailed brief: real proportions and size, separate named parts, enough geometry and smooth shading, materials, and a tidy group. That way you get a finished-looking model instead of a few basic shapes. Exact edits like "make it red" or "play the animation" go through exactly as you said them. Open **Prompt sent to the editor** under a command to read the brief. A detailed build can take a minute or two; "stop" still ends it at once.
6. Every command that changes the scene becomes **one undo step**, named "GazeVoice: …", so Ctrl+Z works as usual. Commands that only look something up don't add one.

**Saying "stop", "undo" or "redo"** skips the AI:

- **"Stop"** is instant. "Cancel", "wait", "never mind" and "no, stop it" work too. It ends the running edit and drops anything queued. If the edit already changed something, that change is its own undo step.
- **"Undo"** and **"redo"** wait until the edit in progress finishes, so they never land in the middle of one.
- While you're holding the key, Esc cancels. So does switching away from Blender.
- While voice is off, the key does nothing except say so in Blender's status bar.

## Talk to Unity

The Unity target reuses the microphone, speech-to-text, cleanup, command queue and dashboard. It includes its own MCP server and a local GazeVoice Editor bridge.

1. In the control center's Routing unit, choose **Unity**. The rack then shows the Unity units.
2. Press **Set up Unity** on the Unity unit. It asks first, listing what it's about to do, then does whatever is still missing:
   - installs Unity Hub and the Unity CLI with Homebrew;
   - downloads the latest Unity LTS for Apple silicon (several GB);
   - makes a new project at `~/Unity/GazeVoice`, or keeps the one you picked;
   - copies the GazeVoice package into the project's `Packages` folder;
   - opens the project in Unity.

   **You sign in once.** Unity's free Personal license can only be activated by signing in to Unity Hub, so GazeVoice can't do that step. Setup opens Unity Hub as soon as it's installed, so you can sign in and add the Personal license while Unity downloads. If Unity still has no license when it's time to make the project, setup stops and says so. Sign in, press **Set up Unity** again, and it carries on from where it stopped.
3. Start voice. Then, in Unity, select what you mean, hold **⌥ Space**, say it, and let go. Your selection is the focus. Typed commands in the dashboard work too.

The key works anywhere in the Editor, except in text fields, which type it instead. To change it, look for "GazeVoice" in Edit › Shortcuts. **Window › GazeVoice** has a Talk control and shows the status line. Switching away from Unity cancels a hold. The package listens on `127.0.0.1:9878`.

Prefer your own project? **Choose…** on the Unity project unit picks it, and **New project** makes another one. Nothing goes into a project until you press **Set up Unity** or **Add to project**, and **Remove from project** takes the package out again. After you update GazeVoice, the bridge unit offers **Update** if the package changed.

The MCP server is built into GazeVoice, so there's nothing separate to download. To use another stdio Unity MCP server, set **Unity MCP command** and **Unity MCP arguments** (a JSON string array) in Routing. The external server must provide its own Unity scene tools and implement the standard MCP stdio protocol.

**What the editor can do.** Nothing in the Unity Editor is out of reach:

- **`execute_csharp`** runs any C# right away, with no recompile, the way Blender's MCP server runs Python. It uses the compiler inside your Unity editor, so there's nothing extra to install.
- **`write_script`** writes a real script into the project, waits while Unity compiles and reloads, and hands back any compile errors so the editor can fix them.
- **`add_package`** adds what a job needs, such as ProBuilder.
- **`read_console`**, **`get_compile_status`** and **`capture_scene_view`** let the editor check its own work.
- Everyday edits have their own tools: inspecting, selecting, color, transform, duplicate, delete and creating primitives.

For animals, people and other organic shapes, the editor can build the model in Blender, if it's installed, and import it into Unity. *Test* on the bridge unit proves C# really runs in your Unity.

**Undo.** Each command is one undo step, even if scripts recompile partway through. A few things can't be undone by voice: files (scripts, imported models, added packages) stay in the project, and a change made in C# without Unity's Undo calls can't be taken back. External MCP tools that change scenes without Unity's Undo API can't be made undoable either.

**Save often.** Like Blender's MCP server, this runs AI-written code inside Unity without guards. C# can reach your files and the network, and an endless loop hangs Unity, so "stop" can't end it.

**More ways to talk in Blender:**

- *Talk* in the sidebar starts listening on the first click and sends on the second.
- The dashboard has *Hold to talk* and a box to **type a command**. Typing is handy for testing without a mic.
- To use a key other than ⌥ Space, change "GazeVoice: Hold to Talk" in Blender's Preferences › Keymap. The dashboard and Blender's hints follow your choice.
- If ⌥ Space does nothing, another app (Alfred, Raycast, a chat app's quick launcher) probably owns that shortcut system-wide. Change one of the two.
- Use the key over the 3D viewport. In Blender's Text Editor or Python Console, ⌥ Space types instead.

**Settings:**

- **New conversation** makes the editor forget earlier commands, so "it" starts fresh.
- In Routing, **Editing** and **Cleanup** each pick their own source:
  - Auto (Claude Code when it's logged in, otherwise Codex);
  - Claude Code;
  - Codex;
  - any API provider you've added.
- For Claude Code, pick a model for each step:
  - the choices are Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5;
  - "Default" lets Claude Code choose (GazeVoice doesn't use your personal Claude Code settings);
  - they need a recent Claude Code, so run `claude update` if a choice fails to start.
- Next to each model, pick how much it **thinks**: Low, Medium, High, Extra high or Max. More thinking can make big builds better, but each command takes longer. Haiku 5.5 keeps Cleanup quick.
- Each command on the dashboard shows which model did the edit. Stop and start voice to apply changes.
- **Codex** works as the brain too, but it's experimental and slower, about 10 seconds or more per command.

**Privacy:**

- The mic is open only while voice is on.
- Audio and command records are saved in GazeVoice's local logs folder.
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

## Use DeepSeek, Qwen or another API

Either step can run on a model you reach with your own API key instead of Claude Code.

1. **Add the key.** In the rack, under Brain, press **Add API key** on DeepSeek or Qwen, paste your key and press Save.
   - Qwen also asks for your account's region. Alibaba keys only work in their own region. If Model Studio gives you a Workspace ID, add it too; regions without a shared address need it.
   - For any other service, press **Add provider** on Custom provider. Give it a name, its address and your key. The address must be an OpenAI-compatible endpoint: the part before `/chat/completions`, such as `https://…/v1`.
2. **Pick it in Routing.** Set Editing, Cleanup or both to that provider.
   - Pick a model. The list comes from the provider; if it doesn't publish one, type the model name.
   - DeepSeek and Qwen also get a thinking menu. Cleanup starts with thinking Off, so it stays quick.
   - Editing needs a model that can call tools; Cleanup doesn't.
3. **Test** on the provider checks your key and the models you picked, for a fraction of a cent. Then press Start voice.

Good to know:

- **The key goes to your Mac's Keychain** (service "GazeVoice"). The dashboard shows only its last four characters. **Remove** deletes it, and any step using that provider goes back to Auto.
- **Your commands go to that provider:** with an API provider, what you say, what's selected and the scene's object names go to that company's servers. The Voice panel says which one.
- **Errors say what to do:** a rejected key, an empty account, a model that can't call tools or a provider that can't be reached each get their own message.

## Where things live

| What | Where |
|---|---|
| Settings, downloads, the MCP server, the speech environment | `~/Library/Application Support/GazeVoice/` (override with `GAZEVOICE_HOME`) |
| The last good microphone test | `…/GazeVoice/state/microphone.json` (delete it to make the mic show "Installed" again) |
| Speech models | `~/.cache/huggingface/` (or `$HF_HOME` if you set it) |
| Spoken commands (audio.wav + record.json each) | `…/GazeVoice/logs/commands/<time>-<n>/` |
| Blender add-ons | Blender's `extensions/user_default/` (`mcp`, `gazevoice_bridge`) |
| API keys for DeepSeek, Qwen and custom providers | macOS Keychain, service "GazeVoice" |

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
| `unity_package/` | Unity Editor package and the matching local bridge protocol |
| `DESIGN.md` | The dashboard's visual system |
| `PRODUCT.md` | What the product is for |
