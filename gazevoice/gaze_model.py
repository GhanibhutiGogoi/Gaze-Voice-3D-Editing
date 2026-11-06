"""The maths behind gaze: a face in, a point on the viewport out. Standard library only.

    features(landmarks, transform)  one camera frame's face as a short list of numbers
    fit(rows, targets, alpha)       learn where those numbers point, from the dots the person looked at
    predict(model, row)             a row of features to (u, v) across the viewport
    check(model, rows, targets)     how far off it is on dots it was not fitted to

The approach (ridge regression from face-landmark features to a screen point, then a calibration of a few
dots) is the one EyeTrax (MIT, github.com/ck-zhang/EyeTrax) uses. This file is written fresh, not adapted
from it. Unlike EyeTrax's it also looks at where the head is, so shifting in your seat doesn't read as
looking somewhere else.

The feature list is the part most likely to change once the accuracy test has run: edit BASE_NAMES and
`_base`, which must agree, and nothing else needs to move. A calibration saved under another list fails
`validate`, so the person is asked to calibrate again instead of getting wrong positions.
"""

from __future__ import annotations

import math

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
# Every pair of the base values multiplied together, a value with itself included, because where a given
# eye turn lands depends on how the head is turned.
FEATURE_NAMES = BASE_NAMES + [
    f"{a} × {b}" for i, a in enumerate(BASE_NAMES) for b in BASE_NAMES[i:]
]
FEATURE_COUNT = len(FEATURE_NAMES)


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
    return base + [a * b for i, a in enumerate(base) for b in base[i:]]


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


def fit(rows: list[list[float]], targets: list[tuple[float, float]], alpha: float = 1.0) -> dict:
    """Ridge regression from feature rows to (u, v), as a plain dict that survives JSON.

    Each column is first moved to mean 0 and spread 1, so `alpha` weighs every feature alike. A larger alpha
    pulls all the weights towards zero, which steadies a fit from few or noisy readings. A column that never
    changes is left out of the sum. Raises ValueError when there is nothing to learn from.
    """
    count = len(rows)
    if count == 0 or count != len(targets):
        raise ValueError("fit needs one target for each row, and at least one row")
    size = len(rows[0])
    if size == 0 or any(len(row) != size for row in rows):
        raise ValueError("every row must have the same number of features")
    columns = list(zip(*rows))
    mean = [math.fsum(col) / count for col in columns]
    scale = [s if s > 1e-9 else 1.0 for s in map(_spread, columns)]
    z = [[(v - m) / s for v in col] for col, m, s in zip(columns, mean, scale)]
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
        "features": size,
        "mean": mean,
        "scale": scale,
        "weights": [[w[0] for w in weights], [w[1] for w in weights]],
        "bias": bias,
        "alpha": alpha,
        "rows": count,
    }


def validate(model: object) -> None:
    """Raise ValueError unless `model` is a fit made for the current feature list."""
    if not isinstance(model, dict) or model.get("kind") != "ridge":
        raise ValueError("this isn't a gaze calibration")
    if model.get("features") != FEATURE_COUNT:
        raise ValueError(
            f"this calibration was made with {model.get('features')} features and now there are {FEATURE_COUNT}"
        )
    try:
        mean, scale, weights, bias = model["mean"], model["scale"], model["weights"], model["bias"]
        sound = (
            len(mean) == len(scale) == FEATURE_COUNT and len(weights) == 2 and len(bias) == 2
            and all(len(w) == FEATURE_COUNT for w in weights)
            and all(math.isfinite(v) for v in [*mean, *scale, *bias, *weights[0], *weights[1]])
            and all(s != 0 for s in scale)
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("this gaze calibration is damaged") from exc
    if not sound:
        raise ValueError("this gaze calibration is damaged")


def predict(model: dict, row: list[float]) -> tuple[float, float]:
    """Where the face in `row` is looking: (u, v), 0 to 1 across the viewport. Not clamped, so it can fall outside."""
    if len(row) != model["features"]:
        raise ValueError(f"the model takes {model['features']} features, not {len(row)}")
    z = [(v - m) / s for v, m, s in zip(row, model["mean"], model["scale"])]
    return (
        model["bias"][0] + math.sumprod(model["weights"][0], z),
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
