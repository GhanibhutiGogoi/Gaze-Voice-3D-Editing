"""What gets said to the brains, and how their answers are read."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

PROMPTS = Path(__file__).parent / "prompts"
EDITOR_PROMPT = PROMPTS / "editor.md"
REFINER_PROMPT = PROMPTS / "refiner.md"
KINDS = ("command", "undo", "redo", "stop", "ignore")

# Said before or after an instant command, these don't change it: "no, wait, stop it right now".
_LEAD_IN = r"(?:no+|nope|oh|oops|wait|hold on|hang on|hey|okay|ok|uh+|um+|er+|ah+|so|please|just|blender)"
_TRAILER = r"(?:now|right now|there|right there|please|thanks|thank you|already|immediately|blender)"
_META = {
    "undo": re.compile(r"^(?:undo|undo (?:that|it|this|last|the last (?:one|thing|change))|take (?:that|it) back|go back)$"),
    "redo": re.compile(r"^(?:redo|redo (?:that|it))$"),
    "stop": re.compile(
        r"^(?:stop|stop (?:that|it|this|everything)|cancel|cancel (?:that|it|this)|never ?mind|abort|halt"
        r"|wait|hold on|hang on)$"
    ),
}
_BLENDER_TERMS = (
    "Blender, bevel, extrude, inset, subdivide, loop cut, knife, bridge edge loops, boolean, union, difference, "
    "modifier, array, mirror, solidify, shrinkwrap, decimate, remesh, smooth, shade smooth, shade flat, "
    "vertex, vertices, edge, face, normals, origin, pivot, scale, rotate, move, duplicate, join, separate, "
    "material, roughness, metallic, emission, UV, unwrap, armature, keyframe, camera, light"
)


def _normalize(text: str) -> str:
    words = re.sub(r"[^\w\s']", " ", text.lower()).split()
    said = " ".join(w for i, w in enumerate(words) if i == 0 or w != words[i - 1])  # "stop, stop!" → "stop"
    while True:  # a lead-in or trailer only goes when something else is left: a lone "wait" is a stop
        trimmed = re.sub(rf"^{_LEAD_IN}\s+(?=\S)", "", said)
        trimmed = re.sub(rf"(?<=\S)\s+{_TRAILER}$", "", trimmed)
        if trimmed == said:
            return said
        said = trimmed


def meta_command(text: str) -> str | None:
    """"undo" / "redo" / "stop" when the whole utterance is just that; None otherwise."""
    said = _normalize(text)
    for kind, pattern in _META.items():
        if said and pattern.match(said):
            return kind
    return None


@dataclass(frozen=True)
class Refined:
    kind: str
    text: str  # what the editor follows: one sentence for an exact edit, a detailed brief for a build
    fallback: bool = False  # True when the refiner's reply couldn't be used
    title: str = ""  # a short name for the dashboard, the status line and the undo step


def first_sentence(text: str, limit: int = 100) -> str:
    match = re.match(r"(.+?[.!?])(?:\s|$)", text.strip(), re.DOTALL)
    sentence = (match.group(1) if match else text.strip())
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


def refiner_message(heard: str, focus: dict | None, names: Sequence[str], previous: Sequence[str]) -> str:
    return json.dumps({
        "heard": heard,
        "focus": focus or {},
        "scene_objects": list(names)[:150],
        "previous_commands": list(previous)[-3:],
    })


def _first_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def parse_refined(reply: str, heard: str) -> Refined:
    """The refiner's JSON, or the transcript as the command if the reply can't be used."""
    data = _first_json_object(reply or "")
    if data is not None:
        kind = data.get("kind")
        text = data.get("text") if isinstance(data.get("text"), str) else ""
        title = data.get("title") if isinstance(data.get("title"), str) else ""
        if kind in KINDS and (kind != "command" or text.strip()):
            text = text.strip()
            return Refined(kind, text, title=title.strip() or first_sentence(text))
    return Refined("command", heard.strip(), fallback=True, title=heard.strip())


def describe_focus(focus: dict | None) -> str:
    if not focus:
        return "unknown (Blender didn't report a selection)"
    selected = focus.get("selected") or []
    mode = focus.get("mode") or "?"
    if not selected:
        return f"nothing selected · mode {mode} · source: selection"
    active = focus.get("active")
    lead = f"selected {', '.join(selected)}"
    if active:
        lead += f" (active: {active})"
    return f"{lead} · mode {mode} · source: selection"


def editor_message(command: str, heard: str, focus: dict | None) -> str:
    lines = [f"Command: {command}"]
    if heard and heard.strip() != command.strip():
        lines.append(f'Heard: "{heard.strip()}"')
    lines.append(f"Focus: {describe_focus(focus)}")
    return "\n".join(lines)


def vocabulary_prompt(names: Sequence[str]) -> str:
    """A short hint for Whisper: 3D terms, then the scene's object names (bounded for its prompt limit)."""
    shown = ", ".join(list(names)[:40])
    return f"{_BLENDER_TERMS}. Objects: {shown}." if shown else f"{_BLENDER_TERMS}."
