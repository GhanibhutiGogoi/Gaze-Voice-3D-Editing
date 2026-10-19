# API Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each voice step (cleanup, editing) run on DeepSeek, Qwen or a saved custom
OpenAI-compatible provider, set up from the dashboard with an API key, alongside Claude Code
and Codex.

**Architecture:**
- `gazevoice/providers.py` holds the presets, the Keychain key store and a stdlib HTTP client for
  OpenAI-compatible chat.
- `gazevoice/voice/api_brain.py` is a session with the same interface as `ClaudeSession`. It runs the
  Blender MCP tool loop itself, so `VoiceService` doesn't change.
- Settings gain a source per step. The control center resolves each step's source, builds rack units
  for providers, and exposes provider endpoints. The dashboard adds provider units and per-step
  source/model/thinking menus.

**Tech Stack:**
- Python 3.12+ standard library only: `urllib`, `http.server` for test fakes, and `subprocess` for
  `security`.
- Plain JS/CSS dashboard.
- `unittest`.

**Spec:** `docs/superpowers/specs/2026-10-19-api-providers-design.md`

## Global Constraints

- **Standard library only.** No provider SDKs.
- **Never write the OpenAI, Google or Anthropic API hosts, their key variable names or their SDK
  imports**, in any file, including tests.
- **Keys:**
  - Stored in the macOS Keychain with service `GazeVoice` and account = provider id.
  - A key never appears in a process argument list, a log line, an HTTP response, a command record,
    an error message or settings.json.
- **Writes keep the existing guards:** 127.0.0.1 only, `X-GazeVoice: 1`, and Origin checks.
- **DeepSeek:**
  - Base `https://api.deepseek.com`: `POST /chat/completions` and `GET /models`.
  - Thinking menu: `default`, `off`, `low`, `high`, `max`.
  - `off` → `"thinking": {"type": "disabled"}`; `low`/`high`/`max` → `"reasoning_effort": <level>`.
  - Every assistant message in a tool turn goes back with its `reasoning_content`.
- **Qwen:**
  - Base `https://{workspace}.{region}.maas.aliyuncs.com/compatible-mode/v1` when a workspace is
    given. Otherwise the shared host: `dashscope-intl.aliyuncs.com` for `ap-southeast-1` and
    `dashscope.aliyuncs.com` for `cn-beijing`, both with path `/compatible-mode/v1`.
  - Regions: `ap-southeast-1` Singapore, `us-east-1` US (Virginia), `cn-beijing` China (Beijing),
    `cn-hongkong` Hong Kong, `eu-central-1` Germany (Frankfurt), `ap-northeast-1` Japan (Tokyo).
  - Thinking menu: `default`, `off`, `on` → top-level `"enable_thinking": false/true`.
- **Custom:** thinking menu `default` only.
- **Tools:** `tool_choice` is never sent; providers default to `auto`.
- **Step caps:** at most 12 model calls per editing command. Conversation memory keeps the last 8
  commands.
- **Settings migration:** a saved `brain` sets both step sources (`auto`/`claude`/`codex`). Default
  source is `auto`.

## Review Focus

1. A key pasted with surrounding spaces or a trailing newline is trimmed and saved. A key with inner
   spaces, quotes or control characters is refused with a plain message and is never echoed. *(Task 1)*
2. Removing a provider that a step uses moves that step back to Auto and says so in the response.
   *(Task 5)*
3. A Custom address pasted with a trailing slash or with `/chat/completions` on the end still works.
   *(Task 1)*
4. A wrong-region Qwen key (401) produces an error that mentions the region. *(Task 1)*
5. A model that rejects tools fails Start voice and Test with "can't call tools" rather than a raw
   400. *(Tasks 1 and 3)*

---

### Task 1: Provider catalog, key store and HTTP client

