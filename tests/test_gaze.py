import json
import threading
import unittest
from unittest import mock

from gazevoice.voice import gaze
from gazevoice.voice.gaze import (
    CONFIDENT_GAP,
    CONFIDENT_SCORE,
    DEFAULT_RADIUS,
    LONE_GUESS_FLOOR,
    MARKER_MIN_SAMPLES,
    MARKER_WINDOW,
    MAX_CANDIDATES,
    MIN_CONFIDENCE,
    MIN_SAMPLES,
    POINTING_WORDS,
    RAYS,
    Area,
    Fixation,
    GazeSample,
    GazeTrack,
    Window,
    area_from,
    fixate,
    ground,
    marker_point,
    mostly_off_screen,
    on_viewport,
    reach_on_viewport,
    pointing_words,
    ray_points,
    score_hits,
    track_on_viewport,
    viewport_area,
)
from gazevoice.voice.service import MAX_HOLD

CHAIR = {"uid": 11, "name": "Chair", "faces": 900, "point": [1.0, 1.0, 1.0]}
TABLE = {"uid": 22, "name": "Table", "faces": 400, "point": [2.0, 2.0, 2.0]}
LAMP = {"uid": 33, "name": "Lamp", "faces": 120, "point": [3.0, 3.0, 3.0]}
RUG = {"uid": 44, "name": "Rug", "faces": 8, "point": [4.0, 4.0, 4.0]}


def votes(*groups, rays=RAYS):
    """Hits for each ray: (object, how many rays) in order, then misses to fill the rest."""
    hits = [hit for hit, count in groups for _ in range(count)]
    return hits + [None] * (rays - len(hits))


def steady(t, u, v, count=8, step=0.05, confidence=0.9):
    return [GazeSample(t + i * step, u, v, confidence) for i in range(count)]


def track_of(*samples, seconds=30.0):
    track = GazeTrack(seconds)
    for sample in samples:
        track.add(sample)
    return track


def said(*spec):
    """Word dicts the way speech-to-text gives them: (text, start, end) in seconds from the clip's start."""
    return [{"text": text, "start": start, "end": end} for text, start, end in spec]


class PointingWordsTest(unittest.TestCase):
    def test_pointing_words_become_clock_times(self):
        found = pointing_words([{"text": "this", "start": 0.4, "end": 0.6}], clip_start=100.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].text, "this")
        self.assertAlmostEqual(found[0].start, 100.4)
        self.assertAlmostEqual(found[0].end, 100.6)

    def test_punctuation_and_case_dont_hide_a_pointing_word(self):
        for spoken in ("This,", "THAT", "there.", "(it)", "Here!"):
            found = pointing_words(said((spoken, 0.0, 0.2)), 50.0)
            self.assertEqual([w.text for w in found], [spoken.strip("(),.!").lower()], spoken)

    def test_every_pointing_word_is_known(self):
        words = said(*[(w, i * 0.5, i * 0.5 + 0.3) for i, w in enumerate(POINTING_WORDS)])
        self.assertEqual([w.text for w in pointing_words(words, 0.0)], list(POINTING_WORDS))

    def test_other_words_are_left_out_in_order(self):
        words = said(("move", 0.0, 0.3), ("this", 0.4, 0.6), ("over", 0.7, 0.9), ("there", 1.0, 1.3),
                     ("it's", 1.4, 1.6), ("thistle", 1.7, 2.0))
        self.assertEqual([w.text for w in pointing_words(words, 0.0)], ["this", "there"])

    def test_no_words_no_pointing(self):
        self.assertEqual(pointing_words([], 10.0), [])

    def test_a_word_without_times_is_skipped(self):
        words = [{"text": "this"}, {"text": "that", "start": 0.1, "end": 0.3}]
        self.assertEqual([w.text for w in pointing_words(words, 0.0)], ["that"])


class GazeTrackTest(unittest.TestCase):
    def test_the_track_forgets_old_samples(self):
        track = GazeTrack(seconds=10.0)
        track.add(GazeSample(100.0, 0.5, 0.5, 0.9))
        track.add(GazeSample(105.0, 0.5, 0.5, 0.9))
        track.add(GazeSample(111.0, 0.5, 0.5, 0.9))
        self.assertEqual([s.t for s in track.between(0, 1000)], [105.0, 111.0])

    def test_the_track_outlasts_the_longest_hold(self):
        self.assertGreater(GazeTrack().seconds, MAX_HOLD)  # with room for the speech-to-text after it
        track = GazeTrack()
        track.add(GazeSample(100.0, 0.5, 0.5, 0.9))
        track.add(GazeSample(100.0 + MAX_HOLD + 5, 0.5, 0.5, 0.9))
        self.assertEqual(len(track.between(100.0, 100.0)), 1)  # the start of a held key is still here when it's let go

    def test_a_copy_of_part_of_the_track_stands_on_its_own(self):
        track = GazeTrack(seconds=10.0)
        for t in (100.0, 101.0, 102.0, 103.0):
            track.add(GazeSample(t, 0.5, 0.5, 0.9))
        copy = track.copy_between(101.0, 102.0)
        self.assertEqual([s.t for s in copy.between(0, 1000)], [101.0, 102.0])
        track.add(GazeSample(500.0, 0.5, 0.5, 0.9))  # the track moves on, and forgets those
        self.assertEqual(track.between(101.0, 102.0), [])
        self.assertEqual([s.t for s in copy.between(0, 1000)], [101.0, 102.0])

    def test_between_is_inclusive_and_ordered(self):
        track = track_of(*[GazeSample(t, 0.5, 0.5, 0.9) for t in (1.0, 2.0, 3.0, 4.0)])
        self.assertEqual([s.t for s in track.between(2.0, 3.0)], [2.0, 3.0])
        self.assertEqual(track.between(5.0, 6.0), [])
        self.assertEqual(track.between(3.0, 2.0), [])

    def test_latest_time(self):
        track = GazeTrack()
        self.assertIsNone(track.latest_time)
        track.add(GazeSample(7.5, 0.1, 0.1, 0.9))
        track.add(GazeSample(8.5, 0.1, 0.1, 0.9))
        self.assertEqual(track.latest_time, 8.5)

    def test_it_can_be_fed_from_another_thread(self):
        track = GazeTrack(seconds=1000.0)

        def feed():
            for i in range(500):
                track.add(GazeSample(float(i), 0.5, 0.5, 0.9))

        feeder = threading.Thread(target=feed)
        feeder.start()
        while feeder.is_alive():
            track.between(0.0, 1000.0)
        feeder.join()
        self.assertEqual(len(track.between(0.0, 1000.0)), 500)

    def test_a_sample_is_read_only(self):
        sample = GazeSample(1.0, 0.2, 0.3, 0.9)
        with self.assertRaises(AttributeError):
            sample.u = 0.9


