import json
import math
import random
import statistics
import time
import unittest

from gazevoice import gaze_model
from gazevoice.components.gaze import CALIBRATION_DOTS, CHECK_DOTS, DOT_ORDER
from gazevoice.gaze_model import BASE_NAMES, FEATURE_COUNT, FEATURE_NAMES, FEATURE_SETS, check, choose, features, fit, predict
from gazevoice.voice.gaze import LOOKING_AWAY_MARGIN

IDENTITY = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]


def face(iris=(0.0, 0.0), opening=0.2, transform=IDENTITY):
    """A made-up face of 478 landmarks: two eyes, each as wide as 0.06 of the picture's height, with the iris
    in the middle plus `iris` (in eye widths, along and across the eye) and the lids `opening` eye widths apart."""
    landmarks = [(0.5, 0.5, 0.0)] * 478
    width = 0.06 * 9 / 16  # the same width in x as 0.06 is in y, on a 16:9 picture
    for corner_a, corner_b, upper, lower, centre, x0 in (
        (33, 133, 159, 145, 468, 0.40),  # the eye on the left of the picture
        (362, 263, 386, 374, 473, 0.54),  # the eye on the right
    ):
        mid_x = x0 + width / 2
        landmarks[corner_a] = (x0, 0.45, 0.0)
        landmarks[corner_b] = (x0 + width, 0.45, 0.0)
        landmarks[upper] = (mid_x, 0.45 - opening * 0.06 / 2, 0.0)
        landmarks[lower] = (mid_x, 0.45 + opening * 0.06 / 2, 0.0)
        landmarks[centre] = (mid_x + iris[0] * width, 0.45 + iris[1] * 0.06, 0.0)
    return landmarks, transform


def noisy_rows(rng, count, size):
    return [[rng.uniform(-1, 1) for _ in range(size)] for _ in range(count)]


# A made-up calibration, shaped like the real run of 2026-11-08. These numbers come from the setup, not from what makes
# a test pass: a 13-inch screen seen from 55 cm, an iris 1.2 cm from the middle of an eye 3 cm wide, jitter of half a
# pixel on an eye 50 pixels wide, and the plan's example of a head that creeps 1.2 cm and 2 degrees over the run.
SCREEN_CM = (30.0, 20.0)
AWAY_CM = 55.0
IRIS_PER_RADIAN = 1.2 / 3.0  # eye widths the iris moves as the eye turns
IRIS_DOWN_SHARE = 0.6  # the lids hide part of the iris, so up and down reads smaller than side to side
IRIS_NOISE = 0.01  # eye widths, each frame
HEAD_TURN_NOISE, HEAD_PLACE_NOISE, HEAD_DISTANCE_NOISE = 0.003, 0.03, 0.15  # radians and cm, each frame
FIXATION = math.radians(0.5)  # the eyes settle a little off each dot, and stay there
CREEP = {"yaw": math.radians(1.75), "pitch": math.radians(1.0), "x": 0.8, "y": 0.6, "distance": -0.7}  # over the run
AT_REST = dict.fromkeys(CREEP, 0.0)
SEEDS = range(1, 21)  # a claim about a fit is checked across these, because one seed can pass by luck
SCREEN_WIDTH_CM = 29.0  # a 13-inch MacBook's screen, about as wide as the one the run of 2026-11-09 was made on


def made_up_base(u, v, done, off, rng, creep=CREEP):
    """The 12 base values of one frame, looking at (u, v) with the run `done` shares through.

    The head creeps and the eyes stay on the dot, so the iris sits differently in the eye as the head moves.
    """
    yaw = 0.02 + creep["yaw"] * done + rng.gauss(0, HEAD_TURN_NOISE)
    pitch = 0.10 + creep["pitch"] * done + rng.gauss(0, HEAD_TURN_NOISE)
    roll = 0.01 + rng.gauss(0, HEAD_TURN_NOISE)
    x = 1.0 + creep["x"] * done + rng.gauss(0, HEAD_PLACE_NOISE)
    y = -1.0 + creep["y"] * done + rng.gauss(0, HEAD_PLACE_NOISE)
    distance = AWAY_CM + creep["distance"] * done + rng.gauss(0, HEAD_DISTANCE_NOISE)
    along = IRIS_PER_RADIAN * (((u - 0.5) * SCREEN_CM[0] - (x - 1.0)) / AWAY_CM - (yaw - 0.02) + off[0])
    across = IRIS_PER_RADIAN * IRIS_DOWN_SHARE * (((v - 0.5) * SCREEN_CM[1] - (y + 1.0)) / AWAY_CM - (pitch - 0.10) + off[1])
    right_open = 0.30 - 0.05 * (v - 0.5) + rng.gauss(0, 0.005)  # the lids follow the eyes down
    left_open = 0.30 - 0.05 * (v - 0.5) + rng.gauss(0, 0.005)
    return [along + rng.gauss(0, IRIS_NOISE), across + rng.gauss(0, IRIS_NOISE),
            along + rng.gauss(0, IRIS_NOISE), across + rng.gauss(0, IRIS_NOISE),
            right_open, left_open, yaw, pitch, roll, x, y, distance]


