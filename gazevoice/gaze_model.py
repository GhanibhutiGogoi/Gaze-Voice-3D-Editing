"""The maths behind gaze: a face in, a point on the viewport out. Standard library only.

    features(landmarks, transform)  one camera frame's face as a short list of numbers
    fit(rows, targets, alpha)       learn where those numbers point, from the dots the person looked at
    choose(rows, targets)           which of those numbers to learn from, by how well each choice does on a dot it never saw
    predict(model, row)             a row of features to (u, v) across the viewport
    check(model, rows, targets)     how far off it is on dots it was not fitted to

The approach (ridge regression from face-landmark features to a screen point, then a calibration of a few
dots) is the one EyeTrax (MIT, github.com/ck-zhang/EyeTrax) uses. This file is written fresh, not adapted
from it. Unlike EyeTrax's it can also read how open the eyes are, because the lids follow a look up and down
more than the iris does.

A fit learns only from the eyes. Where the head is (its x, y and distance) is measured but never learned from. A
calibration is made sitting still, so the head moves a few millimetres at most, and a fit can't learn what a head
that moves does. It would read a centimetre of shifting in your seat as a big change in where you look, and put
the look off the screen. A turn of the head is allowed for by a fixed sum instead of being learned (see
`turn_shift`): it moves the look by the head's distance over the screen's width, times the turn. That sum reads the
head's distance from each row, but only to size the turn, so a small shift in your seat changes it very little.

The feature list is the part most likely to change once the accuracy test has run: edit BASE_NAMES and
`_base`, which must agree, and nothing else needs to move. A calibration saved under another list, one that
reads where the head is, or one made before the turn was allowed for, fails `validate`, so the person is asked to
calibrate again instead of getting wrong positions.
"""

from __future__ import annotations

import math
import statistics
from typing import NamedTuple

CAMERA_ASPECT = 960 / 540  # width over height of the frames the worker reads; only the tests lean on the default
MIN_RADIUS = 0.02  # a patch smaller than this share of the viewport is a promise the camera can't keep
LANDMARKS = 478  # 468 for the face and 10 for the irises

# The 3D landmark numbers of MediaPipe's face model. "Right" and "left" are the person's own, so the
# right eye is on the left of an unmirrored picture. Each eye: the corner on the picture's left, the corner
# on its right, the upper lid, the lower lid, the middle of the iris.
_RIGHT_EYE = (33, 133, 159, 145, 468)
_LEFT_EYE = (362, 263, 386, 374, 473)

BASE_NAMES = [
    "right iris along",  # how far the iris sits along the eye from its middle, in eye widths; right is positive
    "right iris across",  # and across it, down is positive
    "left iris along",
    "left iris across",
    "right eye opening",  # the gap between the lids, in eye widths
    "left eye opening",
    "head yaw",  # turning left and right, radians
    "head pitch",  # nodding
    "head roll",  # tilting
    "head x",  # where the head is, centimetres from the camera's axis
    "head y",
    "head distance",  # and how far from the camera
]
# Every pair of the base values multiplied together, a value with itself included. A fit reads only some of them (see
# FEATURE_SETS). The rest stay in every row because the row's layout, FEATURE_COUNT numbers in this order, is fixed:
# saved calibrations and the worker's rows are both laid out by it, and changing it would stop them matching.
FEATURE_NAMES = BASE_NAMES + [
    f"{a} × {b}" for i, a in enumerate(BASE_NAMES) for b in BASE_NAMES[i:]
]
FEATURE_COUNT = len(FEATURE_NAMES)

