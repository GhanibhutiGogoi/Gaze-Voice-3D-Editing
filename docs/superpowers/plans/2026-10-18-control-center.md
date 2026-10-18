# GazeVoice Control Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local dashboard that shows every GazeVoice component and installs each one when the user clicks. It also connects them and proves the workflow is ready.

**Architecture:** One Python package `gazevoice/`, standard library only. Each component is a small class with `check()` and `action_*()` methods. Long actions run as background jobs that stream log lines over Server-Sent Events. A `ThreadingHTTPServer` on 127.0.0.1:8977 serves a JSON API and a static single-page UI written in vanilla HTML/CSS/JS. Our Blender add-on `gazevoice_bridge` lives in `blender_addon/`.

**Tech Stack:** Python 3.12+ stdlib (`http.server`, `subprocess`, `threading`, `urllib`, `zipfile`, `json`, `unittest`), vanilla JS/CSS, Blender 5.2 extension API, ffmpeg, uv, Homebrew.

**Spec:** `docs/superpowers/specs/2026-10-18-gazevoice-design.md`

## Global Constraints

- **Python and dependencies:**
  - Python ≥ 3.12.
  - `gazevoice/` imports only the standard library.
  - Tests use `unittest`. Run them with `python3 -m unittest discover -s tests -t . -v`.
- **Subprocesses:** never use `shell=True`. Always pass argv lists, because app paths contain spaces (`~/Library/Application Support/...`).
- **Novo guard:** no file may contain LLM-vendor SDK imports or installs, API-key variable names, or API hostnames. Models are reached only through the `claude` and `codex` commands.
- **User-triggered only:** nothing installs, downloads, records audio or calls a model except inside an action the user triggers.
- **Network:**
  - The server binds `127.0.0.1` only, on default port `8977`.
  - It rejects any request whose Host isn't `127.0.0.1:<port>` or `localhost:<port>`.
  - POST and PUT also need the header `X-GazeVoice: 1`, and a matching Origin if one is present.
- **App data root:** `~/Library/Application Support/GazeVoice`, overridable with the env var `GAZEVOICE_HOME`.
- **Pinned values:**
  - Blender minimum is `(5, 1)`.
  - Official add-on: id `mcp`, port `9876`.
  - Release `v1.0.3`:
    - add-on zip: `https://projects.blender.org/lab/blender_mcp/releases/download/v1.0.3/mcp-1.0.3.zip`
    - server bundle: `https://projects.blender.org/lab/blender_mcp/releases/download/v1.0.3/blender-1.0.3.mcpb`
  - Bridge: id `gazevoice_bridge`, version `0.1.0`, port `9877`.
  - Speech engines:
    - `parakeet` = package `parakeet-mlx`, module `parakeet_mlx`, model `mlx-community/parakeet-tdt-0.6b-v2`.
    - `whisper` = package `mlx-whisper`, module `mlx_whisper`, model `mlx-community/whisper-large-v3-turbo`.
- **No commits** until the user OKs committing. Work stays in the working tree.

## Review Focus

1. **Another site attacks the dashboard:** any web page the user visits can POST to `http://127.0.0.1:8977/api/...`. The server must reply 403 without the `X-GazeVoice` header or with a foreign Host or Origin, so no install runs. Tested in Task 6.
2. **A slow or hung check:** `Blender --version`, `mdfind` or `claude auth status` can stall, and on a busy Mac all of them slow down together. The dashboard must still load within its 8 s budget.
   - A check that overruns shows `checking` "Still checking…" (never a fault), or its last known state. Its late result is published as `component.updated`.
   - A preferred brain that's still checking blocks the fallback to Codex.
   - Read-only probes are shared and cached briefly (`ProbeCache`). Refresh (`?fresh=1`) and any finished job clear the cache.
   - Tested in Task 6 and `tests/test_probes.py`. (Changed during the build after measuring 14 s checks on this Mac under load.)
3. **Double clicks:** clicking Install twice, or any action while that component has a running job, returns 409 "already running" and never starts a second `brew` or `uv` run. Tested in Task 2.
4. **Failed or interrupted downloads** (403, 404, offline): the job fails with the HTTP status in its message, and no partial file is left at the final path (only `*.part`, which gets deleted). Tested in Task 9.
5. **Missing prerequisites** (no Homebrew, no uv, no Blender when installing add-ons): the card says exactly what to install first and offers no action that is bound to fail. Tested in Tasks 5, 8, 9 and 10.

---

### Task 1: Scaffold, command runner, app paths

