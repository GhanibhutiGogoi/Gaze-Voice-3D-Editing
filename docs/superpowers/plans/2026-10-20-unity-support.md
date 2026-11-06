# Unity Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Talk to the Unity Editor the way GazeVoice talks to Blender:
- hold a key in Unity, speak, and the scene changes, scoped to the selection;
- each command is one undo step, and stop, undo and redo work by voice;
- the dashboard sets it all up and checks it.

Blender keeps working exactly as it does now.

**Architecture:**
- A **target** (`gazevoice/targets/`) describes one app GazeVoice edits. It holds:
  - the rack units;
  - the bridge port;
  - the MCP server;
  - the prompts and the focus wording;
  - the limits;
  - the last stage of the strip.

  Today's Blender values become the `BLENDER` target, and `UNITY` is the second. The rest of the app reads the active target instead of hard-coded Blender names.
- The **GazeVoice Unity Bridge** (`com.gazevoice.bridge`) is a new editor-only UPM package written in C#. It speaks the same newline-delimited JSON protocol as `blender_addon/gazevoice_bridge/protocol.py`, on 127.0.0.1:9878, so `VoiceService` barely changes.
- The editor brain drives Unity through **Unity's own MCP server**: `unity mcp` from the Unity CLI, with the `com.unity.pipeline` package in the project. This depends on the spike. If the spike fails it, the fallback is MCP for Unity (CoplayDev).
- The dashboard gets an **"Edit in: Blender | Unity"** switch. One app is live at a time.

