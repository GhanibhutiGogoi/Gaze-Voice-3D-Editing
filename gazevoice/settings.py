"""User settings, stored as JSON in the app folder."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping

BRAINS = ("auto", "claude", "codex")
STT_ENGINES = ("parakeet", "whisper")


@dataclass(frozen=True)
class Settings:
    brain: str = "auto"
    stt_engine: str = "parakeet"
    mic_device: str = ""  # "" = auto (prefer the built-in mic); otherwise an AVFoundation index

    def to_dict(self) -> dict:
        return asdict(self)


def _check(key: str, value: Any) -> None:
    if key == "brain" and value not in BRAINS:
        raise ValueError(f"brain must be one of {', '.join(BRAINS)}")
    if key == "stt_engine" and value not in STT_ENGINES:
        raise ValueError(f"stt_engine must be one of {', '.join(STT_ENGINES)}")
    if key == "mic_device" and not (isinstance(value, str) and (value == "" or value.isdigit())):
        raise ValueError('mic_device must be "" (auto) or a device number such as "2"')


def update_settings(settings: Settings, changes: Mapping[str, Any]) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(changes) - known)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(unknown)}")
    for key, value in changes.items():
        _check(key, value)
    return replace(settings, **dict(changes))


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    settings = Settings()
    for f in fields(Settings):
        if f.name in raw:
            try:
                settings = update_settings(settings, {f.name: raw[f.name]})
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
