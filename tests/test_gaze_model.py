import json
import math
import random
import unittest

from gazevoice import gaze_model
from gazevoice.gaze_model import FEATURE_NAMES, check, features, fit, predict

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
        model = fit(rows, [(0.1 * i, 0.2) for i in range(5)])
        gaze_model.validate(model)
        stale = fit([[1.0, 2.0], [2.0, 1.0], [3.0, 5.0]], [(0, 0), (1, 1), (0, 1)])
        for bad in (stale, {}, {"kind": "something else"}, None, "model"):
            with self.assertRaises(ValueError):
                gaze_model.validate(bad)


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