**Files:**
- Create: `gazevoice/providers.py`
- Modify: `gazevoice/runner.py`. `FakeRunner.run` records `input_text` in `call_kwargs`.
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces:
  - `KINDS = ("deepseek", "qwen", "custom")`
  - `QWEN_REGIONS: dict[str, tuple[str, str | None]]`, mapping region → (label, shared host or None)
  - `THINKING: dict[str, tuple[str, ...]]`, with keys `"deepseek"`, `"qwen"` and `"custom"`
  - `ProviderConfig(id, kind, name, base_url="", region="", workspace="", key_hint="")`, a frozen
    dataclass with `.endpoint() -> str`, `.to_dict() -> dict` and
    `ProviderConfig.from_dict(d) -> ProviderConfig` (raises `ValueError`)
  - `preset(kind: str) -> ProviderConfig`, with ids `"deepseek"` (name "DeepSeek") and `"qwen"`
    (name "Qwen")
  - `custom_id(name: str, taken: set[str]) -> str` → `custom_<slug>`, `[a-z0-9_]`, at most 32 chars,
    unique
  - `normalize_base_url(url: str) -> str`
  - `clean_key(raw: str) -> str`
  - `thinking_fields(kind: str, thinking: str) -> dict`
  - `KeyStore(runner)` with `.set(pid, key)`, `.get(pid) -> str | None`, `.has(pid) -> bool` and
    `.delete(pid)`
  - `ProviderError(Exception)`
  - `ChatClient(config: ProviderConfig, key: str, timeout: float = 120)` with
    `.models() -> list[str]` and `.chat(body: dict, timeout: float | None = None) -> dict`

- [ ] **Step 1: Write the failing tests** in `tests/test_providers.py`. They use a local fake
  OpenAI-compatible server built on `http.server`, run on 127.0.0.1 port 0 in a thread, which
  records requests and replies from a queue.
  - `test_deepseek_preset`: `preset("deepseek").endpoint() == "https://api.deepseek.com"`.
  - `test_qwen_endpoints`:
    - The workspace form for `("ws123", "eu-central-1")` is
      `"https://ws123.eu-central-1.maas.aliyuncs.com/compatible-mode/v1"`.
    - Without a workspace, `ap-southeast-1` gives `"https://dashscope-intl.aliyuncs.com/compatible-mode/v1"`.
    - Without a workspace, `us-east-1` raises `ValueError` mentioning "Workspace ID".
  - `test_normalize_base_url`: `"https://x.test/v1/"` and `"https://x.test/v1/chat/completions"`
    both give `"https://x.test/v1"`. `"ftp://x"` and `""` raise `ValueError`.
  - `test_clean_key`: `"  sk-abc123\n"` gives `"sk-abc123"`. Each of `"sk abc"`, `'sk"abc'`,
    `"sk\tabc"` and `""` raises `ValueError`, and the error message doesn't contain the input.
  - `test_custom_id`: `custom_id("My Box!", {"custom_my_box"}) == "custom_my_box_2"`.
  - `test_thinking_fields`:
    - `("deepseek", "off") == {"thinking": {"type": "disabled"}}`
    - `("deepseek", "max") == {"reasoning_effort": "max"}`
    - `("qwen", "on") == {"enable_thinking": True}`
    - `("qwen", "off") == {"enable_thinking": False}`
    - `("custom", "default") == {}` and `("deepseek", "default") == {}`
  - `test_keystore_keeps_the_key_out_of_argv`, with a FakeRunner:
    - `set("deepseek", "sk-secret")` runs `["security", "-i"]` with `input_text` containing
      `add-generic-password -U -s GazeVoice -a deepseek -w "sk-secret"`.
    - `"sk-secret"` appears in no recorded argv.
    - `get` runs `["security", "find-generic-password", "-s", "GazeVoice", "-a", "deepseek", "-w"]`
      and returns the stripped stdout. A non-zero exit returns `None`.
    - `has` runs the same command without `-w`.
    - `delete` runs `delete-generic-password -s GazeVoice -a deepseek`.
  - `test_chat_sends_bearer_key_and_body`: the fake server sees `Authorization: Bearer sk-x`, path
    `/v1/chat/completions` and the JSON body.
  - `test_models_lists_ids`: `{"data": [{"id": "a"}, {"id": "b"}]}` gives `["a", "b"]`.
  - `test_errors_are_plain_and_never_contain_the_key`, one `ProviderError` per server reply:
    - 401 → message contains "key was rejected". For a Qwen config it also contains the region label.
    - 402 → "out of credit".
    - 404 → "doesn't have the model".
    - 429 twice → "rate-limiting". One retry happens after the first 429.
    - 400 with body `{"error": {"message": "This model does not support tools"}}` → "can't call tools".
    - Connection refused → "Couldn't reach".
    - No message contains `"sk-x"`.

