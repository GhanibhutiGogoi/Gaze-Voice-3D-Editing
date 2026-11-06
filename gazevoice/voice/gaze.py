"""What "this" and "there" point at: gaze samples and word timings in, ranked guesses out.

Pure logic. Blender is reached only through the `pick` and `piece` callables the host passes to `ground`.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import threading
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Sequence

POINTING_WORDS = ("this", "that", "these", "those", "it", "them", "here", "there")
BEFORE_WORD = 0.6  # seconds of gaze to read before a pointing word starts: people look, then speak
AFTER_WORD = 0.2  # and a little after it ends
MIN_SAMPLES = 5  # fewer good samples than this isn't a fixation
MIN_CONFIDENCE = 0.5  # a sample below this is a blink or a lost face
RAYS = 24  # votes per pointing word
CONFIDENT_SCORE = 0.6  # the top guess needs at least this share of the rays
CONFIDENT_GAP = 0.25  # and this much more than the next guess
DEFAULT_RADIUS = (0.10, 0.15)  # half the patch the rays cover, as a share of the viewport width and height
MAX_CANDIDATES = 3

_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$")
_SLACK = 1e-9  # 13/24 - 7/24 is 0.24999999999999994, and still a lead of exactly 0.25


def viewport_size(report: object) -> tuple[int, int] | None:
    """The (width, height) in pixels that `report` says, or None if it isn't two positive whole numbers.

    `report` is a `gaze_view` reply or a calibration's "view". Gaze is calibrated for one viewport size: if
    Blender's is a different size now, the same look lands somewhere else.
    """
    try:
        width, height = report["width"], report["height"]  # type: ignore[index]
    except (TypeError, KeyError):
        return None
    sizes = (width, height)
    if all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in sizes):
        return width, height
    return None


@dataclass(frozen=True)
class GazeSample:
    """Where the eyes were at time `t` (`time.time()`). `u, v` run 0 to 1 across the viewport from the top left."""

    t: float
    u: float
    v: float
    confidence: float


class GazeTrack:
    """The last few seconds of gaze, kept in memory only. Safe to feed from one thread and read from another."""

    def __init__(self, seconds: float = 30.0):
        self.seconds = seconds
        self._samples: deque[GazeSample] = deque()
        self._lock = threading.Lock()

    def add(self, sample: GazeSample) -> None:
        """Keep the sample and forget what is more than `seconds` older than it."""
        with self._lock:
            self._samples.append(sample)
            oldest = sample.t - self.seconds
            while self._samples[0].t < oldest:
                self._samples.popleft()

    def between(self, t0: float, t1: float) -> list[GazeSample]:
        """The samples from `t0` to `t1` inclusive, oldest first."""
        with self._lock:
            return [s for s in self._samples if t0 <= s.t <= t1]

    @property
    def latest_time(self) -> float | None:
        """When the newest sample was taken, or None before the first one."""
        with self._lock:
            return self._samples[-1].t if self._samples else None


@dataclass
class PointingWord:
    """A word like "this", with its start and end on the same clock as the gaze samples."""

    text: str
    start: float
    end: float


def pointing_words(words: list[dict], clip_start: float) -> list[PointingWord]:
    """The pointing words among speech-to-text `words` ({"text", "start", "end"}, seconds into the clip).

    `clip_start` is when the clip began on the `time.time()` clock. Case and punctuation don't matter.
    """
    found = []
    for word in words:
        text = _EDGE_PUNCTUATION.sub("", str(word.get("text", "")).lower())
        if text not in POINTING_WORDS:
            continue
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        found.append(PointingWord(text, clip_start + start, clip_start + end))
    return found


@dataclass
class Fixation:
    """Where the eyes settled: the middle of `samples` good readings."""

    u: float
    v: float
    samples: int


def fixate(samples: list[GazeSample]) -> Fixation | None:
    """The median of the samples that are confident enough, or None if there are too few of them."""
    good = [s for s in samples if s.confidence >= MIN_CONFIDENCE]
    if len(good) < MIN_SAMPLES:
        return None
    return Fixation(statistics.median(s.u for s in good), statistics.median(s.v for s in good), len(good))


def _on_viewport(x: float) -> float:
    return min(1.0, max(0.0, x))


def ray_points(fixation: Fixation, radius: tuple[float, float], rays: int = RAYS) -> list[list[float]]:
    """`rays` points spread evenly over the patch around the fixation, centre first, all inside 0 to 1.

    `radius` is the patch's reach left and right, and up and down. The spread is a sunflower spiral, so the
    same fixation always gives the same points.
    """
    if rays <= 0:
        return []
    ru, rv = radius
    points = [[_on_viewport(fixation.u), _on_viewport(fixation.v)]]
    for i in range(1, rays):
        reach = math.sqrt(i / (rays - 1))
        angle = i * _GOLDEN_ANGLE
        points.append([
            _on_viewport(fixation.u + ru * reach * math.cos(angle)),
            _on_viewport(fixation.v + rv * reach * math.sin(angle)),
        ])
    return points


@dataclass
class Candidate:
    """One guess: an object, the share of the rays that hit it, and where to put its label and aim an edit."""

    uid: int
    name: str
    score: float
    faces: int
    u: float
    v: float
    point: list[float]


def score_hits(points: list[list[float]], hits: list[dict | None]) -> list[Candidate]:
    """Each ray votes for the object it hit, and a miss votes for nothing. Best first, at most MAX_CANDIDATES.

    A candidate's `u, v` is the average of the rays that hit it. Its `point` is the 3D hit of the first of them,
    and the patch centre comes first, so that is the hit nearest the centre.
    """
    tally: dict[int, dict] = {}
    for ray, hit in zip(points, hits):
        uid = hit.get("uid") if hit else None
        if uid is None:
            continue
        entry = tally.setdefault(uid, {"hit": hit, "votes": 0, "u": 0.0, "v": 0.0})
        entry["votes"] += 1
        entry["u"] += ray[0]
        entry["v"] += ray[1]
    candidates = [
        Candidate(
            uid=uid,
            name=str(e["hit"].get("name") or ""),
            score=e["votes"] / len(points),
            faces=int(e["hit"].get("faces") or 0),
            u=e["u"] / e["votes"],
            v=e["v"] / e["votes"],
            point=[float(x) for x in e["hit"].get("point") or []],
        )
        for uid, e in tally.items()
    ]
    candidates.sort(key=lambda c: -c.score)  # stable: a tie goes to the one that was hit nearer the centre
    return candidates[:MAX_CANDIDATES]


@dataclass
class Target:
    """What one pointing word refers to. `piece` is the `gaze_piece` reply for the top candidate."""

    word: str
    at: float
    candidates: list[Candidate]
    fixation: Fixation
    piece: dict | None = None
    picture: str | None = None

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "at": self.at,
            "candidates": [asdict(c) for c in self.candidates],
            "fixation": asdict(self.fixation),
            "piece": json.loads(json.dumps(self.piece)),  # a plain copy, or an error if it isn't JSON
            "picture": self.picture,
        }


@dataclass
class Grounding:
    """What the pointing words in a sentence refer to.

    `status` is "resolved" when every target has a clear winner, "clarify" when some target needs the user to
    choose, and "none" when there is no gaze to go on.
    """

    status: str
    targets: list[Target]
    radius: tuple[float, float]

    def to_dict(self) -> dict:
        return {"status": self.status, "targets": [t.to_dict() for t in self.targets], "radius": list(self.radius)}


def is_clear(target: Target) -> bool:
    """Whether the top guess wins by enough that nobody needs to be asked which one was meant."""
    best = target.candidates[0].score
    runner_up = target.candidates[1].score if len(target.candidates) > 1 else 0.0
    return best >= CONFIDENT_SCORE - _SLACK and best - runner_up >= CONFIDENT_GAP - _SLACK


def ground(
    words: list[dict],
    clip_start: float,
    track: GazeTrack,
    radius: tuple[float, float],
    pick: Callable[[list[list[float]]], Sequence[dict | None] | None],
    piece: Callable[[Candidate], dict | None],
) -> Grounding:
    """Work out what each pointing word in `words` refers to.

    For each one, the gaze from BEFORE_WORD before it starts to AFTER_WORD after it ends is settled into a
    fixation, `pick` is asked what the rays across the patch hit, and the top guess is looked at closer with
    `piece`. A word that has no gaze, or whose rays hit nothing, gets no target. `pick` returns None when it
    couldn't ask. A `piece` that returns None or can't reach Blender (OSError or ValueError) still leaves
    the object as a guess, with `piece=None`.
    """
    radius = (float(radius[0]), float(radius[1]))
    targets = []
    for word in pointing_words(words, clip_start):
        fixation = fixate(track.between(word.start - BEFORE_WORD, word.end + AFTER_WORD))
        if fixation is None:
            continue
        points = ray_points(fixation, radius)
        hits = pick(points)
        if not hits:
            continue
        candidates = score_hits(points, list(hits))
        if not candidates:
            continue
        try:
            looked_closer = piece(candidates[0])
        except (OSError, ValueError):
            looked_closer = None
        targets.append(Target(word.text, word.start, candidates, fixation, piece=looked_closer))
    if not targets:
        return Grounding("none", [], radius)
    return Grounding("resolved" if all(is_clear(t) for t in targets) else "clarify", targets, radius)
