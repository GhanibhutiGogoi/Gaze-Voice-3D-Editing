# API providers: DeepSeek, Qwen and custom models

## Why

GazeVoice's brains are the local Claude Code and Codex CLIs. The user wants to run either step,
cleanup or editing, on other well-known models through their official APIs, starting with a
DeepSeek API key they already have. They pick a provider, paste a key, and get going. A Custom
option covers any other service that speaks the same protocol.

This reverses an earlier rule that GazeVoice never asks for an API key. The user changed it on
2026-10-19: keys are asked for only for these providers, never for Claude Code or Codex.

## What the user gets

- **Providers:**
  - DeepSeek and Qwen as presets: the address is built in, and the user only adds a key.
  - Custom: a name, the base address and a key. It's saved under that name and then works like a preset.
  - There can be several Custom providers.
- **A source for each step.** The Editing and Cleanup rows each choose where their model comes from:
  - Auto, which is today's behavior: Claude Code when it's logged in, otherwise Codex
  - Claude Code
  - Codex
  - DeepSeek
  - Qwen
  - any saved Custom provider

  Each step chooses on its own, so both can run on DeepSeek with no Claude Code involved.
- **Model and thinking are chosen per step, not per provider.** With both steps on DeepSeek, Cleanup
  can run `deepseek-flash` while Editing runs `deepseek-v4-pro`, just as Claude steps can be Haiku
  and Opus today. *(My call, following the per-step menus the user approved.)*
- **A model menu filled from the provider.** After a key is saved, GazeVoice asks the provider for
  its model list. If the provider has no list endpoint, the user types the model name.
- **A thinking menu that fits the source.** It offers what that provider actually supports (see below).
  It's hidden when there's no control: for Codex, and for Custom providers.
- **A Test key** for each provider, optional and tagged "uses your API credit". It checks:
  - that the key works;
  - each step model currently set to that provider;
  - for the Editing step's model, that it can call tools.

  It reports clearly when any of these fail.
- **Each command row** shows which provider and model did the edit (it already shows the Claude model).

## Goals

- From DeepSeek key to a working voice command with as few steps as Claude Code takes today.
- Provider-run editing behaves like Claude-run editing:
  - one undo step per command;
  - "stop" works;
  - read-only lookups don't add undo steps;
  - "Couldn't…" replies count as failures;
  - per-command logs.
- Clear, plain errors for the failures people will actually hit:
  - wrong key
  - key from the wrong Qwen region
  - out of credit
  - rate limited
  - a model that can't call tools
  - provider unreachable

## Non-goals

- Presets for OpenAI, Google or Anthropic. A Novo hook on the user's Mac blocks those API addresses
  in code written here, and the user chose to leave them out. Anyone can still reach them through
  Custom by typing the address.
- Streaming tokens to the dashboard, image input, and local model servers (Ollama and the like).
  Ollama would work through Custom if it speaks the same protocol, but it isn't a goal.
- Spending or usage tracking beyond a per-command note.

## Anti-goals

- A key appearing anywhere after it's saved: on the page, in logs, in command records, in errors or
  in settings.json.
- Silently sending commands somewhere the user didn't choose.
- Breaking the Claude Code and Codex paths that work today.

## Constraints

- **Standard library only.** The control center stays Python-standard-library only.
  - HTTP goes through urllib.
  - Provider SDKs aren't allowed. The Novo guard also forbids importing them.
- **Never write the OpenAI, Google or Anthropic API host names, key variable names or SDK imports** in
  any file. The Novo guard rejects them, and it must not be worked around.
- **Local-only server rules still apply:** 127.0.0.1, the `X-GazeVoice` header and Origin checks on
  every write.
  - Saving a key is a write.
  - The key is sent to the server once, in the request body, and is never returned.
- **Keys live in the macOS Keychain.** They're stored with the `security` tool, using service
  `GazeVoice` and one account per provider id.
  - The key must not appear in a process's argument list. Store it through `security -i` with the
    command on stdin, or something equally private.
  - Deleting a provider deletes its key.
- **The existing session interface is the seam.** VoiceService talks to brains through
  `start(**kwargs)`, `send(text, on_tool, timeout)`, `interrupt()`, `close()`, `alive`, `turns`, and
  optionally `init` and `effort`. The provider session must implement the same interface, so
  VoiceService needs no provider-specific code.
- **Editing uses the official Blender MCP server.** It's started with `server_command(...)` and spoken
  to through the existing `StdioMcpClient`. Provider editing gets exactly the same tools Claude Code gets.

## Provider facts (checked 2026-10-19)

### DeepSeek ([docs](https://api-docs.deepseek.com/))

- **Endpoints:** base address `https://api.deepseek.com`; chat at `POST /chat/completions` and the
  model list at `GET /models`. Both use the OpenAI-compatible format with a Bearer key.
- **Models:** currently `deepseek-flash` and `deepseek-v4-pro`. Take them from `/models`; don't
  hard-code them.
- **Tools:** function calling is supported. `tool_choice` of `required` or a named tool is rejected in
  thinking mode, so use `auto`.
- **Thinking is on by default**, at `reasoning_effort` "high". Valid levels are `none`, `low`, `high`
  and `max`, and `thinking: {"type": "disabled"}` turns it off.
  - The menu offers Default, Off, Low, High and Max.