**Files:**
- Create: `pyproject.toml`, `gazevoice/__init__.py`, `gazevoice/runner.py`, `gazevoice/paths.py`, `tests/__init__.py`, `tests/test_runner.py`, `tests/test_paths.py`

**Interfaces:**
- Produces:
  - `gazevoice.__version__ = "0.1.0"`
  - `CommandResult(argv: tuple[str, ...], returncode: int, stdout: str, stderr: str)`, frozen, with property `.ok`
  - `CommandRunner.which(name) -> str | None`
  - `CommandRunner.run(argv, *, timeout=30.0, env=None, cwd=None, input_text=None) -> CommandResult`
  - `CommandRunner.stream(argv, on_line, *, timeout=None, env=None, cwd=None) -> int`
  - `FakeRunner(which: dict[str, str] | None = None)` with `.add(argv, returncode=0, stdout="", stderr="", delay=0.0)` and `.calls: list[tuple[str, ...]]`
  - `AppPaths(root: Path)` with properties `downloads`, `venvs`, `logs`, `work`, `settings_file`, `mcp_server_dir` (= `root/"blender-mcp"`), and `.ensure()`
  - `default_paths() -> AppPaths`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_runner.py
def test_run_captures_output(self):
    r = CommandRunner().run([sys.executable, "-c", "print('hi')"])
    self.assertTrue(r.ok); self.assertEqual(r.stdout.strip(), "hi")
def test_run_missing_executable_returns_127(self):
    r = CommandRunner().run(["definitely-not-a-command-xyz"])
    self.assertEqual(r.returncode, 127); self.assertIn("not found", r.stderr)
def test_run_timeout_returns_124(self):
    r = CommandRunner().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
    self.assertEqual(r.returncode, 124); self.assertIn("timed out", r.stderr)