CHECKS_AT_THE_END = tuple((dot, False) for dot in CALIBRATION_DOTS) + tuple((dot, True) for dot in CHECK_DOTS)


def made_up_run(seed=11, frames=30, tell_the_dot=False, creep=CREEP, order=DOT_ORDER):
    """(rows, targets, check rows, check targets) for the learning dots and the check dots, `frames` rows each: 30 is
    the second each dot is looked at, at 30 frames a second.

    The dots come in the real order of DOT_ORDER, check dots mixed in, or in `order`, and the head creeps over the whole
    run. With `tell_the_dot`, head roll holds a steady number of its own for each dot, which gives away which dot it is.
    One seed gives the same noise whatever `creep` is.
    """
    rng = random.Random(seed)
    rows, targets, check_rows, check_targets = [], [], [], []
    dots = order
    for number, ((u, v), held) in enumerate(dots):
        off = (rng.gauss(0, FIXATION), rng.gauss(0, FIXATION))
        tag = rng.uniform(-0.3, 0.3)
        for frame in range(frames):
            base = made_up_base(u, v, (number + frame / frames) / len(dots), off, rng, creep)
            if tell_the_dot:
                base[BASE_NAMES.index("head roll")] = tag + rng.gauss(0, 0.001)
            (check_rows if held else rows).append(gaze_model.expand(base))
            (check_targets if held else targets).append((u, v))
    return rows, targets, check_rows, check_targets


