# Unity ML-Agents training Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** By voice in Unity, the user can train an ML-Agents agent and put the trained model on it, without touching the terminal.

**Why (the user, 2026-11-11):** "sometimes it needs to access terminal and not only Unity's MCP … Like for Unity ML agents." Today the Unity AI sets up the Unity side and then asks the user to run the Python side in a terminal. The user agreed to build exactly the ML-Agents steps instead of giving the AI a terminal: "Sure, go ahead. Just make sure that it can work with Unity ML agents."

**Architecture:**
- The control center owns the training: one `mlagents-learn` process at a time, started in its own process group. It outlives a spoken command, shows on the dashboard, and is stopped with SIGINT so ML-Agents saves the model.
- A new **ML-Agents** unit on the dashboard installs ML-Agents' Python side into its own uv environment when the user clicks **Install**, shows the run, and has **Stop training**.
- GazeVoice's own Unity MCP server (`gazevoice/unity_mcp.py`) gains four tools that call the control center over HTTP on 127.0.0.1. The Unity AI uses them to check, start, stop and import a run, and uses the existing tools for everything inside Unity.

**Tech stack:** standard-library Python (control center, MCP server); uv; ML-Agents 4.2.0 (Unity package `com.unity.ml-agents`, Python side from the same commit); Unity 6000.x.

**Facts this plan rests on** (research and a real install on this Mac, 2026-11-11):
- The latest Unity package is `com.unity.ml-agents` **4.2.0** (needs Unity 6000.0+). Its trained-model field is `BehaviorParameters.Model`, of type `Unity.InferenceEngine.ModelAsset`.
- **Python side:**
  - PyPI's `mlagents` 1.1.0 is broken on Apple silicon, so the plan installs the 4.2.0 source by tarball instead. It needs Python **3.10.12** exactly (`>=3.10.1,<=3.10.12`).
  - The working, git-free install was tested on this Mac. It takes about 5 minutes from cold; the environment is about 600 MB and the downloads about 1.2 GB:
    - `uv venv --python 3.10.12 <venv>`. uv downloads its own CPython 3.10.12, 16 MB.
    - `uv pip install --python <venv>/bin/python "torch>=2.1.1,<=2.8.0" "mlagents-envs @ https://github.com/Unity-Technologies/ml-agents/archive/82ce4998f18bbe05eb3de834cace09fc6564ba8e.tar.gz#subdirectory=ml-agents-envs" "mlagents @ https://github.com/Unity-Technologies/ml-agents/archive/82ce4998f18bbe05eb3de834cace09fc6564ba8e.tar.gz#subdirectory=ml-agents"`
  - It reports `ml-agents: 1.2.0.dev0` and `PyTorch: 2.8.0`.
- **What `mlagents-learn` prints** (stdout and stderr; logger lines start `[INFO]` or `[ERROR]`, no timestamps or colour codes; a Unicode banner comes first):
  - Waiting: `[INFO] Listening on port 5004. Start training by pressing the Play button in the Unity Editor.`, after about 3 s.
  - Progress: `[INFO] <Behavior>. Step: 50000. Time Elapsed: 120.543 s. Mean Reward: 1.234. Std of Reward: 0.500. Training.`
    - The behavior name can hold spaces and dots, so split at `. Step: `.
    - The line may say `Mean Group Reward` in place of `Std of Reward`, or `No episode was completed since last summary.` in place of the rewards.
    - The last word may be `Not Training`.
  - Model saved: `[INFO] Exported <path>.onnx`, then `[INFO] Copied <src> to <results>/<run>/<Behavior>.onnx.`
  - No Play within `--timeout-wait`: exit 1, ending in `UnityTimeOutException: The Unity environment took too long to respond.…`
  - Run name reused: exit 1 within 2 s, `UnityTrainerException: Previous data from this run ID was found. …`. A run that timed out or was interrupted still leaves its folder.
  - Port busy: exit 1, `OSError: [Errno 48] Address already in use` and `UnityWorkerInUseException`. "5004" does not appear in these.
  - **Stop:** SIGINT to the **process group** makes it print `[INFO] Learning was interrupted. Please wait while the graph is generated.`, save, and exit 0.
    - SIGINT to the main process only leaves a helper hanging for 10 s.
    - SIGTERM to the main process only orphans a helper that holds port 5004.
  - When `max_steps` is reached, it saves and exits 0 by itself, and tells Unity to leave Play mode.
  - Editor training always uses port 5004.