**Tech Stack:**
- Python 3.12+ standard library for the control center (`plistlib` reads Unity's Info.plist files).
- C# for the Unity 6 Editor:
  - a UPM package;
  - Newtonsoft JSON from Unity's own `com.unity.nuget.newtonsoft-json`;
  - Unity Test Framework EditMode tests run in batch mode.
- The Unity CLI (`unity`, 1.0 beta) and `com.unity.pipeline` (0.9, experimental).
- `claude`, `codex` and the existing API tool loop.

**Spec:** there's no separate spec. You asked for the plan directly on 2026-10-20, and the design is sections 1 and 2 below. The base design is `docs/superpowers/specs/2026-10-18-gazevoice-design.md`, and the voice loop is described in `docs/superpowers/plans/2026-10-19-voice-loop.md`.

## Global Constraints

- **Blender stays exactly as it is.**
  - Every default is the Blender target.
  - All 403 current tests pass after every task. Phase 1 only adds tests.
  - The Blender add-on, its protocol and port 9877 don't change.
- **Consent.** Nothing installs, downloads, records or calls a model unless you click for it. That covers:
  - Unity Hub and the Unity CLI, both from Homebrew;
  - every change to a project's `Packages/manifest.json`;
  - the brain's "Connect to Unity" test.

  Model calls keep `uses_plan=True` and their cost labels.
- **Your project is touched only with consent.**
  - Packages go into the one project you picked in the dashboard, after a confirm that names the project path and the exact manifest line.
  - GazeVoice first copies the old file to `Packages/manifest.json.gazevoice-backup`.
  - It refuses to write if the file changed since the dashboard read it.
  - Each package unit can remove its line again.
- **Models.**
  - Models are reached only through `claude`, `codex` and the DeepSeek, Qwen and custom providers.
  - No other vendor SDKs, keys or API hosts appear in any file, tests included.
  - The Unity MCP server must not depend on a hosted model, a Unity AI subscription, AI points or a Unity Cloud project.
- **Keys and accounts.**
  - GazeVoice never asks for or stores Unity credentials. You sign in to Unity in Unity Hub, and with `unity auth login` in Terminal if the spike shows the CLI needs it, the way you run `claude auth login`.
  - The pipeline's bearer token stays in its file. GazeVoice reads only the port number from it.
- **Control center.** It stays standard-library Python, binds 127.0.0.1 only, and keeps the `X-GazeVoice: 1`, Host and Origin guards.
- **Unity side.**
  - Editor only: every asmdef has `"includePlatforms": ["Editor"]`, so nothing ships in a game build.
  - The bridge listens on 127.0.0.1:9878 only, and never in batch mode. Tests use port 0.
- **Versions.**
  - Unity 6 (`6000.0`) or newer is required. Unity 6.3 LTS (`6000.3`) is what we test on.
  - The Unity CLI and MCP package are pinned to the versions the spike verified (S0).
- **Telemetry** is turned off wherever it can be, through the MCP server's environment in `server_command` (S10).
- **Writing.** UI strings, status lines and docs stay in plain language, like the rest of the app.
- **Git.** Commit once per task, once you've said to start building. Never push unless asked.

## Review Focus

1. **A script compile in the middle of a command.**
   - Example: the AI wrote a MonoBehaviour, so Unity reloads its scripts. The bridge's socket closes for a few seconds and comes back with a new boot token.
   - Expected: voice keeps "Working…", reconnects, and still adds exactly one undo step for the whole command.
   - Tests: Task 8 `TheUndoGroupSurvivesAScriptReload`; Task 15 `test_a_reload_mid_edit_still_leaves_one_undo_step`.
2. **Unity in the background.**
   - Example: you type a command in the dashboard, or switch away from Unity. Unity throttles itself when it isn't frontmost, so edits crawl.
   - Expected: the Voice panel says why, instead of looking stuck.
   - Tests: Task 7 `PingAndPollReportFocus`; Task 15 `test_unity_in_the_background_is_named_in_the_reason`.
3. **Play mode starts while a command runs.**
   - The bridge refuses the undo step.
   - Expected: the record says the change is lost when Play mode stops. It doesn't say "Done".
   - Tests: Task 8 `UndoPushIsRefusedInPlayMode`; Task 15 `test_play_mode_during_an_edit_is_reported`.
4. **Two Unity editors are open, or the bridge answers for a different project.**
   - Expected: the bridge unit names the project that's connected and doesn't show Ready.
   - Tests: Task 7 `PingReportsTheProject`; Task 11 `test_bridge_from_another_project_is_not_ready`.
5. **A package line that Unity couldn't resolve** (offline, or a wrong version).
   - The manifest has the line, but `packages-lock.json` doesn't have the package.
   - Expected: the unit says Unity couldn't add it and where to look. It doesn't show "Installed".
   - Test: Task 13 `test_unresolved_package_is_an_error`.

---

## Decisions and assumptions

### D1. What Unity work GazeVoice should be good at (recommendation, Q4)

In priority order:

1. **Scene building and layout.** Placing, arranging, grouping, duplicating in patterns, setting things on the ground.
2. **GameObjects, components and prefabs.** Adding and tuning colliders, Rigidbodies, lights and audio; making and placing prefabs.
3. **Materials and lighting.** The project's Lit material, colors and surfaces, and light setups and mood.
4. **C# behaviours.** For example, "make the door open when I get close". Unity can do this and Blender can't. Scripts follow strict rules (2.4).
5. **Models.** ProBuilder blockouts, hard-surface props, and placing models the project already has. For now, detailed organic models are clearly labeled stand-ins; see 2.7.

### D2. One app at a time (assumption, Q3)

The dashboard gets **"Edit in: Blender | Unity"**. Switching restarts voice, as a Routing change already does.

Why one app at a time:
- **Sessions:** each brain session is tied to one MCP server when it starts. Following the key would mean two warm editor sessions and two MCP servers, which doubles the warm-up.
- **Memory:** the Mac has 16 GB, and it may also be running Unity (2–4 GB), the speech model and Blender.
- **Focus:** "this" stays unambiguous, because the focus always comes from the app voice is on.
- **The other app:** holding the key there does nothing and says "Voice is off", since that bridge isn't being polled.

The design leaves room for more. `VoiceService` takes one target now. A later "follow the key" mode would poll both bridges and keep a session per target.

### D3. The Unity MCP server (recommendation, Q1)

The recommendation is **Unity's CLI MCP (`unity mcp` plus `com.unity.pipeline`)**, provided the spike passes its gates. Otherwise it's MCP for Unity (CoplayDev). The reasons are in section 1.

### Other assumptions

- **A1:** Unity 6.3 LTS, the Apple silicon build, with a Personal license signed in through Unity Hub (Q2).
- **A2:** Edits, undo and redo are refused in Play mode, with a clear status line (Q5).
- **A3:** Hold-to-talk is ⌥ Space in Unity too (Q9).
  - It works anywhere in the Editor, except in text fields, which type it instead.
  - You can rebind it in Edit › Shortcuts under "GazeVoice".
- **A4:** The bridge package is copied into GazeVoice's app folder, and the project refers to it with one `file:` line. It isn't copied into the project's `Packages/` folder (Q6).
- **A5:** New files the AI makes go under `Assets/GazeVoice/` (Q7).
  - Scripts and prefabs are saved there when they're needed or asked for.
  - Materials and meshes stay in the scene unless you ask to save them.
  - Unity can't undo file writes, so voice undo leaves these files, and the command's record lists them.
- **A6:** The limits match Blender's: 150 steps and 10 minutes per command. Revisit them after the spike measures reload times (Q8).
- **A7:** The status line is a Scene view overlay. Nothing shows in the Game view.
- **A8:** Installing Unity editors and signing in happen in Unity Hub. GazeVoice only links out.

---

## 1. Unity MCP options (checked on the web, 2026-10-20 and 21)

| | **Unity CLI MCP** (`unity mcp` + `com.unity.pipeline`) | Unity AI Assistant MCP (`com.unity.ai.assistant`) | **MCP for Unity** (CoplayDev/unity-mcp) | AI Game Developer (IvanMurzak/Unity-MCP) |
|---|---|---|---|---|
| Unity | 6.0 LTS or newer | 6000.0 or newer | 2021.3 or newer (6.x tested) | 2022.3 or newer |
| Install | CLI: `brew install --cask unity-cli`. Package: `unity pipeline install` or one manifest line, per project | Package; a relay app in `~/.unity/relay/` | Git URL `…/unity-mcp.git?path=/MCPForUnity#v10.3.0`, OpenUPM or Asset Store, per project | OpenUPM scoped registry, plus a NuGet resolver that puts DLLs in `Assets/Plugins/NuGet` (most ship in game builds) |
| Server, transport | `unity mcp` over stdio. The CLI talks to an HTTP server inside the Editor on 127.0.0.1, ports 7800–7849, with a bearer token in `<project>/Library/Pipeline/.unity-pipeline-port` that survives domain reloads | stdio to the relay, then local IPC. You approve the first connection in Project Settings | Python (FastMCP): `uvx --from mcpforunityserver mcp-for-unity --transport stdio`. It reaches Unity over TCP 6400 in stdio mode, or a WebSocket hub in HTTP mode (the docs' default) | A separate .NET binary downloaded into `Library/`, reached over SignalR on a per-project port. Agents must use the server Unity runs, or ports clash |
| Run any C# | **Yes.** `script eval` (Roslyn) and `script run` (compiles a .cs in memory, with no domain reload). `script create` writes a file, which compiles on `script recompile` with a domain reload | `Unity_RunCommand` (C#, some namespaces blocked) | `execute_code` (Roslyn or CodeDom), but in the off-by-default `scripting_ext` group. Plus 30 fixed core tools: `manage_gameobject`, `manage_scene`, `manage_material`, `manage_script`, `read_console`, `batch_execute`… | `script-execute` (Roslyn, main thread, 120 s limit) |
| Undo | **Designed in.** Each scene command is one Ctrl+Z step (`AuthoringUndoScope`). `job batch` is one undo step and rolls back on failure. Asset, package and settings writes can't be undone | Not documented | Mixed. Creating and modifying record undo. Deleting uses `DestroyImmediate`, which can't be undone. Reparenting isn't recorded, and `batch_execute` doesn't group. `execute_code` records nothing | `rql-mutate` groups undo, **but also saves changed scenes and assets to disk on its own**. `script-execute` records nothing |
| License, activity | Unity Terms of Service. CLI 1.0.0-beta.13 (2026-10-07), package 0.9.0-exp.1 (2026-09-22). Experimental, with releases every few weeks | Pre-release (2.21.0-pre.1, 2026-10-15). Its MCP server is now **deprecated** in favor of the CLI | MIT. 15.2k stars, v10.3.0 (2026-10-04), about 40 releases in 2026 | Apache-2.0. 4.5k stars, 1.3.0 (2026-10-20). Version 1.0 (2026-10-15) removed most fixed tools |
| Account, AI, phone-home | Free and separate from Unity AI; no hosted model. Unity's setup includes `unity auth login`; whether MCP needs it is unverified. Telemetry unverified | Needs a Unity Cloud-connected project and a Unity AI trial or subscription | No account. **Telemetry is on by default** (tool names, timings, OS) until `DISABLE_TELEMETRY=true`. A daily update check has no off switch. Open macOS issues with an unfocused Unity and around reloads (#1265, #1207) | Defaults to **Cloud** mode (ai-game.dev, OAuth). The local "Custom" mode is free. Paid tiers add hosted AI |

### Recommendation: Unity's CLI MCP, checked by the spike first

- **Official and free.** It's Unity's own, with no AI subscription and no hosted model. That makes it the same kind of choice as Blender Lab's official server.
- **Runs any C#.** It runs C# without a domain reload (`script run` and `script eval`). That's the nearest thing to `execute_blender_code`.
- **Matching undo.** Its undo model already matches ours: one undo step per command, and batches that roll back. GazeVoice collapses a whole voice command into one step on top of that.
- **stdio.** All three brains can use it unchanged, including the API loop's `StdioMcpClient`.
- **One install.** The same `unity` binary also lists editors and opens projects, so one Homebrew install covers detection and the server.
- **The catch.** It's experimental: a beta CLI and an `-exp` package that change quickly and haven't been tested much. That's why the spike has gates, versions are pinned, and there's a named fallback.

### Fallback: MCP for Unity (CoplayDev), pinned at v10.3.0

It's mature, MIT-licensed, and runs on `uv` like the Blender server. To use it, we'd:
- launch it with `DISABLE_TELEMETRY=true` and `UNITY_MCP_DISABLE_FOCUS_NUDGE=1`;
- turn on `scripting_ext` to get `execute_code`;
- tell the editor to delete and reparent through `execute_code` with Undo calls, because its own delete can't be undone.

### Not recommended

- **Unity AI Assistant's MCP** is deprecated, and it needs a Unity AI subscription or trial and a Cloud project.
- **IvanMurzak's** has four problems:
  - Cloud mode is the default;
  - it puts DLLs in `Assets/` that ship in builds;
  - it saves scenes by itself, which breaks "no saving unless asked" and our undo model;
  - its tool API changed six days ago.
- **The others** (CoderGamester/mcp-unity, AnkleBreaker, isuzu-shiranui, FunplayAI and more) are smaller, and none beats the two above on undo or C#.

### Sources (all accessed 2026-10-20 and 21)

- **Unity CLI:**
  - docs.unity.com/en-us/unity-cli: replace-mcp-server-unity-cli, unity-cli-reference and release-notes;
  - docs.unity3d.com/Packages/com.unity.pipeline@0.9: connectivity, commands/scripts, safety-and-mutations and the changelog.
- **AI Assistant:**
  - docs.unity3d.com/Packages/com.unity.ai.assistant@2.21/manual/integration/unity-mcp-overview.html;
  - unity.com/blog/unity-ai-mcp-how-to-get-started.
- **CoplayDev:**
  - github.com/CoplayDev/unity-mcp: releases, `Server/src/core/config.py`, `Editor/Tools/ExecuteCode.cs`, and issues #1090, #1207, #1265 and #1299;
  - coplaydev.github.io/unity-mcp.
- **IvanMurzak:**
  - github.com/IvanMurzak/Unity-MCP: releases, `Runtime/UnityMcpPlugin.Config.cs`, `Rql/RqlUnity.cs`, and issues #370, #463 and #829;
  - package.openupm.com/com.ivanmurzak.unity.mcp.
- **Editor facts:**
  - docs.unity3d.com/6000.3/Documentation/ScriptReference: ClutchShortcutAttribute, Overlay, Undo, ObjectChangeEvents, SessionState and HandleUtility.PickGameObject;
  - unity.com/releases/unity-6/support;
  - Unity's license activation and test-framework command-line pages.

---

## 2. Design

### 2.1 The target abstraction

| Blender-specific today | Moves to | Blender | Unity |
|---|---|---|---|
| Unit list (`components/__init__.py`), dashboard `GROUPS` | `Target.units` | blender, mcp_addon, mcp_server, bridge | unity, unity_project, unity_mcp_package, unity_mcp_server, unity_bridge |
| `BridgeClient()` port, ping's `"blender"` field | `bridge_port`, `version_key` | 9877, `"blender"` | 9878, `"unity"` |
| `server_command()`; `"blender"` in `write_mcp_config`, `codex_mcp_args`, `--allowedTools mcp__blender`, `require_mcp`, `blender_mcp()` | `server_command(ctx)`, `mcp_name`, `blocked_tools`, `api_tools` | `uv run … blender-mcp`, `"blender"`, (), () | `unity mcp …` (S2), `"unity"`, S2, S9 |
| `EDITOR_PROMPT`, `REFINER_PROMPT` | `editor_prompt`, `refiner_prompt` | editor.md, refiner.md | unity_editor.md, unity_refiner.md |
| Warm-up and its MCP check | `mcp_name` (the warm-up text is already generic) | | |
| `describe_focus` ("Blender didn't report a selection"); `_capture_context` keeps 3 keys | `name`; the focus keeps every key the bridge sends | names, mode | names, paths, scene, mode |
| `_BLENDER_TERMS` (the Whisper hint) | `vocabulary` | `BLENDER_TERMS` | `UNITY_TERMS` |
| Last strip stage, `STEPS`, `connect_blender` | `app_parts`, `edit_parts`, `check_steps`, `connect_action`, `connect_label`, `connect_prompt` | as today | 2.3 |
| `_NEEDED` (readiness) | `needed` | as today | 2.3 |
| `READ_ONLY_TOOLS`, `UNDO_NAME_BYTES`, `BUSY_WAIT`, `EDIT_SECONDS` | `read_only_tools`, `undo_name_bytes`, `busy_wait`, `edit_seconds`, `wait_when_unreachable` | as today, False | S2 (else every tool counts as a change), 120, 60 s, 600 s, True |
| "Blender isn't connected…" texts | `name`, `not_running`, `disconnected` | as today | 2.2 |

These stay shared:
- the mic, speech-to-text and the refiner and editor sessions;
- Routing, the providers and the Voice panel;
- the protocol and VoiceService's threads.

Blender's add-on never sends either of these protocol additions, so Blender is unaffected:
- **A refusal with a message.** `begin_edit`, `undo`, `redo` and `undo_push` may answer `{"ok": false, "error": "playing", "message": "…"}`. VoiceService then fails the command with that message. When `begin_edit` is the one refusing, this happens before the editor runs.
- **A busy `begin_edit`.** `begin_edit` may answer `busy` while Unity compiles. VoiceService waits up to `busy_wait`, the way undo already does.

How the refactor stays green:
- Every new parameter defaults to the Blender value.
- Module names that tests import (`STEPS`, `BRIDGE_PORT`, `EDITOR_PROMPT`, `blender_mcp`) stay, as names for the Blender values.
- No existing test is edited.

### 2.2 The Unity bridge package

The package lives in `unity_package/com.gazevoice.bridge/`, version 0.1.0. Its protocol is `protocol.py`'s, with these differences:

```
{"cmd": "ping"}       -> {"ok": true, "bridge": "0.1.0", "unity": "6000.3.7f1", "key": "⌥ Space",
                          "project": "/Users/you/Games/Glade", "playing": false, "compiling": false, "focused": true}
{"cmd": "selection"}  -> {"ok": true, "active": "Tower", "selected": ["Tower", "Eagle"],
                          "paths": ["World/Tower", "World/Eagle"], "scene": "Assets/Scenes/Main.unity", "mode": "edit"}
                          mode is "edit", "prefab" (Prefab Mode; scene is the prefab's path) or "play"
{"cmd": "poll", …}    -> as in Blender, plus "focused"
{"cmd": "begin_edit"} -> {"ok": true}, or {"ok": false, "error": "busy"} while compiling,
                          or {"ok": false, "error": "playing", "message": PLAYING}
{"cmd": "undo_push", …} -> {"ok": true, "pushed": true, "assets": ["Assets/GazeVoice/Scripts/Spin.cs"]}
PLAYING = "Unity is in Play mode. Stop playing first: changes made while playing are lost when it stops."
```

Everything else is identical: `status`, `scene_names`, `poll` ack and boot, and busy undo and redo. A shared case file proves it (Task 5).

**Hold-to-talk**
- The shortcut is `[ClutchShortcut("GazeVoice/Hold to Talk", KeyCode.Space, ShortcutModifiers.Alt)]` with no context, so it works anywhere in the Editor. Begin queues "down" and End queues "up".
- Text fields take the key first, so ⌥ Space still types in them.
- A hold is cancelled:
  - when Unity loses focus (`EditorApplication.focusChanged`);
  - after 60 s;
  - when a script reload cuts it off.
- **While voice is off** (no poll for 3 s), a press queues nothing. It shows "Voice is off. Turn it on in the GazeVoice dashboard." as a Scene view notification.
- **Talk button.** A "Talk" button in the overlay and the Tools › GazeVoice › Talk menu item both queue "toggle".
- **Key label.** The label comes from `ShortcutManager.instance.GetShortcutBinding` and is written with Mac symbols.

**Status line**
- It's `[Overlay(typeof(SceneView), "gazevoice-status", "GazeVoice", true)]`, an `Overlay` that implements `ITransientOverlay`.
- It shows while voice is on, and for 6 s after any status (the Test key's, for example).
- It displays:
  - "GazeVoice · <text>" in the state's color (Blender's colors);
  - the last result;
  - the Talk button;
  - the key hint.

**Selection**
- The selection comes from `Selection.gameObjects` and `activeGameObject`.
- Paths come from `SearchUtils.GetHierarchyPath(go, false)`.
- The scene is the active scene, or the open prefab stage.
- The mode comes from `EditorApplication.isPlayingOrWillChangePlaymode` and `PrefabStageUtility.GetCurrentPrefabStage()`.
- `scene_names` walks the loaded scenes (or the prefab stage) depth first, up to 300 names.

**One undo group per command**
- **`begin_edit`:**
  1. calls `Undo.IncrementCurrentGroup()`;
  2. remembers `mark = Undo.GetCurrentGroup()`;
  3. clears the change flag and the list of created assets.
- **`undo_push`:**
  - With `only_if_changed` and nothing changed, it answers `pushed: false`.
  - Otherwise it calls `Undo.CollapseUndoOperations(mark)`, then `Undo.SetCurrentGroupName(name)`, then `Undo.IncrementCurrentGroup()`. S4 confirms the order that names the merged step.
- **What "changed" means.** `ObjectChangeEvents.changesPublished` or `Undo.postprocessModifications` fired since `begin_edit`. Both only see changes made through Undo, so the editor prompt requires Undo calls.
- **Created assets.** These come from an `AssetPostprocessor`: paths imported since `begin_edit` that weren't there before.
- **The step's name** is "GazeVoice: <title>", cut at 120 bytes with "…".
- **Mouse clicks.** A mouse-down also advances Unity's group index. So anything you click into during a long command is merged into that command's step. This is accepted and documented.

**Main thread**
- A `TcpListener` thread accepts connections, and each client gets a reading thread.
- `ping` and `poll` are answered right there, from thread-safe state: the event queue and a snapshot refreshed on every Editor tick. Polling stays live while the main thread is busy.
- Every other request goes into a `ConcurrentQueue` and runs in `EditorApplication.update`.
- If the main thread hasn't started a request within 1.5 s, the client thread cancels it and answers `busy`. A compare-and-swap on the request's state makes sure a cancelled request never runs later, so a late undo can't happen twice. The Python side already retries `busy`.

**Domain reloads and compiles**
- `[InitializeOnLoad]` starts everything again after every reload.
- **Before a reload,** `AssemblyReloadEvents.beforeAssemblyReload` closes the listener and the client sockets. It saves these into `SessionState`:
  - `mark`;
  - the change flag;
  - the created assets;
  - the status;
  - whether a hold was in progress.
- **After the reload:**
  - a fresh event queue means a new boot token, so the app resets its ack;
  - the saved state is restored;
  - a "cancel" is queued if a hold was cut off.
- The bridge also stops on `EditorApplication.quitting`, and it never listens in batch mode.

**Play mode**
- `begin_edit`, `undo`, `redo` and `undo_push` answer "playing" with the message `PLAYING`. Status and selection still work.
- With domain reload on Play enabled (Unity's default), entering Play mode is a reload like any other.

**Busy**
- Busy means `EditorApplication.isCompiling || EditorApplication.isUpdating || GUIUtility.hotControl != 0`. The last is a drag in progress, like Blender's G/R/S.
- `begin_edit`, `undo`, `redo` and `undo_push` answer busy. Unity's `busy_wait` is 60 s, so a compile can finish.

**Gaze, later** (reserved, not built)
- A future `{"cmd": "pick", "x": 0.42, "y": 0.61}` takes coordinates in the last active Scene view's viewport.
- It returns the GameObject under that point, plus the hit point. It finds them through `SceneView.lastActiveSceneView.camera.ViewportPointToRay` and `HandleUtility.PickGameObject`, which picks by renderer, so no colliders are needed.
- The focus will then gain `"source": "gaze"` and the objects you looked at. `describe_focus` already prints the source.

### 2.3 Dashboard units and setup

Checks never change anything. Actions run only when you click.

| Unit (id) | Checks | Offers |
|---|---|---|
| Unity (`unity`) | Unity Hub at `/Applications/Unity Hub.app`; editors under `/Applications/Unity/Hub/Editor/*` and in the folder named in Hub's `secondaryInstallPath.json`; each version from that `Unity.app`'s Info.plist (`CFBundleVersion`, which S0 confirms); `pgrep -x Unity` | No Hub: **Install Unity Hub** (`brew install --cask unity-hub`, with a confirm), or a link without Homebrew. Hub but no Unity 6: **Open Unity Hub**, saying "Install Unity 6.3 LTS (Apple silicon) in Unity Hub, and sign in there to activate the free Personal license." The project's editor missing: an error naming the version. Ready: **Launch** runs `open -a <Unity.app> --args -projectPath <project>` |
| Unity project (`unity_project`) | The picked folder has `Assets/`, `ProjectSettings/ProjectVersion.txt` and `Packages/manifest.json`; its editor version; its render pipeline (URP, HDRP or built-in, from the manifest) | **Choose…** (the macOS folder picker, through `osascript`) and a path field |
| Unity MCP package (`unity_mcp_package`) | The manifest line; the resolved version in `packages-lock.json`; whether it's listening (port from the pipeline's port file, never the token); a conflict when `com.unity.ai.assistant` is older than 2.13 | **Add to project**, **Update**, **Remove from project**, each with a confirm naming the project and the line |
| Unity MCP server (`unity_mcp_server`) | The `unity` CLI's path and version; the sign-in, if S1 shows MCP needs it | **Install** (`brew install --cask unity-cli`, with a confirm). **Test** starts `unity mcp`, lists its tools (count and size) and, if Unity is listening, reads the scene. It calls no model |
| GazeVoice Unity Bridge (`unity_bridge`) | The manifest's `file:` line points at GazeVoice's copy; ping on 9878; the bridge version; the project it answers for | **Add to project**, **Update**, **Remove from project**; **Test** (status line, selection, names) |

**Install or link out.** License and sign-in decide which a unit offers.
- **Unity Hub and the Unity CLI** need no account to install, so their units offer Install through Homebrew, after a confirm.
- **Unity editors** get a link out, because of how Unity works:
  - a Personal license can be activated only by signing in to Unity Hub (Unity's docs say command-line activation doesn't apply to Personal);
  - the Hub CLI has been deprecated since Hub 3.18, and it installs Intel builds unless told otherwise;
  - an editor install involves choosing modules.

  So the unit opens Unity Hub and says what to install there.
- **License state.** GazeVoice never reads license files or claims a license state. A project that opens and a bridge that answers prove you have one.
- **CLI sign-in.** If the CLI needs `unity auth login` (S1), the server unit says "Run `unity auth login` in Terminal", like the Claude Code unit does.

**Picking a project**
- The settings gain `unity_project`, which stays empty until you pick one.
- Picking writes nothing into the project.
- Packages go in only through the package units' **Add**, with consent.

**Workflow strip and Check everything**
- **The strip** still has five stages. With Unity as the target they read Microphone → Speech-to-text → Refiner → Editor → **Unity**.
  - Editor is the brain plus `unity_mcp_server`.
  - Unity is `unity`, `unity_project`, `unity_mcp_package` and `unity_bridge`.
- **Check everything** for Unity runs, in order:
  1. the microphone test;
  2. the speech test;
  3. the `unity_mcp_server` test;
  4. the `unity_bridge` test;
  5. the editor brain's `connect_unity`, or the provider's test.

  The confirm text doesn't change: it already names the 3 s recording and the one small brain question.
- **The rack** shows the active target's shelf in full. The other target's shelf shrinks to one line with its health and "Switch in Routing".
- **Routing** gets "Edit in: Blender | Unity" as its first row.

### 2.4 Prompts (draft copy; tool names come from S2 and S3)

**Unity editor prompt** (`gazevoice/voice/prompts/unity_editor.md`):

```text
You are the editing engine inside GazeVoice. A person is building a scene in the Unity Editor and talking to it. You never chat with them: your final reply appears as one short status line in Unity's Scene view.

Each message has:
- Command: what they want, written up from what they said. For something new it's a detailed brief: follow it.
- Heard: what speech-to-text heard. Use it only when the command seems off.
- Focus: what they're focused on right now: their selected GameObjects with hierarchy paths, the open scene, and whether they're in Prefab Mode. "this", "that", "it", "these", "here" mean the focus unless the command names something else.

How to work
- Use the unity tools. Make changes with <run-C# tool>, which runs C# in the Editor right away with no recompile. Look things up first when you need to (<hierarchy tool>, <inspect tool>).
- Record every change with Undo, so saying "undo" takes it back: Undo.RegisterCreatedObjectUndo after creating an object, Undo.RecordObject before changing one, Undo.AddComponent to add a component, Undo.SetTransformParent to reparent, Undo.DestroyObjectImmediate to delete. Never use DestroyImmediate or change anything without Undo.
- Don't add undo groups yourself (no Undo.IncrementCurrentGroup or Undo.CollapseUndoOperations). GazeVoice turns everything a command did into one undo step.
- For something big, work in steps: one call per part, then check the result (<hierarchy tool>, or <screenshot tool> of the Scene view when the look matters) and fix what's off before you finish.
- Change only the focus objects, unless the command clearly names other objects or asks you to add something new. Put new things next to the focus, or at the Scene view's pivot when nothing is selected.
- Unity is Y-up and 1 unit is 1 meter. Keep sizes real: a door is about 2 m tall.
- For edits to existing things, keep the change as small as the command. "Taller" changes height, not width. For vague amounts pick a modest step (about 25% for "bigger", 15 degrees for "rotate a bit") and say what you chose.
- When you make something new, make it look finished: a parent GameObject named after it with named child parts, realistic proportions, materials that suit it, and colliders when it should be solid. Build shapes with ProBuilder if the project has it, or with meshes you generate; never leave a pile of untouched primitives unless low-poly or a blockout was asked for. For animals, people and plants, look for a matching model or prefab in the project first and use it; otherwise build a careful stand-in and say it's a stand-in.
- Materials: check the render pipeline once (GraphicsSettings.currentRenderPipeline) and use its Lit shader. To recolor one object, give it its own material instead of changing a material other objects share, unless the command is about that material.
- Change lights, the skybox or the environment only when asked.
- Files: don't create, change or delete files in the project unless the command needs it. New materials and meshes can live in the scene. Save scripts, and prefabs or assets they asked to keep, under Assets/GazeVoice/ (Scripts, Prefabs, Materials, Meshes), named after what they're for.
- Scripts: write a C# script only when the command asks for something that behaves in the game (moves, reacts, spawns, plays) or asks for a script. One public class per file, the file named like the class, in namespace GazeVoice.Generated, no UnityEditor code in game scripts, public fields for the values someone would tune. After writing it, wait until Unity has finished compiling, read the console, and fix every compile error before doing anything else. Then add the component and set its fields. A compile error stops every other change in the project, so never finish with one.
- Never enter Play mode, build the player, save scenes, change Project Settings or packages, delete assets, or quit Unity, unless the command asks for exactly that.
- Never ask a question. If the command is ambiguous, do the most likely thing and say so in your reply. If it can't be done, change nothing and reply with a sentence that starts with "Couldn't", like "Couldn't find a lamp in this scene."
- Spoken names are approximate. Match them to real GameObject names case-insensitively, ignoring suffixes like " (1)".

Reply with one plain sentence of at most 20 words saying what changed, like "Made Tower twice as tall." No markdown and no preamble.
```

**Unity prompt writer** (`gazevoice/voice/prompts/unity_refiner.md`). It has the same JSON input and reply as `refiner.md` and the same kinds. Its differences:

```text
You are the prompt writer inside GazeVoice. Someone is working in the Unity Editor by voice. You turn what they said into the instruction an editing engine (an AI that drives Unity with C# and Unity's tools) will follow, so it does what they meant, and does it well. You never edit anything yourself.

Each message is JSON:
{"heard": "raw speech-to-text", "focus": {"active": ..., "selected": [...], "paths": [...], "scene": ..., "mode": ...}, "scene_objects": ["names"], "previous_commands": ["recent requests"]}

Reply with JSON only, and nothing else:
{"kind": "command", "title": "<short title>", "text": "<the instruction>"}
where "kind" is one of "command", "undo", "redo", "stop", "ignore".

First, understand what was said
- Drop fillers, false starts and repeated words. Apply self-corrections and keep the final intent.
- Fix Unity words speech-to-text often gets wrong when the context makes it clear: "game object" to GameObject, "pre fab" to prefab, "rigid body" to Rigidbody, "collide her" to collider, "mono behavior" to MonoBehaviour, "nav mesh" to NavMesh, "pro builder" to ProBuilder, "you are P" to URP, "sky box" to skybox, "in spector" to Inspector, "scriptable object" to ScriptableObject, "cinema scene" to Cinemachine, "post processing" to post-processing volume, and object names that sound like a name in scene_objects.
- Use previous_commands for follow-ups. Keep pointing words as said (this, that, these, it, here); the editing engine resolves them from the focus.

Then write the instruction
- Exact edits stay exactly as asked: moving, scaling, rotating, recoloring, renaming, deleting, selecting, enabling or disabling, parenting, adding one component, or changing one setting. One clear sentence; add nothing that wasn't said.
- Requests to make, build, set up, lay out, light, script, or "make it do" something get a detailed brief, so the result looks finished. In 80 to 200 words, cover what applies:
  - what it is: its parts as a hierarchy (a parent GameObject with named children), real sizes in meters (Y is up, 1 unit is 1 meter), and where it goes relative to the focus;
  - shape: ProBuilder or generated meshes with enough detail for a clean silhouette; for animals, people and plants, a matching model from the project if there is one, else a careful stand-in;
  - components and their key values: colliders, Rigidbody, lights, audio sources;
  - materials: the render pipeline's Lit material, base color, metallic, smoothness, emission;
  - lighting and mood, when asked: light types, color temperature, intensity, shadows, skybox, fog;
  - behavior, when asked: what moves or reacts, speeds and timings, what triggers it, written as a MonoBehaviour with public fields to tune;
  - organization: clear names, and a prefab only if they asked for one or will reuse it.
- Stay within what they asked for. Add craft, not new objects or scene changes they didn't ask for. If they named a style (low-poly, cartoon, realistic), follow it.
- "title" is the request in at most 12 words, as an imperative. For exact edits it's the same as "text".
- "undo", "redo", "stop" and their variants are those kinds. Talk that isn't meant for Unity is "ignore".
```

### 2.5 Brains

**Claude Code** already works with a second server:
- `--strict-mcp-config --mcp-config mcp-unity.json --allowedTools mcp__unity` allows every tool of that server and nothing else.
- The warm-up checks that `init.mcp_servers` lists `unity` as connected.
- Tools the editor must never use go in `--disallowedTools mcp__unity__<tool>` (`Target.blocked_tools`, from S2). Examples: build, package install, quit, enter Play mode, delete assets.

**Codex** uses the same `-c mcp_servers.<name>.command/args/env` overrides with the name `unity`. If S8 confirms Codex accepts it, the blocked tools become `mcp_servers.unity.disabled_tools`. Codex stays experimental, as it is for Blender.

**The API tool loop**
- `unity mcp` speaks stdio, so `StdioMcpClient` works as it is.
- **Tool count and size.** Every step resends all the tool definitions. If S9 measures more than about 40 tools or 60 KB of schemas (about 15k tokens), `Target.api_tools` becomes an allowlist: run C#, hierarchy and inspect, screenshot, console. The blocked tools are dropped too.
- **Cost.** With 15k tokens of tools, a command that used all 150 steps would send about 2.3M input tokens. DeepSeek and Qwen cache repeated prefixes, so most of those are billed at the cache rate.
- The provider's Test logs the tool count and size.

**Limits.** 150 steps and 600 s per command stay.
- A domain reload costs 5–20 s (S5), so a command that writes two scripts spends up to about 40 s waiting.
- `Target.edit_seconds` lets Unity's limit rise, up to 900 s, if S11 shows real builds need it.

**Several MCP processes.** Each brain session and each Test starts its own `unity mcp`. With Unity's CLI these are thin clients of the Editor's HTTP server, so they can run side by side. S2 checks this with a Test while voice is on.

### 2.6 Tests

**Python** (standard-library `unittest`, with fakes):
- Each Phase 1 task tests the new parameters with a made-up "Testapp" target and leaves the Blender tests alone.
- `tests/fake_unity_bridge.py` is a real TCP server on port 0 that speaks the Unity variant of the protocol:
  - shared commands use Blender's `protocol.handle` and a fake scene;
  - the Unity fields are added on top;
  - `go_away(seconds)` closes it like a domain reload and brings it back with a new boot token.
- The shared protocol cases (`protocol_cases.json`) also run against Blender's `protocol.py`.
- Static checks of the C# package: its versions and ports match the Python constants, and its asmdefs are editor-only.
- Unity units are tested with:
  - fake app bundles, with Info.plist files written by `plistlib`;
  - temporary projects;
  - `FakeRunner` for `brew`, `osascript` and `pgrep`;
  - the existing `tests/fixtures/fake_mcp_server.py` for the server's Test.

**C#** (Unity Test Framework EditMode tests, run in batch mode):
- `ProtocolCaseTests` runs the same JSON cases against the C# protocol.
- Plain tests cover the protocol, event queue, line framing, undo names and key labels.
- Tests against the real Editor cover:
  - undo grouping with the real `Undo` API;
  - the selection and its paths;
  - the server on port 0 with a manual pump, and cancel-before-run;
  - Play mode, entered with `EnterPlayMode`;
  - a `[UnityTest]` that writes a script, waits for the reload with `WaitForDomainReload`, and checks the undo group and the restored state.
- **Run:** `"$UNITY" -batchmode -nographics -projectPath unity_package/TestProject -runTests -testPlatform EditMode -testResults "$TMPDIR/gazevoice-unity-tests.xml" -logFile -`
  - `$UNITY` is `/Applications/Unity/Hub/Editor/<S0 version>/Unity.app/Contents/MacOS/Unity`.
  - Leave out `-quit`: Unity doesn't support it with `-runTests`.
  - A pass is `result="Passed"` and `failed="0"` on the XML's `<test-run>`.
  - It needs the Personal license already activated on this Mac, and the test project closed in the Editor.
  - These tests aren't part of the Python suite. Each Phase 2 task runs them.

**By hand:** the checklist in section 4.

### 2.7 What "make a detailed eagle" can mean in Unity

Unity isn't a modeling tool: it has no modifier stack, no sculpting and no subdivision surfaces. What it does have:
- **ProBuilder 6.1** (for Unity 6.3) does box modeling from code: `ShapeGenerator`, `ExtrudeElements.Extrude` and `ProBuilderMesh.Create`. It's good for buildings, props and blockouts, and poor at feathers.
- **Generated meshes** come from the `Mesh` API, with the AI writing the vertices: lathes, lofts, terrain and curved panels. They're usable for simple organic forms and rough for detailed ones.
- **Imported models:** Unity reads FBX and OBJ natively, `.blend` when Blender is installed (Unity converts it through FBX), and glTF through glTFast.

So in this plan, "make a detailed eagle" in Unity means:
1. **If the project has an eagle model or prefab,** place it at a real size (about 0.9 m long, with a 2 m wingspan), with materials, next to the focus.
2. **If it doesn't,** build a careful stand-in under one "Eagle" parent, from generated or ProBuilder parts with materials:
   - body and head;
   - hooked beak;
   - layered wing feathers;
   - tail and talons.

   It's recognizable and tidy, but stylized, and the reply says it's a stand-in.
3. **What Unity adds on top:** "make it circle the tower" becomes a script, and "make it land on the branch" becomes an animation.

For eagle-quality models, the right path is Blender, which GazeVoice already drives at that level. A later "build in Blender, use in Unity" step would export FBX, rig and animation included, into `Assets/GazeVoice/Models/` and place it in Unity. That step isn't in this plan.

---

## 3. Tasks

Every phase leaves main working. Run the full Python suite with:

```bash
python3 -m unittest discover -s tests -t . -v
```

The expected result is `OK`, with 403 tests plus the new ones.

### Phase 0: Spike (by hand; you approve it before anything is built)

#### Task 0: Check Unity's CLI MCP on a real project

**Who:**
- I run Terminal commands, after you OK each install.
- You do the Unity Hub sign-in and the steps inside Unity.
- No GazeVoice code changes, and nothing goes into any existing project.

**Installs you approve first:**
- Unity Hub, if it's missing;
- Unity 6.3 LTS for Apple silicon, through Hub, if it's missing;
- the Unity CLI (Homebrew cask `unity-cli`);
- `com.unity.pipeline`, into a new throwaway project `~/Unity/GazeVoiceSpike` (3D URP template).

**Model calls:**
- about ten short Claude Code runs and a few Codex runs on your plans;
- one long "eagle" build (Opus 5.5, a few minutes).

**Output:** `docs/superpowers/specs/<date>-unity-spike.md`, with answers S0–S13, the gate results, and every change this plan needs. It's written before Phase 1 starts.

- [ ] **S0 Versions.** Record:
  - the Unity editor's version (`CFBundleVersion` in Info.plist);
  - the Unity Hub version;
  - `unity --version`;
  - the pipeline version from `packages-lock.json`;
  - `which unity`.

  These become the pins.
- [ ] **S1 Install and sign-in.**
  - Run `brew install --cask unity-cli`.
  - Add the package with `unity pipeline install`. Record whether it takes a project path and exactly what it changes in `manifest.json`. If it can't, add the manifest line by hand.
  - Record where the port file is and what it looks like, without copying the token.
  - Check whether `unity mcp` works while signed out. If it doesn't, run `unity auth login`.
- [ ] **S2 The server by hand.**
  - Write `~/Unity/GazeVoiceSpike/spike-unity.json` as `{"mcpServers": {"unity": {"command": "<abs path>/unity", "args": ["mcp", …]}}}`, and find the args that pick the project.
  - Run `claude -p --strict-mcp-config --mcp-config spike-unity.json --allowedTools mcp__unity "Count the GameObjects at the root of the open scene. Reply with only the number."`.
  - Save the `tools/list` reply, using a one-off script in the scratch folder that drives `gazevoice.mcp_client.StdioMcpClient`. Record:
    - the tool names and count;
    - the total JSON size;
    - which tools are read-only (they become `read_only_tools`);
    - which tools to block.
  - Start a second server process while the first is connected.
- [ ] **S3 Running C#.**
  - Find the tool that runs multi-statement C# without a domain reload.
  - Time a trivial snippet.
  - Check that return values, logs and compile errors come back.
  - Check that UnityEditor and ProBuilder APIs work from it.
- [ ] **S4 Undo.** Check each of these:
  - (a) A fixed tool creates a cube. Does one Ctrl+Z remove it?
  - (b) C# with `Undo.RegisterCreatedObjectUndo`. Can it be undone?
  - (c) C# without Undo. Expected: it can't be undone.
  - (d) The bridge's pattern, run through the C# tool:
    1. `IncrementCurrentGroup`, and keep the mark;
    2. three separate tool calls;
    3. `CollapseUndoOperations(mark)` and `SetCurrentGroupName("GazeVoice: test")`, in both orders.

    Does one Ctrl+Z undo all three, and does the Edit menu read "Undo GazeVoice: test"?
  - (e) Does `ObjectChangeEvents.changesPublished` fire for the tools' changes?
- [ ] **S5 Reloads.**
  - Ask the brain to write a MonoBehaviour that rotates the cube, and to attach it.
  - Time the compile and the reload.
  - Check whether `unity mcp` reconnects by itself.
  - Check whether the undo history and `Undo.GetCurrentGroup()` survive the reload.
  - Try a deliberate compile error and record what happens.
- [ ] **S6 Background.**
  - Time the same five-step command with Unity in the background.
  - Note the memory Activity Monitor shows for Unity, Claude and the speech worker.
- [ ] **S7 Play mode.** Record what the tools do while Unity is playing.
- [ ] **S8 Codex.**
  - Run `codex exec --json --skip-git-repo-check --sandbox read-only -c mcp_servers.unity.command=… -c mcp_servers.unity.args=[…] "<the count prompt>"`.
  - Check whether `mcp_servers.unity.disabled_tools` is accepted.
- [ ] **S9 API loop size.**
  - Estimate tokens from S2's list (characters ÷ 4).
  - Propose an `api_tools` allowlist if it's over 40 tools or 60 KB.
- [ ] **S10 Phone-home.**
  - Record what the CLI and the package send: from their docs, their settings, and `lsof -i -n -P` during a session.
  - Record the opt-outs.
- [ ] **S11 Eagle.**
  - Run one "make a detailed eagle" through Claude Code (Opus 5.5, high effort), with the Unity editor prompt from 2.4 filled in.
  - Record the time, the steps and a Scene view screenshot. This sets what 2.7 promises.
- [ ] **S12 Local package and tests.**
  - Check that a `file:` package path containing spaces resolves. GazeVoice's app folder is under "Application Support".
  - Check that `-batchmode -runTests` runs a one-test EditMode suite with the Personal license.
- [ ] **S13 Shortcut.**
  - Check that ⌥ Space is free in Unity 6.3: Edit › Shortcuts shows no conflict.
  - Check that a throwaway `ClutchShortcut` script fires Begin and End from the Scene view and from the Hierarchy.

**Gates.** All must pass for the recommended server, and you approve the result:
- **G1 Local and free.** No Unity AI subscription and no hosted model. A Unity sign-in is acceptable if you say so.
- **G2 C#.** Multi-statement C# runs without a domain reload and reports its errors.
- **G3 Undo.** The collapse pattern gives one named undo step over several tool calls, and the step survives a reload.
- **G4 Reloads.** After a script compile, the tools work again within 30 s, with no manual step.
- **G5 Brains.** Claude Code and `StdioMcpClient` work end to end. Codex is a nice-to-have; it's experimental for Blender too.
- **G6 Size.** The tool list fits the API loop: under about 60 KB, after an allowlist.

**If G1–G4 fail,** repeat S1–S9 with MCP for Unity v10.3.0 (stdio, `DISABLE_TELEMETRY=true`, `scripting_ext` on) and recommend whichever passes. Task 12 says what changes.

### Phase 1: Targets (Blender only; nothing changes for you)

#### Task 1: Describe Blender as a target

**Files:**
- Create: `gazevoice/targets/__init__.py`, `gazevoice/targets/blender.py`
- Modify: `gazevoice/components/brains.py`. `CONNECT_PROMPT` becomes `BLENDER.connect_prompt`, and the name stays.
- Test: `tests/test_targets.py`

**Interfaces:**
- Produces: `Target`, a frozen dataclass in `gazevoice/targets/__init__.py`, with these fields:

  | Field | Type | `BLENDER` value |
  |---|---|---|
  | `id` | `str` | `"blender"` |
  | `name` | `str` | `"Blender"` |
  | `units` | `tuple[str, ...]` | `("blender", "mcp_addon", "mcp_server", "bridge")` |
  | `app_parts` | `tuple[str, ...]` | `("blender", "mcp_addon", "bridge")` |
  | `edit_parts` | `tuple[str, ...]` | `("mcp_server",)` |
  | `needed` | `tuple[tuple[str, frozenset[Health], str], ...]` | the four Blender rows of `wiring._NEEDED` |
  | `check_steps` | `tuple[tuple[str, str], ...]` | `(("mcp_server", "test"), ("bridge", "test"))` |
  | `connect_action` | `str` | `"connect_blender"` |
  | `connect_label` | `str` | `"Connect to Blender"` |
  | `connect_prompt` | `str` | today's `CONNECT_PROMPT` text |
  | `bridge_port` | `int` | `9877` |
  | `version_key` | `str` | `"blender"` |
  | `mcp_name` | `str` | `"blender"` |
  | `editor_prompt` | `Path` | `PROMPTS / "editor.md"` |
  | `refiner_prompt` | `Path` | `PROMPTS / "refiner.md"` |
  | `vocabulary` | `str` | today's `_BLENDER_TERMS` text |
  | `read_only_tools` | `tuple[str, ...]` | `("get_", "search_", "render_viewport_to_path")` |
  | `blocked_tools` | `tuple[str, ...]` | `()` |
  | `api_tools` | `tuple[str, ...]` | `()` (empty means every tool) |
  | `undo_name_bytes` | `int` | `63` |
  | `busy_wait` | `float` | `10.0` |
  | `edit_seconds` | `float` | `600.0` |
  | `wait_when_unreachable` | `bool` | `False` |
  | `not_running` | `str` | today's "Blender isn't connected. Launch Blender from the dashboard…" |
  | `disconnected` | `str` | today's "Blender isn't connected. Voice picks up again when it's back." |
  | `server_command` | `Callable[[Context], dict]` | `blender_mcp.server_command(ctx.paths, find_blender(ctx.runner))` |
  | `mcp_reachable` | `Callable[[Context], bool]` | `server_installed(ctx.paths) and port_open("127.0.0.1", ADDON_PORT)` |

- Also produces:
  - `TARGETS: dict[str, Target]`, which is `{"blender": BLENDER}` for now;
  - `target_for(settings) -> Target`, which reads `getattr(settings, "target", "blender")` and returns `BLENDER` for an unknown id.

- [ ] **Step 1: Write the failing tests** in `tests/test_targets.py`:
  - `test_blender_target_matches_todays_behaviour`. Every value in the table equals today's constant it replaces:
    - `BLENDER.bridge_port == blender_socket.BRIDGE_PORT`;
    - `BLENDER.editor_prompt == commands.EDITOR_PROMPT`;
    - `BLENDER.read_only_tools == service.READ_ONLY_TOOLS`;
    - `BLENDER.undo_name_bytes == service.UNDO_NAME_BYTES`;
    - `BLENDER.busy_wait == service.BUSY_WAIT`;
    - `BLENDER.edit_seconds == service.EDIT_SECONDS`;
    - `BLENDER.check_steps == tuple(workflow.STEPS[2:])`.
  - `test_unknown_target_falls_back_to_blender`: `target_for(Settings()) is BLENDER`, and `target_for(SimpleNamespace(target="nope")) is BLENDER`.
  - `test_blender_server_command_is_blender_labs`: with a `FakeRunner` whose `mdfind` finds nothing, `BLENDER.server_command(ctx) == server_command(ctx.paths, None)`.
  - `test_blender_mcp_is_reachable_only_with_the_server_and_the_addon_port`.
- [ ] **Step 2: Run them and see them fail.** Run `python3 -m unittest tests.test_targets -v`. Expected: ImportError.
- [ ] **Step 3: Implement.** Import `components` lazily inside `server_command` and `mcp_reachable`, so `targets` never imports `components.brains` at load time.
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Describe Blender as a target".

#### Task 2: Brains name their MCP server

**Files:**
- Modify: `gazevoice/components/brains.py`, `gazevoice/voice/brains.py`, `gazevoice/voice/api_brain.py`
- Test: `tests/test_brains.py`, `tests/test_voice_brains.py`, `tests/test_api_brain.py`, all with new tests only

**Interfaces:**
- Consumes: `Target`, `TARGETS` (Task 1).
- Produces:
  - `write_mcp_config(paths, server_cmd, name="blender") -> Path` writes `paths.root / f"mcp-{name}.json"` = `{"mcpServers": {name: server_cmd}}`.
  - `codex_mcp_args(cmd, name="blender", disabled=()) -> list[str]` writes `mcp_servers.<name>.…`. It adds `-c mcp_servers.<name>.disabled_tools=[…]` only when `disabled` isn't empty.
  - `claude_session_argv(system_prompt_file, model, mcp_config, max_turns=12, effort=None, mcp_name="blender", blocked=())` adds `--allowedTools mcp__<name>`, plus `--disallowedTools mcp__<name>__<tool>…` when `blocked` isn't empty.
  - `stdio_mcp(server_cmd, timeout=180.0)`, with `blender_mcp = stdio_mcp` kept as a name.
  - `mcp_tools(tools, allow=(), block=())`. `allow` empty means every tool.
  - `ApiSession(..., app="Blender", allow=(), block=())`. Its errors read "Couldn't start the {app} MCP server: …" and "The {app} MCP server isn't running."
  - `_Brain.connect(job, target) -> dict` asks `target.connect_prompt` through `target`'s server. `action_connect_blender` calls it with `BLENDER`.
  - `_Brain.brain_actions()` offers one connect action per target in `TARGETS` whose `mcp_reachable(ctx)` is true, labeled `connect_label`. Codex adds " (experimental)".

- [ ] **Step 1: Write the failing tests:**
  - `test_mcp_config_is_named_after_the_server`: name `"unity"` gives `mcp-unity.json` with the key `unity`.
  - `test_codex_args_use_the_server_name`: the args contain `mcp_servers.unity.command=`. `disabled=("x",)` adds `mcp_servers.unity.disabled_tools=["x"]`.
  - `test_claude_argv_allows_only_that_server`: `mcp_name="unity"` gives `--allowedTools mcp__unity`. `blocked=("build",)` gives `--disallowedTools mcp__unity__build`.
  - `test_api_tools_can_be_allowed_and_blocked`: three MCP tools with `allow=("a", "b")` and `block=("b",)` leave only `a`.
  - `test_api_session_names_the_app_in_errors`: the factory raises `McpError`, `app="Unity"`, and the `BrainError` contains "Unity MCP server".
  - `test_brain_offers_one_connect_action_per_reachable_target`: patch `TARGETS` with two fake targets, one reachable, and only that target's action is offered.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** Every default reproduces today's argv exactly. Import `TARGETS` inside `brain_actions` to avoid an import cycle.
- [ ] **Step 4: Run the full suite.** Expected: OK, with every Blender argv test unchanged.
- [ ] **Step 5: Commit** with the message "Let brains connect to any named MCP server".

#### Task 3: Commands and the voice loop take a target

**Files:**
- Modify: `gazevoice/voice/commands.py`, `gazevoice/voice/service.py`
- Create: `tests/voice_fakes.py`. Move `FakeBridge`, `FakeMic`, `FakeStt` and `FakeSession` here from `tests/test_voice_service.py`, unchanged, and import them back.
- Test: `tests/test_voice_commands.py`, `tests/test_voice_service.py` (new tests)

**Interfaces:**
- Consumes: `BLENDER` (Task 1).
- Produces:
  - `BLENDER_TERMS` is the public name for `_BLENDER_TERMS`, and `vocabulary_prompt(names, terms=BLENDER_TERMS)` takes it.
  - `describe_focus(focus, app="Blender")`:
    - unknown → `f"unknown ({app} didn't report a selection)"`;
    - non-empty `paths` are listed in place of `selected`;
    - `" · scene <scene>"` is added when the focus has a scene;
    - Blender's output is byte-for-byte unchanged.
  - `editor_message(command, heard, focus, app="Blender")`.
  - `changes_scene(tools, read_only=BLENDER.read_only_tools)`, where an empty `read_only` means every tool counts.
  - `undo_name(command, suffix="", limit=63)`.
  - `VoiceService(logs_dir, bus, factories, engine="parakeet", brain="claude", clock=time.time, target=BLENDER)`.
    - Its messages use `target.name`, `target.not_running` and `target.disconnected`: "That didn't sound like a {name} command…", "{name} is busy…" and "{name} couldn't {kind}."
    - The snapshot gains `"app": target.name` and `"app_connected"`. `blender_connected` stays until Task 4 moves the dashboard over.
  - `_capture_context` keeps every key of the selection reply except `"ok"`.
  - `_app_op(request, run, wait=None)` replaces `_blender_op`, with `wait` defaulting to `target.busy_wait`. When `target.wait_when_unreachable` is set, a missing reply is retried too, until the deadline.
  - `_edit` sends `begin_edit` through `_app_op`. A reply of `{"ok": False, "message": m}` fails the command with `m` before the editor runs, and adds no undo step.
  - `_meta` and `_push_undo` report a refusal's `message` the same way: the command fails with it.
  - The edit timeout is `target.edit_seconds`.

- [ ] **Step 1: Write the failing tests.** They use `TESTAPP = replace(BLENDER, id="testapp", name="Testapp", wait_when_unreachable=True, busy_wait=1.0, read_only_tools=(), undo_name_bytes=30)`.
  - `test_messages_name_the_target`:
    - starting without a bridge raises `VoiceError(TESTAPP.not_running)`;
    - a disconnect sets `reason == TESTAPP.disconnected`;
    - an "ignore" command's reply contains "a Testapp command".
  - `test_focus_keeps_every_field_the_bridge_reports`:
    - the fake's selection adds `"paths": ["World/Tower"]` and `"scene": "Main"`;
    - `record["focus"]` has both;
    - the editor message contains `World/Tower` and `scene Main`.
  - `test_begin_edit_refusal_fails_the_command_without_the_editor`:
    - `begin_edit` answers `{"ok": False, "error": "playing", "message": "In Play mode."}`;
    - the record fails with "In Play mode.";
    - the editor got nothing;
    - no `undo_push` was sent.
  - `test_an_unreachable_app_is_waited_for_when_the_target_reloads`:
    - the fake refuses connections for 0.3 s at `undo_push`, then answers;
    - with `TESTAPP`, `record["undo_step"]` is True;
    - with `BLENDER`, the same run returns at once with no step, which is today's behaviour.
  - `test_every_tool_counts_as_a_change_when_the_target_lists_no_read_only_tools`: tools `["get_x"]` still send `undo_push`.
  - `test_undo_name_respects_the_target_limit`: the name fits in 30 bytes and keeps its suffix.
  - `test_describe_focus_lists_paths_and_scene` and `test_describe_focus_names_the_app` in `test_voice_commands.py`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK. The existing VoiceService tests pass untouched.
- [ ] **Step 5: Commit** with the message "Let the voice loop work with any target".

#### Task 4: Readiness, the strip, Check everything and the dashboard follow the target

**Files:**
- Modify:
  - `gazevoice/voice/wiring.py`
  - `gazevoice/workflow.py`
  - `gazevoice/settings.py`
  - `gazevoice/server/app.py`
  - `gazevoice/server/static/app.js`
- Test:
  - `tests/test_voice_wiring.py`
  - `tests/test_workflow.py`
  - `tests/test_workflow_check.py`
  - `tests/test_settings.py`
  - `tests/test_server.py`

  All with new tests only.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - **Settings:** `Settings.target: str = "blender"`, checked against `TARGET_IDS = ("blender",)` in `settings.py`. Task 15 adds `"unity"`.
  - **Readiness:** `voice_readiness(states, sources, settings=None, target=None)` lists the microphone and speech, then the step lines, then `target.needed`.
  - **Stages:** `stages_for(target) -> tuple`.
    - The fourth stage is `("edit", "Editor", (EDITOR, *target.edit_parts))`, and the fifth is `(target.id, target.name, target.app_parts)`.
    - `STAGES = stages_for(BLENDER)`.
    - `workflow_stages(states, sources, settings=None, target=None)`.
  - **Check steps:** `steps_for(target)` is the microphone and speech tests plus `target.check_steps`, and `STEPS = steps_for(BLENDER)`. `WorkflowCheck` uses `target_for(self.settings)`, and its brain step becomes `(source, target.connect_action)`.
  - **Building voice:** `build_voice(ctx, bus, sources, keys=None, target=None)` uses:
    - `BridgeClient(port=target.bridge_port)`;
    - `target.server_command(ctx)`;
    - `write_mcp_config(…, name=target.mcp_name)`;
    - `require_mcp=target.mcp_name`;
    - the target's prompts, `blocked_tools` and `api_tools`;
    - `ApiSession(app=target.name)`;
    - `VoiceService(target=target)`.
  - **ControlCenter:**
    - `_voice_config_now` adds `settings.target`;
    - the snapshot adds `"target": target.id`, `"targets": [{"id", "name", "units"} for every target]` and `"step_stages"` (each check step's unit mapped to the stage it proves);
    - the voice-start log says "In {name}, hold {key}…".
  - **Dashboard:**
    - the last rack groups come from `state.targets`, with hint "Where the edits land";
    - `STEP_STAGES` comes from `state.step_stages`;
    - the voice texts use `state.voice.app` and `app_connected`;
    - `blender_connected` is removed from the snapshot.

- [ ] **Step 1: Write the failing tests:**
  - `test_stages_follow_the_target`: a fake target's last stage has its id, label and parts.
  - `test_check_steps_follow_the_target`: a fake target's steps follow the microphone and speech tests, and the brain's step is its `connect_action`.
  - `test_readiness_lists_the_targets_units`.
  - `test_settings_target_must_be_known`: `"blender"` is accepted and `"unity"` raises `ValueError`, for now.
  - `test_snapshot_names_the_target`: the snapshot's `target` is `"blender"`, `targets[0]["units"] == BLENDER.units`, and `step_stages == {"mcp_server": ["edit"], "bridge": ["blender"], …}`.
  - `test_build_voice_uses_the_targets_port_and_server_name`:
    - with a fake target (port 9999, `mcp_name="testapp"`), the bridge factory's port is 9999;
    - `mcp-testapp.json` is written;
    - the editor argv has `mcp__testapp`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.**
  - Expected: OK.
  - Then open the dashboard (`python3 -m gazevoice`). The rack, strip, Voice panel and Check everything look and read exactly as before.
  - Run one Blender voice command, "make it red", to check nothing changed. It uses a little of your plan.
- [ ] **Step 5: Commit** with the message "Read units, stages and checks from the active target".

### Phase 2: The Unity bridge package

#### Task 5: Shared protocol cases

**Files:**
- Create: `unity_package/com.gazevoice.bridge/Tests/Editor/protocol_cases.json`, `tests/test_protocol_cases.py`

**Interfaces:**
- **The case format.** Each case is `{"name", "scene", "steps"}`.
  - `scene` sets the fake scene's state. Its keys are `busy`, `changed`, `undo_push_result`, `undo_result`, `active`, `selected`, `mode` and `names`.
  - Each step is either `{"event": {…}}`, which adds an event to the queue, or `{"request": {…}, …}`. A request step can also carry:
    - `expect`: these keys must equal these values, with `"$boot"` meaning the queue's boot;
    - `expect_keys`: keys that must be present, whatever their value;
    - `expect_event_seqs`: the seqs the reply's events must have;
    - `expect_status`: the `[text, state]` the scene was last given.
- **The cases:**
  1. `ping_has_bridge_and_key`
  2. `status_needs_text`
  3. `status_is_cut_to_200`
  4. `unknown_status_state_is_idle`
  5. `poll_without_ack_drains`
  6. `poll_ack_keeps_later_events`
  7. `ack_from_an_earlier_boot_acknowledges_nothing`
  8. `begin_edit_then_nothing_changed_pushes_nothing`
  9. `undo_push_needs_a_message`
  10. `undo_push_while_busy_is_busy`
  11. `undo_and_redo_while_busy_are_busy`
  12. `scene_names_stop_at_300`
  13. `unknown_command`
  14. `invalid_line_is_an_unknown_command`

- [ ] **Step 1: Write the cases and `test_blender_bridge_passes_every_shared_case`.** It runs one `subTest` per case against `blender_addon/gazevoice_bridge/protocol.py`'s `handle`, with a `FakeScene` built from `scene`. Case 14 goes through `decode_lines`.
- [ ] **Step 2: Run it.** Run `python3 -m unittest tests.test_protocol_cases -v`. Expected: PASS. Blender already speaks the protocol; a failure means the case is wrong.
- [ ] **Step 3: Commit** with the message "Write down the bridge protocol as shared test cases".

#### Task 6: The C# protocol core, the test project and package checks

**Files:**
- Create in `unity_package/com.gazevoice.bridge/`:
  - `package.json`: `{"name": "com.gazevoice.bridge", "version": "0.1.0", "displayName": "GazeVoice Bridge", "description": "Hold-to-talk, selection, status line and one undo step per voice command for GazeVoice.", "unity": "6000.0", "dependencies": {"com.unity.nuget.newtonsoft-json": "<the version the spike project resolves>"}}`
  - `Editor/GazeVoice.Bridge.Editor.asmdef`: `includePlatforms ["Editor"]`, `overrideReferences true`, `precompiledReferences ["Newtonsoft.Json.dll"]`
  - `Editor/Protocol.cs`, `Editor/IScene.cs`, `Editor/EventQueue.cs`, `Editor/Lines.cs`
  - `Tests/Editor/GazeVoice.Bridge.Editor.Tests.asmdef`: references the editor asmdef, `UnityEngine.TestRunner` and `UnityEditor.TestRunner`; precompiled `nunit.framework.dll` and `Newtonsoft.Json.dll`; `includePlatforms ["Editor"]`; `defineConstraints ["UNITY_INCLUDE_TESTS"]`
  - `Tests/Editor/ProtocolCaseTests.cs`, `UnityProtocolTests.cs`, `EventQueueTests.cs`, `LinesTests.cs`, `FakeScene.cs`
- Create in `unity_package/TestProject/`:
  - `Packages/manifest.json` with `"com.gazevoice.bridge": "file:../../com.gazevoice.bridge"` and `"testables": ["com.gazevoice.bridge"]`
  - `ProjectSettings/ProjectVersion.txt`, from the spike's editor
- Modify: `.gitignore` adds `unity_package/TestProject/{Library,Temp,Logs,UserSettings,obj}/`, `*.csproj` and `*.sln`. Commit the `.meta` files Unity generates for the package on first import.
- Test: `tests/test_unity_package.py`

**Interfaces** (namespace `GazeVoice.Bridge`):
- `public interface IScene`, with these methods:
  - `string Version()`
  - `string Project()`
  - `string ActiveName()`
  - `IReadOnlyList<string> SelectedNames()`
  - `IReadOnlyList<string> SelectedPaths()`
  - `string SceneName()`
  - `string Mode()`
  - `void SetStatus(string text, string state)`
  - `IReadOnlyList<string> ObjectNames()`
  - `void BeginEdit()`
  - `bool UndoPush(string name)`
  - `bool Undo()`
  - `bool Redo()`
  - `IReadOnlyList<string> CreatedAssets()`
  - `string KeyLabel()`
  - `bool Busy()`
  - `bool Playing()`
  - `bool Compiling()`
  - `bool Focused()`
  - `bool Changed()`

  `Version`, `Project`, `KeyLabel`, `Playing`, `Compiling` and `Focused` must be safe to call off the main thread.
- `public sealed class EventQueue`:
  - constructor `EventQueue(int limit = 100, Func<double> clock = null)`;
  - `string Boot`, 8 hex characters;
  - `void Add(string type, JObject fields = null)`;
  - `JArray Poll(int? ack)`;
  - `bool Live(double within = 3.0)`;
  - thread-safe.
- `public static class Protocol`:
  - `const int Port = 9878`
  - `const string Version = "0.1.0"`
  - `MaxStatus = 200`, `MaxNames = 300`, `MaxLine = 65536`
  - `const string PlayingMessage` (2.2's `PLAYING`)
  - `static bool OffMainThread(string cmd)` is true for `ping` and `poll`
  - `static JObject Handle(JObject request, IScene scene, EventQueue events)`, with the Unity replies in 2.2
  - `static string UndoName(string message, int limitBytes = 120)`
  - `static string KeyLabel(string key, bool control, bool alt, bool shift, bool command)`
- `public static class Lines`:
  - `static List<JObject> Decode(List<byte> buffer)` turns a bad line, or a buffer over `MaxLine`, into `{"cmd": "__invalid__"}`;
  - `static byte[] Encode(JObject message)`.

- [ ] **Step 1: Write the failing tests.**
  - `ProtocolCaseTests`: one `[TestCaseSource]` per case in `protocol_cases.json`, read from `Path.GetFullPath("Packages/com.gazevoice.bridge/Tests/Editor/protocol_cases.json")`.
  - `UnityProtocolTests`:
    - `PingReportsUnityVersionProjectPlayingCompilingAndFocus`
    - `SelectionReportsPathsSceneAndMode`
    - `BeginEditWhilePlayingIsRefusedWithTheMessage`
    - `BeginEditWhileCompilingIsBusy`
    - `UndoAndRedoWhilePlayingAreRefused`
    - `UndoPushReportsCreatedAssets`
    - `UndoNameCutsAt120BytesWithAnEllipsis`
    - `KeyLabelUsesMacSymbols`: `"⌥ Space"`, and `"⌃⇧ F5"`
  - `EventQueueTests`:
    - `AckKeepsLaterEvents`
    - `AnEarlierBootAcknowledgesNothing`
    - `KeepsTheLast100`
    - `LiveMeansPolledWithinThreeSeconds`
    - `SeqsStayUniqueAcrossFourThreads`: 1000 adds
  - `LinesTests`:
    - `PartialLineStaysInTheBuffer`
    - `BadJsonIsInvalid`
    - `OverlongBufferIsClearedAndInvalid`
  - Python, in `tests/test_unity_package.py`:
    - `test_package_json_and_protocol_cs_agree_on_the_version`, with `Protocol.cs` read by regex;
    - `test_bridge_port_is_9878`;
    - `test_every_asmdef_is_editor_only`;
    - `test_tests_compile_only_as_testables` (`UNITY_INCLUDE_TESTS`).
- [ ] **Step 2: Run the C# tests and see them fail.** Run the batch-mode command in 2.6. Expected: a compile failure, because the types don't exist yet.
- [ ] **Step 3: Implement** `Protocol`, `EventQueue` and `Lines`. Mirror `protocol.py`'s logic line for line, so the shared cases mean the same thing in both languages.
- [ ] **Step 4: Run** the batch-mode command (expected: `result="Passed"`, `failed="0"`), then the Python suite (expected: OK).
- [ ] **Step 5: Commit** with the message "Add the GazeVoice Unity Bridge package: protocol, events, framing".

#### Task 7: The live Editor side: server, selection, status line and hold-to-talk

**Files:**
- Create in `unity_package/com.gazevoice.bridge/Editor/`:
  - `BridgeServer.cs`
  - `UnityScene.cs`
  - `Status.cs`
  - `HoldToTalk.cs`
  - `StatusOverlay.cs`
  - `BridgeLifetime.cs`
- Test: `Tests/Editor/ServerTests.cs`, `SelectionTests.cs`, `HoldToTalkTests.cs`

**Interfaces:**
- `public sealed class BridgeServer`:
  - constructor `BridgeServer(int port, IScene scene, EventQueue events, double requestTimeout = 1.5)`;
  - `int Start()` returns the port it bound;
  - `void Stop()`;
  - `void Pump()`, called on the main thread;
  - `int Pending`.
  - A request that isn't started within `requestTimeout` is cancelled and answered `{"ok": false, "error": "busy"}`, and it never runs later.
- `public sealed class Status`:
  - `string Text`, `State`, `Last`;
  - `double At`;
  - `bool Visible(bool live, double now)`, which is true while live, or for 6 s after a status.
- `public sealed class UnityScene : IScene` uses the Unity APIs in 2.2. It refreshes a thread-safe snapshot (version, project, key label, playing, compiling, focused) in `Tick()`.
- `public static class HoldToTalk`:
  - `const string Id = "GazeVoice/Hold to Talk"`;
  - the `[ClutchShortcut(Id, KeyCode.Space, ShortcutModifiers.Alt)] static void OnShortcut(ShortcutArguments a)` shortcut;
  - testable parts `Press(ShortcutStage stage, double now)`, `FocusChanged(bool focused)` and `Tick(double now)`;
  - a `[MenuItem("Tools/GazeVoice/Talk")]` menu item that queues "toggle".
- `StatusOverlay`: `[Overlay(typeof(SceneView), "gazevoice-status", "GazeVoice", true)]`, an `Overlay` that implements `ITransientOverlay`.
- `BridgeLifetime`: `[InitializeOnLoad]`. It doesn't listen in batch mode. It pumps the server and ticks the scene on `EditorApplication.update`.

- [ ] **Step 1: Write the failing tests:**
  - `ServerTests`:
    - `PingIsAnsweredWithoutThePump`: a server on port 0 and a `TcpClient` get a reply with no `Pump()`.
    - `PingAndPollReportFocus`.
    - `PingReportsTheProject`.
    - `SelectionWaitsForThePump`: the reply comes after `Pump()`.
    - `AnUnpumpedRequestIsBusyAndNeverRunsLater`: no pump for 1.6 s gives "busy"; a `Pump()` afterwards doesn't call `SetStatus`.
    - `TwoClientsAreServedInOrder`.
  - `SelectionTests`:
    - `PathsComeFromTheHierarchy`: selecting `Parent/Child` reports the path `"Parent/Child"`.
    - `ObjectNamesCoverLoadedScenesUpTo300`.
    - `PrefabModeIsReported`.
  - `HoldToTalkTests`:
    - `IsRefusedWhileVoiceIsOff`: no event is queued.
    - `QueuesDownThenUp`.
    - `LosingFocusCancels`.
    - `AHoldOverAMinuteCancels`, with a clock you can set.
    - `TheTalkMenuQueuesToggle`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the C# tests.** Expected: Passed. Then check by hand:
  1. Open `unity_package/TestProject` in Unity.
  2. Run `python3 -c "from gazevoice.blender_socket import bridge_request; print(bridge_request({'cmd': 'ping'}, port=9878))"`. It prints the Unity ping.
  3. `bridge_request({'cmd': 'status', 'text': 'Hello', 'state': 'done'}, port=9878)` shows the overlay for 6 s.
  4. ⌥ Space shows "Voice is off".
- [ ] **Step 5: Commit** with the message "Unity bridge: socket server, selection, Scene view status and hold-to-talk".

#### Task 8: One undo step, Play mode and script reloads

**Files:**
- Create in `unity_package/com.gazevoice.bridge/Editor/`:
  - `UndoGroup.cs`
  - `ChangeTracker.cs` (`ObjectChangeEvents`, `Undo.postprocessModifications`, plus an `AssetPostprocessor` for created assets)
  - `BridgeState.cs` (the `SessionState` keys below)
- Modify: `UnityScene.cs`, `BridgeLifetime.cs`
- Test: `Tests/Editor/UndoGroupTests.cs`, `ReloadTests.cs`, `PlayModeTests.cs`

**Interfaces:**
- `UndoGroup`:
  - `void Begin()` increments the group, sets `Mark` and clears the tracker;
  - `bool Push(string name)` collapses to `Mark`, names the step (in the order S4 found) and increments;
  - `int Mark`, which is -1 when no edit is open.
- `ChangeTracker`:
  - `bool Changed`;
  - `IReadOnlyList<string> Created`;
  - `void Reset()`.
- `BridgeState.Save(UndoGroup, ChangeTracker, Status, bool holding)` and `BridgeState.Restore(...) -> bool wasHolding`. The keys are:
  - `GazeVoice.Mark`
  - `GazeVoice.Changed`
  - `GazeVoice.Created`
  - `GazeVoice.Status` (JSON)
  - `GazeVoice.Holding`
- **Reload hooks:**
  - `beforeAssemblyReload` saves the state and stops the server;
  - the static constructor restores the state, starts a new `EventQueue`, and queues "cancel" when `wasHolding`.

- [ ] **Step 1: Write the failing tests:**
  - `UndoGroupTests`:
    - `OneUndoStepForEverythingSinceBegin`: `[UnityTest]`.
      1. `Begin()`.
      2. Create three GameObjects with `Undo.RegisterCreatedObjectUndo`, each after `Undo.IncrementCurrentGroup()`, like separate tool calls.
      3. Wait a frame.
      4. `Push("GazeVoice: Make three cubes")`.
      5. One `Undo.PerformUndo()` removes all three, and one `PerformRedo()` brings them back.
    - `TheStepIsNamedAfterTheCommand`: after `Push`, the Edit menu's undo text, read through `Undo.GetCurrentGroupName()` before the increment, is the name.
    - `NothingRecordedMeansNothingPushed`: `undo_push` with `only_if_changed` answers `pushed: false`.
    - `ChangesWithoutUndoAreNotSeen`: this documents the limit. A direct `transform.position` set leaves `Changed == false`.
    - `CreatedAssetsAreListed`: a material saved under `Assets/GazeVoice/` appears in `Created`.
  - `ReloadTests`:
    - `TheUndoGroupSurvivesAScriptReload`: `[UnityTest]`.
      1. `Begin()`.
      2. Create one object with Undo.
      3. Write `Assets/GazeVoiceReloadProbe.cs`, call `AssetDatabase.Refresh()`, and `yield return new WaitForDomainReload()`.
      4. `Mark` and `Changed` are restored.
      5. Create a second object.
      6. `Push`, then one undo removes both objects.
      7. Delete the probe and wait for the reload again.
    - `AHoldCutOffByAReloadBecomesCancel`.
  - `PlayModeTests`:
    - `EditsAreRefusedInPlayMode`: `yield return new EnterPlayMode()`; `begin_edit`, `undo` and `redo` answer "playing" with `PlayingMessage`; `yield return new ExitPlayMode()`.
    - `UndoPushIsRefusedInPlayMode`: `Begin()` in Edit mode, enter Play mode, and `undo_push` is refused with the message.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the C# tests.** Expected: Passed.
- [ ] **Step 5: Commit** with the message "Unity bridge: one undo step per command, Play mode refusals, surviving script reloads".

### Phase 3: Unity units in the rack

Until Task 13 defines the Unity target, the new units sit in the rack's "Other" shelf. Blender is untouched.

#### Task 9: Finding Unity

**Files:**
- Create: `gazevoice/components/unity.py`
- Modify:
  - `gazevoice/components/__init__.py`: register `Unity` after `Bridge`;
  - `gazevoice/settings.py`: `unity_project: str = ""`, which must be `""` or an absolute, printable path of at most 1024 characters;
  - `tests/helpers.py`: `fake_unity_app(root, version) -> Path` writes `Unity.app/Contents/Info.plist` with `plistlib`, and `with_unity(t, versions=("6000.3.7f1",), running=False)`.
- Test: `tests/test_unity.py`

**Interfaces:**
- `HUB_APP = Path("/Applications/Unity Hub.app")`
- `EDITOR_ROOT = Path("/Applications/Unity/Hub/Editor")`
- `HUB_CONFIG = Path.home() / "Library/Application Support/UnityHub"`
- `MIN_UNITY = (6000, 0)`
- `RECOMMENDED = "Unity 6.3 LTS"`
- `@dataclass(frozen=True) class UnityEditor(version: str, app: Path)`, with a `.binary` property
- `parse_unity_version(text) -> tuple[int, int, int, str] | None`, so `"6000.3.7f1"` gives `(6000, 3, 7, "f1")`
- `installed_editors(roots=None, hub_config=None) -> list[UnityEditor]` scans `EDITOR_ROOT` and the path in `secondaryInstallPath.json`. It reads `CFBundleVersion` and skips any app whose plist it can't read.
- `project_editor_version(project: Path) -> str | None` reads `m_EditorVersion`.
- `is_unity_running(runner, probes=None) -> bool` runs `pgrep -x Unity`.
- `class Unity(Component)`: `id = "unity"`, `name = "Unity"`, `role = "Game engine"`. Its actions are `install_hub`, `open_hub` and `launch`, as in the 2.3 table.

- [ ] **Step 1: Write the failing tests:**
  - `test_parse_unity_version`
  - `test_installed_editors_read_info_plist`: two fake apps and one broken one give two editors.
  - `test_the_secondary_install_path_is_scanned`
  - `test_project_editor_version`
  - `test_no_hub_offers_homebrew_install`: the confirm mentions Homebrew.
  - `test_no_hub_and_no_homebrew_links_out`
  - `test_hub_without_unity_6_only_opens_the_hub`: no install action, and the summary mentions signing in and the Personal license.
  - `test_the_projects_editor_missing_is_an_error_naming_the_version`
  - `test_an_old_editor_is_an_error`
  - `test_ready_offers_launch_into_the_project`: `launch` runs `["open", "-a", "<app>", "--args", "-projectPath", "<project>"]`.
  - `test_running_unity_offers_no_launch`
  - `test_unity_project_setting_must_be_an_absolute_path`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the Unity unit: find Unity Hub and editors, launch the project".

#### Task 10: The Unity project unit and safe manifest edits

**Files:**
- Create: `gazevoice/unity_manifest.py`, `gazevoice/components/unity_project.py`
- Modify:
  - `gazevoice/components/__init__.py`;
  - `gazevoice/server/static/app.js`: when the `unity_project.choose` job ends, PUT `{"unity_project": result.path}`. The unit also gets a path field that PUTs the same setting.
- Test: `tests/test_unity_manifest.py`, `tests/test_unity_project.py`

**Interfaces:**
- `ManifestError(Exception)`
- `read_manifest(project: Path) -> dict`
- `dependency(project, name) -> str | None`
- `locked_version(project, name) -> str | None`, from `Packages/packages-lock.json`
- `manifest_sha(project) -> str`
- `manifest_line(name, ref) -> str`, which gives `'"name": "ref"'` for confirm texts
- `set_dependency(project, name, ref: str | None, expect_sha: str) -> None`:
  - `ref=None` removes the line;
  - the other keys and their order are kept;
  - it writes 2-space indented JSON with a final newline;
  - the previous file is copied to `manifest.json.gazevoice-backup`;
  - the write is atomic (tempfile, then `os.replace`);
  - it raises `ManifestError` if the JSON is invalid or the sha differs.
- `project_problem(path: Path) -> str | None` returns a plain sentence, or None for a real project.
- `render_pipeline(project) -> str` gives `"URP"`, `"HDRP"` or `"Built-in"`.
- `class UnityProject(Component)`: `id = "unity_project"`, `name = "Unity project"`, `role = "The project GazeVoice edits"`. `action_choose` runs `osascript -e 'POSIX path of (choose folder with prompt "Pick your Unity project folder")'` and returns `{"path": …}`. It checks the path and changes no settings itself.

- [ ] **Step 1: Write the failing tests:**
  - `test_set_dependency_keeps_order_and_format`
  - `test_removing_a_dependency`
  - `test_the_backup_holds_the_previous_file`
  - `test_invalid_json_is_refused_and_left_alone`
  - `test_a_file_changed_since_it_was_shown_is_refused`
  - `test_locked_version`
  - `test_no_project_offers_choose`
  - `test_choose_returns_the_picked_folder`: osascript prints `"/Users/x/Game/\n"`.
  - `test_a_cancelled_picker_is_a_plain_error`: exit 1 gives "No folder was picked."
  - `test_a_folder_that_isnt_a_project_is_an_error`
  - `test_a_project_shows_its_editor_and_pipeline`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the Unity project unit and safe manifest edits".

#### Task 11: The GazeVoice Unity Bridge unit and a fake Unity bridge

**Files:**
- Create: `gazevoice/components/unity_bridge.py`, `tests/fake_unity_bridge.py`
- Modify:
  - `gazevoice/paths.py`: `unity_packages` is `root / "unity"`;
  - `gazevoice/components/__init__.py`.
- Test: `tests/test_unity_bridge_component.py`, and an extended `tests/test_unity_package.py`

**Interfaces:**
- `UNITY_BRIDGE_PORT = 9878`
- `UNITY_BRIDGE_VERSION = "0.1.0"`
- `PACKAGE_NAME = "com.gazevoice.bridge"`
- `PACKAGE_SRC = <repo>/unity_package/com.gazevoice.bridge`
- `copy_package(src, dest) -> Path` stages a copy next to `dest`, then swaps it in. It skips `Library`, `obj` and `.DS_Store`.
- `class UnityBridge(Component)`: `id = "unity_bridge"`, `name = "GazeVoice Unity Bridge"`, `role = "Our Unity package · selection, status line, undo"`. It has states and actions as in 2.3. The manifest ref is `"file:" + str(paths.unity_packages / PACKAGE_NAME)`.
- `FakeUnityBridge(project="/tmp/Game", version="0.1.0", unity="6000.3.7f1")` is a context manager with:
  - `.port` and `.requests`;
  - settable `.playing`, `.compiling`, `.focused`, `.selection`, `.names` and `.replies`, where `.replies` overrides the reply for a given cmd;
  - `.go_away(seconds)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_no_project_means_pick_one_first`
  - `test_not_in_the_manifest_offers_add_with_the_line_in_the_confirm`
  - `test_add_copies_the_package_and_writes_the_file_line`
  - `test_a_line_pointing_elsewhere_offers_update`
  - `test_unity_running_but_not_answering_says_switch_to_unity`
  - `test_an_old_bridge_offers_update`
  - `test_bridge_from_another_project_is_not_ready`
  - `test_ready_offers_test_and_test_shows_the_status_line`
  - `test_remove_takes_the_line_out`
  - In `test_unity_package.py`: `test_python_and_csharp_agree_on_the_bridge_version_and_port`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the GazeVoice Unity Bridge unit".

#### Task 12: The Unity MCP package and server units (shaped by the spike)

**Files:**
- Create: `gazevoice/components/unity_mcp.py`
- Modify: `gazevoice/components/__init__.py`
- Test: `tests/test_unity_mcp.py`

**Interfaces.** These assume the recommended server. The values marked S… come from the spike.
- `CLI_CASK = "unity-cli"`
- `PIPELINE_PACKAGE = "com.unity.pipeline"`
- `PIPELINE_VERSION = "<S0>"`
- `PORT_FILE = "<S1>"`, which is `Library/Pipeline/.unity-pipeline-port` per Unity's docs
- `ASSISTANT_MIN = (2, 13)`
- `find_unity_cli(runner) -> Path | None` tries `which unity` and then the S0 path.
- `unity_mcp_command(cli: Path, project: Path) -> dict` returns `{"command": str(cli), "args": ["mcp", *<S2 project args>], "env": {<S10 opt-outs>}}`.
- `mcp_listening(project) -> bool` reads only the port from `PORT_FILE`, then checks `port_open`.
- `class UnityMcpPackage(Component)`: `id = "unity_mcp_package"`, `name = "Unity MCP package"`, `role = "Lets AI tools act inside Unity"`.
- `class UnityMcpServer(Component)`: `id = "unity_mcp_server"`, `name = "Unity MCP server"`, `role = "Speaks MCP for Claude Code / Codex"`. Its `command_override` lets tests use `tests/fixtures/fake_mcp_server.py`.
- **If the spike picked CoplayDev instead,** the ids and states stay the same, and these values change:
  - package `com.coplaydev.unity-mcp` at `https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#v10.3.0`;
  - server `uvx --from mcpforunityserver==10.3.0 mcp-for-unity --transport stdio`, with env `DISABLE_TELEMETRY=true` and `UNITY_MCP_DISABLE_FOCUS_NUDGE=1`;
  - "listening" means TCP 6400, found through `~/.unity-mcp/unity-mcp-status-*.json`;
  - Install for the server becomes `uv`'s check, as for Blender.

- [ ] **Step 1: Write the failing tests:**
  - `test_package_missing_offers_add_with_the_line_in_the_confirm`
  - `test_add_and_remove_edit_only_that_line`
  - `test_an_old_ai_assistant_package_is_a_conflict`
  - `test_listening_reads_only_the_port`
  - `test_the_token_never_leaves_the_port_file`: the token string isn't in the state's JSON, the job log or the command.
  - `test_no_cli_offers_homebrew_install`
  - `test_signed_out_cli_says_to_log_in`, if S1 shows it's needed
  - `test_test_lists_tools_and_reads_the_scene_when_listening`, through the fake MCP server
  - `test_test_without_unity_still_lists_tools`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Add the Unity MCP package and server units".

#### Task 13: The Unity target and its prompts

**Files:**
- Create: `gazevoice/targets/unity.py`, `gazevoice/voice/prompts/unity_editor.md`, `gazevoice/voice/prompts/unity_refiner.md`
- Modify:
  - `gazevoice/targets/__init__.py`: `TARGETS` adds `"unity"`;
  - `gazevoice/voice/commands.py`: adds `UNITY_TERMS`, and "unity" joins `_LEAD_IN` and `_TRAILER`;
  - `gazevoice/components/unity_mcp.py`: adds the unresolved-package check.
- Test: `tests/test_targets.py`, `tests/test_voice_commands.py`, `tests/test_unity_mcp.py`

**Interfaces:**
- Produces: `UNITY: Target`, with these values:
  - `units`, `app_parts`, `edit_parts` and `check_steps` as in 2.1 and 2.3, with `check_steps = (("unity_mcp_server", "test"), ("unity_bridge", "test"))`;
  - `needed`: `unity`, `unity_project`, `unity_mcp_package` and `unity_bridge` must be READY, and `unity_mcp_server` READY or INSTALLED;
  - `connect_action = "connect_unity"`, `connect_label = "Connect to Unity"`;
  - `connect_prompt = "Use the unity tools to count the GameObjects at the root of the open scene. Reply with only the number."`;
  - `bridge_port = 9878`, `version_key = "unity"`, `mcp_name = "unity"`;
  - `read_only_tools`, `blocked_tools` and `api_tools` from S2 and S9;
  - `undo_name_bytes = 120`, `busy_wait = 60.0`, `edit_seconds = 600.0`, `wait_when_unreachable = True`;
  - `not_running = "Unity isn't connected. Open your project in Unity with the GazeVoice Unity Bridge added, then turn voice on."`;
  - `disconnected = "Unity isn't answering. It may be reloading scripts. Voice picks up again when it's back."`;
  - `server_command = unity_mcp_command(find_unity_cli(ctx.runner), Path(ctx.settings.unity_project))`;
  - `mcp_reachable` is true when the CLI is found and `mcp_listening(project)`.
- `UNITY_TERMS` is a comma-separated string for Whisper: Unity, GameObject, prefab, component, Rigidbody, collider, MonoBehaviour, ProBuilder, URP, material, shader, skybox, Inspector, Hierarchy, Scene view, NavMesh, Animator, Cinemachine, particle system, point light, directional light, spot light, terrain, transform, parent, child.

- [ ] **Step 1: Write the failing tests:**
  - `test_unity_target_fields`
  - `test_unity_prompts_exist_and_cover_the_rules`:
    - the editor prompt names the S3 tool and contains `Undo.RegisterCreatedObjectUndo`, `Play mode`, `Assets/GazeVoice/` and `compile error`;
    - the refiner prompt contains `GameObject`, `Rigidbody`, `"kind"` and `"title"`.
  - `test_saying_unity_before_stop_is_still_stop`: `meta_command("unity, stop") == "stop"`.
  - `test_whisper_hint_has_unity_terms`: `vocabulary_prompt(["Tower"], UNITY_TERMS)` contains `GameObject` and `Tower`.
  - `test_unresolved_package_is_an_error`: Unity is running, and `packages-lock.json` is newer than the manifest but lacks the package. The unit shows an error saying to open Window › Package Manager.
  - `test_snapshot_shows_a_unity_shelf`: the units leave "Other".
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** The prompt text is 2.4's, with the S2 and S3 tool names filled in.
- [ ] **Step 4: Run the full suite.** Expected: OK. The dashboard now shows a Unity shelf, and the target can't be selected yet.
- [ ] **Step 5: Commit** with the message "Describe Unity as a target, with its own prompts".

### Phase 4: Voice in Unity

#### Task 14: Brains connect to Unity

**Files:**
- Modify: `gazevoice/components/brains.py`, `gazevoice/components/providers.py` (the provider's Test logs the tool count and size)
- Test: `tests/test_brains.py`, `tests/test_provider_units.py`

**Interfaces:**
- Consumes: `UNITY` (Task 13), and `_Brain.connect` (Task 2).
- Produces:
  - `ClaudeCode.action_connect_unity` uses `--mcp-config mcp-unity.json --allowedTools mcp__unity`, plus the blocked tools and `--max-turns 6`.
  - `Codex.action_connect_unity` uses `codex_mcp_args(cmd, "unity", disabled=UNITY.blocked_tools if S8 else ())`.

- [ ] **Step 1: Write the failing tests:**
  - `test_connect_unity_uses_only_the_unity_mcp`
  - `test_connect_unity_is_offered_only_when_unity_listens`
  - `test_codex_connect_unity_blocks_tools_only_if_supported`
  - `test_provider_test_reports_the_tool_count_and_size`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK.
- [ ] **Step 5: Commit** with the message "Let Claude Code and Codex connect to Unity".

#### Task 15: Switch voice to Unity

**Files:**
- Modify:
  - `gazevoice/settings.py`: `TARGET_IDS = ("blender", "unity")`;
  - `gazevoice/server/app.py`: `_voice_config_now` adds `unity_project`;
  - `gazevoice/voice/service.py`: two changes:
    - `poll` replies with `"focused": false` set the reason to "Unity is in the background, so edits run slowly until you switch back to it.", which clears when focus returns;
    - `record["assets"]` takes `undo_push`'s `assets`.

    An `undo_push` refused while playing already fails the record with the bridge's message (Task 3). This task only tests it.
- Test: `tests/test_voice_unity.py`, which runs the real `BridgeClient` against `FakeUnityBridge`, with the fakes from `tests/voice_fakes.py`.

**Interfaces:**
- Consumes: Tasks 3, 4, 11 and 13.

- [ ] **Step 1: Write the failing tests:**
  - `test_happy_path_in_unity`:
    - the selection's paths reach the editor message;
    - one `undo_push` is sent, named "GazeVoice: …" within 120 bytes.
  - `test_a_reload_mid_edit_still_leaves_one_undo_step`:
    - the fake goes away for 1 s during the edit and comes back with a new boot token;
    - the status stays "Working…";
    - `undo_step` is True.
  - `test_unity_in_the_background_is_named_in_the_reason`
  - `test_play_mode_during_an_edit_is_reported`: the record fails with `PLAYING`.
  - `test_begin_edit_waits_while_unity_compiles`: the fake is busy for 0.5 s, then the edit runs.
  - `test_files_a_command_created_are_recorded`
  - `test_voice_restarts_when_the_target_or_project_changes`: `needs_restart` is True after a PUT of `target` or `unity_project`.
  - `test_unity_readiness_lists_unity_units`
  - `test_check_everything_for_unity_runs_unity_tests_then_connect_unity`
  - `test_build_voice_for_unity_uses_port_9878_and_mcp_unity`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite.** Expected: OK. The Blender voice tests are unchanged.
- [ ] **Step 5: Commit** with the message "Let voice drive Unity".

#### Task 16: The dashboard's "Edit in" switch and the Unity shelf

**Files:**
- Modify:
  - `gazevoice/server/static/app.js`
  - `gazevoice/server/static/index.html`
  - `gazevoice/server/static/app.css`
  - `DESIGN.md`: the switch and the collapsed shelf, in the existing component spec style.

**Interfaces:**
- Consumes: the snapshot's `target`, `targets` and `step_stages`, and `voice.app` (Tasks 4 and 15).

**What to build:**
- **Routing.** Add "Edit in" as the first row: a two-way segmented control that PUTs `target`. When voice is on, it shows the existing "Restart voice to apply" note.
- **Rack.** The active target's shelf shows in full. The other target's shelf is one row: its name, its overall health LED, and "Switch in Routing".
- **Unity project unit.** It gets a path field and a **Choose…** key.
- **Copy.** It follows the target:
  - the skeleton strip's last label;
  - "In Unity, hold ⌥ Space, say what you want, and let go. Your selection is what “this” means.";
  - the empty-history hint, with "make this twice as tall" kept;
  - `KIND_WORDS.ignore` becomes "Not a Unity command".

- [ ] **Step 1: Implement.**
- [ ] **Step 2: Check by hand,** with `python3 -m gazevoice`:
  1. Switch to Unity: the strip's last stage reads Unity, the Blender shelf collapses, and the Unity shelf opens.
  2. Switch back: everything reads exactly as before Task 1.
  3. With voice on, switching shows the restart note.
  4. Check keyboard focus order and screen-reader labels on the switch.
  5. Take `?snapshot` screenshots in both states.
- [ ] **Step 3: Run the full suite.** Expected: OK.
- [ ] **Step 4: Commit** with the message "Add the Edit in switch and the Unity shelf to the dashboard".

### Phase 5: Docs, a real project and review

#### Task 17: Docs, the real-project checklist and a final review

**Files:**
- Modify:
  - `README.md`: a "Talk to Unity" section covering setup unit by unit, what to expect, the files it creates, Play mode, and Unity in the background;
  - `PRODUCT.md`: the components and capabilities;
  - `docs/superpowers/specs/2026-10-18-gazevoice-design.md`: a Unity line in Phasing.

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run the C# tests in batch mode and the full Python suite.** Expected: both pass.
- [ ] **Step 3: Run the checklist in section 4 on a real project.** It uses your plan; the cost is noted on each item. Write down any failures, each with a test and a fix.
- [ ] **Step 4: Final review.** A fresh reviewer goes over the whole branch. Each fix it asks for comes with a test.
- [ ] **Step 5: Commit** with the message "Document Unity support".

### Later (not in this plan)

- **Follow the key:** keep both apps connected and act on whichever one you're holding the key in.
- **Build in Blender, use in Unity:** a hand-off that exports Blender builds as FBX into `Assets/GazeVoice/Models/`.
- **Gaze:** the `pick` command (2.2) plus the eye tracker's mapping onto the Scene view.

---

## 4. Manual checklist for a real project

Use a fresh Unity 6.3 URP project. Items marked ($) use a little of your plan.

1. In the dashboard:
   - pick the project;
   - choose **Add to project** on the MCP package unit and on the Bridge unit;
   - switch to Unity so it imports both.

   Every Unity unit should show Ready.
2. Check everything passes. ($)
3. Set Edit in → Unity, then Start voice. The Scene view overlay reads "Ready · hold ⌥ Space to talk".
4. Select Cube and say "make it red". ($)
   - Only Cube turns red.
   - The Edit menu shows "Undo GazeVoice: Make it red".
   - ⌘Z reverts it.
5. Say "make it twice as tall". ($) Only the Y scale changes.
6. Say "add three trees around the house". ($)
   - You get a named hierarchy.
   - One undo removes all of it.
7. Say "make the cube spin slowly". ($)
   - A script appears in `Assets/GazeVoice/Scripts/`, compiles, and is attached.
   - The overlay stays "Working…" through the reload.
   - One undo removes the component.
   - The record lists the script file, which stays on disk.
8. Say "stop" during a long build. ($)
   - It stops at once.
   - What it did so far is one "(stopped)" undo step.
9. Say "undo", then "redo".
10. Press Play and say "make it blue". It's refused with the Play mode line, and nothing changes.
11. Hold ⌥ Space in the Inspector's name field. It types and doesn't talk.
12. Hold the key, then ⌘Tab away. The hold is cancelled.
13. Type a command in the dashboard while Unity is in the background. ($)
    - The reason line says Unity is in the background.
    - The command finishes once you switch back.
14. Quit and reopen Unity while voice is on. Voice picks up again by itself.
15. Set Edit in → Blender and repeat items 4, 8 and 9 in Blender. ($) Everything works exactly as before.
16. Run the EditMode tests in batch mode with the project closed. They pass.

---

## 5. Risks and unknowns

| # | Risk or unknown | How we find out, and what we do |
|---|---|---|
| R1 | Unity's CLI MCP is experimental and changes fast. The Hub updates the CLI by itself | The spike gates; pinned package versions; the server unit shows the CLI version and warns when it isn't the tested one. Fallback: CoplayDev |
| R2 | `unity mcp` may not expose a multi-statement C# tool that avoids a domain reload | S3 and G2 |
| R3 | Collapsing over the pipeline's own undo scopes may not give one named step | S4(d) and Task 8's tests |
| R4 | Undo history and `GetCurrentGroup` across a domain reload are undocumented | S5 and Task 8 `TheUndoGroupSurvivesAScriptReload` |
| R5 | Unity throttles itself in the background, by design and with no setting. Compiles can stall until Unity is focused | S6 measures it; the `focused` flag names it in the Voice panel; the README says to keep Unity in front |
| R6 | Your own clicks during a long command merge into its undo step | Accepted. S4 observes it, and the README says so |
| R7 | The AI changes something without an Undo call, so it can't be undone and isn't seen | The prompt rules; S4(c); the record shows "no undo step". A scene snapshot could come later if this bites |
| R8 | Files the AI creates (scripts, saved materials and prefabs) aren't undoable | Kept under `Assets/GazeVoice/` and listed in each record. Q7 asks whether undo should delete them |
| R9 | The Personal license in batch mode (`Unity_lic.ulf` isn't always written by Hub) | S12; if it fails, the C# tests run from the Test Runner window by hand |
| R10 | Port 9878 is taken, or a second Unity editor is open | The bind error shows in the overlay; ping's `project` lets the unit say which project is connected |
| R11 | A `file:` path containing spaces ("Application Support") | S12. Fallback: copy the package into a path without spaces under `~/.gazevoice/` |
| R12 | ⌥ Space is claimed by Unity or another app | S13; you can rebind it in Edit › Shortcuts, and the dashboard shows the bound key |
| R13 | The Unity tool list is too big for API models | S9, then the `api_tools` allowlist |
| R14 | Codex with a second MCP server, and blocking its tools | S8; Codex stays experimental |
| R15 | The CLI needs a Unity sign-in for MCP | S1, then a unit message telling you to run `unity auth login` |
| R16 | Telemetry or phone-home by the CLI or the package | S10; opt-outs go into `server_command`'s env |
| R17 | Memory on 16 GB with Unity, the speech model, brains and Blender all running | S6 measures it; the README suggests quitting Blender while you work in Unity |
| R18 | An AI-written compile error blocks the whole project | The prompt rule; the bridge answers busy while compiling; S5 tries a deliberate error |

---

## Questions for me

Most important first. Each has my guess, and the plan uses that guess until you answer.

1. **Which MCP server goes to the spike?** My guess is Unity's own CLI MCP (`unity mcp` + `com.unity.pipeline`), with CoplayDev's MCP for Unity as the fallback. Its experimental status and a possible Unity sign-in are the trade-offs. OK? (D3)
2. **Your Unity setup:**
   - Is Unity installed on this Mac, and which version?
   - Do you have a Unity ID with a Personal license?
   - Is a new throwaway project fine for the spike? My guess: it isn't installed yet, and 6.3 LTS with a throwaway project is fine. (A1)
3. **One app at a time with an "Edit in" switch,** rather than both connected and following the key? My guess is the switch, with following the key as a later step. (D2)
4. **Is my ranking of Unity work right?** The order is scene layout, objects and prefabs, materials and lighting, behaviours, then models. And are clearly labeled stand-ins acceptable for organic models until a Blender hand-off exists? (D1, 2.7)
5. **Play mode:** refuse edits with a clear line (my guess), or allow them with a warning? (A2)
6. **Files the AI creates:** is `Assets/GazeVoice/` OK? Should undo leave them on disk and list them (my guess), or also delete the ones that command created? (A5, R8)
7. **The bridge package:** referenced with one `file:` line from GazeVoice's app folder (my guess), or copied into the project's `Packages/` folder, where it shows up in your version control? (A4)
8. **Limits:** keep 150 steps and 10 minutes for Unity (my guess), or allow more for builds that compile scripts? (A6)
9. **The key:** ⌥ Space in Unity too? (A3)
10. **When we build:** subagent-driven, with a fresh implementer and reviewer per task, or native in one session? I recommend subagent-driven. The C# package and the Python target refactor are separate skills, and a mistake in the shared protocol would hit both apps.