class FeaturesTest(unittest.TestCase):
    def test_features_are_the_same_length_every_time(self):
        rng = random.Random(3)
        lengths = set()
        for _ in range(5):
            landmarks = [(rng.random(), rng.random(), rng.uniform(-0.05, 0.05)) for _ in range(478)]
            transform = [[rng.uniform(-1, 1) for _ in range(4)] for _ in range(4)]
            lengths.add(len(features(landmarks, transform)))
        lengths.add(len(features(*face())))
        lengths.add(len(features(*face(iris=(0.3, -0.2), opening=0.05))))
        self.assertEqual(lengths, {len(FEATURE_NAMES)})

    def test_a_row_is_its_base_values_then_every_pair_of_them(self):
        row = features(*face(iris=(0.2, -0.1)))
        base = row[:len(BASE_NAMES)]
        self.assertEqual(gaze_model.expand(base), row)

    def test_the_names_and_the_values_line_up(self):
        self.assertEqual(len(set(FEATURE_NAMES)), len(FEATURE_NAMES))
        self.assertEqual(len(features(*face())), gaze_model.FEATURE_COUNT)
        self.assertEqual(gaze_model.FEATURE_COUNT, len(FEATURE_NAMES))

    def test_an_iris_moving_along_the_eye_is_measured_in_eye_widths(self):
        centred = dict(zip(FEATURE_NAMES, features(*face())))
        right = dict(zip(FEATURE_NAMES, features(*face(iris=(0.25, 0.0)))))
        for name in ("right iris along", "left iris along"):
            self.assertAlmostEqual(centred[name], 0.0, places=6)
            self.assertAlmostEqual(right[name], 0.25, places=6)
        self.assertAlmostEqual(right["right iris across"], 0.0, places=6)

    def test_an_iris_moving_down_reads_as_positive_across(self):
        down = dict(zip(FEATURE_NAMES, features(*face(iris=(0.0, 0.1)))))
        up = dict(zip(FEATURE_NAMES, features(*face(iris=(0.0, -0.1)))))
        self.assertAlmostEqual(down["left iris across"], 0.1, places=6)
        self.assertAlmostEqual(up["right iris across"], -0.1, places=6)

    def test_a_wider_open_eye_reads_larger(self):
        shut = dict(zip(FEATURE_NAMES, features(*face(opening=0.05))))
        open_ = dict(zip(FEATURE_NAMES, features(*face(opening=0.30))))
        self.assertAlmostEqual(shut["right eye opening"], 0.05, places=6)
        self.assertAlmostEqual(open_["left eye opening"], 0.30, places=6)

    def test_the_head_comes_from_the_transform(self):
        turned = [[math.cos(0.3), 0, math.sin(0.3), 3.0], [0, 1, 0, 4.0], [-math.sin(0.3), 0, math.cos(0.3), -12.0], [0, 0, 0, 1]]
        got = dict(zip(FEATURE_NAMES, features(*face(transform=turned))))
        self.assertAlmostEqual(got["head yaw"], 0.3, places=6)
        self.assertAlmostEqual(got["head pitch"], 0.0, places=6)
        self.assertAlmostEqual(got["head roll"], 0.0, places=6)
        self.assertEqual((got["head x"], got["head y"]), (3.0, 4.0))
        self.assertAlmostEqual(got["head distance"], 13.0)

    def test_products_pair_every_value_with_every_value(self):
        n = len(gaze_model.BASE_NAMES)
        self.assertEqual(len(FEATURE_NAMES), n + n * (n + 1) // 2)
        got = dict(zip(FEATURE_NAMES, features(*face(iris=(0.2, 0.1), transform=[[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -30], [0, 0, 0, 1]]))))
        self.assertAlmostEqual(got["right iris along × head distance"], 0.2 * 30)
        self.assertAlmostEqual(got["right iris along × right iris along"], 0.04)

    def test_a_flat_eye_is_refused_not_divided_by(self):
        landmarks, transform = face()
        landmarks[33] = landmarks[133]
        with self.assertRaises(ValueError):
            features(landmarks, transform)

    def test_too_few_landmarks_is_refused(self):
        landmarks, transform = face()
        with self.assertRaises(ValueError):
            features(landmarks[:468], transform)


class FitTest(unittest.TestCase):
    def test_fit_learns_a_known_mapping(self):
        rng = random.Random(1)
        rows = noisy_rows(rng, 200, 6)
        targets = [(0.5 + 0.3 * r[0] - 0.2 * r[1], 0.4 + 0.25 * r[3] + 0.1 * r[4]) for r in rows]
        model = fit(rows, targets)
        for row in noisy_rows(rng, 50, 6):
            u, v = predict(model, row)
            self.assertAlmostEqual(u, 0.5 + 0.3 * row[0] - 0.2 * row[1], delta=0.01)
            self.assertAlmostEqual(v, 0.4 + 0.25 * row[3] + 0.1 * row[4], delta=0.01)

    def test_fit_learns_it_from_the_real_features_too(self):
        rng = random.Random(5)
        rows, targets = [], []
        for _ in range(120):
            along, across = rng.uniform(-0.3, 0.3), rng.uniform(-0.2, 0.2)
            rows.append(features(*face(iris=(along, across))))
            targets.append((0.5 + 1.4 * along, 0.5 + 1.1 * across))
        model = fit(rows, targets, alpha=0.01)
        along, across = 0.12, -0.08
        u, v = predict(model, features(*face(iris=(along, across))))
        self.assertAlmostEqual(u, 0.5 + 1.4 * along, delta=0.01)
        self.assertAlmostEqual(v, 0.5 + 1.1 * across, delta=0.01)

    def test_more_alpha_steadies_a_noisy_fit(self):
        rng = random.Random(7)

        def truth(row):
            return 0.5 + 0.6 * row[0], 0.5 - 0.6 * row[1]

        # 30 noisy readings of 20 features, only two of which matter
        train = noisy_rows(rng, 30, 20)
        targets = [(u + rng.gauss(0, 0.2), v + rng.gauss(0, 0.2)) for u, v in map(truth, train)]
        fresh = noisy_rows(rng, 200, 20)
        errors, sizes = [], []
        for alpha in (0.001, 1.0, 5.0):
            model = fit(train, targets, alpha=alpha)
            errors.append(sum(math.dist(predict(model, row), truth(row)) for row in fresh) / len(fresh))
            sizes.append(sum(w * w for ws in model["weights"] for w in ws))
        self.assertEqual(errors, sorted(errors, reverse=True))  # the loose fit chases the noise, so misses most
        self.assertLess(errors[2], errors[0] * 0.75)
        self.assertEqual(sizes, sorted(sizes, reverse=True))  # more alpha, smaller weights

    def test_a_model_survives_json(self):
        rng = random.Random(2)
        rows = noisy_rows(rng, 40, 5)
        model = fit(rows, [(r[0], r[1]) for r in rows])
        again = json.loads(json.dumps(model))
        self.assertEqual(predict(again, rows[3]), predict(model, rows[3]))

    def test_a_column_that_never_changes_is_harmless(self):
        rows = [[1.0, float(i % 7)] for i in range(40)]
        model = fit(rows, [(0.1 * (i % 7), 0.5) for i in range(40)])
        u, v = predict(model, [1.0, 3.0])
        self.assertAlmostEqual(u, 0.3, delta=0.01)
        self.assertAlmostEqual(v, 0.5, delta=0.01)

    def test_nothing_to_learn_from_is_refused(self):
        with self.assertRaises(ValueError):
            fit([], [])
        with self.assertRaises(ValueError):
            fit([[1.0, 2.0]], [(0.1, 0.2), (0.3, 0.4)])

    def test_a_row_of_the_wrong_length_is_refused(self):
        model = fit([[0.0, 1.0], [1.0, 0.0], [1.0, 1.0]], [(0, 0), (1, 0), (1, 1)])
        with self.assertRaises(ValueError):
            predict(model, [1.0, 2.0, 3.0])

    def test_validate_tells_a_usable_model_from_a_stale_one(self):
        rows = [list(map(float, range(i, i + len(FEATURE_NAMES)))) for i in range(5)]
        model = fit(rows, [(0.1 * i, 0.2) for i in range(5)], columns=FEATURE_SETS["eyes"])
        gaze_model.validate(model)
        stale = fit([[1.0, 2.0], [2.0, 1.0], [3.0, 5.0]], [(0, 0), (1, 1), (0, 1)])
        for bad in (stale, {}, {"kind": "something else"}, None, "model"):
            with self.assertRaises(ValueError):
                gaze_model.validate(bad)


class ChoosingTest(unittest.TestCase):
    """Choosing what to fit, on made-up runs. They show what head drift does, not why the real run missed by 32%."""

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.targets, _, _ = made_up_run()
        cls.choice = choose(cls.rows, cls.targets)

    def test_with_the_check_dots_mixed_in_head_drift_is_not_marked_as_a_miss(self):
        # With the check dots at the end, as before 2026-11-09, a head that creeps over the run sits further from where
        # it sat for the learning dots, and the mark measured the creep as well as the fit: a fit on every feature came
        # out nearly twice as far off as on a still run. Mixed in, the check dots see the head the fit saw.
        def drift_over_still(order):
            return statistics.median(self.miss_of_every_feature(seed, CREEP, order) /
                                     self.miss_of_every_feature(seed, AT_REST, order) for seed in SEEDS)

        self.assertGreater(drift_over_still(CHECKS_AT_THE_END), 1.5)
        self.assertLess(drift_over_still(DOT_ORDER), 1.2)

    @staticmethod
    def miss_of_every_feature(seed, creep, order=DOT_ORDER):
        rows, targets, check_rows, check_targets = made_up_run(seed=seed, creep=creep, order=order)
        return check(fit(rows, targets), check_rows, check_targets)["median"]

    def test_a_chosen_model_lands_on_places_it_never_saw(self):
        # These runs walk the real CALIBRATION_DOTS, so this fails if their order goes back to following time.
        landed = 0
        for seed in SEEDS:
            rows, targets, check_rows, check_targets = made_up_run(seed=seed)
            model = gaze_model.fit_choice(rows, targets, choose(rows, targets))
            landed += check(model, check_rows, check_targets)["median"] < 0.15
        self.assertGreaterEqual(landed, 18)

    def test_choosing_leaves_out_a_whole_dot_at_a_time(self):
        rows, targets, _, _ = made_up_run(tell_the_dot=True)
        picked = choose(rows, targets)
        self.assertNotIn(BASE_NAMES.index("head roll"), picked.columns)

    def test_a_fit_on_some_columns_predicts_from_a_full_row(self):
        columns = FEATURE_SETS["eyes"]
        model = fit(self.rows, self.targets, alpha=5.0, columns=columns)
        self.assertEqual(model["features"], FEATURE_COUNT)
        self.assertEqual(model["columns"], columns)
        for numbers in (model["mean"], model["scale"], *model["weights"]):
            self.assertEqual(len(numbers), len(columns))
        gaze_model.validate(model)
        narrow = fit([[row[c] for c in columns] for row in self.rows], self.targets, alpha=5.0)
        full_row = self.rows[7]
        self.assertEqual(predict(model, full_row), predict(narrow, [full_row[c] for c in columns]))
        with self.assertRaises(ValueError):
            predict(model, [full_row[c] for c in columns])

    def test_a_fit_on_some_columns_survives_json(self):
        model = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes and lids"])
        again = json.loads(json.dumps(model))
        gaze_model.validate(again)
        self.assertEqual(predict(again, self.rows[3]), predict(model, self.rows[3]))

    def test_columns_that_arent_there_are_refused(self):
        for columns in ([], [FEATURE_COUNT], [-1], [0, 0]):
            with self.subTest(columns), self.assertRaises(ValueError):
                fit(self.rows, self.targets, columns=columns)

    def test_validate_refuses_columns_that_dont_match_the_weights(self):
        good = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"])
        gaze_model.validate(good)
        for change in ({"columns": good["columns"][:-1]}, {"columns": [0, 1, 2, FEATURE_COUNT]}, {"columns": [0, 0, 1, 2]},
                       {"columns": [0.0, 1.0, 2.0, 3.0]}, {"columns": [True, 1, 2, 3]}, {"columns": None},
                       {"mean": good["mean"][:-1]}, {"scale": good["scale"] + [1.0]},
                       {"weights": [good["weights"][0][:-1], good["weights"][1]]}):
            with self.subTest(change), self.assertRaises(ValueError):
                gaze_model.validate({**good, **change})

    def test_a_calibration_from_before_columns_is_out_of_date(self):
        old = fit(self.rows, self.targets)
        del old["columns"]
        with self.assertRaises(ValueError):
            gaze_model.validate(old)

    def test_validate_refuses_a_calibration_that_reads_where_the_head_is(self):
        # The calibration that failed on 2026-11-08 was a fit on every one of the 90 numbers.
        place = [FEATURE_NAMES.index(name) for name in ("head x", "head y", "head distance")]
        eyes = FEATURE_SETS["eyes"]
        for name, columns in {"everything": list(range(FEATURE_COUNT)), "an eye with head x": eyes + place[:1],
                              "an eye with head distance": eyes + place[2:],
                              "eyes with a head product": eyes + [FEATURE_NAMES.index("right iris along × head y")],
                              "eyes with head roll": eyes + [BASE_NAMES.index("head roll")],
                              "eyes and turn, as before 2026-11-09": eyes + [BASE_NAMES.index("head yaw"),
                                                                              BASE_NAMES.index("head pitch")]}.items():
            with self.subTest(name):
                old = fit(self.rows, self.targets, columns=columns)
                with self.assertRaises(ValueError) as caught:
                    gaze_model.validate(old)
                self.assertEqual(
                    str(caught.exception),
                    "this calibration reads numbers gaze no longer uses, such as where your head sits. "
                    "Calibrate gaze again.")

    def test_validate_accepts_each_of_the_sets(self):
        for name, columns in FEATURE_SETS.items():
            with self.subTest(name):
                gaze_model.validate(fit(self.rows, self.targets, columns=columns))

    def test_the_sets_are_the_named_features(self):
        def names(set_name):
            return [FEATURE_NAMES[c] for c in FEATURE_SETS[set_name]]

        eyes = ["right iris along", "right iris across", "left iris along", "left iris across"]
        lids = ["right eye opening", "left eye opening"]
        self.assertEqual(list(FEATURE_SETS), ["eyes", "eyes and lids", "eyes, curved", "eyes and lids, curved"])
        self.assertEqual(names("eyes"), eyes)
        self.assertEqual(names("eyes and lids"), eyes + lids)
        self.assertEqual(len(names("eyes, curved")), 14)
        self.assertIn("right iris along × left iris across", names("eyes, curved"))
        self.assertIn("left iris across × left iris across", names("eyes, curved"))
        self.assertEqual(len(names("eyes and lids, curved")), 24)
        self.assertIn("left iris across × left eye opening", names("eyes and lids, curved"))
        self.assertNotIn("right eye opening × left eye opening", names("eyes and lids, curved"))
        for columns in FEATURE_SETS.values():
            self.assertEqual(len(set(columns)), len(columns))

    def test_no_set_learns_from_the_head_turn(self):
        # A head turn is allowed for by a fixed sum from the screen's size (see FixedTurnTest), not learned: the learned
        # head-turn sets did worse than it on the real run of 2026-11-09.
        for set_name, columns in FEATURE_SETS.items():
            for column in columns:
                parts = FEATURE_NAMES[column].split(" × ")
                for turn in ("head yaw", "head pitch", "head roll"):
                    with self.subTest(set=set_name, feature=FEATURE_NAMES[column]):
                        self.assertNotIn(turn, parts)

    def test_no_set_reads_where_the_head_is(self):
        # A calibration is made sitting still, so it can't learn what a head that moves does. A turn of the head isn't
        # learned either: it is allowed for by a fixed sum (see FixedTurnTest and test_no_set_learns_from_the_head_turn).
        for set_name, columns in FEATURE_SETS.items():
            for column in columns:
                parts = FEATURE_NAMES[column].split(" × ")  # a product names each of its two values
                for place in ("head x", "head y", "head distance"):
                    with self.subTest(set=set_name, feature=FEATURE_NAMES[column]):
                        self.assertNotIn(place, parts)

    def test_a_tie_goes_to_the_fewest_columns_then_the_larger_share(self):
        # Nothing to tell the places apart: every candidate misses equally.
        rows = [gaze_model.expand([0.0] * len(BASE_NAMES)) for _ in range(10 * len(CALIBRATION_DOTS))]
        targets = [dot for dot in CALIBRATION_DOTS for _ in range(10)]
        picked = choose(rows, targets)
        self.assertEqual((picked.name, picked.alpha_share), ("eyes", max(gaze_model.ALPHA_SHARES)))
        self.assertEqual(picked.columns, FEATURE_SETS["eyes"])

    def test_columns_that_never_change_are_no_trouble(self):
        rows = [[u, v] + [0.0] * (FEATURE_COUNT - 2) for u, v in CALIBRATION_DOTS for _ in range(10)]
        targets = [dot for dot in CALIBRATION_DOTS for _ in range(10)]
        picked = choose(rows, targets)
        self.assertEqual(picked.name, "eyes")
        self.assertLess(picked.score, 0.05)

    def test_one_place_gives_nothing_to_leave_out_so_the_smallest_candidate_stands(self):
        rows = [[0.5, 0.5] + [0.0] * (FEATURE_COUNT - 2)] * 30
        picked = choose(rows, [(0.5, 0.5)] * 30)
        self.assertEqual((picked.name, picked.alpha_share), ("eyes", max(gaze_model.ALPHA_SHARES)))
        gaze_model.validate(gaze_model.fit_choice(rows, [(0.5, 0.5)] * 30, picked))

    def test_thinning_keeps_evenly_spaced_rows_of_each_dot(self):
        rows = [[float(i)] for i in range(200)]
        targets = [(0.1, 0.1)] * 100 + [(0.9, 0.9)] * 90 + [(0.5, 0.5)] * 10
        kept, kept_at = gaze_model.thin(rows, targets)
        self.assertEqual(kept_at.count((0.1, 0.1)), gaze_model.KEEP_PER_DOT)
        self.assertEqual(kept_at.count((0.9, 0.9)), gaze_model.KEEP_PER_DOT)
        self.assertEqual(kept_at.count((0.5, 0.5)), 10)  # fewer than the limit: all of them
        first_dot = [row[0] for row, at in zip(kept, kept_at) if at == (0.1, 0.1)]
        self.assertEqual(first_dot[0], 0.0)
        self.assertEqual(sorted(set(b - a for a, b in zip(first_dot, first_dot[1:]))), [2.0, 3.0])  # none bunched up

    def test_a_chosen_fit_is_thinned_and_its_alpha_is_a_share_of_the_rows_kept(self):
        rows, targets, _, _ = made_up_run(frames=60)  # twice the frames a dot gives, as from a camera at 60 a second
        model = gaze_model.fit_choice(rows, targets, self.choice)
        kept = gaze_model.KEEP_PER_DOT * len(CALIBRATION_DOTS)
        self.assertEqual(model["rows"], kept)
        self.assertAlmostEqual(model["alpha"], self.choice.alpha_share * kept)
        self.assertEqual(model["columns"], self.choice.columns)

    def test_a_dot_of_one_second_is_kept_whole(self):
        # A second of looking at 30 frames a second is under KEEP_PER_DOT, so thinning only steps in for a faster camera.
        self.assertGreater(gaze_model.KEEP_PER_DOT, 30)
        self.assertEqual(gaze_model.fit_choice(self.rows, self.targets, self.choice)["rows"], len(self.rows))

    def test_choosing_a_run_with_far_more_frames_than_a_real_one_takes_under_ten_seconds(self):
        # The worker has 30 seconds to choose, fit, mark and fit again, and thinning keeps choosing from growing with
        # the frames: this is 24 dots of 200 frames, nearly seven times what a run gives.
        rows, targets, _, _ = made_up_run(frames=200)
        self.assertEqual(len(rows), 200 * len(CALIBRATION_DOTS))
        began = time.monotonic()
        choose(rows, targets, screen_width_cm=SCREEN_WIDTH_CM)
        self.assertLess(time.monotonic() - began, 10.0)


SEAT = (0.0, -2.5, 44.0)  # head x, y and distance, in cm, while the still calibration is made


def still_face(u, v, rng, seat=SEAT):
    """The 12 base values of a face looking at (u, v). The irises carry the look. The head follows it by a few
    millimetres and a fraction of a degree, as a head does when it sits still (a little nearer for a look down), and
    every number has a little of MediaPipe's noise."""
    def noise(spread):
        return rng.gauss(0, spread)

    along, across = 0.12 * (u - 0.5) + noise(0.006), 0.10 * (v - 0.5) + noise(0.006)
    x, y, distance = seat
    return [along - 0.05 + noise(0.003), across - 0.04 + noise(0.003), along + 0.02 + noise(0.003),
            across - 0.05 + noise(0.003), 0.185 - 0.02 * (v - 0.5) + noise(0.003), 0.181 - 0.02 * (v - 0.5) + noise(0.003),
            0.07 + 0.03 * (u - 0.5) + noise(0.004), -0.10 - 0.03 * (v - 0.5) + noise(0.004), -0.05 + noise(0.003),
            x + 0.3 * (u - 0.5) + noise(0.05), y - 0.2 * (v - 0.5) + noise(0.05), distance - 0.3 * (v - 0.5) + noise(0.15)]


class HeadShiftTest(unittest.TestCase):
    """A calibration is made sitting still, so it never sees the head's place change. In use, the person shifts."""

    SEATS = (("still", SEAT), ("1 cm up", (0.0, -1.5, 44.0)), ("1 cm down", (0.0, -3.5, 44.0)),
             ("1.5 cm aside", (1.5, -2.5, 44.0)), ("3 cm nearer", (0.0, -2.5, 41.0)), ("3 cm further", (0.0, -2.5, 47.0)))
    FIXED_SHARE = 0.01  # the share of alpha every set is fitted at, so that none escapes by not being the one chosen

    @classmethod
    def setUpClass(cls):
        cls.rng = random.Random(7)
        cls.rows, cls.targets = [], []
        for dot in CALIBRATION_DOTS:
            for _ in range(60):
                cls.rows.append(gaze_model.expand(still_face(*dot, cls.rng)))
                cls.targets.append(dot)

    def models(self):
        """The fit `choose` makes, and a fit of every set in FEATURE_SETS, each at the same share of alpha. Each both with
        the head's turn allowed for (which reads the head's distance from every row) and without."""
        for width in (None, 29.0):
            allowed = "" if width is None else ", turn allowed for"
            chosen = choose(self.rows, self.targets, width)
            yield f"chosen ({chosen.name}{allowed})", gaze_model.fit_choice(self.rows, self.targets, chosen, width)
            for name, columns in FEATURE_SETS.items():
                choice = gaze_model.Choice(name, columns, self.FIXED_SHARE, 0.0)
                yield f"{name}{allowed}", gaze_model.fit_choice(self.rows, self.targets, choice, width)

    def test_moving_the_head_a_centimetre_still_reads_as_looking_at_the_middle(self):
        for model_name, model in self.models():
            for seat_name, seat in self.SEATS:
                readings = [predict(model, gaze_model.expand(still_face(0.5, 0.5, self.rng, seat))) for _ in range(30)]
                u, v = (statistics.median(r[k] for r in readings) for k in (0, 1))
                with self.subTest(model=model_name, seat=seat_name):
                    # 1 cm at arm's length moves a look by about 1 cm on a screen some 18 cm tall: well under 0.15
                    self.assertLess(abs(u - 0.5), 0.15, (u, v))
                    self.assertLess(abs(v - 0.5), 0.15, (u, v))
                    self.assertTrue(all(-LOOKING_AWAY_MARGIN <= x <= 1 + LOOKING_AWAY_MARGIN for x in (u, v)), (u, v))


TURN_AWAY_CM = 30.0  # where the guide seats the person now


def turning_face(u, v, yaw, rng, away=TURN_AWAY_CM, width=SCREEN_WIDTH_CM, noise=0.0):
    """The 12 base values of a face looking at (u, v) with its head turned `yaw` radians, in MediaPipe's sense.

    MediaPipe's yaw is positive when the face turns toward the person's own left, which is the screen's left (u = 0). The
    eyes are fixed in the head unless they turn in it, so to keep looking at (u, v) they turn back by the same angle: how
    far they sit from the middle of the eye goes with (the look's angle + yaw). The iris moves to the picture's right
    (positive "along") when the eyes turn to the person's left. The lids follow the look up and down.
    """
    def jitter():
        return rng.gauss(0, noise) if noise else 0.0

    look_across = (u - 0.5) * width / away  # radians toward the screen's right
    look_down = (v - 0.5) * 0.65 * width / away
    along = -IRIS_PER_RADIAN * (look_across + yaw)
    across = IRIS_PER_RADIAN * IRIS_DOWN_SHARE * look_down
    opening = 0.30 - 0.05 * (v - 0.5)
    return [along + jitter(), across + jitter(), along + jitter(), across + jitter(), opening + jitter(), opening + jitter(),
            yaw, 0.05, 0.0, 0.0, -2.0, away]


class FixedTurnTest(unittest.TestCase):
    """A turn of the head moves where the eyes point by (head distance ÷ screen width) × the turn, across the screen.
    The fit allows for that with a fixed sum rather than learning it: learned head-turn sets did worse on the real run."""

    @classmethod
    def setUpClass(cls):
        rng = random.Random(3)
        cls.rows, cls.targets = [], []
        for dot in CALIBRATION_DOTS:
            for _ in range(30):
                cls.rows.append(gaze_model.expand(turning_face(*dot, rng.gauss(0, 0.01), rng, noise=0.002)))
                cls.targets.append(dot)

    def guess_while_turning(self, model, yaw):
        return predict(model, gaze_model.expand(turning_face(0.5, 0.5, yaw, random.Random(0))))

    def test_turning_the_head_while_looking_at_one_point_leaves_the_guess_where_it_was(self):
        for name, columns in FEATURE_SETS.items():
            model = fit(self.rows, self.targets, alpha=0.01 * len(self.rows), columns=columns,
                        screen_width_cm=SCREEN_WIDTH_CM)
            still = self.guess_while_turning(model, 0.0)
            for degrees in (-8, -4, 4, 8):
                with self.subTest(set=name, degrees=degrees):
                    u, v = self.guess_while_turning(model, math.radians(degrees))
                    self.assertLess(math.dist((u, v), still), 0.02, (u, v, still))

    def test_without_the_screens_size_a_turn_moves_the_guess_as_it_did_before(self):
        # So the test above could fail: the same turn of 8 degrees, with nothing allowed for, moves the guess a long way.
        model = fit(self.rows, self.targets, alpha=0.01 * len(self.rows), columns=FEATURE_SETS["eyes"])
        still = self.guess_while_turning(model, 0.0)
        u, _ = self.guess_while_turning(model, math.radians(8))
        self.assertGreater(abs(u - still[0]), 0.1)

    def test_the_sum_is_the_studys_size_and_sign(self):
        # On the run of 2026-11-09, 39.6 cm from a 29.0 cm wide screen: -1.37 of the screen's width for each radian.
        rows = [gaze_model.expand(turning_face(u, 0.5, 0.0, random.Random(1), away=39.6)) for u in (0.2, 0.5, 0.8)] * 5
        model = fit(rows, [(u, 0.5) for u in (0.2, 0.5, 0.8)] * 5, columns=FEATURE_SETS["eyes"], screen_width_cm=29.0)
        straight = gaze_model.expand(turning_face(0.5, 0.5, 0.0, random.Random(1), away=39.6))
        turned = list(straight)
        turned[BASE_NAMES.index("head yaw")] = 0.1  # the head turns and the eyes don't
        self.assertAlmostEqual((predict(model, turned)[0] - predict(model, straight)[0]) / 0.1, -39.6 / 29.0, places=6)

    def test_the_head_distance_is_read_from_each_row(self):
        rows = [gaze_model.expand(turning_face(u, 0.5, 0.0, random.Random(1))) for u in (0.2, 0.5, 0.8)] * 5
        model = fit(rows, [(u, 0.5) for u in (0.2, 0.5, 0.8)] * 5, columns=FEATURE_SETS["eyes"],
                    screen_width_cm=SCREEN_WIDTH_CM)
        straight = predict(model, rows[1])[0]
        near, far = list(rows[1]), list(rows[1])
        for row, away in ((near, 20.0), (far, 40.0)):
            row[BASE_NAMES.index("head yaw")] = 0.1  # the same turn, with the same eyes, from twice as far
            row[BASE_NAMES.index("head distance")] = away
        self.assertAlmostEqual(predict(model, far)[0] - straight, 2 * (predict(model, near)[0] - straight), places=6)
        self.assertAlmostEqual(predict(model, near)[0] - straight, -20.0 / SCREEN_WIDTH_CM * 0.1, places=6)

    def test_the_model_keeps_the_screens_width_and_says_when_it_had_none(self):
        allowed = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"], screen_width_cm=SCREEN_WIDTH_CM)
        plain = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"])
        self.assertEqual(allowed["screen_width_cm"], SCREEN_WIDTH_CM)
        self.assertIsNone(plain["screen_width_cm"])
        for model in (allowed, plain):
            gaze_model.validate(json.loads(json.dumps(model)))

    def test_a_turn_can_only_be_allowed_for_from_a_whole_row(self):
        with self.assertRaises(ValueError):
            fit([[0.0, 1.0], [1.0, 0.0], [1.0, 1.0]], [(0, 0), (1, 0), (1, 1)], screen_width_cm=SCREEN_WIDTH_CM)

    def test_a_calibration_from_before_the_turn_was_allowed_for_is_refused_plainly(self):
        old = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"])
        del old["screen_width_cm"]
        with self.assertRaises(ValueError) as caught:
            gaze_model.validate(old)
        self.assertTrue(str(caught.exception).endswith("Calibrate gaze again."), str(caught.exception))

    def test_a_width_that_makes_no_sense_is_refused(self):
        good = fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"], screen_width_cm=SCREEN_WIDTH_CM)
        for width in (0, -29.0, float("nan"), float("inf"), "29", True, [29.0]):
            with self.subTest(width=width), self.assertRaises(ValueError):
                gaze_model.validate({**good, "screen_width_cm": width})
            with self.subTest(fit_width=width), self.assertRaises(ValueError):
                fit(self.rows, self.targets, columns=FEATURE_SETS["eyes"], screen_width_cm=width)

    def test_choosing_allows_for_the_turn_too(self):
        # Every dot is looked at with the head turned its own way, as a head wanders: only a fit that allows for the turn
        # can tell the eyes' part from the head's, and choosing marks each candidate with that allowance.
        rng = random.Random(5)
        rows, targets, check_rows, check_targets = [], [], [], []
        for dots, into, at in ((CALIBRATION_DOTS, rows, targets), (CHECK_DOTS, check_rows, check_targets)):
            for dot in dots:
                yaw = rng.uniform(-0.08, 0.08)
                for _ in range(30):
                    into.append(gaze_model.expand(turning_face(*dot, yaw + rng.gauss(0, 0.003), rng, noise=0.002)))
                    at.append(dot)
        allowed = choose(rows, targets, screen_width_cm=SCREEN_WIDTH_CM)
        model = gaze_model.fit_choice(rows, targets, allowed, screen_width_cm=SCREEN_WIDTH_CM)
        self.assertEqual(model["screen_width_cm"], SCREEN_WIDTH_CM)
        self.assertLess(allowed.score, 0.02)
        landed = check(model, check_rows, check_targets)["median"]
        self.assertLess(landed, 0.02)
        plain = gaze_model.fit_choice(rows, targets, choose(rows, targets))
        self.assertGreater(check(plain, check_rows, check_targets)["median"], 3 * landed)


class CheckTest(unittest.TestCase):
    def setUp(self):
        # a model that reads u and v straight off the row, exactly
        rows = [[float(i), float(j)] for i in range(5) for j in range(5)]
        self.model = fit(rows, [(r[0], r[1]) for r in rows], alpha=0.0)

    def test_check_reports_median_p90_and_radius(self):
        rows = [[0.5, 0.5]] * 10
        # off by 0.01, 0.02 … 0.10 to the right: the 90th percentile sits between the 9th and 10th
        targets = [(0.5 - 0.01 * k, 0.5) for k in range(1, 11)]
        got = check(self.model, rows, targets)
        self.assertEqual(set(got), {"median", "p90", "radius"})
        self.assertAlmostEqual(got["median"], 0.055, places=6)
        self.assertAlmostEqual(got["p90"], 0.091, places=6)
        self.assertAlmostEqual(got["radius"][0], 0.091, places=6)
        self.assertEqual(got["radius"][1], gaze_model.MIN_RADIUS)  # no error up or down; never a zero-size patch

    def test_the_radius_has_a_side_for_each_axis(self):
        rows = [[0.5, 0.5]] * 4
        targets = [(0.5, 0.5 + d) for d in (0.0, 0.1, 0.2, 0.3)]
        got = check(self.model, rows, targets)
        self.assertAlmostEqual(got["radius"][1], 0.27, places=6)
        self.assertEqual(got["radius"][0], gaze_model.MIN_RADIUS)

    def test_a_perfect_fit_checks_out_at_the_floor(self):
        rows = [[1.0, 2.0], [3.0, 1.0]]
        got = check(self.model, rows, [(1.0, 2.0), (3.0, 1.0)])
        self.assertAlmostEqual(got["median"], 0.0, places=6)
        self.assertEqual(got["radius"], [gaze_model.MIN_RADIUS, gaze_model.MIN_RADIUS])

    def test_checking_nothing_is_refused(self):
        with self.assertRaises(ValueError):
            check(self.model, [], [])


if __name__ == "__main__":
    unittest.main()