- [ ] **Step 2: Run them and see them fail**
  - Run: `python3 -m unittest tests.test_providers -v`
  - Expected: errors (the `providers` module doesn't exist yet).

- [ ] **Step 3: Implement `gazevoice/providers.py`.**
  - `KeyStore.set` writes one `add-generic-password` line to `security -i` through
    `runner.run(["security", "-i"], input_text=..., timeout=15)`. The key has already been cleaned,
    so it has no quotes.
  - `ChatClient` uses `urllib.request` with a 120 s default timeout.
  - The 429 retry sleeps `min(Retry-After, 10)` seconds once.
  - The error text for each status comes from the list in step 1.
  - `ProviderConfig.from_dict` checks `kind in KINDS`, an id matching `^[a-z0-9_]{1,32}$`, and Qwen
    regions in `QWEN_REGIONS`.

- [ ] **Step 4: Run** `python3 -m unittest tests.test_providers -v`. Expected: OK.

- [ ] **Step 5: Commit**: `git add gazevoice/providers.py gazevoice/runner.py tests/test_providers.py`,
  then `git commit -m "Add DeepSeek, Qwen and custom provider plumbing: presets, Keychain keys, HTTP client"`.

### Task 2: Settings for step sources and providers

**Files:**
- Modify: `gazevoice/settings.py`
- Test: `tests/test_settings.py` and `tests/test_voice_api.py` (`SettingsTest`)

**Interfaces:**
- Consumes: `ProviderConfig` and `THINKING` (Task 1).
- Produces:
  - New `Settings` fields:
    - `editor_source="auto"` and `refiner_source="auto"`
    - `editor_api_model=""` and `refiner_api_model=""`
    - `editor_api_thinking="default"` and `refiner_api_thinking="default"`
    - `providers: tuple[dict, ...] = ()`, each item being `ProviderConfig.to_dict()`
  - The `brain` field is removed.
  - `BUILTIN_SOURCES = ("auto", "claude", "codex")`
  - `provider_ids(settings) -> set[str]`

- [ ] **Step 1: Write the failing tests:**
  - `test_step_sources`:
    - defaults are `("auto", "auto")`;
    - `update_settings(Settings(), {"editor_source": "codex"})` works;
    - `{"editor_source": "deepseek"}` raises `ValueError` while no provider `deepseek` exists, and
      works once `providers` contains it.
  - `test_saved_brain_becomes_both_sources`: settings.json with `{"brain": "codex"}` loads with
    `editor_source == refiner_source == "codex"`.
  - `test_unknown_brain_key_is_ignored_on_load_and_refused_on_update`: `update_settings(Settings(),
    {"brain": "auto"})` raises `ValueError`.
  - `test_api_thinking_values`:
    - each of `default`, `off`, `on`, `low`, `high`, `max` is accepted for `editor_api_thinking`;
    - `"ludicrous"` raises `ValueError`.
  - `test_providers_round_trip`: a DeepSeek and a custom provider survive save then load. A provider
    dict with `kind: "gpt"` is dropped on load.
  - Update `tests/test_settings.py::test_round_trip` and `test_defaults`, which use `brain`, to use
    `editor_source`.

- [ ] **Step 2: Run them and see them fail**
  - Run: `python3 -m unittest tests.test_settings tests.test_voice_api -v`
  - Expected: failures on the new tests.

- [ ] **Step 3: Implement.**
  - Validate sources against `BUILTIN_SOURCES ∪ provider_ids` after applying all changes, so one
    request can add a provider and point a step at it.
  - `load_settings` maps `brain` to both sources only when neither `*_source` key is saved.

- [ ] **Step 4: Run** `python3 -m unittest tests.test_settings tests.test_voice_api -v`. Expected: OK.

- [ ] **Step 5: Commit**: "Store a source for each voice step, and the saved providers, in settings".

### Task 3: ApiSession, the provider brain with its own Blender tool loop

**Files:**
- Create: `gazevoice/voice/api_brain.py`
- Test: `tests/test_api_brain.py`

**Interfaces:**
- Consumes: `ChatClient` and `ProviderError` (Task 1); `TurnResult` and `BrainError` from
  `gazevoice/voice/brains.py`.
- Produces: `ApiSession(client, model: str, system_prompt: str, kind: str = "custom",
  thinking: str = "default", mcp_factory: Callable[[], Any] | None = None, max_steps: int = 12,
  keep_commands: int = 8)`, with:
  - `start(timeout=90, **_ignored)`, `send(text, on_tool=None, timeout=180) -> TurnResult`,
    `interrupt()` and `close()`;
  - `alive` (property), `turns`, `init == {"model": model}` and `effort == thinking or None`.
- `mcp_factory()` returns an object with `initialize()`, `list_tools() -> list[dict]`,
  `call_tool(name, arguments) -> dict` and `close()`. Production wraps `StdioMcpClient` (entered with
  `__enter__`, closed with `__exit__`).

- [ ] **Step 1: Write the failing tests.** They use a fake `ChatClient`, a class whose `chat` pops
  scripted replies and records request bodies, and a fake MCP object.
  - `test_refiner_is_a_plain_chat`:
    - `send("…")` returns the assistant `content`;
    - the request has `model`, the system message first, no `tools`, and fields from
      `thinking_fields`.
  - `test_editor_runs_the_tool_loop`:
    - The first reply has a `tool_calls` entry (`execute_blender_code`, args `{"code": "x"}`). The
      second reply is plain content "Made it red.".
    - `on_tool` gets `"execute_blender_code"`.
    - The MCP fake sees `call_tool("execute_blender_code", {"code": "x"})`.
    - The second request contains a `role: "tool"` message with the matching `tool_call_id`.
    - The result is `TurnResult("Made it red.", False, ["execute_blender_code"])`.
  - `test_tools_come_from_mcp`: the request `tools` equals
    `[{"type": "function", "function": {"name": t["name"], "description": t["description"],
    "parameters": t["inputSchema"]}}]` for the fake tools.
  - `test_reasoning_content_goes_back_in_tool_turns`: a DeepSeek tool reply carrying
    `reasoning_content: "think"` reappears unchanged in the next request's messages.
  - `test_memory_spans_commands_and_is_bounded`:
    - The second `send` includes the first command's user message.
    - After 10 sends with `keep_commands=8`, the oldest user message is gone.
    - `close()` then `start()` starts empty.
  - `test_step_cap`: endless tool calls stop after 12 `chat` calls with `is_error=True`.
  - `test_interrupt_ends_the_turn_quickly`: a `chat` that blocks for 5 s, with `interrupt()` after
    0.2 s, returns within 1.5 s with `interrupted=True` and runs no tools.
  - `test_provider_error_becomes_brain_error`: `chat` raising `ProviderError("Your DeepSeek account is
    out of credit.")` makes `send` raise `BrainError` with that message.
  - `test_start_checks_the_model_and_tools`:
    - For the editor, `start()` calls `chat` once with `tools` and `max_tokens` ≤ 16.
    - A `ProviderError("… can't call tools …")` there raises `BrainError` containing "can't call tools".

- [ ] **Step 2: Run them and see them fail**
  - Run: `python3 -m unittest tests.test_api_brain -v`
  - Expected: errors (the module is missing).

- [ ] **Step 3: Implement.**
  - Each `chat` runs in a daemon thread. `send` waits on a queue in 0.1 s slices and checks
    `_interrupted` and the deadline. A late reply after an interrupt or timeout is dropped.
  - Assistant messages are stored exactly as returned.
  - Memory is a list of per-command message groups.
  - MCP tool result text is the joined `text` parts of `content`, at most 20,000 characters. A
    non-text part becomes `"[image omitted]"`.

- [ ] **Step 4: Run** `python3 -m unittest tests.test_api_brain -v`. Expected: OK.

- [ ] **Step 5: Commit**: "Add ApiSession: provider models clean up and edit through GazeVoice's own Blender tool loop".

### Task 4: Provider units in the rack

**Files:**
- Create: `gazevoice/components/providers.py`
- Modify:
  - `gazevoice/components/base.py`: `ComponentState` gains `provider: dict | None = None`, and
    `to_dict` includes it when set.
  - `gazevoice/components/__init__.py`: `build_components` appends `provider_units(ctx)`.
- Test: `tests/test_provider_units.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces:
  - `ApiProviderUnit(ctx, config: ProviderConfig, keys: KeyStore)`:
    - `id = config.id`, `name = config.name` and `role = "Brain · API key"`;
    - `check()` → `MISSING` "Add your API key to use {name}." when there's no key, otherwise
      `READY` "Key saved ••••{hint}";
    - `details`: Used for (the steps using it, or "Not used yet") and Last test;
    - `actions`: `[ActionSpec("test", "Test", uses_plan=True)]` when a key is saved;
    - `provider`: id, kind, name, base_url, region, workspace, has_key and key_hint.
  - `CustomProviderSetup(ctx)`, with `id = "custom_new"`, `name = "Custom provider"` and `provider`
    set to `{"kind": "custom", "new": True}`.
  - `provider_units(ctx, keys: KeyStore | None = None) -> list[Component]`, in the order deepseek,
    qwen, saved customs, then `custom_new`.
  - `ApiProviderUnit.action_test(job) -> dict` checks that the key lists or answers. For each step
    whose source is this provider, it sends one tiny chat with that step's model, and passes the MCP
    tool definitions for the Editing step. It returns `{"models": [...], "editing": "ok"|None,
    "cleanup": "ok"|None}` and stores `last test` in memory on the unit.

- [ ] **Step 1: Write the failing tests:**
  - `test_units_without_keys_ask_for_one`: DeepSeek is `MISSING` and offers no `test`, and
    `to_dict()["provider"]["has_key"] is False`.
  - `test_saved_key_shows_only_its_hint`: with `key_hint="1234"`, the summary contains "••••1234" and
    `to_dict()` contains no other part of the key.
  - `test_custom_units_follow_settings`: two saved customs give units in the order `deepseek, qwen,
    custom_a, custom_b, custom_new`.
  - `test_test_action_checks_each_step_model`:
    - It uses the fake HTTP server from Task 1, with `editor_source="deepseek"` and
      `editor_api_model="deepseek-v4-pro"`.
    - The chat request carries `tools`, and the result has `editing == "ok"`.
  - `test_provider_list_lists_cleanup_and_editing`: a provider used by both steps shows "Cleanup,
    Editing".

- [ ] **Step 2: Run them and see them fail**
  - Run: `python3 -m unittest tests.test_provider_units -v`
  - Expected: errors.

- [ ] **Step 3: Implement.**
  - The MCP tool list for the test comes from a short-lived `StdioMcpClient` on
    `server_command(paths, find_blender(runner))`.
  - In the unit tests, `ApiProviderUnit` takes an optional `mcp_factory` like `ApiSession`.

- [ ] **Step 4: Run** `python3 -m unittest tests.test_provider_units -v`. Expected: OK.

- [ ] **Step 5: Commit**: "Show DeepSeek, Qwen and custom providers as units in the rack".

### Task 5: Control center: per-step sources, readiness, endpoints and wiring

**Files:**
- Modify:
  - `gazevoice/components/brains.py`: add `resolve_sources(states, settings) -> dict[str, str | None]`
    with keys `"refiner"` and `"editor"`, plus `sources_pending(states, settings) -> bool`.
  - `gazevoice/workflow.py`:
    - `workflow_stages(states, sources)`;
    - Check everything's last step is `(editor source, "test")` for a provider and
      `(editor source, "connect_blender")` for claude/codex;
    - `CHECK_CONFIRM` mentions "your brain or API provider".
  - `gazevoice/voice/wiring.py`:
    - `voice_readiness(states, sources, settings) -> list[str]`: per step, "Cleanup/Editing: …" when
      the source is unresolved, has no key, or has no model;
    - `build_voice(ctx, bus, sources, keys=None)`: a per-step factory for `claude`, `codex` or a
      provider (`ApiSession` with `ChatClient(config, keys.get(id))`).
  - `gazevoice/server/app.py`:
    - `ControlCenter.sources(states)`;
    - the snapshot adds `"sources"` and keeps `"brain"` equal to the editor's source;
    - `_voice_config_now` includes both sources, both api models and thinking, and `providers`;
    - `save_provider(pid, body) -> dict`, `remove_provider(pid) -> dict`, `provider_models(pid) -> dict`
      and `reload_components()`;
    - routes `POST /api/providers/<id>`, `POST /api/providers/<id>/remove` and
      `GET /api/providers/<id>/models`, with `<id>` matching `[a-z0-9_]+`.
- Test:
  - `tests/test_voice_api.py`, plus a new `tests/test_provider_api.py`;
  - update `tests/test_workflow.py` (or wherever `workflow_stages` and `WorkflowCheck` are tested) for
    the new signature.

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - The HTTP contract the dashboard uses (Task 6).
  - `POST /api/providers/<id>` with body
    `{"kind": "deepseek"|"qwen"|"custom", "name"?, "base_url"?, "region"?, "workspace"?, "key"?}`
    returns `200 {"provider": <public dict>, "settings": <settings>}`. The id `custom_new` creates a
    new id from the name.
  - `POST /api/providers/<id>/remove` returns `200 {"settings": …, "moved_to_auto": ["editor", …]}`.
  - `GET /api/providers/<id>/models` returns `200 {"models": [...]}` or
    `200 {"models": [], "error": "…"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_save_deepseek_key`:
    - With a FakeRunner key store, `POST /api/providers/deepseek {"kind": "deepseek",
      "key": " sk-1234 "}` returns 200.
    - The response JSON has `key_hint == "1234"` and no `"sk-1234"`.
    - The saved settings.json has no `"sk-1234"`.
    - The key store received `sk-1234`.
  - `test_provider_writes_are_guarded`: the same POST without `X-GazeVoice` gets 403.
  - `test_custom_new_gets_an_id_from_its_name`: `name="Home Box"` gives id `custom_home_box`.
  - `test_remove_moves_steps_to_auto`: with `editor_source="deepseek"`, remove gives
    `moved_to_auto == ["editor"]`, and the key store's `delete` was called.
  - `test_models_endpoint_reports_errors_plainly`: an unreachable base gives `error` containing
    "Couldn't reach".
  - `test_readiness_names_the_step`:
    - `editor_source="deepseek"` with no key gives a missing line starting "Editing: DeepSeek";
    - with both steps on a keyed provider with models set, Claude/Codex `MISSING` doesn't block.
  - `test_stages_follow_each_step`: `workflow_stages` with `{"refiner": "claude", "editor": "deepseek"}`
    lists parts `["claude"]` for refine and `["deepseek", "mcp_server"]` for edit.
  - `test_build_voice_uses_api_sessions_for_providers`: the editor factory returns an `ApiSession`
    whose `init["model"]` is the step's `editor_api_model`.
  - `test_changing_a_source_asks_for_a_restart`: `needs_restart` turns true after changing
    `refiner_source` while voice is on.

- [ ] **Step 2: Run them and see them fail**
  - Run: `python3 -m unittest tests.test_provider_api tests.test_voice_api tests.test_workflow -v`
  - Expected: failures.

- [ ] **Step 3: Implement.**
  - Each save or remove saves settings and publishes `settings.updated`. It then calls
    `reload_components()`, which rebuilds the provider units and keeps the others. `self._last` and
    the probes are cleared.
  - `provider_models` caches results in memory for 10 minutes per provider.

- [ ] **Step 4: Run the full suite**
  - Run: `python3 -m unittest discover -s tests -t .`
  - Expected: OK.

- [ ] **Step 5: Commit**: "Pick each step's brain separately and serve provider setup from the control center".

### Task 6: Dashboard: provider units and per-step menus

**Files:**
- Modify:
  - `gazevoice/server/static/app.js`
  - `gazevoice/server/static/app.css`
  - `gazevoice/server/static/index.html` (only if new containers are needed)

**Interfaces:**
- Consumes: the Task 5 HTTP contract; `comp.provider` from Task 4; `S.state.sources`.
- Produces: no new APIs.

- [ ] **Step 1: The Brain shelf.**
  - `GROUPS` Brain ids become claude, codex, then every component whose `provider` is set, in
    snapshot order.
  - A provider unit renders:
    - an **API key** password input (`autocomplete="off"`, never pre-filled);
    - for Qwen, a **Region** select (labels from the server) and an optional **Workspace ID** input;
    - for `custom_new`, **Name** and **Address** inputs;
    - a **Save** key that POSTs to `/api/providers/<id>`.
  - Saved units show "Key saved ••••1234", **Change key**, **Test** ("uses your API credit") and
    **Remove**, which uses the existing arm/confirm pattern.
  - Inputs keep their text across re-renders, the same way `S.typed` does.
- [ ] **Step 2: Routing.**
  - Remove the Brain row.
  - Editing and Cleanup each get:
    - a **source** menu: Auto, Claude Code, Codex, then each provider, labelled "DeepSeek · add a key
      in the rack" when it has no key;
    - for Claude sources, the existing model and thinking menus;
    - for providers, a model menu filled from `GET /api/providers/<id>/models`, falling back to a text
      input with "Model name" when the list is empty, and a thinking menu from the kind's choices
      (none for custom);
    - Codex keeps its note.
- [ ] **Step 3: The voice note.** When either step's source is a provider, the note reads "Your
  commands and scene details go to {names}."
- [ ] **Step 4: Verify.**
  - Run: `node --check gazevoice/server/static/app.js`
  - Take screenshots with the scratchpad `shot.py` against a demo instance: a provider unit with no
    key, a saved key, and Routing with Editing on DeepSeek. Check desktop dark and 390px light.
- [ ] **Step 5: Commit**: "Set up DeepSeek, Qwen and custom providers from the dashboard".

### Task 7: Docs, real check, review

**Files:**
- Modify: `README.md` (a "Use DeepSeek, Qwen or another API" section) and `DESIGN.md` (the
  provider unit and form).

- [ ] **Step 1:** Write the README section:
  1. Setup: Add API key, then pick the source in Routing, then Start voice.
  2. Where keys live (the Keychain) and how to remove them.
  3. That commands and scene names go to the chosen provider.
  4. Qwen's region and Workspace ID.
  5. Custom needs an OpenAI-compatible address.
- [ ] **Step 2:** Run the full suite on Python 3.14 and 3.12. Expected: OK, both times.
- [ ] **Step 3:** Restart the dashboard and check through the UI that DeepSeek and Qwen units and the
  source menus appear. The real DeepSeek call is the user's own test with their key.
- [ ] **Step 4:** Have a fresh reviewer go over the branch. Fix what it finds, with tests.
- [ ] **Step 5: Commit**: "Document API providers".