_EYES = ["right iris along", "right iris across", "left iris along", "left iris across"]
# The iris hardly moves up and down, but the lids follow the eyes: on the run of 2026-11-09 how open the eyes were cut
# the miss up and down by about 40%.
_LIDS = ["right eye opening", "left eye opening"]
_EYE_PAIRS = [f"{a} × {b}" for i, a in enumerate(_EYES) for b in _EYES[i:]]
_EYE_BY_LIDS = [f"{a} × {b}" for a in _EYES for b in _LIDS]
# What a fit may read, as places in a full row. None of them reads where the head is (see the top of this file), or
# how it is turned: a turn is allowed for by a fixed sum instead (see `turn_shift`). Rows at one dot are near copies,
# so a run is really one piece of evidence per dot, and most of the 90 numbers are more than that can settle.
FEATURE_SETS = {
    name: [FEATURE_NAMES.index(feature) for feature in chosen]
    for name, chosen in {
        "eyes": _EYES,
        "eyes and lids": _EYES + _LIDS,
        "eyes, curved": _EYES + _EYE_PAIRS,
        "eyes and lids, curved": _EYES + _LIDS + _EYE_PAIRS + _EYE_BY_LIDS,
    }.items()
}
ALLOWED_COLUMNS = frozenset(column for columns in FEATURE_SETS.values() for column in columns)
_YAW = BASE_NAMES.index("head yaw")
_DISTANCE = BASE_NAMES.index("head distance")
ALPHA_SHARES = (0.001, 0.01, 0.1, 1.0)  # alpha is this share of the rows being fitted
# Evenly spaced rows per dot, because the rest are near copies, and so a dot that gave more frames doesn't count for
# more. A dot of one second at 30 frames a second is under it and kept whole; it steps in for a faster camera.
KEEP_PER_DOT = 40
CLOSE_ENOUGH = 0.005  # candidates this near the best score count as level with it, and the simpler one is taken


def _eye(landmarks, eye: tuple[int, ...], aspect: float) -> tuple[float, float, float]:
    """(along, across, opening) of one eye. Lengths are measured with x stretched by `aspect`, so a unit
    along the picture is as long as a unit down it."""
    left, right, upper, lower, iris = eye
    (lx, ly), (rx, ry) = landmarks[left][:2], landmarks[right][:2]
    lx, rx = lx * aspect, rx * aspect
    width = math.hypot(rx - lx, ry - ly)
    if width < 1e-6:
        raise ValueError("an eye with no width can't be measured")
    ux, uy = (rx - lx) / width, (ry - ly) / width  # along the eye, from the picture's left corner
    dx, dy = landmarks[iris][0] * aspect - (lx + rx) / 2, landmarks[iris][1] - (ly + ry) / 2
    along = (dx * ux + dy * uy) / width
    across = (dy * ux - dx * uy) / width  # the way that is down when the eye is level
    gap = math.hypot((landmarks[upper][0] - landmarks[lower][0]) * aspect, landmarks[upper][1] - landmarks[lower][1])
    return along, across, gap / width


def _head(transform) -> tuple[float, ...]:
    """(yaw, pitch, roll, x, y, distance) from the 4x4 matrix that carries the canonical face onto this one."""
    turn = [[float(transform[row][col]) for col in range(3)] for row in range(3)]
    for col in range(3):  # the matrix may carry a scale as well as a turn
        size = math.hypot(turn[0][col], turn[1][col], turn[2][col]) or 1.0
        for row in range(3):
            turn[row][col] /= size
    yaw = math.atan2(turn[0][2], turn[2][2])
    pitch = math.atan2(-turn[1][2], math.hypot(turn[0][2], turn[2][2]))
    roll = math.atan2(turn[1][0], turn[1][1])
    x, y, z = (float(transform[row][3]) for row in range(3))
    return yaw, pitch, roll, x, y, math.sqrt(x * x + y * y + z * z)


def _base(landmarks, transform, aspect: float) -> list[float]:
    right_along, right_across, right_open = _eye(landmarks, _RIGHT_EYE, aspect)
    left_along, left_across, left_open = _eye(landmarks, _LEFT_EYE, aspect)
    return [right_along, right_across, left_along, left_across, right_open, left_open, *_head(transform)]


def expand(base: list[float]) -> list[float]:
    """A full row from the base values: them, then every pair of them multiplied, in the order of FEATURE_NAMES."""
    return base + [a * b for i, a in enumerate(base) for b in base[i:]]


def features(
    landmarks: list[tuple[float, float, float]], transform: list[list[float]], aspect: float = CAMERA_ASPECT
) -> list[float]:
    """One frame's face as FEATURE_COUNT numbers, always in the order of FEATURE_NAMES.

    `landmarks` are the 478 (x, y, z) points MediaPipe finds, x and y as shares of the picture. `transform` is
    its 4x4 facial transformation matrix. `aspect` is the picture's width over its height. Raises ValueError
    when the face can't be measured, which the caller treats as no face.
    """
    if len(landmarks) < LANDMARKS:
        raise ValueError(f"expected {LANDMARKS} landmarks, got {len(landmarks)}")
    try:
        base = [float(v) for v in _base(landmarks, transform, aspect)]
    except (IndexError, TypeError) as exc:
        raise ValueError(f"landmarks or transform in the wrong shape: {exc}") from exc
    if not all(math.isfinite(v) for v in base):
        raise ValueError("the face measured as not-a-number")
    return expand(base)


