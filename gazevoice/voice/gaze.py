"""What "this" and "there" point at: gaze samples and word timings in, ranked guesses out.

Pure logic. Blender is reached only through the `pick` and `piece` callables the host passes to `ground`.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import threading
import unicodedata
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

POINTING_WORDS = ("this", "that", "these", "those", "it", "them", "here", "there")
BEFORE_WORD = 0.6  # seconds of gaze to read before a pointing word starts: people look, then speak
AFTER_WORD = 0.2  # and a little after it ends
MIN_SAMPLES = 5  # fewer good samples than this isn't a fixation
MIN_CONFIDENCE = 0.5  # a sample below this is a blink or a lost face
RAYS = 24  # votes per pointing word
CONFIDENT_SCORE = 0.6  # the top guess needs at least this share of the rays
CONFIDENT_GAP = 0.25  # and this much more than the next guess
LONE_GUESS_FLOOR = 0.2  # when only one object was hit, this share of the rays is enough: the rest hit nothing
DEFAULT_RADIUS = (0.10, 0.15)  # half the patch the rays cover, as a share of the viewport width and height
LOOKING_AWAY_MARGIN = 0.1  # a reading this far or less outside the area is noise; further out is looking away
OFF_SCREEN_SHARE = 0.8  # this share or more of the readings around a command zeroed as off the screen is "reading off the screen"
MARKER_WINDOW = 0.2  # seconds of gaze the live marker is made from: short, so it follows the eyes
MARKER_MIN_SAMPLES = 3  # and it needs at least this many good readings in that time to show at all
MAX_CANDIDATES = 3
TRACK_SECONDS = 90.0  # gaze kept in memory: more than the longest hold, so all of a clip is still here once it's heard
PICTURE_MAX_CHARS = 120  # a picture's sentence is cut to this many characters

_GOLDEN_ANGLE = math.pi * (3 - math.sqrt(5))
_EDGE_PUNCTUATION = re.compile(r"^\W+|\W+$")
_SLACK = 1e-9  # 13/24 - 7/24 is 0.24999999999999994, and still a lead of exactly 0.25
# Every mark that reads as a quote: straight, curly, angle, full-width, corner and prime marks, and the grave accent.
_QUOTES = dict.fromkeys(map(ord, "\"'`\u00b4\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f\u00ab\u00bb\u2039\u203a"
                                 "\u2032\u2033\u2035\u2036\u301d\u301e\u301f\uff02\uff07\uff40"))
_UNCLEAR = re.compile(r"\W*unclear\W*", re.IGNORECASE)


def one_line(text: object, limit: int) -> str:
    """The first line of `text` that has anything in it, safe to put in front of a model, at most `limit` characters.

    No quote marks, no characters that don't print (control, format like zero-width and right-to-left marks,
    unassigned), whitespace of any kind as single spaces. Empty when there is nothing to keep. Doing it twice
    changes nothing.
    """
    if not isinstance(text, str):
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    kept = []
    for char in lines[0].translate(_QUOTES):
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char)[0] != "C":
            kept.append(char)
    return " ".join("".join(kept).split())[:limit].rstrip()


def clean_description(text: object) -> str | None:
    """A model's reading of a picture, made safe to put in front of other models; None when there is nothing to use.

    What a model says about a picture of the viewport is untrusted: the viewport can show any text at all. So it is
    reduced by `one_line` to at most `PICTURE_MAX_CHARS` characters. An empty reply, or "unclear" (the describer's way
    of saying it couldn't tell), is None. Cleaning a result again changes nothing.
    """
    line = one_line(text, PICTURE_MAX_CHARS)
    return None if not line or _UNCLEAR.fullmatch(line) else line


@dataclass(frozen=True)
class Area:
    """A rectangle on this Mac's screens, in points from the top left of the main screen."""

    left: float
    top: float
    width: float
    height: float

    def holds(self, other: Area) -> bool:
        """Whether the middle of `other` is inside this area."""
        x, y = other.left + other.width / 2, other.top + other.height / 2
        return self.left <= x <= self.left + self.width and self.top <= y <= self.top + self.height


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def area_from(report: object) -> Area | None:
    """The area `report` describes, or None unless it gives a place and a size above zero."""
    if not isinstance(report, dict):
        return None
    left, top, width, height = values = [report.get(key) for key in ("left", "top", "width", "height")]
    if not all(_number(value) for value in values) or width <= 0 or height <= 0:
        return None
    return Area(left, top, width, height)


def viewport_area(reply: object, frame_of: Callable[[int, int, int], Sequence[float] | None]) -> Area | None:
    """Where Blender's viewport is on the screen; None when that can't be worked out.

    `reply` is a gaze_view reply: the viewport's size and its place in its window, in the window's own pixels from
    the bottom left. `frame_of(pid, width, height)` asks macOS where the window is, title bar included. It has to be
    macOS: Blender gets the window's size right, but the height it reports for it on the screen is off by some amount.
    """
    if not isinstance(reply, dict):
        return None
    keys = ("width", "height", "x", "y", "scale", "window_width", "window_height", "pid")
    width, height, x, y, scale, window_width, window_height, pid = values = [reply.get(key) for key in keys]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return None
    if min(width, height, scale, window_width, window_height) <= 0:
        return None
    frame = frame_of(pid, window_width, window_height)
    if frame is None:
        return None
    left, top, _, frame_height = frame
    bottom = top + frame_height - y / scale
    return Area(left + x / scale, bottom - height / scale, width / scale, height / scale)


def on_viewport(u: float, v: float, screen: Area, view: Area) -> tuple[float, float]:
    """A place given as shares of `screen`, as shares of `view`. Outside 0 to 1 when it is outside the viewport."""
    x, y = screen.left + u * screen.width, screen.top + v * screen.height
    return (x - view.left) / view.width, (y - view.top) / view.height


def reach_on_viewport(radius: Sequence[float], screen: Area, view: Area) -> tuple[float, float]:
    """A patch's half-size given as shares of `screen`, as shares of `view`."""
    return radius[0] * screen.width / view.width, radius[1] * screen.height / view.height


@dataclass(frozen=True)
class GazeSample:
    """Where the eyes were at time `t` (`time.time()`). `u, v` run 0 to 1 from the top left of the viewport, or of the
    screen a camera was calibrated on until `track_on_viewport` has moved them."""

    t: float
    u: float
    v: float
    confidence: float
    away: bool = False  # the tracker zeroed `confidence` because the reading was well off the screen, not for a blink


class GazeTrack:
    """The last stretch of gaze, kept in memory only. Safe to feed from one thread and read from another."""

    def __init__(self, seconds: float = TRACK_SECONDS):
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

    def copy_between(self, t0: float, t1: float) -> GazeTrack:
        """A track of its own with just the samples from `t0` to `t1`, whatever happens to this one afterwards."""
        copy = GazeTrack(self.seconds)
        copy._samples.extend(self.between(t0, t1))
        return copy

    @property
    def latest_time(self) -> float | None:
        """When the newest sample was taken, or None before the first one."""
        with self._lock:
            return self._samples[-1].t if self._samples else None


def track_on_viewport(track: GazeTrack, screen: Area, view: Area) -> GazeTrack:
    """A copy of `track` with its places as shares of `view`. A look well outside it isn't a look at it."""
    moved = GazeTrack(track.seconds)
    for sample in track.between(-math.inf, math.inf):
        u, v = on_viewport(sample.u, sample.v, screen, view)
        inside = all(-LOOKING_AWAY_MARGIN <= share <= 1 + LOOKING_AWAY_MARGIN for share in (u, v))
        moved.add(GazeSample(sample.t, u, v, sample.confidence if inside else 0.0, sample.away))
    return moved


def marker_point(track: GazeTrack, now: float, screen: Area, view: Area) -> tuple[float, float] | None:
    """Where the live marker goes: the middle of the last `MARKER_WINDOW` seconds of gaze, as shares of `view`.

    Only readings at or above `MIN_CONFIDENCE` count, and there have to be `MARKER_MIN_SAMPLES` of them, else None:
    there is no look to show. So is a middle further outside the viewport than `LOOKING_AWAY_MARGIN`, which is
    looking away. One within the margin is noise at the edge, and it is put on the edge, because the bridge takes
    0 to 1 only.
    """
    good = [s for s in track.between(now - MARKER_WINDOW, now) if s.confidence >= MIN_CONFIDENCE]
    if len(good) < MARKER_MIN_SAMPLES:
        return None
    u, v = on_viewport(statistics.median(s.u for s in good), statistics.median(s.v for s in good), screen, view)
    if not all(-LOOKING_AWAY_MARGIN <= share <= 1 + LOOKING_AWAY_MARGIN for share in (u, v)):
        return None
    return _on_viewport(u), _on_viewport(v)


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
class Miss:
    """A pointing word said while the eyes had settled on empty space: every ray missed, so there's no 3D point."""

    word: str
    at: float


@dataclass
class Window:
    """What the gaze held around one pointing word: `readings` taken in its window, `good` of them confident enough
    to use, and `off_screen` of them zeroed by the tracker as well off the screen."""

    word: str
    at: float
    readings: int
    good: int
    off_screen: int


def mostly_off_screen(windows: list[Window]) -> bool:
    """Whether OFF_SCREEN_SHARE or more of all the readings in `windows` were zeroed as off the screen.

    That is what a calibration that reads wrong looks like: every reading lands off the screen, and none is left to
    use. Windows with no readings at all say nothing, because that is gaze that was lost, not gaze that is off.
    """
    readings = sum(window.readings for window in windows)
    return readings > 0 and sum(window.off_screen for window in windows) >= OFF_SCREEN_SHARE * readings


@dataclass
class Grounding:
    """What the pointing words in a sentence refer to.

    `status` is "resolved" when every target has a clear winner, "clarify" when some target needs the user to
    choose, and "none" when there is no gaze to go on. `misses` are the words that looked at nothing at all.
    """

    status: str
    targets: list[Target]
    radius: tuple[float, float]
    misses: list[Miss] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)  # one for each pointing word, found or not

    def to_dict(self) -> dict:
        return {"status": self.status, "targets": [t.to_dict() for t in self.targets], "radius": list(self.radius),
                "misses": [asdict(m) for m in self.misses], "windows": [asdict(w) for w in self.windows]}