def test_stream_emits_lines_and_returns_code(self):
    lines = []
    code = CommandRunner().stream([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"], lines.append)
    self.assertEqual(lines, ["a", "b"]); self.assertEqual(code, 3)
def test_fake_runner_replays_and_records(self):
    f = FakeRunner(which={"brew": "/opt/homebrew/bin/brew"}); f.add(["brew", "--version"], stdout="Homebrew 4.6\n")
    self.assertEqual(f.run(["brew", "--version"]).stdout, "Homebrew 4.6\n")
    self.assertEqual(f.run(["nope"]).returncode, 127)
    self.assertEqual(f.calls, [("brew", "--version"), ("nope",)]); self.assertEqual(f.which("brew"), "/opt/homebrew/bin/brew")
# tests/test_paths.py
def test_env_override_and_ensure(self):  # GAZEVOICE_HOME=tmp → default_paths().root == tmp; ensure() creates downloads/venvs/logs/work
```

- [ ] **Step 2: Run** `python3 -m unittest discover -s tests -t . -v`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement.**
  - `pyproject.toml`: name `gazevoice`, `requires-python = ">=3.12"`, no dependencies, script `gazevoice = "gazevoice.__main__:main"`.
  - `runner.py`: `subprocess.run` and `Popen` with argv lists. `FileNotFoundError` becomes 127. `TimeoutExpired` becomes 124 with `"timed out after {t}s"`.
  - `stream` merges stderr into stdout, strips each line's trailing newline, and kills the process on timeout, returning 124.
  - `FakeRunner.run` honours `delay` via `time.sleep`, which Task 6's timeout test needs.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 2: Event bus and job manager

**Files:**
- Create: `gazevoice/events.py`, `gazevoice/jobs.py`, `tests/test_events.py`, `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `Event(kind: str, data: dict, seq: int)`
  - `EventBus.publish(kind, data) -> Event`
  - `EventBus.subscribe(maxsize=1000) -> Subscription`
  - `Subscription.get(timeout=None) -> Event | None` and `.close()`
  - `JobState` with values `running`, `succeeded`, `failed`
  - `Job(id, component_id, action, state, lines, started_at, finished_at=None, error=None, result=None)` with `.log(line)` and `.to_dict()`
  - `JobError(Exception)`
  - `JobAlreadyRunning(Exception)`
  - `JobManager(bus, max_history=100)` with `.start(component_id, action, fn: Callable[[Job], dict | None]) -> Job`, `.get(id)`, `.recent(limit=20)`, `.wait(id, timeout) -> Job`
  - Events published: `job.started` `{"job": dict}`, `job.log` `{"job_id", "line"}`, `job.finished` `{"job": dict}`

- [ ] **Step 1: Write failing tests**

```python
def test_subscriber_receives_events_in_order(self): ...        # publish a,b → get() returns a then b, seq increasing
def test_full_subscriber_drops_oldest_not_blocks(self): ...    # maxsize=2, publish 3 → first get() is the 2nd event
def test_job_success_records_result_and_lines(self):
    job = mgr.start("blender", "install", lambda j: (j.log("step 1"), {"ok": 1})[1])
    done = mgr.wait(job.id, 2); self.assertEqual(done.state, JobState.SUCCEEDED); self.assertEqual(done.result, {"ok": 1}); self.assertEqual(done.lines, ["step 1"])
def test_job_error_message_is_user_facing(self):  # fn raises JobError("Homebrew not found") → FAILED, error == "Homebrew not found"
def test_unexpected_exception_fails_with_traceback_lines(self):  # ValueError → FAILED, error startswith "Unexpected error", lines contain "Traceback"
def test_second_job_for_same_component_rejected(self):  # fn waits on an Event; second start("blender", ...) raises JobAlreadyRunning
def test_events_published(self):  # subscriber sees job.started, job.log, job.finished in order
```

- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Use a thread per job and a lock around the job table. Job ids come from `uuid4().hex[:8]`. Only the newest `max_history` finished jobs are kept.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 3: Component model, settings, registry

**Files:**
- Create: `gazevoice/settings.py`, `gazevoice/components/__init__.py`, `gazevoice/components/base.py`, `tests/test_settings.py`, `tests/test_components_base.py`

**Interfaces:**
- Produces:
  - `Settings(brain="auto", stt_engine="parakeet", mic_device="0")` with `.to_dict()`
  - `load_settings(path) -> Settings`. A missing or corrupt file gives the defaults.
  - `save_settings(path, s)`
  - `update_settings(s, changes) -> Settings`. Raises `ValueError` for an unknown key or a value outside `brain ∈ {auto, claude, codex}`, `stt_engine ∈ {parakeet, whisper}`, or a `mic_device` that isn't a digit string.
  - `Health` with values `missing`, `installed`, `ready`, `error`
  - `ActionSpec(id, label, confirm=None, uses_plan=False)`
  - `ComponentState(id, name, role, health, summary, details: tuple[tuple[str, str], ...] = (), actions: tuple[ActionSpec, ...] = ())` with `.to_dict()`
  - `Context(runner, paths, settings)`
  - `Component` (ABC) with attributes `id`, `name`, `role`, `__init__(ctx)`, abstract `check() -> ComponentState`, and `state(health, summary, details=(), actions=()) -> ComponentState`
  - `Component.run_action(action, job) -> dict | None` dispatches to `action_<id>` and raises `JobError(f"Unknown action {action}")` otherwise.
  - `build_components(ctx) -> list[Component]`, which each later task extends in pipeline order: microphone, speech, claude, codex, blender, mcp_addon, mcp_server, bridge.

- [ ] **Step 1: Write failing tests:** round-trip save/load; a corrupt file gives defaults; `update_settings({"brain": "gpt"})` raises `ValueError`; an unknown key raises `ValueError`; `ComponentState.to_dict()` gives `{"id","name","role","health":"ready","summary","details":[["Version","5.2.2"]],"actions":[{"id":"install","label":"Install","confirm":None,"uses_plan":False}]}`; `run_action("nope", job)` raises `JobError`.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Settings live at `paths.settings_file` as JSON.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 4: Brains (Claude Code, Codex)

**Files:**
- Create: `gazevoice/components/brains.py`, `tests/test_brains.py`
- Modify: `gazevoice/components/__init__.py`, to register both components

**Interfaces:**
- Consumes: Task 1 `CommandRunner` and Task 3 `Component`/`Context`.
- Produces:
  - `ClaudeCode`: id `claude`, name "Claude Code", role "Brain · first choice"
  - `Codex`: id `codex`, name "Codex", role "Brain · fallback"
  - `pick_brain(states: dict[str, ComponentState], preference: str) -> str | None`
  - `CLAUDE_ISOLATION = ["--tools", "", "--setting-sources", "project", "--disable-slash-commands", "--strict-mcp-config"]`
  - `claude_headless_argv(prompt: str, extra: Sequence[str] = ()) -> list[str]` returns `["claude", "-p", "--output-format", "json", *CLAUDE_ISOLATION, *extra, prompt]`
  - `codex_exec_argv(prompt: str, extra: Sequence[str] = ()) -> list[str]` returns `["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json", *extra, prompt]`
  - `parse_codex_jsonl(text: str) -> str | None`: the last agent-message text, read from `item.completed` events whose `item.type == "agent_message"`.
  - Action `test` (`uses_plan=True`) on both, which returns `{"reply": str, "seconds": float}`.

- [ ] **Step 1: Write failing tests** with a FakeRunner:
  - no `claude` on PATH gives `missing`, summary containing "Install Claude Code";
  - `claude auth status` exit 0 with stdout `{"authMethod":"claude.ai"}` gives `ready`, summary "Logged in · Claude subscription", and details including `("Version", "2.1.260")` (parsed from `--version` output `"2.1.260 (Claude Code)"`);
  - `authMethod` `"api_key"` gives summary "Logged in · API key";
  - exit 1 gives `installed`, summary containing "claude auth login";
  - codex `login status` stdout "Logged in using an API key - 049d***" gives `ready` "Logged in · API key", and "Logged in using ChatGPT" gives "Logged in · ChatGPT plan";
  - "Not logged in" gives `installed`, summary containing "codex login";
  - `pick_brain`: auto with both ready returns "claude"; auto with only codex ready returns "codex"; preference "codex" with codex not ready returns None;
  - `parse_codex_jsonl` returns "ready" from a two-line JSONL sample;
  - `test` action on Claude with faked JSON `{"type":"result","is_error":false,"result":"ready"}` returns reply "ready", and `is_error: true` raises `JobError`.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The test prompt is exactly `Reply with exactly the word: ready`.
  - Commands run with `cwd=paths.work` and `timeout=120`.
  - The token never appears in output: summaries never echo the `login status` text after "API key".
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 5: Blender

**Files:**
- Create: `gazevoice/components/blender.py`, `tests/test_blender.py`
- Modify: `gazevoice/components/__init__.py`

**Interfaces:**
- Produces:
  - `MIN_BLENDER = (5, 1)`
  - `find_blender(runner, candidates=None) -> Path | None`: returns the binary at `<app>/Contents/MacOS/Blender`. It tries `/Applications/Blender.app` and `~/Applications/Blender.app` first, then `mdfind "kMDItemCFBundleIdentifier == 'org.blenderfoundation.blender'"`.
  - `parse_blender_version(text) -> tuple[int, int, int] | None`
  - `blender_version(runner, binary) -> tuple | None`, cached by `(path, mtime)`
  - `extensions_dir(version, home=Path.home()) -> Path` returns `home/"Library/Application Support/Blender/{major}.{minor}/extensions/user_default"`
  - `install_extension(runner, binary, zip_path, job)` streams `[binary, "--command", "extension", "install-file", "-r", "user_default", "-e", str(zip_path)]`, and a non-zero exit raises `JobError`.
  - `is_running(runner) -> bool` uses `pgrep -x Blender`.
  - `Blender`: id `blender`, name "Blender", role "3D app", with actions `install` (confirm "Download and install Blender with Homebrew (~400 MB)?") and `launch`.
  - `app_path(binary) -> Path`

- [ ] **Step 1: Write failing tests:**
  - `parse_blender_version("Blender 5.2.2 LTS\n...") == (5, 2, 2)` and `parse_blender_version("garbage") is None`;
  - not found with brew present gives `missing` with an `install` action;
  - not found and no brew gives `missing`, summary containing "Homebrew", and no actions;
  - found at version 4.5.14 gives `error`, summary containing "5.1";
  - found at 5.2.2 and not running gives `ready`, summary "Blender 5.2.2 · not running", with a `launch` action;
  - `launch` runs `["open", "-a", "<app>", "--args", "--online-mode"]`;
  - `install` streams `["brew", "install", "--cask", "blender"]`;
  - `extensions_dir((5, 2, 2), home=tmp)` gives the documented path.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement**, with a 60 s timeout on `--version`.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 6: HTTP server, workflow strip, entry point

**Files:**
- Create: `gazevoice/workflow.py`, `gazevoice/server/__init__.py`, `gazevoice/server/app.py`, `gazevoice/__main__.py`, `gazevoice/server/static/index.html` (a placeholder until Task 7), `tests/test_workflow.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces:
  - `workflow_stages(states: dict[str, ComponentState], brain: str | None) -> list[dict]`. The stages, in order, are:
    - `listen`: `microphone`
    - `transcribe`: `speech`
    - `refine`: the brain
    - `edit`: the brain and `mcp_server`
    - `blender`: `blender`, `mcp_addon`, `bridge`
  - Each stage dict is `{"id", "label", "health"}`. The health is the worst of its parts, ordered error > missing > installed > ready. An absent component counts as `missing`, and so does a missing brain.
  - `ControlCenter(ctx, components, jobs, bus, check_timeout=8.0)` with:
    - `.snapshot(fresh=False) -> dict`, which returns `{"components": [...], "brain": str|None, "brain_pending": bool, "workflow": [...]}`. Checks run in parallel threads:
      - a check that overruns, or whose probe raises `ProbeTimedOut`, gives `checking` "Still checking…" (or its last known state);
      - an exception gives `error` "Check failed: <msg>";
      - `fresh=True` (`?fresh=1`) clears the probe cache first.
    - `run_action` refuses unit actions while "Check everything" runs, and the reverse, with `JobAlreadyRunning`.
    - `.run_action(cid, aid) -> Job`, which raises `KeyError` (unknown component), `ValueError` (action not offered in the current state) or `JobAlreadyRunning`.
    - `.settings_dict()`
    - `.update_settings(changes) -> dict`
  - `make_server(center, host="127.0.0.1", port=8977) -> ThreadingHTTPServer`
  - `main(argv=None) -> int` takes `--port` and `--no-browser`.
- Routes:

  | Method | Path | Response |
  |---|---|---|
  | GET | `/` | index |
  | GET | `/static/<name>` | files from the static dir only (404 otherwise) |
  | GET | `/api/state` | snapshot |
  | POST | `/api/components/<cid>/actions/<aid>` | 202 `{"job"}`, or 404, 400, 409 |
  | GET | `/api/jobs` | `{"jobs"}` |
  | GET | `/api/jobs/<id>` | the job |
  | GET | `/api/events` | SSE: `event: <kind>\ndata: <json>\n\n`, plus a `: ping` every 15 s |
  | GET | `/api/settings` | settings |
  | PUT | `/api/settings` | updated settings, or 400 |

- [ ] **Step 1: Write failing tests.** Start the server on port 0 in a thread and talk to it with `http.client`, using fake components.
  - `GET /api/state` returns 200 JSON with 2 components and a workflow;
  - POST without `X-GazeVoice` gives 403;
  - a `Host: evil.example:8977` header gives 403;
  - `Origin: http://evil.example` gives 403;
  - a valid POST gives 202 and the job finishes;
  - a second POST while it runs gives 409;
  - an unknown component gives 404;
  - an action not offered gives 400;
  - a component whose `check` sleeps 2 s with `check_timeout=0.3` makes `/api/state` return within 1 s with that component as `error` "Check timed out";
  - `GET /static/../settings.json` gives 404;
  - the SSE stream yields the `job.started` frame after a POST.
  - Separately, `workflow_stages` maps healths as specified.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The handler class is a `BaseHTTPRequestHandler` subclass with `protocol_version = "HTTP/1.1"`.
  - JSON responses send `Cache-Control: no-store`.
  - `main` builds the real `Context` (settings from `paths.settings_file`), prints `GazeVoice control center → http://127.0.0.1:<port>`, and opens the browser unless `--no-browser`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Smoke check.** Run `python3 -m gazevoice --no-browser --port 8977 &`, then `curl -s -H 'Host: 127.0.0.1:8977' 127.0.0.1:8977/api/state`. Expected: JSON listing claude, codex and blender with real states. Stop the server afterwards.

### Task 7: Dashboard UI

**Files:**
- Create: `gazevoice/server/static/index.html`, `app.css`, `app.js`

**Interfaces:**
- Consumes: Task 6 routes and the event kinds from Task 2.
- Produces: no Python API. The UI always calls with header `X-GazeVoice: 1`.

Behaviour, to be designed using the impeccable skill:
- **Header:** product name, overall readiness ("Workflow ready" or "N steps need attention"), the active brain, and Refresh.
- **Workflow strip:** the five stages from `workflow`. Each shows a status shape plus a text label, so meaning never depends on colour alone.
- **Component cards:** name, role, a status pill (Ready / Installed / Not installed / Problem), summary, a details list, and action buttons.
  - Show a confirm dialog when `confirm` is set.
  - Show a "uses your plan" tag when `uses_plan` is set.
  - While a job for that component runs, its button shows a spinner and is disabled.
- **Activity panel:** the jobs list with live log tails from SSE, expandable per job. On failure it shows the error.
- **Settings panel:**
  - brain (Auto / Claude Code / Codex)
  - speech engine (Parakeet / Whisper)
  - mic device, offered as a select of `Devices` from the microphone card's details when available
- **Other UI rules:**
  - "Check everything" appears when the snapshot lists a `workflow` component; it is added in Task 11.
  - No external network requests: no CDN and no web fonts.
  - Light and dark themes via `prefers-color-scheme`.
  - Works at a 1280×800 window and down to 900 px wide.
  - On `job.finished` it refetches `/api/state`.
  - SSE auto-reconnects.

- [ ] **Step 1: Build it.** Then serve with `python3 -m gazevoice --no-browser` and check it in the browser: cards render, a job's logs stream, and a failed job shows its error.
- [ ] **Step 2: Run the test suite again.** Expected: PASS.

### Task 8: Bridge add-on and its component

**Files:**
- Create:
  - `blender_addon/gazevoice_bridge/blender_manifest.toml`
  - `blender_addon/gazevoice_bridge/__init__.py`
  - `blender_addon/gazevoice_bridge/protocol.py`
  - `gazevoice/blender_socket.py`
  - `gazevoice/components/bridge.py`
  - `tests/test_bridge_protocol.py`
  - `tests/test_blender_socket.py`
  - `tests/test_bridge_component.py`
- Modify: `gazevoice/components/__init__.py`

**Interfaces:**
- `protocol.py` must not import `bpy`. It provides:
  - `BRIDGE_PORT = 9877`
  - `BRIDGE_VERSION = "0.1.0"`
  - `decode_lines(buffer: bytearray) -> list[dict]`: consumes complete lines; a bad line becomes `{"cmd": "__invalid__"}`.
  - `encode(msg) -> bytes`
  - `handle(request, scene) -> dict`, where:
    - `ping` returns `{"ok": True, "bridge": "0.1.0", "blender": scene.version()}`
    - `selection` returns `{"ok": True, "active": …, "selected": [...], "mode": …}`
    - `status` returns `{"ok": True}` after calling `scene.set_status(text)`. The text is truncated to 200 characters, and a non-string gives `{"ok": False, "error": "text must be a string"}`.
    - anything else returns `{"ok": False, "error": "unknown command"}`
  - The `SceneAccess` protocol: `version()`, `active_name()`, `selected_names()`, `mode()`, `set_status(text)`
- `__init__.py` (bpy):
  - A non-blocking listener on 127.0.0.1:9877, served by a `bpy.app.timers` callback every 0.1 s.
  - A POST_PIXEL draw handler on `SpaceView3D` that draws the status text at the bottom-left with `blf`.
  - `register` and `unregister` start and stop both.
- `blender_manifest.toml`: `schema_version = "1.0.0"`, `id = "gazevoice_bridge"`, `version = "0.1.0"`, `name = "GazeVoice Bridge"`, `type = "add-on"`, `blender_version_min = "5.1.0"`, `license = ["SPDX:GPL-3.0-or-later"]`, `[permissions] network = "Local socket on 127.0.0.1 for the GazeVoice app"`.
- `blender_socket.py`:
  - `port_open(host, port, timeout=0.5) -> bool`
  - `bridge_request(request, host="127.0.0.1", port=9877, timeout=2.0) -> dict`
- `Bridge` component: id `bridge`, name "GazeVoice Bridge", role "Our Blender add-on · selection + status line". Its actions are `install` and `test`. `build_bridge_zip(src_dir, dest_zip) -> Path` puts the files at the zip root.

- [ ] **Step 1: Write failing tests:**
  - `decode_lines` with two lines plus a partial line returns 2 dicts and leaves the partial in the buffer;
  - `handle` covers each command against a FakeScene;
  - a status text over 200 characters is truncated;
  - `bridge_request` round-trips against a stdlib echo server thread on port 0;
  - `port_open` is False on a closed port;
  - with Blender missing, the component is `missing` "Install Blender first" with no actions;
  - with Blender present, the add-on dir absent and the port closed, it is `missing` with `install`;
  - with the dir present and the port closed, it is `installed`;
  - with the dir present and the port open (test server), it is `ready`;
  - the `build_bridge_zip` output contains `blender_manifest.toml` at the zip root.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `test` sends `status` "GazeVoice connected", then `selection`, then `ping`, and returns them.
  - `install` builds the zip into `paths.downloads` and calls `install_extension`.
- [ ] **Step 4: Run tests and** `python3 -m py_compile blender_addon/gazevoice_bridge/__init__.py`. Expected: PASS.

### Task 9: MCP client, official add-on, official server

**Files:**
- Create:
  - `gazevoice/mcp_client.py`
  - `gazevoice/download.py`
  - `gazevoice/components/blender_mcp.py`
  - `tests/fixtures/fake_mcp_server.py`
  - `tests/test_mcp_client.py`
  - `tests/test_download.py`
  - `tests/test_blender_mcp.py`
- Modify: `gazevoice/components/__init__.py`

**Interfaces:**
- `StdioMcpClient(argv, env=None, cwd=None, timeout=60.0)` is a context manager with:
  - `.initialize() -> dict`: protocol `"2025-06-18"`, clientInfo `{"name": "gazevoice", "version": __version__}`, then the `notifications/initialized` notification.
  - `.list_tools() -> list[dict]`, which follows `nextCursor`.
  - `.call_tool(name, arguments) -> dict`
  - `McpError`, which carries the last 20 stderr lines.
- `download(url, dest, job, opener=urllib.request.urlopen) -> Path`:
  - sends User-Agent `GazeVoice/0.1.0`;
  - writes to `dest.with_suffix(dest.suffix + ".part")`, then renames;
  - logs progress at each 10%;
  - on an HTTP error raises `JobError(f"Download failed: HTTP {code} for {url}")` and deletes the `.part` file.
- `McpAddon`: id `mcp_addon`, name "Blender MCP add-on", role "Lets AI tools act inside Blender". Its action is `install`. It is `ready` when the extension dir `…/user_default/mcp` exists and port 9876 is open.
- `McpServer`: id `mcp_server`, name "Blender MCP server", role "Speaks MCP for Claude Code / Codex".
  - Actions: `install` (download, extract to `paths.mcp_server_dir`, `uv sync --directory <dir>`) and `test` (initialize, list_tools, then `get_blendfile_summary_datablocks` if 9876 is open). `test` returns `{"tools": int, "blender": "connected" | "not running"}`.
  - With no uv it is `error` "uv is needed…" with no actions.
- `server_command(paths, blender_binary) -> dict` returns `{"command": "uv", "args": ["run", "--directory", str(dir), "blender-mcp"], "env": {"BLENDER_PATH": str(binary)}}`.

- [ ] **Step 1: Write failing tests:**
  - The fake MCP server, a stdlib script on stdin/stdout, answers initialize, `tools/list` in 2 pages, and `tools/call` echo. `list_tools` returns 3 tools across the pages, and `call_tool` echoes its arguments.
  - A server that exits early raises `McpError` with its stderr.
  - `download` with a fake opener giving bytes writes the file and leaves no `.part`.
  - A fake 403 raises `JobError` containing "HTTP 403", and no file is left.
  - Component states: no Blender, no uv, not installed, installed with the port closed, and ready.
  - `install` on `mcp_server` extracts a small zip containing `pyproject.toml` and runs `uv sync --directory <dir>`.
  - A bundle without `pyproject.toml` raises `JobError` containing "Unexpected bundle layout".
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The client reads stdout lines in a thread, matches responses by id and ignores notifications.
  - Every `install` downloads into `paths.downloads`.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 10: Microphone and speech-to-text

**Files:**
- Create:
  - `gazevoice/components/microphone.py`
  - `gazevoice/components/speech.py`
  - `gazevoice/speech_worker.py`
  - `tests/test_microphone.py`
  - `tests/test_speech.py`
- Modify: `gazevoice/components/__init__.py`

**Interfaces:**
- `parse_avfoundation_devices(stderr) -> list[tuple[str, str]]` returns the audio devices only, from the section after "AVFoundation audio devices:".
- `wav_level_dbfs(path) -> float`: the RMS of 16-bit mono PCM. Silence returns `-120.0`.
- `Microphone`: id `microphone`, name "Microphone", role "Hears your commands".
  - Action `test` (confirm "Record 3 seconds from the microphone?") records with `["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", f":{mic_device}", "-t", "3", "-ac", "1", "-ar", "16000", "-y", out]`.
  - Below −60 dBFS it raises `JobError` that mentions System Settings › Privacy & Security › Microphone.
  - With ffmpeg missing it is `missing`, with `install` (`brew install ffmpeg`) when brew is present.
- `EngineSpec(key, label, package, module, model)` and `ENGINES` hold the values from Global Constraints.
- `hf_cached(model, hf_home=None) -> bool` checks `(<HF_HOME> or ~/.cache/huggingface)/hub/models--{org}--{name}/snapshots` for a non-empty directory.
- `Speech`: id `speech`, name "Speech-to-text", role "Turns your voice into text on this Mac". It uses the venv `paths.venvs/"speech"`.
  - `install` runs:
    1. `uv venv --python 3.12 <venv>`, if the venv is missing
    2. `uv pip install --python <venv>/bin/python <package>`
    3. `<venv>/bin/python <worker> --engine <key> --model <model> --download-only`
  - `test` runs `say -o <work>/sample.aiff "Make the selected cube twice as tall"`, then ffmpeg to a 16 kHz mono wav, then the worker. It returns `{"text", "seconds"}`.
- `speech_worker.py` takes `--engine`, `--model`, `--audio` and `--download-only`, and prints one JSON line `{"text": str, "seconds": float}`.
  - It imports the engine module lazily.
  - Parakeet: `from parakeet_mlx import from_pretrained; from_pretrained(model).transcribe(audio).text`
  - Whisper: `mlx_whisper.transcribe(audio, path_or_hf_repo=model)["text"]`
  - `--download-only` uses `huggingface_hub.snapshot_download(model)`.

- [ ] **Step 1: Write failing tests:**
  - parse a real-looking ffmpeg device listing with 2 video and 2 audio devices, which gives the 2 audio devices;
  - a generated sine wav reads about −3 dBFS (±1), and a zero wav gives −120;
  - speech states: no uv gives `error`; no venv gives `missing` with `install`; the venv present and the module import failing gives `missing`; the import OK and the model not cached gives `installed`; everything present gives `ready` and the summary contains the engine label;
  - the install argv sequence matches exactly with FakeRunner;
  - the worker argument parsing works with `--download-only`.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The import check runs `<venv>/bin/python -c "import <module>"` with a 20 s timeout.
  - The worker path is `Path(gazevoice.__file__).parent / "speech_worker.py"`.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 11: Connect the brain to Blender, and a full workflow check

**Files:**
- Modify: `gazevoice/components/brains.py` (action `connect_blender`), `gazevoice/workflow.py` (the `WorkflowCheck` component), `gazevoice/components/__init__.py`, `tests/test_brains.py`, `tests/test_workflow.py`

**Interfaces:**
- Consumes: `server_command` (Task 9), `find_blender` (Task 5), `port_open` (Task 8).
- Produces:
  - `write_mcp_config(paths, server_cmd) -> Path` writes `paths.root/"mcp-blender.json"` as `{"mcpServers": {"blender": server_cmd}}`.
  - Claude `connect_blender` (uses_plan) runs `claude_headless_argv("Use the blender tools to count the objects in the open scene. Reply with only the number.", ["--mcp-config", cfg, "--allowedTools", "mcp__blender", "--max-turns", "6"])` with timeout 180 and returns `{"reply", "seconds"}`.
  - Codex `connect_blender` (uses_plan, marked experimental in its label) runs `codex_exec_argv(prompt, ["-c", f'mcp_servers.blender.command="uv"', "-c", f"mcp_servers.blender.args={toml_list}"])`.
  - `WorkflowCheck`: id `workflow`, name "Workflow check", role "Runs every connection test in order".
    - Its only action is `check` (uses_plan, confirm "Run all connection tests? This records 3 s of audio and makes one small request with your plan.").
    - The order is: microphone.test → speech.test → mcp_server.test → bridge.test → <brain>.connect_blender.
    - Each step is logged as `✓`/`✗` plus a summary; it stops at the first failure and raises `JobError` naming the failed step.
    - Its state is always `installed`. The UI uses only its action, as the header's "Check everything" key, and readiness lives in the workflow strip. (Changed during the build: recomputing every stage inside this check would have run all checks twice.)

- [ ] **Step 1: Write failing tests:**
  - `write_mcp_config` JSON shape;
  - Claude `connect_blender` argv includes `--mcp-config <cfg>` and `--allowedTools mcp__blender`;
  - when the MCP server isn't installed, `connect_blender` is not among the offered actions;
  - `WorkflowCheck.check` calls the component actions in order on fakes and stops at the first failure with the step named.
- [ ] **Step 2: Run tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Also show the `workflow` card in the UI as the "Check everything" header button rather than a card.
- [ ] **Step 4: Run tests.** Expected: PASS.

### Task 12: README and final verification

**Files:**
- Modify: `README.md`, with what this is, `python3 -m gazevoice`, what each card does, and where data lives

- [ ] **Step 1:** Write the README.
- [ ] **Step 2:** `python3 -m unittest discover -s tests -t . -v`. Expected: all PASS.
- [ ] **Step 3:** `grep -rniE "<the guard's key and host patterns>" gazevoice blender_addon tests`. Expected: no matches. Use the hook's own regexes via `python3 ~/.local/lib/novoai/provider-access-guard.py` on a synthetic payload if needed.
- [ ] **Step 4:** Start the server and load the dashboard. Every card shows the real state of this Mac: Claude ready, Codex ready, Blender missing, and so on. Nothing installs on load.