def _good_columns(columns: object, length: int) -> bool:
    return (
        isinstance(columns, list) and bool(columns) and all(type(c) is int and 0 <= c < length for c in columns)
        and len(set(columns)) == len(columns)
    )


def _spread(values) -> float:
    mean = math.fsum(values) / len(values)
    return math.sqrt(math.fsum((v - mean) ** 2 for v in values) / len(values))


def _solve(matrix: list[list[float]], rhs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Solve matrix · w = rhs for two right-hand sides at once, by Gaussian elimination with pivoting."""
    size = len(matrix)
    rows = [row[:] + list(r) for row, r in zip(matrix, rhs)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(rows[r][col]))
        if abs(rows[pivot][col]) < 1e-12:
            raise ValueError("these readings don't settle on one fit; look at more places")
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, size):
            factor = rows[r][col] / rows[col][col]
            if factor:
                rows[r][col:] = [x - factor * y for x, y in zip(rows[r][col:], rows[col][col:])]
    solution: list[tuple[float, float]] = [(0.0, 0.0)] * size
    for i in range(size - 1, -1, -1):
        known = [sum(rows[i][j] * solution[j][k] for j in range(i + 1, size)) for k in (0, 1)]
        solution[i] = tuple((rows[i][size + k] - known[k]) / rows[i][i] for k in (0, 1))
    return solution


def _good_width(width: object) -> bool:
    return type(width) in (int, float) and math.isfinite(width) and width > 0


def turn_shift(row: list[float], screen_width_cm: float | None) -> float:
    """How far a turn of the head moves where the eyes point, across the screen, as a share of its width.

    Eyes that don't turn in the head point where the head does, so a turn of `yaw` radians moves the look about
    (head distance ÷ screen width) × yaw. MediaPipe's yaw is positive when the face turns toward the person's left,
    which is the screen's left, so the look moves toward u = 0. The study of the run of 2026-11-09 found that size and
    sign (-1.37 a radian, 39.6 cm from a 29.0 cm wide screen), and that it did better than learning the turn from a run.
    The head distance is the row's own. With no screen width there is nothing to allow for.
    """
    if screen_width_cm is None:
        return 0.0
    return -row[_DISTANCE] / screen_width_cm * row[_YAW]


def fit(
    rows: list[list[float]], targets: list[tuple[float, float]], alpha: float = 1.0, columns: list[int] | None = None,
    screen_width_cm: float | None = None,
) -> dict:
    """Ridge regression from feature rows to (u, v), as a plain dict that survives JSON.

    `columns` says which places in a row to learn from, or None for all of them. `mean`, `scale` and each list of
    `weights` have one entry per column, and `features` is still the length of a whole row. Each column is first
    moved to mean 0 and spread 1, so `alpha` weighs every feature alike. A larger alpha pulls all the weights towards
    zero, which steadies a fit from few or noisy readings. A column that never changes is left out of the sum.

    With `screen_width_cm` (the width of the part of the screen the dots were on), the eyes learn where they point
    before the head's turn moves it, u minus `turn_shift`, and `predict` adds the shift back. Its rows must be whole,
    because the turn and the distance are read from them. Without it the fit is as it was before the turn was allowed
    for. Raises ValueError when there is nothing to learn from.
    """
    count = len(rows)
    if count == 0 or count != len(targets):
        raise ValueError("fit needs one target for each row, and at least one row")
    length = len(rows[0])
    if length == 0 or any(len(row) != length for row in rows):
        raise ValueError("every row must have the same number of features")
    columns = list(range(length)) if columns is None else list(columns)
    if not _good_columns(columns, length):
        raise ValueError(f"these aren't places in a row of {length} features: {columns}")
    if screen_width_cm is not None:
        if not _good_width(screen_width_cm):
            raise ValueError(f"a screen's width has to be a number of centimetres, not {screen_width_cm!r}")
        if length != FEATURE_COUNT:
            raise ValueError("a turn of the head can only be allowed for from a whole row of features")
        targets = [(u - turn_shift(row, screen_width_cm), v) for row, (u, v) in zip(rows, targets)]
    size = len(columns)
    whole = list(zip(*rows))
    picked = [whole[at] for at in columns]
    mean = [math.fsum(col) / count for col in picked]
    scale = [s if s > 1e-9 else 1.0 for s in map(_spread, picked)]
    z = [[(v - m) / s for v in col] for col, m, s in zip(picked, mean, scale)]
    bias = [math.fsum(t[k] for t in targets) / count for k in (0, 1)]
    centred = [[t[k] - bias[k] for t in targets] for k in (0, 1)]
    gram = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i, size):
            gram[i][j] = gram[j][i] = math.sumprod(z[i], z[j])
        gram[i][i] += alpha
    weights = _solve(gram, [(math.sumprod(z[i], centred[0]), math.sumprod(z[i], centred[1])) for i in range(size)])
    return {
        "kind": "ridge",
        "features": length,
        "columns": columns,
        "mean": mean,
        "scale": scale,
        "weights": [[w[0] for w in weights], [w[1] for w in weights]],
        "bias": bias,
        "alpha": alpha,
        "rows": count,
        "screen_width_cm": screen_width_cm,  # what the turn is allowed for with, or None for not at all
    }


def validate(model: object) -> None:
    """Raise ValueError unless `model` is a fit made for the current feature list."""
    if not isinstance(model, dict) or model.get("kind") != "ridge":
        raise ValueError("this isn't a gaze calibration")
    if model.get("features") != FEATURE_COUNT:
        raise ValueError(
            f"this calibration was made with {model.get('features')} features and now there are {FEATURE_COUNT}"
        )
    if "screen_width_cm" not in model:
        raise ValueError("this calibration was made before gaze allowed for turning your head. Calibrate gaze again.")
    try:
        columns, mean, scale, weights, bias = (model[k] for k in ("columns", "mean", "scale", "weights", "bias"))
        width = model["screen_width_cm"]
        sound = (
            _good_columns(columns, FEATURE_COUNT)
            and len(mean) == len(scale) == len(columns) and len(weights) == 2 and len(bias) == 2
            and all(len(w) == len(columns) for w in weights)
            and all(math.isfinite(v) for v in [*mean, *scale, *bias, *weights[0], *weights[1]])
            and all(s != 0 for s in scale)
            and (width is None or _good_width(width))
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("this gaze calibration is damaged") from exc
    if not sound:
        raise ValueError("this gaze calibration is damaged")
    if not set(columns) <= ALLOWED_COLUMNS:
        raise ValueError(
            "this calibration reads numbers gaze no longer uses, such as where your head sits. Calibrate gaze again."
        )


def predict(model: dict, row: list[float]) -> tuple[float, float]:
    """Where the face in `row` is looking: (u, v), 0 to 1 across the viewport. Not clamped, so it can fall outside."""
    if len(row) != model["features"]:
        raise ValueError(f"the model takes {model['features']} features, not {len(row)}")
    z = [(row[c] - m) / s for c, m, s in zip(model["columns"], model["mean"], model["scale"])]
    return (
        model["bias"][0] + math.sumprod(model["weights"][0], z) + turn_shift(row, model.get("screen_width_cm")),
        model["bias"][1] + math.sumprod(model["weights"][1], z),
    )


def _percentile(values: list[float], share: float) -> float:
    """The value `share` of the way up the sorted list, between neighbours the way spreadsheets do it."""
    ordered = sorted(values)
    place = share * (len(ordered) - 1)
    low = math.floor(place)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (place - low)


def check(model: dict, rows: list[list[float]], targets: list[tuple[float, float]]) -> dict:
    """How far off `model` is on dots it was not fitted to.

    `median` and `p90` are the distance from the dot, in shares of the viewport. `radius` is [ru, rv]: the
    90th percentile of the miss to the side and of the miss up or down, never below MIN_RADIUS. That is the
    reach a patch of rays needs to catch where the person was really looking nine times in ten.
    """
    if not rows or len(rows) != len(targets):
        raise ValueError("check needs one target for each row, and at least one row")
    across, down = [], []
    for row, (tu, tv) in zip(rows, targets):
        u, v = predict(model, row)
        across.append(abs(u - tu))
        down.append(abs(v - tv))
    misses = [math.hypot(a, d) for a, d in zip(across, down)]
    return {
        "median": _percentile(misses, 0.5),
        "p90": _percentile(misses, 0.9),
        "radius": [max(MIN_RADIUS, _percentile(across, 0.9)), max(MIN_RADIUS, _percentile(down, 0.9))],
    }


class Choice(NamedTuple):
    """What `choose` settled on, and `score`: its typical miss on a dot it was not fitted to, as a share of the viewport."""

    name: str
    columns: list[int]
    alpha_share: float
    score: float


def _by_dot(targets: list[tuple[float, float]]) -> dict[tuple[float, float], list[int]]:
    """Where each dot's rows are, in the order the dots came. Each dot is one target."""
    dots: dict[tuple[float, float], list[int]] = {}
    for at, target in enumerate(targets):
        dots.setdefault(tuple(target), []).append(at)
    return dots


def _evenly(places: list[int]) -> list[int]:
    if len(places) <= KEEP_PER_DOT:
        return places
    return [places[i * len(places) // KEEP_PER_DOT] for i in range(KEEP_PER_DOT)]


def thin(rows: list[list[float]], targets: list[tuple[float, float]]) -> tuple[list[list[float]], list[tuple[float, float]]]:
    """At most KEEP_PER_DOT evenly spaced rows of each dot, with their targets."""
    kept = sorted(at for places in _by_dot(targets).values() for at in _evenly(places))
    return [rows[at] for at in kept], [targets[at] for at in kept]


def choose(
    rows: list[list[float]], targets: list[tuple[float, float]], screen_width_cm: float | None = None
) -> Choice:
    """The set of features and the share of alpha that do best on a dot they were not fitted to.

    Rows at one dot are near copies, so leaving out single rows would only ask a fit about rows it has nearly seen.
    So each dot is left out whole in turn: a candidate fits the thinned rows of the other dots, then is marked on that
    dot's rows, and its score is the median over the dots. The lowest wins. Near enough to it, the fewest columns win,
    then the larger share. With only one dot there is nothing to leave out, so the simplest candidate stands. Every
    candidate allows for the head's turn with `screen_width_cm`, as the fit it stands for will (see `fit`).
    """
    dots = _by_dot(targets)
    turns = []
    for left_out, places in dots.items():
        kept = [at for dot, there in dots.items() if dot != left_out for at in _evenly(there)]
        if kept:
            turns.append(([rows[at] for at in kept], [targets[at] for at in kept],
                          [rows[at] for at in places], [targets[at] for at in places]))
    candidates = []
    for name, columns in FEATURE_SETS.items():
        for share in ALPHA_SHARES:
            misses = [
                check(fit(kept, kept_at, share * len(kept), columns, screen_width_cm), seen, seen_at)["median"]
                for kept, kept_at, seen, seen_at in turns
            ]
            candidates.append(Choice(name, columns, share, statistics.median(misses) if misses else math.inf))
    best = min(candidate.score for candidate in candidates)
    close = [candidate for candidate in candidates if candidate.score <= best + CLOSE_ENOUGH]
    return min(close, key=lambda candidate: (len(candidate.columns), -candidate.alpha_share))


def fit_choice(
    rows: list[list[float]], targets: list[tuple[float, float]], choice: Choice, screen_width_cm: float | None = None
) -> dict:
    """Fit what `choose` picked: on thinned rows, the way it was scored, with the same screen width."""
    kept, kept_at = thin(rows, targets)
    return fit(kept, kept_at, choice.alpha_share * len(kept), choice.columns, screen_width_cm)


def middle_miss(targets: list[tuple[float, float]], check_targets: list[tuple[float, float]]) -> float:
    """The median miss on `check_targets` of always saying the middle of the dots in `targets`.

    A model that learned nothing would score about this, so a real one has to beat it.
    """
    dots = list(_by_dot(targets))
    middle = (math.fsum(u for u, _ in dots) / len(dots), math.fsum(v for _, v in dots) / len(dots))
    return _percentile([math.dist(middle, target) for target in check_targets], 0.5)


def head_moved(rows: list[list[float]], targets: list[tuple[float, float]]) -> dict[str, float]:
    """How far the head went between the first dot and the last of a run: `cm`, and `degrees` it turned."""
    dots = list(_by_dot(targets).values())

    def middle(places: list[int], name: str) -> float:
        return statistics.median(rows[at][BASE_NAMES.index(name)] for at in places)

    def apart(names: tuple[str, ...]) -> float:
        return math.dist([middle(dots[0], name) for name in names], [middle(dots[-1], name) for name in names])

    return {"cm": apart(("head x", "head y", "head distance")), "degrees": math.degrees(apart(("head yaw", "head pitch")))}