class FixateTest(unittest.TestCase):
    def test_the_middle_of_steady_gaze(self):
        fixation = fixate(steady(10.0, 0.4, 0.6))
        self.assertEqual((fixation.u, fixation.v, fixation.samples), (0.4, 0.6, 8))

    def test_fixate_ignores_blinks(self):
        good = steady(10.0, 0.4, 0.6, count=6)
        blinks = steady(11.0, 0.9, 0.1, count=10, confidence=MIN_CONFIDENCE - 0.1)
        fixation = fixate(good + blinks)
        self.assertEqual((fixation.u, fixation.v, fixation.samples), (0.4, 0.6, 6))

    def test_fixate_needs_enough_samples(self):
        self.assertIsNone(fixate(steady(10.0, 0.4, 0.6, count=MIN_SAMPLES - 1)))
        self.assertEqual(fixate(steady(10.0, 0.4, 0.6, count=MIN_SAMPLES)).samples, MIN_SAMPLES)

    def test_blinks_dont_count_towards_enough(self):
        mixed = steady(10.0, 0.4, 0.6, count=MIN_SAMPLES - 1) + steady(11.0, 0.4, 0.6, count=5, confidence=0.1)
        self.assertIsNone(fixate(mixed))

    def test_nothing_to_fixate_on(self):
        self.assertIsNone(fixate([]))

    def test_a_median_shrugs_off_one_wild_sample(self):
        samples = steady(10.0, 0.40, 0.60, count=6) + [GazeSample(11.0, 0.99, 0.01, 0.9)]
        fixation = fixate(samples)
        self.assertAlmostEqual(fixation.u, 0.40)
        self.assertAlmostEqual(fixation.v, 0.60)
        self.assertEqual(fixation.samples, 7)

    def test_the_confidence_floor_counts(self):
        self.assertIsNotNone(fixate(steady(10.0, 0.4, 0.6, confidence=MIN_CONFIDENCE)))


class RayPointsTest(unittest.TestCase):
    def test_ray_points_stay_on_the_viewport(self):
        for centre in ((0.01, 0.99), (0.0, 0.0), (1.0, 1.0), (0.5, 0.5)):
            points = ray_points(Fixation(*centre, 8), DEFAULT_RADIUS)
            self.assertEqual(len(points), RAYS)
            for u, v in points:
                self.assertTrue(0.0 <= u <= 1.0 and 0.0 <= v <= 1.0, (centre, u, v))

    def test_the_centre_comes_first(self):
        points = ray_points(Fixation(0.4, 0.6, 8), DEFAULT_RADIUS)
        self.assertEqual(points[0], [0.4, 0.6])

    def test_the_centre_is_clamped_too(self):
        self.assertEqual(ray_points(Fixation(1.2, -0.1, 8), DEFAULT_RADIUS)[0], [1.0, 0.0])

    def test_repeatable(self):
        fixation = Fixation(0.4, 0.6, 8)
        self.assertEqual(ray_points(fixation, DEFAULT_RADIUS), ray_points(fixation, DEFAULT_RADIUS))

    def test_the_spread_fills_the_patch_and_no_more(self):
        ru, rv = DEFAULT_RADIUS
        points = ray_points(Fixation(0.5, 0.5, 8), DEFAULT_RADIUS)
        for u, v in points:
            self.assertLessEqual(((u - 0.5) / ru) ** 2 + ((v - 0.5) / rv) ** 2, 1.0 + 1e-9)
        # every direction gets some rays, near and far
        self.assertTrue(any(u < 0.5 - ru / 2 for u, _ in points))
        self.assertTrue(any(u > 0.5 + ru / 2 for u, _ in points))
        self.assertTrue(any(v < 0.5 - rv / 2 for _, v in points))
        self.assertTrue(any(v > 0.5 + rv / 2 for _, v in points))
        self.assertEqual(len({tuple(p) for p in points}), RAYS)

    def test_a_smaller_patch_stays_closer(self):
        small = ray_points(Fixation(0.5, 0.5, 8), (0.02, 0.03))
        for u, v in small:
            self.assertLessEqual(abs(u - 0.5), 0.02 + 1e-9)
            self.assertLessEqual(abs(v - 0.5), 0.03 + 1e-9)

    def test_a_number_of_rays_can_be_asked_for(self):
        self.assertEqual(len(ray_points(Fixation(0.5, 0.5, 8), DEFAULT_RADIUS, rays=7)), 7)
        self.assertEqual(ray_points(Fixation(0.5, 0.5, 8), DEFAULT_RADIUS, rays=1), [[0.5, 0.5]])
        self.assertEqual(ray_points(Fixation(0.5, 0.5, 8), DEFAULT_RADIUS, rays=0), [])