## Global Constraints

- **Consent:** nothing installs, downloads, records or calls a model unless the user clicks for it. Only the dashboard's **Install** button installs ML-Agents. No voice tool, HTTP route or MCP tool installs anything.
- Models are reached only through the `claude` and `codex` commands or the dashboard's providers. No other vendor SDKs, keys or API hosts in the code. This machine has hooks that block those names; don't work around them.
- The control center stays standard-library Python and binds only to 127.0.0.1.
- Plain language in messages, comments and docs.
- No commits, branches or pushes unless the user asks.
- **Exact values:**
  - Python `3.10.12`.
  - ML-Agents commit `82ce4998f18bbe05eb3de834cace09fc6564ba8e` (release/4.2.0).
  - Unity package `com.unity.ml-agents` (version 4.2.0 when one is needed).
  - venv `paths.venvs / "mlagents"`.
  - Results `<project>/MLAgents/results`.
  - Configs `<project>/MLAgents/configs/<run_id>.yaml`.
  - Imported models `Assets/GazeVoice/MLAgents/<run_id>/<Behavior>.onnx`.
  - Log `paths.logs / "mlagents-<run_id>.log"`.
  - `--timeout-wait 120`.
  - Stop waits up to 60 s after SIGINT, then SIGKILLs the group.
  - Run id must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`.

## Review Focus

1. **Play never pressed** (the AI forgot it, or Unity was busy compiling): the run ends as "failed" with a sentence saying Unity didn't start Play mode within 2 minutes. It must not stay "waiting" forever. Tested in Task 1.
2. **The dashboard is stopped mid-training:** training is stopped with SIGINT to its group and the model is saved. No helper is left holding port 5004. Tested in Task 2.
3. **The same run name twice, or a second run while one is going:** a plain sentence, and nothing half-started. Tested in Task 1.
4. **A voice edit while Unity is in Play mode for training:** the AI must not edit the scene, because Play mode throws those changes away. It says so instead. Pinned in the prompt test, Task 3.
5. **"Train it" when ML-Agents isn't installed:** nothing installs. The reply tells the user to press Install on the ML-Agents unit. Tested in Task 3.

---

### Task 1: The training service

**Files:**
- Create: `gazevoice/mlagents.py`
- Create: `tests/test_mlagents.py`, `tests/fixtures/fake_mlagents_learn.py`

**Interfaces (produces):**
- `class TrainingError(Exception)`, carrying a plain sentence for the user.
- `RESULTS_DIR = Path("MLAgents") / "results"`, `CONFIGS_DIR = Path("MLAgents") / "configs"`, `MODELS_DIR = Path("Assets") / "GazeVoice" / "MLAgents"`, `TIMEOUT_WAIT = 120`, `STOP_SECONDS = 60.0`, `RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")`.
- `parse_line(line: str) -> dict | None`, a pure function that turns one output line into an event:
  - `{"event": "waiting"}` from the Listening line.
  - `{"event": "progress", "behavior": str, "step": int, "seconds": float, "mean_reward": float | None, "training": bool}`.
  - `{"event": "connected"}` from a line containing `Connected to Unity environment` or `Connected new brain`.
  - `{"event": "exported", "path": str}` from `Exported …onnx`.
  - `{"event": "interrupted"}`.
  - `{"event": "error", "kind": "timeout" | "previous_run" | "port_busy"}`.
  - None for anything else.