- **Reasoning must be sent back during tool calls.** Within a turn that uses tools, every assistant
  message must go back with its `reasoning_content`, or the API answers 400. Between turns without
  tools, reasoning may be dropped.
  ([thinking guide](https://api-docs.deepseek.com/guides/thinking_mode))

### Qwen, on Alibaba Cloud Model Studio ([docs](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions))

- **Endpoints are per account:** `https://{WorkspaceId}.{region}.maas.aliyuncs.com/compatible-mode/v1`.
  - The regions are Singapore (`ap-southeast-1`), US Virginia (`us-east-1`), China Beijing
    (`cn-beijing`), Hong Kong (`cn-hongkong`), Germany Frankfurt (`eu-central-1`) and Japan Tokyo
    (`ap-northeast-1`).
  - Older shared addresses still work for some regions: `dashscope-intl.aliyuncs.com` for Singapore
    and `dashscope.aliyuncs.com` for Beijing, both under `/compatible-mode/v1`.
  - The Qwen preset therefore asks for a region and an optional Workspace ID. It uses the
    workspace address when an ID is given, and the shared address otherwise, where one exists.
- **Keys work only in their own region.** A wrong-region key fails as an invalid key, and the error
  message should name this likely cause.
- **Tools:** supported. `tool_choice: "required"` isn't.
- **Thinking:** a top-level `enable_thinking` boolean, plus an optional `thinking_budget` token cap.
  - Per-model defaults vary.
  - The menu offers Default, Off and On; Low, Medium and High may map to budgets (builder's call on
    the numbers).
- **Model list:** `GET /models` isn't documented for compatible-mode. Try it, and fall back to a typed
  model name.

### Custom

- Any OpenAI-compatible endpoint: the user gives a base address ending before `/chat/completions`.
- The model list comes from `GET /models` when it answers, otherwise from a typed name.
- No thinking menu.

## How a provider brain works

- **Cleanup:** a plain chat with the existing refiner prompt.
  - The reply is parsed by the existing `parse_refined`.
  - Recent commands go in each message, as today, so cleanup needs no long memory.
- **Editing:** GazeVoice runs the tool loop itself.
  1. Start the Blender MCP server, list its tools, and convert them to function tools (name,
     description, JSON schema).
  2. Send the editor system prompt, the conversation so far and the new command.
  3. When the reply asks for tools, call them through MCP, append the results as `tool` messages, and
     ask again. Stop when the model answers without tool calls, or after a cap that matches Claude's
     12 turns.
  4. Report each tool to `on_tool` as it starts, so the dashboard and undo logic work unchanged.
- **Memory:** the conversation carries across commands, like Claude's session, so "it" still works.
  - Keep it bounded; the builder chooses how, for example the last N commands.
  - "New conversation" clears it.
- **Stop:** `interrupt()` ends the turn within about a second: it abandons the in-flight HTTP request
  and runs no more tools. The turn result is marked interrupted, as Claude's is.
- **Warm-up:** `start()` checks the key and model with one small request, and for the editor that the
  MCP server is reachable. A failure stops Start voice with a plain message, as today.
- **Timeouts:** the same limits as Claude sessions. A timed-out turn leaves no request running behind it.

## Dashboard

- **Brain shelf in the rack:** new units for DeepSeek and Qwen, plus one for Custom providers.
  - Not set up: the unit offers "Add API key". It's a small inline form with the key (and the Qwen
    region and optional Workspace ID), saved with the consent pattern.
  - Set up: it shows "Key saved ••••1234", which steps use it, the last test result, and Test and
    Remove keys. Models are chosen in Routing, per step.
- **Routing:** the Editing and Cleanup rows each get a source menu, then the model and thinking menus
  for that source.
  - The global Brain row (Auto, Claude Code, Codex) goes away, because each step's source menu
    replaces it. *(My call.)*
  - Changing anything while voice is on shows the existing "stop and start voice" note.
- **Privacy note:** when a step runs on a provider, the Voice panel says the commands and scene
  details go to that provider.
- **Signal path:** the Refiner and Editor stages name their source, and are ready when that source is
  ready. A provider is ready when a key is saved and the step has a model chosen. *(My call.)*
  - A passed Test isn't required, so a restart never makes the user pay for another Test before
    voice can start.
  - Start voice's warm-up already checks the key, the model and, for Editing, tool calling, and stops
    with a plain message if any of them fails.
  - The last Test result appears only on the provider's rack unit.
- **Readiness:** Start voice lists what's missing for each step's source. With both steps on providers,
  Claude Code and Codex aren't required. The Blender MCP server and add-on still are.

## Settings

Non-secret provider settings live in settings.json:
- the providers, with id, kind, display name, base address, and Qwen region and workspace;
- for each step:
  - its source;
  - its provider model and provider thinking value, kept separately from its Claude model and Claude
    thinking (today's `editor_model`/`editor_effort` and `refiner_model`/`refiner_effort`).

  Switching a step's source therefore never wipes its other choices.

Older settings must load and keep behaving the same:
- The current `editor_model`, `refiner_model` and `*_effort` stay as they are.
- A saved global `brain` becomes both steps' source: `auto` → Auto, `claude` → Claude Code,
  `codex` → Codex. Someone who chose Codex stays on Codex.
- Auto stays the default source.

## Left to the builder

- Module layout. For example, `gazevoice/providers.py` for presets, the Keychain and HTTP, and
  `gazevoice/voice/api_brain.py` for the session.
- How memory is bounded, retry and backoff for rate limits, and Qwen thinking budgets.
- The exact dashboard layout within the existing design system (DESIGN.md).
- Test strategy. Tests must not call real providers. A fake OpenAI-compatible HTTP server in the tests
  should cover tool loops, `reasoning_content` round trips, errors and interrupts.