def is_clear(target: Target) -> bool:
    """Whether the top guess wins by enough that nobody needs to be asked which one was meant.

    A guess with no rival only has to clear LONE_GUESS_FLOOR: rays that hit nothing vote for nothing, so a wing
    against empty background isn't in doubt just because it fills part of the patch. Rivals need the full rule.
    """
    best = target.candidates[0].score
    if len(target.candidates) == 1:
        return best >= LONE_GUESS_FLOOR - _SLACK
    return best >= CONFIDENT_SCORE - _SLACK and best - target.candidates[1].score >= CONFIDENT_GAP - _SLACK


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
    `piece`. A word that has no gaze gets no target, and one whose rays all hit nothing gets a Miss in its place.
    `pick` returns None when it couldn't ask. A `piece` that returns None or can't reach Blender (OSError or
    ValueError) still leaves the object as a guess, with `piece=None`. Each word also leaves a Window in
    `windows`, found or not: how many readings it had, and how many of those were good enough to use.
    """
    radius = (float(radius[0]), float(radius[1]))
    targets = []
    misses = []
    windows = []
    for word in pointing_words(words, clip_start):
        around = track.between(word.start - BEFORE_WORD, word.end + AFTER_WORD)
        windows.append(Window(word.text, word.start, len(around), sum(s.confidence >= MIN_CONFIDENCE for s in around),
                              sum(s.away for s in around)))
        fixation = fixate(around)
        if fixation is None:
            continue
        points = ray_points(fixation, radius)
        hits = pick(points)
        if not hits:
            continue
        candidates = score_hits(points, list(hits))
        if not candidates:
            misses.append(Miss(word.text, word.start))
            continue
        try:
            looked_closer = piece(candidates[0])
        except (OSError, ValueError):
            looked_closer = None
        targets.append(Target(word.text, word.start, candidates, fixation, piece=looked_closer))
    if not targets:
        return Grounding("none", [], radius, misses, windows)
    return Grounding("resolved" if all(is_clear(t) for t in targets) else "clarify", targets, radius, misses, windows)