class ScoreHitsTest(unittest.TestCase):
    def points(self, count=RAYS):
        return ray_points(Fixation(0.5, 0.5, 8), DEFAULT_RADIUS, rays=count)

    def test_score_counts_votes_and_misses(self):
        hits = votes((CHAIR, 12), (TABLE, 6))
        found = score_hits(self.points(), hits)
        self.assertEqual([c.name for c in found], ["Chair", "Table"])
        self.assertEqual([c.score for c in found], [0.5, 0.25])
        self.assertEqual([c.uid for c in found], [11, 22])
        self.assertEqual([c.faces for c in found], [900, 400])

    def test_best_first_whatever_the_order_of_the_rays(self):
        hits = votes((TABLE, 5), (CHAIR, 12), (TABLE, 1))
        found = score_hits(self.points(), hits)
        self.assertEqual([(c.name, c.score) for c in found], [("Chair", 0.5), ("Table", 0.25)])

    def test_the_same_name_on_two_objects_stays_two_guesses(self):
        twin = dict(CHAIR, uid=99)
        found = score_hits(self.points(), votes((CHAIR, 8), (twin, 4)))
        self.assertEqual([(c.uid, c.name) for c in found], [(11, "Chair"), (99, "Chair")])

    def test_only_the_best_few_are_kept(self):
        hits = votes((CHAIR, 9), (TABLE, 7), (LAMP, 5), (RUG, 3))
        found = score_hits(self.points(), hits)
        self.assertEqual(len(found), MAX_CANDIDATES)
        self.assertEqual([c.name for c in found], ["Chair", "Table", "Lamp"])

    def test_ties_go_to_the_one_nearest_the_centre(self):
        found = score_hits(self.points(), votes((LAMP, 4), (RUG, 4)))
        self.assertEqual([c.name for c in found], ["Lamp", "Rug"])

    def test_all_misses_give_no_guesses(self):
        self.assertEqual(score_hits(self.points(), votes()), [])

    def test_nothing_to_score(self):
        self.assertEqual(score_hits([], []), [])

    def test_the_point_comes_from_the_ray_nearest_the_centre(self):
        first = dict(CHAIR, point=[0.1, 0.2, 0.3])
        later = dict(CHAIR, point=[9.0, 9.0, 9.0])
        found = score_hits(self.points(), votes((TABLE, 2), (first, 1), (later, 10)))
        chair = next(c for c in found if c.name == "Chair")
        self.assertEqual(chair.point, [0.1, 0.2, 0.3])
        self.assertEqual(found[0].name, "Chair")

    def test_the_first_ray_that_hit_gives_the_point_even_after_misses(self):
        hits = [None, None, dict(LAMP, point=[7.0, 8.0, 9.0]), dict(LAMP, point=[1.0, 1.0, 1.0])]
        found = score_hits(self.points(4), hits)
        self.assertEqual(found[0].point, [7.0, 8.0, 9.0])

    def test_the_label_goes_at_the_middle_of_the_rays_that_hit(self):
        points = [[0.10, 0.20], [0.30, 0.40], [0.90, 0.90]]
        found = score_hits(points, [CHAIR, CHAIR, None])
        self.assertAlmostEqual(found[0].u, 0.20)
        self.assertAlmostEqual(found[0].v, 0.30)
        self.assertAlmostEqual(found[0].score, 2 / 3)

    def test_the_point_is_a_copy(self):
        hit = dict(CHAIR)
        found = score_hits(self.points(1), [hit])
        found[0].point.append(5.0)
        self.assertEqual(hit["point"], [1.0, 1.0, 1.0])

    def test_a_hit_without_an_object_counts_as_a_miss(self):
        found = score_hits(self.points(2), [{"name": "Ghost"}, CHAIR])
        self.assertEqual([(c.name, c.score) for c in found], [("Chair", 0.5)])