- `class Training`:
  - `__init__(self, logs: Path)`.
  - `subscribe(self, callback: Callable[[], None]) -> None`, called after every state change from any thread.
  - `start(self, mlagents_learn: Path, project: Path, run_id: str, config_yaml: str | None = None, resume: bool = False, overwrite: bool = False, timeout_wait: int = TIMEOUT_WAIT) -> dict`. `timeout_wait` exists so tests and Task 4 can use a short wait. The control center always uses the default.
  - `status(self) -> dict`
  - `stop(self, timeout: float = STOP_SECONDS) -> dict`
  - `models(self, project: Path, run_id: str) -> list[str]`, the behavior names that have `<results>/<run_id>/<name>.onnx`.
  - `import_model(self, project: Path, run_id: str, behavior: str | None = None) -> dict` returns `{"asset_path": "Assets/GazeVoice/MLAgents/<run_id>/<Behavior>.onnx", "behavior": ...}`.
  - `shutdown(self) -> None`.
- `status()` keys:
  - `state`: one of `idle`, `waiting`, `training`, `stopping`, `finished`, `stopped`, `failed`.
  - `run_id`, `project`.
  - `behaviors`: `{name: {"step", "mean_reward", "seconds"}}`.
  - `message`: one plain sentence.
  - `log`: the log path.
  - `log_tail`: the last 15 lines.
  - `models`, `exit_code`.

**Behaviour:**
- **`start`:**
  - Refuses while a run is `waiting`, `training` or `stopping`: "A training run is already going. Stop it first."
  - Refuses a bad run id, or a project without `Assets/`.
  - Writes `config_yaml`, if given and at most 64 KB, to `<project>/MLAgents/configs/<run_id>.yaml`.
  - Runs `[mlagents_learn, <config?>, "--run-id", run_id, "--results-dir", <project>/MLAgents/results, "--timeout-wait", "120", ("--resume" | "--force")?]` with `cwd=project` and `start_new_session=True`, stdout and stderr merged.
  - A reader thread writes every line to the log, keeps the tail, and applies `parse_line`.
  - State starts as `waiting`. It becomes `training` on `connected` or the first `progress`.
- **When the process exits:**
  - `stopped` if `stop()` asked for it.
  - `finished` on exit 0.
  - `failed` otherwise, with a message by error kind:
    - timeout: "Unity didn't start Play mode within 2 minutes, so training gave up. Start it again, then press Play."
    - previous_run: "A run called <id> already exists. Resume it, overwrite it, or use a new name."
    - port_busy: "Another training run is already using ML-Agents' connection to Unity. Stop it first."
    - anything else: "Training stopped with an error. The log is at <path>."
- **`stop`:** SIGINT to the process group once, wait up to `timeout` for exit, then SIGKILL the group. The message says whether a model was saved.
- **`import_model`:**
  - Copies the `.onnx` into the project under `MODELS_DIR`, overwriting an older copy.
  - If `behavior` is None and there is exactly one model, it uses that one. With several, it raises and names them. With none: "Run <id> hasn't saved a model yet."
- **Other messages:**
  - waiting: "Waiting for Unity: press Play in the Editor to start training."
  - training: "Training <id>: step <n>, mean reward <r>." or "…no finished episode yet."
  - finished: "Training <id> finished. Its model is ready to use."

- [ ] **Step 1:** Write `tests/fixtures/fake_mlagents_learn.py`, a small script that reads its argv and an env var `FAKE_ML_SCRIPT` to act out one case. The cases:
  - wait then time out (print the real traceback tail, exit 1);
  - previous run (exit 1);
  - port busy (exit 1);
  - connect then print progress lines;
  - wait and, on SIGINT, print the interrupted line, write `<results>/<run>/<Behavior>.onnx`, print the Exported line and exit 0;
  - run to the end and exit 0.

  Use the exact lines from "Facts" above.
- [ ] **Step 2:** Write failing tests in `tests/test_mlagents.py`:
  - **`parse_line`** (table-driven), including a behavior name with spaces and dots, `Mean Group Reward`, `No episode was completed`, `Not Training`, and the banner lines giving None.
  - **`Training` with the fixture**, one test per case:
    - start → waiting → timeout → failed, with the exact sentence;
    - progress → training, with step and reward;
    - stop → SIGINT reaches the group → stopped with a model;
    - natural finish → finished;
    - a second start while running is refused;
    - previous run → failed, with the sentence;
    - port busy → failed, with the sentence;
    - bad run id refused;
    - project without `Assets/` refused;
    - config written to the right path;
    - `--resume` and `--force` passed through;
    - `import_model` copies to the right path, picks the only model, and refuses when there are several or none;
    - `subscribe` called on changes;
    - `shutdown` stops a running run.

  Run `python3 -m unittest tests.test_mlagents` and see them fail.
