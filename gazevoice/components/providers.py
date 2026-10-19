"""API providers in the rack: DeepSeek, Qwen, each saved custom provider, and "add a custom provider"."""

from __future__ import annotations

import time

from ..jobs import Job, JobError
from ..providers import (
    QWEN_REGIONS,
    THINKING,
    ChatClient,
    KeyStore,
    ProviderConfig,
    ProviderError,
    preset,
    thinking_fields,
)
from ..settings import Settings
from .base import ActionSpec, Component, ComponentState, Health

STEPS = (("refiner", "Cleanup"), ("editor", "Editing"))
# A stand-in tool for Test: enough to learn whether the model accepts tools at all.
_PROBE_TOOL = {"type": "function", "function": {
    "name": "count_objects", "description": "Count the objects in the scene.",
    "parameters": {"type": "object", "properties": {}},
}}
_LAST_TEST: dict[tuple[str, str], str] = {}  # (app folder, provider id) → result; memory only


def saved_configs(settings: Settings) -> dict[str, ProviderConfig]:
    configs = {}
    for item in settings.providers:
        try:
            config = ProviderConfig.from_dict(item)
        except ValueError:
            continue
        configs[config.id] = config
    return configs


def config_for(settings: Settings, provider_id: str) -> ProviderConfig | None:
    saved = saved_configs(settings).get(provider_id)
    if saved is not None:
        return saved
    return preset(provider_id) if provider_id in ("deepseek", "qwen") else None


def used_for(settings: Settings, provider_id: str) -> list[str]:
    return [step for step, _ in STEPS if getattr(settings, f"{step}_source") == provider_id]


class ApiProviderUnit(Component):
    role = "Brain · API key"

    def __init__(self, ctx, config: ProviderConfig, keys: KeyStore | None = None):
        super().__init__(ctx)
        self.config = config
        self.id = config.id
        self.name = config.name
        self.keys = keys or KeyStore(ctx.runner)

    def _config(self) -> ProviderConfig:
        return config_for(self.settings, self.id) or self.config

    def _public(self, config: ProviderConfig, has_key: bool) -> dict:
        public = {
            "id": config.id, "kind": config.kind, "name": config.name, "base_url": config.base_url,
            "region": config.region, "workspace": config.workspace, "has_key": has_key,
            "key_hint": config.key_hint if has_key else "", "used_for": used_for(self.settings, config.id),
            "thinking": list(THINKING[config.kind]), "new": False,
        }
        if config.kind == "qwen":
            public["regions"] = [[region, label] for region, (label, _) in QWEN_REGIONS.items()]
        return public

    def check(self) -> ComponentState:
        config = self._config()
        has_key = self.keys.has(config.id)
        public = self._public(config, has_key)
        details: list[tuple[str, object]] = []
        if config.kind == "qwen":
            details.append(("Region", config.region_label))
        if config.kind == "custom":
            details.append(("Address", config.base_url))
        if not has_key:
            return self.state(Health.MISSING, f"Add your API key to use {config.name}.", details, provider=public)
        steps = [label for step, label in STEPS if step in public["used_for"]]
        details += [("Used for", ", ".join(steps) or "Not used yet"),
                    ("Last test", _LAST_TEST.get((str(self.paths.root), config.id), "Not run yet"))]
        summary = f"Key saved ••••{config.key_hint}" if config.key_hint else "Key saved"
        return self.state(Health.READY, summary, details, [ActionSpec("test", "Test", uses_plan=True)], provider=public)

    def action_test(self, job: Job) -> dict:
        config = self._config()
        key = self.keys.get(config.id)
        if not key:
            raise JobError(f"Save a {config.name} API key first.")
        client = ChatClient(config, key, timeout=60)
        result: dict = {"models": [], "editing": None, "cleanup": None}
        try:
            try:
                result["models"] = client.models()[:50]
                job.log(f"The key works. {config.name} offers {len(result['models'])} models.")
            except ProviderError as exc:
                if "doesn't list its models" not in str(exc):
                    raise
                job.log(str(exc))
            for step, label in STEPS:
                if getattr(self.settings, f"{step}_source") != config.id:
                    continue
                model = getattr(self.settings, f"{step}_api_model")
                if not model:
                    raise JobError(f"Pick a model for {label} in Routing first.")
                body = {"model": model, "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
                        "max_tokens": 16, **thinking_fields(config.kind, getattr(self.settings, f"{step}_api_thinking"))}
                if step == "editor":
                    body["tools"] = [_PROBE_TOOL]
                job.log(f"Asking {model} one small question for {label}…")
                client.chat(body, timeout=60)
                result[label.lower()] = "ok"
        except (ProviderError, JobError) as exc:
            _LAST_TEST[(str(self.paths.root), config.id)] = f"Failed {time.strftime('%H:%M')}: {exc}"
            raise JobError(str(exc)) from None
        _LAST_TEST[(str(self.paths.root), config.id)] = f"Passed {time.strftime('%H:%M')}"
        return result


class CustomProviderSetup(Component):
    id = "custom_new"
    name = "Custom provider"
    role = "Brain · any OpenAI-compatible API"

    def check(self) -> ComponentState:
        return self.state(
            Health.MISSING,
            "Add any OpenAI-compatible service: give it a name, its address and an API key.",
            provider={"id": self.id, "kind": "custom", "name": "", "new": True, "has_key": False,
                      "key_hint": "", "used_for": [], "thinking": list(THINKING["custom"])},
        )


def provider_units(ctx, keys: KeyStore | None = None) -> list[Component]:
    keys = keys or KeyStore(ctx.runner)
    saved = saved_configs(ctx.settings)
    units: list[Component] = [ApiProviderUnit(ctx, saved.get(kind) or preset(kind), keys) for kind in ("deepseek", "qwen")]
    units += [ApiProviderUnit(ctx, config, keys) for config in saved.values() if config.kind == "custom"]
    units.append(CustomProviderSetup(ctx))
    return units
