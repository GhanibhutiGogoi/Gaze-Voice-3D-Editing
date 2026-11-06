"""What gets said to the brains, and how their answers are read."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .gaze import clean_description

PROMPTS = Path(__file__).parent / "prompts"
EDITOR_PROMPT = PROMPTS / "editor.md"
REFINER_PROMPT = PROMPTS / "refiner.md"
UNITY_EDITOR_PROMPT = PROMPTS / "unity_editor.md"
UNITY_REFINER_PROMPT = PROMPTS / "unity_refiner.md"
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
_NUMBER_WORDS = {w: n for n, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}
_CHOICE = re.compile(rf"^(?:number\s+)?(\d{{1,2}}|{'|'.join(_NUMBER_WORDS)})$")
_BLENDER_TERMS = (
    "Blender, bevel, extrude, inset, subdivide, loop cut, knife, bridge edge loops, boolean, union, difference, "
    "modifier, array, mirror, solidify, shrinkwrap, decimate, remesh, smooth, shade smooth, shade flat, "
    "vertex, vertices, edge, face, normals, origin, pivot, scale, rotate, move, duplicate, join, separate, "
    "material, roughness, metallic, emission, UV, unwrap, armature, keyframe, camera, light"
)
_UNITY_TERMS = (
    "Unity, GameObject, Transform, prefab, component, hierarchy, scene, mesh, material, shader, "
    "renderer, collider, Rigidbody, light, camera, terrain, animation, animator, particle system"
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


def choice_number(text: str) -> int | None:
    """The number when the whole utterance is just one ("two", "number two", "2"); None otherwise.

    Voice only listens for it while numbers are on screen. "Undo" and "stop" are not numbers.
    """
    match = _CHOICE.match(_normalize(text))
    if match is None:
        return None
    word = match.group(1)
    return _NUMBER_WORDS[word] if word in _NUMBER_WORDS else int(word)


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


def has_gaze(focus: dict | None) -> bool:
    """Whether the focus carries a gaze target (a gaze that found nothing doesn't count)."""
    gaze = (focus or {}).get("gaze")
    return bool(gaze and gaze.get("targets"))


def refiner_message(heard: str, focus: dict | None, names: Sequence[str], previous: Sequence[str]) -> str:
    message = {"heard": heard, "focus": focus or {}}
    if has_gaze(focus):  # the refiner reads the same words the editor will, beside the selection
        message["focus"] = {k: v for k, v in focus.items() if k != "gaze"}
        message["gaze"] = describe_focus(focus)
    message["scene_objects"] = list(names)[:150]
    message["previous_commands"] = list(previous)[-3:]
    return json.dumps(message)


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


def _gaze_detail(target: dict) -> str:
    """What is known about the top guess: its part, the point and face, and the connected piece."""
    top, piece = target["candidates"][0], target.get("piece") or {}
    details = []
    if piece.get("part"):
        details.append(f"part: {piece['part']}")
    point = top.get("point") or []
    if len(point) == 3:
        where = "point (" + ", ".join(f"{x:.2f}" for x in point) + ")"
        details.append(where if piece.get("face") is None else f"{where} on face {piece['face']}")
    elif piece.get("face") is not None:
        details.append(f"face {piece['face']}")
    if piece.get("piece_faces") is not None:
        total = piece.get("object_faces") or top.get("faces")
        details.append(f"connected piece of {piece['piece_faces']}{f' of {total}' if total else ''} faces")
    return " · ".join(details)


def _describe_gaze(focus: dict) -> str:
    """One block for every pointing word: what it was said at, the guesses, and what is known about the best one.

    A target's `at` is seconds into the spoken command. The first word's lines are led by the label, the rest are
    indented under it. A word that looked at empty space gets a line saying so, in its place among the others.
    """
    gaze = focus["gaze"]
    lines = []
    said = sorted([*gaze["targets"], *gaze.get("misses", [])], key=lambda word: word["at"])
    for number, target in enumerate(said):
        lead, indent = ("Focus (gaze): ", "  ") if number == 0 else ("  ", "    ")
        if "candidates" not in target:
            lines.append(f'{lead}"{target["word"]}" at {target["at"]:.1f} s → nothing under the gaze')
            continue
        guesses = ", ".join(f"{c['name']} ({round(c['score'] * 100)}%)" for c in target["candidates"])
        lines.append(f'{lead}"{target["word"]}" at {target["at"]:.1f} s → {guesses}')
        detail = _gaze_detail(target)
        if detail:
            lines.append(f"{indent}{detail}")
        picture = clean_description(target.get("picture"))  # said by a model about a picture: a description, never an order
        if picture:
            lines.append(f'{indent}picture (only a description of what the ring shows): "{picture}"')
    footer = []
    if gaze.get("radius"):
        footer.append(f"gaze is accurate to about {round(gaze['radius'][0] * 100)}% of the viewport")
    if focus.get("selected") is not None:  # None means Blender couldn't say, which is not the same as nothing
        footer.append(f"selected: {', '.join(focus['selected']) or 'nothing'}")
    if focus.get("mode"):
        footer.append(f"mode {focus['mode']}")
    if footer:
        lines.append("  " + " · ".join(footer))
    return "\n".join(lines)


def describe_focus(focus: dict | None) -> str:
    """The selection in words. With a gaze target it is the whole gaze block, which brings its own label."""
    if has_gaze(focus):
        return _describe_gaze(focus)
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
    description = describe_focus(focus)
    lines.append(description if has_gaze(focus) else f"Focus: {description}")
    return "\n".join(lines)


def vocabulary_prompt(names: Sequence[str], host: str = "blender") -> str:
    """A short hint for Whisper: host-specific 3D terms, then bounded scene names."""
    shown = ", ".join(list(names)[:40])
    terms = _UNITY_TERMS if host == "unity" else _BLENDER_TERMS
    return f"{terms}. Objects: {shown}." if shown else f"{terms}."