- [ ] **Step 3:** Implement `gazevoice/mlagents.py` until the tests pass. Then run the full suite: `python3 -m unittest discover -s tests -t .`.

### Task 2: The ML-Agents unit on the dashboard

**Files:**
- Create: `gazevoice/components/mlagents.py`, `tests/test_mlagents_unit.py`
- Modify:
  - `gazevoice/components/base.py`: `Context` gains `training: Training | None = None`, and a `__post_init__` that sets `self.training = Training(self.paths.logs)` when it is None. So `ctx.training` exists everywhere a `Context` is built, tests included.
  - `gazevoice/components/__init__.py`: register after the Unity units.
  - `gazevoice/server/app.py`: subscribe to training changes and publish `component.updated` for `mlagents`; `ControlCenter.shutdown` calls `ctx.training.shutdown()`.
  - `gazevoice/server/static/app.js`: `NAMES.mlagents = "ML-Agents"`, and add `"mlagents"` to the Unity group in `GROUPS`.
  - The tests for server and dashboard JS where those are pinned.

**Interfaces:**
- Consumes `Training` (Task 1).
- Produces `class MLAgents(Component)`:
  - `id = "mlagents"`, `name = "ML-Agents"`, `role = "Trains Unity agents on this Mac"`.
  - `VENV_PYTHON = "3.10.12"`, `ML_AGENTS_COMMIT = "82ce4998f18bbe05eb3de834cace09fc6564ba8e"`.
  - Properties `venv`, `python`, `mlagents_learn` (`venv/bin/mlagents-learn`).
  - `installed(self) -> bool`.

**Behaviour:**
- **`check()`:**
  - No uv: ERROR, worded like Speech's.
  - Environment folder present but its Python gone: the same message and rebuild as Speech's: "ML-Agents' Python environment doesn't work any more, usually because the Python it was made from was removed. Press Install to set it up again."
  - Not installed: MISSING, "Not installed. ML-Agents trains your Unity agents on this Mac.", with the Install action.
  - Installed, which means a probe of `import mlagents.trainers` succeeds:
    - READY, with summary "Ready to train" or the training message from `ctx.training.status()`.
    - Details: Python side "ML-Agents 4.2.0 · Python 3.10.12", the runs folder when a Unity project is set, and the last run's state.
    - Actions: **Stop training** (`stop`) while a run is `waiting` or `training`.
- **Install:**
  - The action's confirm text: "Install ML-Agents' Python side? This sets up its own Python 3.10 environment and downloads ML-Agents and PyTorch (about 1 GB, about 5 minutes)."
  - Use the same guard as Speech: an existing folder is rebuilt only if it holds `pyvenv.cfg` or is empty. Then:
    - `["uv", "venv", "--clear", "--python", "3.10.12", venv]`
    - `["uv", "pip", "install", "--python", python, "torch>=2.1.1,<=2.8.0", "mlagents-envs @ https://github.com/Unity-Technologies/ml-agents/archive/<commit>.tar.gz#subdirectory=ml-agents-envs", "mlagents @ https://github.com/Unity-Technologies/ml-agents/archive/<commit>.tar.gz#subdirectory=ml-agents"]`

    Both run through `runner.stream`, with timeout 3600.
- **`action_stop`** calls `ctx.training.stop()` and returns its status.

- [ ] **Step 1:** Write failing tests:
  - **`tests/test_mlagents_unit.py`**, modelled on `tests/test_speech.py`:
    - Each `check()` state.
    - The exact install argv.
    - The broken-environment guard, with these cases: a non-environment folder is left alone, an empty folder goes ahead, and a working environment isn't rebuilt.
    - Stop appears only while training, and `action_stop` calls through to `Training` (use a stub `Training`).
  - **`tests/test_server.py`:**
    - `ControlCenter.shutdown` calls `training.shutdown()`.
    - A training change publishes `component.updated` for `mlagents`.
  - **Dashboard JS test:** the unit is in the Unity group.

  Watch them fail.
