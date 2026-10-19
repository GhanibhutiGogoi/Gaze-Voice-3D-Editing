"""User settings, stored as JSON in the app folder."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping

from .providers import THINKING, ProviderConfig

BUILTIN_SOURCES = ("auto", "claude", "codex")  # "auto" = Claude Code when it's logged in, otherwise Codex
API_THINKING = tuple(dict.fromkeys(value for values in THINKING.values() for value in values))
STT_ENGINES = ("parakeet", "whisper")
# Claude Code models by full name; "default" leaves the choice to Claude Code.
CLAUDE_MODELS = ("default", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
EFFORTS = ("default", "low", "medium", "high", "xhigh", "max")  # how much Claude thinks (`claude --effort`)
_RENAMED_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


@dataclass(frozen=True)
class Settings:
    stt_engine: str = "parakeet"
    mic_device: str = ""  # "" = auto (prefer the built-in mic); otherwise an AVFoundation index
    # Each step has a source (a built-in brain or a provider id) and remembers its choices for both kinds.
    editor_source: str = "auto"
    editor_model: str = "default"
    editor_effort: str = "default"
    editor_api_model: str = ""
    editor_api_thinking: str = "default"
    refiner_source: str = "auto"
    refiner_model: str = "claude-haiku-5-5"  # the cleanup step should be quick
    refiner_effort: str = "default"
    refiner_api_model: str = ""
    refiner_api_thinking: str = "off"  # on DeepSeek the default is long reasoning; cleanup should be quick
    providers: tuple = ()  # ProviderConfig.to_dict() per saved provider; keys live in the Keychain

    def to_dict(self) -> dict:
        return asdict(self)


def provider_ids(settings: Settings) -> set[str]:
    return {p["id"] for p in settings.providers}


def _providers(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError("providers must be a list")
    configs = tuple(ProviderConfig.from_dict(item).to_dict() for item in value)
    if len({c["id"] for c in configs}) != len(configs):
        raise ValueError("Two providers have the same id")
    return configs


def _check(key: str, value: Any) -> None:
    if key in ("editor_source", "refiner_source") and not (isinstance(value, str) and value):
        raise ValueError(f"{key} must be a source name")
    if key in ("editor_api_model", "refiner_api_model") and not (
        isinstance(value, str) and len(value) <= 120 and value.isprintable()
    ):
        raise ValueError(f"{key} must be a model name")
    if key in ("editor_api_thinking", "refiner_api_thinking") and value not in API_THINKING:
        raise ValueError(f"{key} must be one of {', '.join(API_THINKING)}")
    if key == "stt_engine" and value not in STT_ENGINES:
        raise ValueError(f"stt_engine must be one of {', '.join(STT_ENGINES)}")
    if key == "mic_device" and not (isinstance(value, str) and (value == "" or value.isdigit())):
        raise ValueError('mic_device must be "" (auto) or a device number such as "2"')
    if key in ("editor_model", "refiner_model") and value not in CLAUDE_MODELS:
        raise ValueError(f"{key} must be one of {', '.join(CLAUDE_MODELS)}")
    if key in ("editor_effort", "refiner_effort") and value not in EFFORTS:
        raise ValueError(f"{key} must be one of {', '.join(EFFORTS)}")


def update_settings(settings: Settings, changes: Mapping[str, Any]) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(changes) - known)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(unknown)}")
    changes = dict(changes)
    if "providers" in changes:
        changes["providers"] = _providers(changes["providers"])
    for key, value in changes.items():
        _check(key, value)
    updated = replace(settings, **changes)
    sources = set(BUILTIN_SOURCES) | provider_ids(updated)
    for key in ("editor_source", "refiner_source"):
        if getattr(updated, key) not in sources:
            raise ValueError(f"{key} must be one of {', '.join(sorted(sources))}")
    return updated


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    settings = Settings()
    if isinstance(raw.get("providers"), list):  # first, so sources can point at them; bad entries drop alone
        good = []
        for item in raw["providers"]:
            try:
                good.append(ProviderConfig.from_dict(item).to_dict())
            except ValueError:
                continue
        try:
            settings = replace(settings, providers=_providers(good))
        except ValueError:
            pass
    if raw.get("brain") in BUILTIN_SOURCES and "editor_source" not in raw and "refiner_source" not in raw:
        raw = {**raw, "editor_source": raw["brain"], "refiner_source": raw["brain"]}  # saved before per-step sources
    for f in fields(Settings):
        if f.name in raw and f.name != "providers":
            value = raw[f.name]
            if f.name in ("editor_model", "refiner_model") and value in _RENAMED_MODELS:
                value = _RENAMED_MODELS[value]  # saved before models were chosen by full name
            try:
                settings = update_settings(settings, {f.name: value})
            except ValueError:
                continue
    return settings


def save_settings(path: Path, settings: Settings) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        json.dump(settings.to_dict(), handle, indent=2)
    os.replace(tmp, path)