class GroundTest(unittest.TestCase):
    """ground() with a fake host: `pick` casts the rays and `piece` looks closer at the top guess."""

    CLIP = 100.0

    def setUp(self):
        self.picks = []
        self.pieces = []

    def pick_with(self, *groups):
        def pick(points):
            self.picks.append(points)
            return votes(*groups, rays=len(points))
        return pick

    def piece(self, candidate):
        self.pieces.append(candidate)
        return {"ok": True, "face": 3120, "part": "Wing.L", "piece_faces": 480, "object_faces": candidate.faces}

    def look(self, groups, piece=None, words=None, track=None):
        if words is None:
            words = said(("make", 0.0, 0.3), ("this", 0.4, 0.6), ("blue", 0.7, 1.0))
        if track is None:
            track = track_of(*steady(self.CLIP, 0.4, 0.6))
        return ground(words, self.CLIP, track, DEFAULT_RADIUS, self.pick_with(*groups), piece or self.piece)

    def test_the_top_guess_gets_its_piece(self):
        grounding = self.look([(CHAIR, 20), (TABLE, 3)])
        self.assertEqual(len(self.pieces), 1)
        self.assertEqual(self.pieces[0].name, "Chair")
        self.assertIs(self.pieces[0], grounding.targets[0].candidates[0])
        self.assertEqual(grounding.targets[0].piece["part"], "Wing.L")
        self.assertEqual(grounding.targets[0].piece["object_faces"], 900)

    def test_a_failed_piece_lookup_still_gives_the_object(self):
        def refused(candidate):
            raise OSError("Blender went away")

        def confused(candidate):
            raise ValueError("bad reply")

        for lookup in (lambda c: None, refused, confused):
            grounding = self.look([(CHAIR, 20), (TABLE, 3)], piece=lookup)
            self.assertEqual(len(grounding.targets), 1)
            self.assertEqual(grounding.targets[0].candidates[0].name, "Chair")
            self.assertIsNone(grounding.targets[0].piece)
            self.assertEqual(grounding.status, "resolved")

    def test_a_piece_lookup_that_breaks_for_another_reason_is_not_hidden(self):
        def broken(candidate):
            raise KeyError("bug")

        with self.assertRaises(KeyError):
            self.look([(CHAIR, 20)], piece=broken)

    def test_the_target_remembers_the_word_the_time_and_the_gaze(self):
        grounding = self.look([(CHAIR, 20)])
        target = grounding.targets[0]
        self.assertEqual(target.word, "this")
        self.assertAlmostEqual(target.at, 100.4)
        self.assertEqual((target.fixation.u, target.fixation.v), (0.4, 0.6))
        self.assertIsNone(target.picture)
        self.assertEqual(grounding.radius, DEFAULT_RADIUS)

    def test_each_pointing_word_records_how_many_readings_it_had_and_how_many_were_good(self):
        # "this" is said 0.4 s to 0.6 s into the clip, so its window is the clip's 99.8 s to 100.8 s (see BEFORE_WORD)
        words = said(("make", 0.0, 0.3), ("this", 0.4, 0.6), ("blue", 0.7, 1.0), ("there", 20.0, 20.3))
        track = track_of(
            *steady(100.0, 0.4, 0.6, count=8),  # eight good readings, inside the window
            *steady(100.45, 0.4, 0.6, count=4, confidence=MIN_CONFIDENCE / 2),  # four blinks, inside it too
            *steady(90.0, 0.4, 0.6, count=3),  # three before the window, which don't count
        )
        grounding = self.look([(CHAIR, 20)], words=words, track=track)
        self.assertEqual(grounding.windows, [Window("this", 100.4, readings=12, good=8, off_screen=0),
                                             Window("there", 120.0, readings=0, good=0, off_screen=0)])

    def test_a_word_with_no_good_reading_still_records_its_window(self):
        track = track_of(*steady(100.0, 0.4, 0.6, count=20, confidence=0.0))
        grounding = self.look([(CHAIR, 20)], track=track)
        self.assertEqual(grounding.status, "none")
        self.assertEqual(grounding.windows, [Window("this", 100.4, readings=17, good=0, off_screen=0)])  # 100.0 to 100.8 s

    def test_readings_the_tracker_zeroed_as_off_the_screen_are_counted_apart_from_blinks(self):
        track = track_of(
            *[GazeSample(100.0 + i * 0.05, 3.0, 0.5, 0.0, away=True) for i in range(10)],
            *steady(100.5, 0.4, 0.6, count=2, confidence=0.0),
        )
        grounding = self.look([(CHAIR, 20)], track=track)
        self.assertEqual(grounding.windows, [Window("this", 100.4, readings=12, good=0, off_screen=10)])

    def test_the_window_counts_go_into_the_dict(self):
        track = track_of(*steady(100.0, 0.4, 0.6, count=8))
        data = self.look([(CHAIR, 20)], track=track).to_dict()
        self.assertEqual(data["windows"], [{"word": "this", "at": 100.4, "readings": 8, "good": 8, "off_screen": 0}])
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_the_host_is_asked_about_the_rays_for_the_fixation(self):
        self.look([(CHAIR, 20)])
        self.assertEqual(self.picks, [ray_points(Fixation(0.4, 0.6, 8), DEFAULT_RADIUS)])

    def test_one_clear_winner_is_resolved(self):
        grounding = self.look([(CHAIR, 20), (TABLE, 3)])  # 0.83 and 0.13
        self.assertEqual(grounding.status, "resolved")
        scores = [c.score for c in grounding.targets[0].candidates]
        self.assertGreaterEqual(scores[0], CONFIDENT_SCORE)
        self.assertGreaterEqual(scores[0] - scores[1], CONFIDENT_GAP)

    def test_two_close_guesses_need_clarifying(self):
        grounding = self.look([(CHAIR, 11), (TABLE, 10)])  # 0.46 and 0.42
        self.assertEqual(grounding.status, "clarify")
        self.assertEqual(len(grounding.targets), 1)

    def test_the_score_needed_is_the_limit_not_just_a_lead(self):
        self.assertEqual(self.look([(CHAIR, 13), (TABLE, 4)]).status, "clarify")  # 0.54 leads by 0.38
        self.assertEqual(self.look([(CHAIR, 15), (TABLE, 9)]).status, "resolved")  # 0.625 leads by 0.25

    def test_a_lead_that_is_too_small_needs_clarifying(self):
        with mock.patch.object(gaze, "CONFIDENT_SCORE", 0.4):
            self.assertEqual(self.look([(CHAIR, 11), (TABLE, 10)]).status, "clarify")  # 0.46 leads by 0.04
            self.assertEqual(self.look([(CHAIR, 14), (TABLE, 8)]).status, "resolved")  # 0.58 leads by 0.25

    def test_a_lead_of_exactly_the_gap_counts_as_clear(self):
        # 13/24 - 7/24 comes out as 0.24999999999999994 in floating point, and is a lead of 0.25
        with mock.patch.object(gaze, "CONFIDENT_SCORE", 0.5):
            self.assertEqual(self.look([(CHAIR, 13), (TABLE, 7)]).status, "resolved")

    def test_a_lone_guess_at_or_above_the_floor_is_resolved(self):
        for votes_for_it in (15, 14, 10, 5):  # 0.625, then 0.58 (a wing on empty background), 0.42 and 0.21
            with self.subTest(votes=votes_for_it):
                self.assertEqual(self.look([(CHAIR, votes_for_it)]).status, "resolved")

    def test_a_lone_guess_exactly_on_the_floor_is_resolved(self):
        with mock.patch.object(gaze, "LONE_GUESS_FLOOR", 6 / 24):
            self.assertEqual(self.look([(CHAIR, 6)]).status, "resolved")

    def test_a_lone_guess_below_the_floor_needs_clarifying(self):
        self.assertLess(4 / 24, LONE_GUESS_FLOOR)
        self.assertEqual(self.look([(CHAIR, 4)]).status, "clarify")  # 0.17 of the rays

    def test_a_wing_against_empty_background_needs_no_question(self):
        def wing(points):  # 30% by 16% of the viewport, centred where the person looked
            return [CHAIR if abs(u - 0.5) <= 0.15 and abs(v - 0.5) <= 0.08 else None for u, v in points]

        grounding = ground(said(("this", 0.4, 0.6)), self.CLIP, track_of(*steady(100.0, 0.5, 0.5)), DEFAULT_RADIUS,
                           wing, self.piece)
        self.assertAlmostEqual(grounding.targets[0].candidates[0].score, 0.58, places=2)
        self.assertEqual(grounding.status, "resolved")

    def test_the_floor_is_only_for_a_lone_guess(self):
        grounding = self.look([(CHAIR, 13), (TABLE, 1)])  # 0.54 leads by 0.5, but something else was hit
        self.assertEqual(grounding.status, "clarify")

    def test_each_pointing_word_gets_its_own_target(self):
        words = said(("move", 0.0, 0.3), ("this", 0.4, 0.6), ("over", 0.7, 0.9), ("there", 1.0, 1.3))
        track = track_of(*steady(100.0, 0.2, 0.3, count=6), *steady(101.0, 0.8, 0.7, count=6))
        looked_at = {0.2: CHAIR, 0.8: TABLE}

        def pick(points):
            return [looked_at[round(points[0][0], 1)]] * len(points)

        grounding = ground(words, self.CLIP, track, DEFAULT_RADIUS, pick, self.piece)
        self.assertEqual([t.word for t in grounding.targets], ["this", "there"])
        self.assertEqual([t.candidates[0].name for t in grounding.targets], ["Chair", "Table"])
        self.assertAlmostEqual(grounding.targets[0].at, 100.4)
        self.assertAlmostEqual(grounding.targets[1].at, 101.0)
        self.assertEqual(grounding.status, "resolved")
        self.assertEqual([c.name for c in self.pieces], ["Chair", "Table"])

    def this_and_there(self, pick):
        words = said(("move", 0.0, 0.3), ("this", 0.4, 0.6), ("over", 0.7, 0.9), ("there", 1.0, 1.3))
        track = track_of(*steady(100.0, 0.2, 0.3, count=6), *steady(101.0, 0.8, 0.7, count=6))
        return ground(words, self.CLIP, track, DEFAULT_RADIUS, pick, self.piece)

    def test_a_word_whose_rays_all_hit_nothing_is_noted(self):
        grounding = self.this_and_there(
            lambda points: votes((CHAIR, 22), rays=len(points)) if points[0][0] < 0.5 else [None] * len(points))
        self.assertEqual([t.word for t in grounding.targets], ["this"])
        self.assertEqual(grounding.status, "resolved")  # empty space needs no question
        self.assertEqual([m.word for m in grounding.misses], ["there"])
        self.assertAlmostEqual(grounding.misses[0].at, 101.0)

    def test_every_word_missing_is_no_gaze_to_go_on_and_still_noted(self):
        grounding = self.this_and_there(lambda points: [None] * len(points))
        self.assertEqual((grounding.status, grounding.targets), ("none", []))
        self.assertEqual([m.word for m in grounding.misses], ["this", "there"])

    def test_a_word_with_no_gaze_to_go_on_is_not_a_miss(self):
        grounding = self.look([(CHAIR, 20)], track=GazeTrack())
        self.assertEqual((grounding.status, grounding.misses), ("none", []))

    def test_blender_being_unable_to_say_is_not_a_miss(self):
        grounding = self.this_and_there(lambda points: None)
        self.assertEqual((grounding.status, grounding.misses), ("none", []))

    def test_a_hit_is_not_a_miss(self):
        self.assertEqual(self.look([(CHAIR, 20)]).misses, [])

    def test_one_unclear_target_makes_the_whole_thing_unclear(self):
        words = said(("this", 0.4, 0.6), ("there", 1.0, 1.3))
        track = track_of(*steady(100.0, 0.2, 0.3, count=6), *steady(101.0, 0.8, 0.7, count=6))

        def pick(points):
            if points[0][0] < 0.5:
                return votes((CHAIR, 22), rays=len(points))
            return votes((TABLE, 11), (LAMP, 10), rays=len(points))

        grounding = ground(words, self.CLIP, track, DEFAULT_RADIUS, pick, self.piece)
        self.assertEqual(len(grounding.targets), 2)
        self.assertEqual(grounding.status, "clarify")

    def test_a_word_with_no_gaze_is_left_out_but_the_others_stay(self):
        words = said(("this", 0.4, 0.6), ("there", 5.0, 5.3))
        track = track_of(*steady(100.0, 0.2, 0.3, count=6))
        grounding = ground(words, self.CLIP, track, DEFAULT_RADIUS, self.pick_with((CHAIR, 22)), self.piece)
        self.assertEqual([t.word for t in grounding.targets], ["this"])

    def test_gaze_outside_the_word_window_is_ignored(self):
        for start in (99.0, 101.0):  # "this" at 100.4-100.6 looks from 99.8 to 100.8
            track = track_of(*steady(start, 0.4, 0.6, count=6))
            self.assertEqual(self.look([(CHAIR, 20)], track=track).status, "none", start)

    def test_gaze_just_inside_the_window_counts(self):
        track = track_of(*steady(99.9, 0.4, 0.6, count=2, step=0.0), *steady(100.75, 0.4, 0.6, count=3, step=0.0))
        self.assertEqual(self.look([(CHAIR, 20)], track=track).status, "resolved")

    def test_no_pointing_word_means_no_gaze(self):
        words = said(("make", 0.0, 0.3), ("the", 0.4, 0.5), ("cube", 0.6, 0.9), ("blue", 1.0, 1.3))
        grounding = self.look([(CHAIR, 20)], words=words)
        self.assertEqual(grounding.status, "none")
        self.assertEqual(grounding.targets, [])
        self.assertEqual(self.picks, [])
        self.assertEqual(self.pieces, [])

    def test_no_words_means_no_gaze(self):
        self.assertEqual(self.look([(CHAIR, 20)], words=[]).status, "none")

    def test_no_samples_means_no_gaze(self):
        grounding = self.look([(CHAIR, 20)], track=GazeTrack())
        self.assertEqual(grounding.status, "none")
        self.assertEqual(self.picks, [])

    def test_too_few_samples_means_no_gaze(self):
        track = track_of(*steady(100.0, 0.4, 0.6, count=MIN_SAMPLES - 1))
        self.assertEqual(self.look([(CHAIR, 20)], track=track).status, "none")

    def test_nothing_hit_means_no_gaze(self):
        grounding = self.look([])
        self.assertEqual(grounding.status, "none")
        self.assertEqual(grounding.targets, [])
        self.assertEqual(self.pieces, [])
        self.assertEqual(grounding.radius, DEFAULT_RADIUS)

    def test_empty_here_can_hold_a_work_plane_point(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        calls = []
        def place(u, v):
            calls.append((u, v))
            return {"point": [2.0, 0.0, 3.0], "plane": "y=0"}
        result = ground(said(("here", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS,
                        lambda points: [None] * len(points), self.piece, place)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.to_dict()["misses"],
                         [{"word": "here", "at": 100.4, "point": [2.0, 0.0, 3.0], "plane": "y=0"}])
        self.assertEqual(calls, [(0.4, 0.6)])
        self.assertEqual(result.targets, [])
        self.assertEqual(ground(said(("this", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS,
                                lambda points: [None] * len(points), self.piece, place).status, "none")
        self.assertEqual(len(calls), 1)

    def test_unavailable_work_plane_keeps_original_miss(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        result = ground(said(("here", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS,
                        lambda points: [None] * len(points), self.piece, lambda u, v: None)
        self.assertEqual(result.status, "none")
        self.assertEqual(result.to_dict()["misses"], [{"word": "here", "at": 100.4}])

    def test_empty_center_uses_plane_even_when_patch_touches_neighbor(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        result = ground(said(("here", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS,
                        lambda points: [None] + [CHAIR] * (len(points) - 1), self.piece,
                        lambda u, v: {"point": [2.0, 0.0, 3.0], "plane": "y=0"})
        self.assertEqual(result.targets, [])
        self.assertEqual(result.misses[0].point, [2.0, 0.0, 3.0])

    def test_blender_keeps_clear_neighbor_vote_on_empty_center(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        result = ground(said(("there", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS,
                        lambda points: [None] + [CHAIR] * (len(points) - 1), self.piece,
                        lambda u, v: {"point": [2.0, 0.0, 3.0], "plane": "y=0"},
                        prefer_empty_center=False)
        self.assertEqual(result.misses, [])
        self.assertEqual(result.targets[0].candidates[0].name, "Chair")

    def test_a_failed_pick_means_no_gaze(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        words = said(("this", 0.4, 0.6))
        grounding = ground(words, self.CLIP, track, DEFAULT_RADIUS, lambda points: None, self.piece)
        self.assertEqual(grounding.status, "none")
        self.assertEqual(grounding.targets, [])
        self.assertEqual(self.pieces, [])

    def test_an_empty_pick_reply_means_no_gaze(self):
        track = track_of(*steady(100.0, 0.4, 0.6))
        grounding = ground(said(("this", 0.4, 0.6)), self.CLIP, track, DEFAULT_RADIUS, lambda p: [], self.piece)
        self.assertEqual(grounding.status, "none")

    def test_the_radius_the_host_gave_is_kept_and_used(self):
        track = track_of(*steady(100.0, 0.5, 0.5))
        words = said(("this", 0.4, 0.6))
        grounding = ground(words, self.CLIP, track, (0.02, 0.03), self.pick_with((CHAIR, 22)), self.piece)
        self.assertEqual(grounding.radius, (0.02, 0.03))
        self.assertTrue(all(abs(u - 0.5) <= 0.02 + 1e-9 for u, _ in self.picks[0]))


class ToDictTest(unittest.TestCase):
    def grounding(self):
        words = said(("this", 0.4, 0.6))
        track = track_of(*steady(100.0, 0.4, 0.6))
        return ground(
            words, 100.0, track, DEFAULT_RADIUS,
            lambda points: votes((CHAIR, 15), (TABLE, 6), rays=len(points)),
            lambda c: {"ok": True, "face": 3120, "part": "Wing.L", "piece_faces": None, "object_faces": 900},
        )

    def test_it_is_plain_json_data(self):
        data = self.grounding().to_dict()
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_it_says_everything_that_was_worked_out(self):
        data = self.grounding().to_dict()
        self.assertEqual(data["status"], "resolved")
        self.assertEqual(data["radius"], list(DEFAULT_RADIUS))
        target = data["targets"][0]
        self.assertEqual(target["word"], "this")
        self.assertAlmostEqual(target["at"], 100.4)
        self.assertEqual(target["fixation"], {"u": 0.4, "v": 0.6, "samples": 8})
        self.assertEqual([c["name"] for c in target["candidates"]], ["Chair", "Table"])
        self.assertEqual(target["candidates"][0]["score"], 15 / 24)
        self.assertEqual(target["candidates"][0]["point"], [1.0, 1.0, 1.0])
        self.assertEqual(target["candidates"][0]["uid"], 11)
        self.assertEqual(set(target["candidates"][0]), {"uid", "name", "score", "faces", "u", "v", "point"})
        self.assertEqual(target["piece"]["part"], "Wing.L")
        self.assertIsNone(target["picture"])

    def test_no_gaze_still_says_so(self):
        data = ground([], 0.0, GazeTrack(), DEFAULT_RADIUS, lambda p: None, lambda c: None).to_dict()
        self.assertEqual(data, {"status": "none", "targets": [], "radius": list(DEFAULT_RADIUS), "misses": [],
                                "windows": []})
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_changing_the_dict_leaves_the_grounding_alone(self):
        grounding = self.grounding()
        data = grounding.to_dict()
        data["targets"][0]["candidates"][0]["point"].append(5.0)
        data["targets"][0]["piece"]["part"] = "changed"
        self.assertEqual(grounding.targets[0].candidates[0].point, [1.0, 1.0, 1.0])
        self.assertEqual(grounding.targets[0].piece["part"], "Wing.L")

    def test_a_picture_comes_through(self):
        grounding = self.grounding()
        grounding.targets[0].picture = "a wing tip"
        self.assertEqual(grounding.to_dict()["targets"][0]["picture"], "a wing tip")


# Measured on a real Blender 5.2 window on this project's Mac: macOS said the window filled this frame, title bar
# included, and Blender reported the rest, its regions in double-density pixels from the window's bottom left.
FRAME = (120, 390, 900, 632)
REPLY = {"ok": True, "width": 1471, "height": 1043, "x": 4, "y": 104, "scale": 2, "pid": 7,
         "window_width": 900, "window_height": 600}
PLACED = Area(122.0, 448.5, 735.5, 521.5)


class AreaTest(unittest.TestCase):
    def test_four_numbers_make_an_area(self):
        self.assertEqual(area_from({"left": 0, "top": 38, "width": 1710, "height": 1074}), Area(0, 38, 1710, 1074))
        self.assertEqual(area_from({"left": -1710.5, "top": 0, "width": 1, "height": 1}), Area(-1710.5, 0, 1, 1))

    def test_anything_else_is_no_area_at_all(self):
        whole = {"left": 0, "top": 0, "width": 100, "height": 100}
        for report in (None, {}, [], "area", {**whole, "width": 0}, {**whole, "height": -1}, {"left": 0},
                       {**whole, "left": "0"}, {**whole, "top": True}, {**whole, "left": float("nan")},
                       {**whole, "width": float("inf")}):
            self.assertIsNone(area_from(report), report)

    def test_it_says_whether_it_holds_most_of_another_by_its_middle(self):
        screen = Area(0, 0, 1000, 1000)
        self.assertTrue(screen.holds(Area(100, 100, 200, 200)))
        self.assertTrue(screen.holds(Area(900, 0, 150, 100)))  # hangs off the edge, but its middle is on this screen
        self.assertFalse(screen.holds(Area(1000, 0, 800, 600)))  # on the screen beside it


class ViewportAreaTest(unittest.TestCase):
    def frame(self, pid, width, height):
        self.asked = (pid, width, height)
        return FRAME

    def test_the_viewport_is_placed_from_its_window_and_where_it_sits_inside(self):
        self.assertEqual(viewport_area(REPLY, self.frame), PLACED)
        self.assertEqual(self.asked, (7, 900, 600))

    def test_a_window_macos_cant_find_has_no_place(self):
        self.assertIsNone(viewport_area(REPLY, lambda pid, width, height: None))

    def test_a_reply_without_the_window_has_no_place(self):
        for reply in (None, {}, {"ok": False, "error": "no 3D viewport"}, {"ok": True, "width": 1840, "height": 1100},
                      {**REPLY, "scale": 0}, {**REPLY, "pid": "7"}, {**REPLY, "width": 0}, {**REPLY, "x": None}):
            self.assertIsNone(viewport_area(reply, self.frame), reply)


class OntoTheViewportTest(unittest.TestCase):
    SCREEN = Area(0, 0, 1000, 800)
    VIEW = Area(500, 200, 400, 400)  # a viewport in the right half of the screen

    def test_a_place_on_the_screen_becomes_a_place_in_the_viewport(self):
        self.assertEqual(on_viewport(0.7, 0.5, self.SCREEN, self.VIEW), (0.5, 0.5))
        self.assertEqual(on_viewport(0.5, 0.25, self.SCREEN, self.VIEW), (0.0, 0.0))
        self.assertEqual(on_viewport(0.9, 0.75, self.SCREEN, self.VIEW), (1.0, 1.0))

    def test_a_screen_that_doesnt_start_at_the_corner_is_allowed_for(self):
        self.assertEqual(on_viewport(0.0, 0.0, Area(100, 38, 800, 800), Area(100, 38, 400, 400)), (0.0, 0.0))

    def test_a_patch_grows_as_the_viewport_gets_smaller_than_the_screen(self):
        self.assertEqual(reach_on_viewport((0.1, 0.2), self.SCREEN, self.VIEW), (0.25, 0.4))

    def test_a_track_moves_onto_the_viewport_with_its_times_and_confidence(self):
        track = GazeTrack()
        track.add(GazeSample(1.0, 0.7, 0.5, 0.9))
        moved = track_on_viewport(track, self.SCREEN, self.VIEW)
        self.assertEqual(moved.between(0, 2), [GazeSample(1.0, 0.5, 0.5, 0.9)])
        self.assertEqual(track.between(0, 2), [GazeSample(1.0, 0.7, 0.5, 0.9)])  # the track it came from is untouched

    def test_a_look_well_outside_the_viewport_is_not_a_look_at_it(self):
        track = GazeTrack()
        track.add(GazeSample(1.0, 0.1, 0.5, 0.9))  # the left half of the screen: nowhere near the viewport
        track.add(GazeSample(2.0, 0.91, 0.5, 0.9))  # just past its edge: noise, kept as before
        moved = track_on_viewport(track, self.SCREEN, self.VIEW).between(0, 3)
        self.assertEqual([s.confidence for s in moved], [0.0, 0.9])


class OffScreenTest(unittest.TestCase):
    def test_moving_a_track_onto_the_viewport_keeps_which_readings_were_off_the_screen(self):
        screen, view = Area(0, 0, 1000, 800), Area(100, 100, 500, 400)
        track = track_of(GazeSample(1.0, 3.0, 0.5, 0.0, away=True), GazeSample(2.0, 0.5, 0.5, 0.9))
        moved = track_on_viewport(track, screen, view).between(0, 3)
        self.assertEqual([s.away for s in moved], [True, False])

    def test_nearly_all_readings_off_the_screen_is_reading_off_the_screen(self):
        def windows(readings, off):
            return [Window("this", 1.0, readings=readings, good=readings - off, off_screen=off)]

        self.assertTrue(mostly_off_screen(windows(30, 30)))
        self.assertTrue(mostly_off_screen(windows(30, 24)))  # four in five is the least that counts
        self.assertFalse(mostly_off_screen(windows(30, 23)))
        self.assertFalse(mostly_off_screen(windows(30, 0)))
        self.assertFalse(mostly_off_screen(windows(0, 0)))  # nothing read at all is lost gaze, not gaze off the screen
        self.assertFalse(mostly_off_screen([]))

    def test_one_look_off_the_screen_among_lost_face_frames_is_not_reading_off_the_screen(self):
        # One confident glance away, then 29 frames with no face (the worker says the last place again at confidence 0).
        track = track_of(GazeSample(100.0, 2.0, 0.5, 0.0, away=True),
                         *[GazeSample(100.0 + i * 0.02, 2.0, 0.5, 0.0) for i in range(1, 30)])
        grounding = ground(said(("this", 0.4, 0.6)), 100.0, track, DEFAULT_RADIUS, lambda points: None, lambda c: None)
        self.assertEqual(grounding.windows, [Window("this", 100.4, readings=30, good=0, off_screen=1)])
        self.assertFalse(mostly_off_screen(grounding.windows))

    def test_the_readings_of_all_the_pointing_words_count_together(self):
        words = [Window("this", 1.0, readings=10, good=0, off_screen=10), Window("there", 2.0, readings=10, good=8, off_screen=0)]
        self.assertFalse(mostly_off_screen(words))


class MarkerPointTest(unittest.TestCase):
    """Where the marker goes: the middle of the last moment's good readings, on the viewport."""

    SCREEN = Area(0, 0, 1000, 800)
    VIEW = Area(500, 200, 400, 400)  # a viewport in the right half of the screen: screen u 0.5 to 0.9, v 0.25 to 0.75

    def track(self, *looks, now=10.0, spread=0.01):
        """A track with one reading per look, `spread` apart, the last at `now`. A look is (u, v) or (u, v, confidence)."""
        track = GazeTrack()
        for i, look in enumerate(looks):
            u, v, confidence = look if len(look) == 3 else (*look, 0.9)
            track.add(GazeSample(now - (len(looks) - 1 - i) * spread, u, v, confidence))
        return track

    def point(self, track, now=10.0):
        return marker_point(track, now, self.SCREEN, self.VIEW)

    def test_the_numbers_are_the_ones_the_marker_was_designed_with(self):
        self.assertEqual((MARKER_WINDOW, MARKER_MIN_SAMPLES), (0.2, 3))

    def test_it_is_the_median_of_the_last_moment_on_the_viewport(self):
        track = self.track((0.7, 0.5), (0.7, 0.5), (0.95, 0.3), (0.7, 0.5), (0.7, 0.5))
        self.assertEqual(self.point(track), (0.5, 0.5))  # the one stray reading doesn't pull it

    def test_it_follows_the_eyes(self):
        self.assertEqual(self.point(self.track(*[(0.6, 0.5)] * 4)), (0.25, 0.5))
        self.assertEqual(self.point(self.track(*[(0.8, 0.6)] * 4)), (0.75, 0.7))

    def test_readings_older_than_the_window_are_left_out(self):
        old = [(0.9, 0.7)] * 6  # a look that ended before the window began
        track = GazeTrack()
        for i, (u, v) in enumerate(old):
            track.add(GazeSample(9.0 + i * 0.01, u, v, 0.9))
        for i in range(3):
            track.add(GazeSample(9.85 + i * 0.05, 0.7, 0.5, 0.9))
        self.assertEqual(self.point(track), (0.5, 0.5))
        self.assertIsNone(self.point(track, now=10.5))  # and by then there is nothing new

    def test_readings_from_after_now_are_not_known_yet(self):
        track = self.track((0.7, 0.5), (0.7, 0.5), now=10.0)
        track.add(GazeSample(10.1, 0.7, 0.5, 0.9))
        self.assertIsNone(self.point(track))

    def test_fewer_than_three_good_readings_show_nothing(self):
        self.assertIsNone(self.point(self.track((0.7, 0.5), (0.7, 0.5))))
        self.assertIsNone(self.point(GazeTrack()))
        self.assertIsNotNone(self.point(self.track((0.7, 0.5), (0.7, 0.5), (0.7, 0.5))))

    def test_a_blink_or_a_lost_face_does_not_count(self):
        blink = MIN_CONFIDENCE - 0.01
        self.assertIsNone(self.point(self.track((0.7, 0.5), (0.7, 0.5), (0.7, 0.5, blink), (0.7, 0.5, 0.0))))
        self.assertIsNotNone(self.point(self.track((0.7, 0.5), (0.7, 0.5), (0.7, 0.5, MIN_CONFIDENCE))))

    def test_looking_well_off_the_viewport_shows_nothing(self):
        for u, v in ((0.2, 0.5),   # the left half of the screen
                     (0.98, 0.5),  # 20% of the viewport's width past its right edge
                     (0.3, 0.5),   # and past its left edge
                     (0.7, 0.9),   # below it
                     (0.7, 0.05)):  # above it
            with self.subTest(u=u, v=v):
                self.assertIsNone(self.point(self.track(*[(u, v)] * 4)))

    def test_a_little_past_the_edge_is_noise_and_the_marker_stays_on_the_edge(self):
        self.assertEqual(self.point(self.track(*[(0.93, 0.5)] * 4)), (1.0, 0.5))  # 7% past
        self.assertEqual(self.point(self.track(*[(0.47, 0.5)] * 4)), (0.0, 0.5))
        self.assertEqual(self.point(self.track(*[(0.7, 0.77)] * 4)), (0.5, 1.0))
        self.assertEqual(self.point(self.track(*[(0.7, 0.23)] * 4)), (0.5, 0.0))

    def test_the_answer_is_always_inside_zero_to_one(self):
        for u in (0.46, 0.5, 0.7, 0.9, 0.94):
            for v in (0.21, 0.25, 0.5, 0.75, 0.79):
                point = self.point(self.track(*[(u, v)] * 4))
                self.assertTrue(point is None or all(0.0 <= share <= 1.0 for share in point), (u, v, point))

    def test_the_track_is_left_as_it_was(self):
        track = self.track(*[(0.7, 0.5)] * 4)
        before = track.between(0, 20)
        self.point(track)
        self.assertEqual(track.between(0, 20), before)


if __name__ == "__main__":
    unittest.main()