- [ ] **Step 2:** Implement until they pass, then run the full suite.

### Task 3: The Unity AI's training tools

**Files:**
- Modify:
  - `gazevoice/server/app.py`: routes; `ControlCenter.url`; pass it to `build_voice`.
  - `gazevoice/voice/wiring.py`: `build_voice(..., control_url: str | None = None)`, passed into `unity_mcp.server_command`.
  - `gazevoice/unity_mcp.py`
  - `gazevoice/voice/prompts/unity_editor.md`
  - `README.md`, `PRODUCT.md`
- Tests: `tests/test_unity_mcp.py`, `tests/test_server.py`, `tests/test_voice_wiring.py`, `tests/test_unity_package.py`.

**Interfaces:**
- Consumes `Training` (Task 1) and `MLAgents.installed()` / `mlagents_learn` (Task 2).
- Produces the following.

**Control center HTTP routes**, on 127.0.0.1 with the same Host and `X-GazeVoice` checks as the other POSTs:
- `GET /api/mlagents` → `{"ok": true, "installed": bool, "project": str | None, **training.status()}`.
- `POST /api/mlagents/train` with `{"run_id", "config_yaml"?, "resume"?, "overwrite"?}`:
  - The project is `settings.unity_project`.
  - If ML-Agents isn't installed: 409 with "ML-Agents' Python side isn't installed. Press Install on the ML-Agents unit in the dashboard." It never installs anything.
  - With no project set: 409 with "Pick your Unity project in the dashboard first."
  - On success: 200 with the status.
  - A `TrainingError` gives 409 with its sentence.
- `POST /api/mlagents/stop` → 200 with the status.
- `POST /api/mlagents/import` with `{"run_id", "behavior"?}` → 200 with the `import_model` result, or 409.

**`ControlCenter.url: str | None`:** set by `make_server` to `http://127.0.0.1:<bound port>`, and passed by the voice factory to `build_voice`.

**`unity_mcp.server_command(settings, control_url: str | None = None)`:**
- The built-in server's `env` gains `GAZEVOICE_URL` when `control_url` is given.
- A custom server (`unity_mcp_command`) is unchanged and doesn't get the ML-Agents tools. Ruling: the user's own MCP server is theirs. Cost if wrong: one env line.

**`unity_mcp.py`:** `LOCAL_TOOLS` holds four tools answered in Python, not by the bridge:
- `mlagents_status` {}
- `mlagents_train` {run_id (required), config_yaml, resume, overwrite}
- `mlagents_stop` {}
- `mlagents_import_model` {run_id (required), behavior}

Each tool:
- Calls the control center with `urllib` and the header `X-GazeVoice: 1`.
- Timeouts: 30 s, or 90 s for stop.
- Returns the JSON as text, with `isError` when the reply isn't ok.
- With no `GAZEVOICE_URL`, or the dashboard unreachable, it returns `{"ok": false, "error": "The GazeVoice dashboard isn't reachable, so ML-Agents tools can't run."}`.

Tool descriptions say plainly what each does. `mlagents_train` says to press Play right after with `execute_csharp` (`EditorApplication.isPlaying = true;`).

`test_every_tool_is_handled_in_the_bridge` checks every tool except `LOCAL_TOOLS`.

**Prompt (`unity_editor.md`), a new "ML-Agents" section.** In short:
- Unity side:
  - If `com.unity.ml-agents` is missing, add it with `add_package`.
  - Agents are `write_script` classes deriving from `Unity.MLAgents.Agent`, with observations, actions, rewards and a `Heuristic`.
  - Give `BehaviorParameters` a clear Behavior Name, and add a `DecisionRequester`.
- To train:
  - Call `mlagents_status`. If it isn't installed, change nothing for training and reply "Couldn't start training: press Install on the ML-Agents unit in the dashboard first."
  - Otherwise call `mlagents_train` with a new run id (the agent's name and the time), and a `config_yaml` for its behavior names unless asked otherwise: PPO, `max_steps` 200000 unless the user names an amount.
  - Then enter Play mode at once with `execute_csharp`.
  - Reply that training started and is shown on the dashboard.
- "How is training going": `mlagents_status`.
- "Stop training": `mlagents_stop`. Unity leaves Play mode by itself; if it is still playing, set `EditorApplication.isPlaying = false`.
- "Use the trained model":
  - Call `mlagents_import_model`.
  - Then in `execute_csharp`: `AssetDatabase.Refresh();`, `var model = AssetDatabase.LoadAssetAtPath<Unity.InferenceEngine.ModelAsset>(path);`, get `Unity.MLAgents.Policies.BehaviorParameters`, `Undo.RecordObject(bp, …)`, then `bp.Model = model;`.
- While training is running (Unity is in Play mode for it), don't change the scene, because Play mode throws the changes away. Reply "Couldn't: training is running. Stop it first."
- The Limits line about Play mode adds "or to train ML-Agents".

**Docs:**
- README: a short "Train agents with ML-Agents" section:
  - Install on the unit (what it downloads, size and time).
  - Then say things like "train the agent", "how's training going", "stop training", "use the trained model".
  - Training stops when the dashboard stops; its model is saved.
- PRODUCT.md: one line.

- [ ] **Step 1:** Write failing tests:
  - **Routes** (with a stub `Training` and a fake installed check):
    - status;
    - train refused when not installed, with the exact sentence and no install job started;
    - train refused with no project;
    - train passes its arguments through;
    - stop;
    - import;
    - a POST without `X-GazeVoice` is refused.
  - **`make_server`** sets `center.url`.
  - **`server_command`** includes `GAZEVOICE_URL` for the built-in server only.
  - **`build_voice`** passes `control_url` through.
  - **`unity_mcp`:**
    - the four tools are listed;
    - each calls the right method and path on a tiny local `http.server` stub, with the header;
    - an unreachable URL gives the plain error;
    - an unknown tool is still refused.
  - **Prompt test:** the section's key sentences are in `unity_editor.md`: the not-installed reply, the Play-mode warning, and the `ModelAsset` line.

  Watch them fail.
- [ ] **Step 2:** Implement until they pass, then run the full suite.

### Task 4: Prove it works with real ML-Agents on this Mac

No product code changes unless a check finds a bug. A bug gets a failing test first, then the fix. Work only in the session scratchpad; never touch `~/Library/Application Support/GazeVoice` or the user's Unity project (`/Users/ghanibhutigogoi/Unity/GazeVoice3`). Delete everything made here at the end, except notes.

- [ ] **Step 1:** **Real install.** Run the unit's own install argv, taken from `MLAgents` in code, against a scratch venv path. Check `import mlagents.trainers` and that `mlagents-learn --help` works.
- [ ] **Step 2:** **Real `mlagents-learn` through `Training`,** with a scratch project folder that has `Assets/`:
  - start → `waiting` (the real line parsed) → `stop()` → `stopped`, with no process left in the group;
  - a short `TIMEOUT_WAIT` override if one exists, or a patched module constant → `failed` with the timeout sentence;
  - the same run id again → `failed` with the previous-run sentence.
- [ ] **Step 3:** **Real Unity, end to end,** with Unity 6000.6.8f1 at `/Applications/Unity/Hub/Editor/6000.6.8f1` in batch mode:
  1. Make a scratch project with `com.unity.ml-agents` 4.2.0 and one tiny agent (for example, move to a target), with its own `BehaviorParameters` name.
  2. Build a macOS player.
  3. Train it with the venv's `mlagents-learn --env <player> --no-graphics` and a config with small `max_steps` (about 5000) and `summary_freq` (about 1000), using the same results layout.
  4. Feed every real output line through `parse_line`, and check that progress, exported and connected events come out.
  5. Run `Training.import_model` on the result.
  6. In batch mode, run the prompt's exact C# for loading `ModelAsset` and setting `BehaviorParameters.Model`, and check the field is set.

  If Unity can't run in batch mode here (licence), report that plainly and don't count this step as passed.
- [ ] **Step 4:** Report what passed, with the real output lines, and anything that differed from the "Facts".

## Done when
- Tasks 1 to 3 each pass their own review, and a final review of all of it finds nothing important.
- The full suite passes, run by the main session.
- Task 4's real checks pass, or anything that couldn't be checked is said plainly.
