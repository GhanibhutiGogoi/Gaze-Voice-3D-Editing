"""The dashboard's script has no test harness of its own, so the functions that decide what the person reads are pulled
out of app.js and run with node. Skipped where node isn't installed."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "gazevoice" / "server" / "static"
APP_JS = STATIC / "app.js"
CALIBRATE_JS = STATIC / "calibrate.js"
CALIBRATE_HTML = STATIC / "calibrate.html"
CALIBRATE_CSS = STATIC / "calibrate.css"
NODE = shutil.which("node")


def result_line(result: dict) -> str:
    """What the dashboard writes under a succeeded job with this `result`, by running the real resultLine."""
    source = APP_JS.read_text()
    match = re.search(r"^function resultLine\(job\) \{.*?^\}\n", source, re.DOTALL | re.MULTILINE)
    assert match, "resultLine is not where this test expects it"
    program = (
        "const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;\n"
        f"{match.group(0)}\n"
        f"console.log(JSON.stringify(resultLine({{ result: {json.dumps(result)} }})));\n"
    )
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def run_function(name: str, call: str, stubs: str = "", script: Path = APP_JS):
    """What the real function `name` in app.js, or `script`, returns for the JavaScript expression `call`."""
    match = re.search(rf"^function {name}\(.*?^\}}\n", script.read_text(), re.DOTALL | re.MULTILINE)
    assert match, f"{name} is not where this test expects it"
    program = f"{stubs}\n{match.group(0)}\nconsole.log(JSON.stringify({call}));\n"
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def constants(script: Path, *names: str) -> str:
    """The `const NAME = …;` lines of `script` that these functions lean on, to put before the function under test."""
    source = script.read_text()
    lines = []
    for name in names:
        match = re.search(rf"^const {name} = .*$", source, re.MULTILINE)
        assert match, f"{name} is not where this test expects it"
        lines.append(match.group(0))
    return "\n".join(lines)


def privacy_note(voice: dict, sources=None) -> str:
    stubs = (f"const S = {{ state: {{ sources: {json.dumps(sources or {'editor': 'claude'})} }} }};\n"
             "const providerNames = () => [];\n")
    return run_function("privacyNote", f"privacyNote({json.dumps(voice)})", stubs)


@unittest.skipUnless(NODE, "node isn't installed")
class CalibrateKeyTest(unittest.TestCase):
    def press(self, action):
        stubs = ("const location = { href: 'the dashboard' };\n"
                 "const S = {};\nconst ARM_MS = 1;\nconst preservingFocus = () => {};\nconst renderRack = () => {};\n"
                 "const document = { querySelector: () => null };\nconst CSS = { escape: (x) => x };\n"
                 "let ran = null;\nconst runAction = (cid, aid) => { ran = aid; };\n")
        return run_function("pressKey", f"(pressKey({{ id: 'gaze' }}, {json.dumps(action)}), {{ page: location.href, ran }})", stubs)

    def test_a_key_with_a_page_opens_it_and_starts_nothing(self):
        self.assertEqual(self.press({"id": "calibrate", "href": "/calibrate"}), {"page": "/calibrate", "ran": None})

    def test_any_other_key_runs_its_action(self):
        self.assertEqual(self.press({"id": "test2", "href": None}), {"page": "the dashboard", "ran": "test2"})

    def test_the_result_line_measures_against_the_screen(self):
        self.assertEqual(result_line({"calibrated": True, "median": 0.12}), "Typically off by 12% of the screen")


@unittest.skipUnless(NODE, "node isn't installed")
class CalibratePageTest(unittest.TestCase):
    WINDOW = {"innerWidth": 1710, "innerHeight": 1074, "outerHeight": 1074, "screenX": 0, "screenY": 0}
    SCREEN = {"width": 1710}

    def area(self, event, window=None, fullscreen=True):
        call = f"pageArea({json.dumps(event)}, {json.dumps(window or self.WINDOW)}, {json.dumps(self.SCREEN)}, {json.dumps(fullscreen)})"
        return run_function("pageArea", call, script=CALIBRATE_JS)

    def test_a_click_shows_where_the_page_really_is_even_below_the_notch(self):
        event = {"detail": 1, "screenX": 855, "screenY": 575, "clientX": 855, "clientY": 537}
        self.assertEqual(self.area(event), {"left": 0, "top": 38, "width": 1710, "height": 1074})

    def test_a_page_in_a_window_is_measured_from_the_click_too(self):
        event = {"detail": 1, "screenX": 300, "screenY": 400, "clientX": 200, "clientY": 250}
        window = {"innerWidth": 800, "innerHeight": 600, "outerHeight": 700, "screenX": 100, "screenY": 50}
        self.assertEqual(self.area(event, window, fullscreen=False), {"left": 100, "top": 150, "width": 800, "height": 600})

    def test_browser_zoom_is_allowed_for_in_full_screen(self):
        event = {"detail": 1, "screenX": 855, "screenY": 575, "clientX": 427.5, "clientY": 268.5}
        window = {**self.WINDOW, "innerWidth": 855, "innerHeight": 537}
        self.assertEqual(self.area(event, window), {"left": 0, "top": 38, "width": 1710, "height": 1074})

    def test_a_key_press_has_no_place_so_the_window_is_used(self):
        event = {"detail": 0, "screenX": 0, "screenY": 0, "clientX": 0, "clientY": 0}
        window = {**self.WINDOW, "screenX": 0, "screenY": 38, "outerHeight": 1074}
        self.assertEqual(self.area(event, window), {"left": 0, "top": 38, "width": 1710, "height": 1074})

    def result(self, job):
        measured = re.search(r"^const MEASURED = .*$", CALIBRATE_JS.read_text(), re.MULTILINE).group(0)
        return run_function("resultText", f"resultText({json.dumps(job)})", measured, CALIBRATE_JS)

    def test_a_good_result_gives_the_accuracy_in_plain_words(self):
        shown = self.result({"state": "succeeded", "result": {"median": 0.12, "p90": 0.2}, "lines": []})
        self.assertEqual(shown["title"], "Calibrated")
        self.assertIn("off by 12% of your screen", shown["text"])
        self.assertIn("20%", shown["text"])

    def test_a_failure_gives_the_reason_and_what_was_measured(self):
        lines = ["Dot 1 of 14", "The camera sent 540 frames while the dots were up.", "Readings kept: 12 to learn from."]
        shown = self.result({"state": "failed", "error": "It wasn't saved.", "lines": lines})
        self.assertEqual(shown["title"], "Calibration didn't finish")
        self.assertEqual(shown["text"], "It wasn't saved.")
        self.assertEqual(shown["lines"], lines[1:])

    def test_a_run_that_was_refused_is_not_called_unfinished(self):
        job = {"state": "failed", "error": "That calibration was typically off by 31%. It wasn't saved.",
               "result": {"calibrated": False, "refused": True}, "lines": []}
        shown = self.result(job)
        self.assertEqual(shown["title"], "Not calibrated")
        self.assertEqual(shown["text"], job["error"])

    def test_a_run_that_stopped_is_still_called_unfinished(self):
        for job in ({"state": "failed", "error": "The calibration was stopped.", "result": None, "lines": []},
                    {"state": "failed", "error": "The calibration was stopped.", "lines": []},
                    {"state": "failed", "error": "The calibration was stopped.", "result": {}, "lines": []}):
            with self.subTest(job.get("result", "no result")):
                self.assertEqual(self.result(job)["title"], "Calibration didn't finish")

    def test_how_far_the_head_moved_is_shown_too(self):
        line = "Your head moved about 1.2 cm and turned about 2 degrees during the run."
        for job in ({"state": "failed", "error": "No.", "lines": ["Dot 1 of 14", line]},
                    {"state": "succeeded", "result": {"median": 0.12, "p90": 0.2}, "lines": ["Dot 1 of 14", line]}):
            with self.subTest(job["state"]):
                self.assertEqual(self.result(job)["lines"], [line])


@unittest.skipUnless(NODE, "node isn't installed")
class HiddenPageTest(unittest.TestCase):
    """A hidden tab's timers still fire, about once a second. If the page kept saying it was there, the job would keep the
    camera on for the whole guide. So a hidden page says nothing, and the job ends within PAGE_SECONDS."""

    def run_it(self, hidden: bool):
        stubs = (constants(CALIBRATE_JS, "BEAT_MS") + "\n"
                 "const S = { job: 'j1', beat: null };\n"
                 "const sent = [], timers = [];\n"
                 "const post = (path, body) => { sent.push([path, body]); return Promise.resolve(); };\n"
                 f"const document = {{ hidden: {json.dumps(hidden)} }};\n"
                 "const setInterval = (fn, ms) => { timers.push(ms); return 7; };\n")
        sayalive = re.search(r"^function sayAlive\(.*?^\}\n", CALIBRATE_JS.read_text(), re.DOTALL | re.MULTILINE).group(0)
        return run_function("startBeat", "(startBeat(), { sent, timers, beat: S.beat })", stubs + sayalive, CALIBRATE_JS)

    def test_a_page_in_view_says_it_is_there_and_keeps_saying_so(self):
        done = self.run_it(hidden=False)
        self.assertEqual(done["sent"], [["/api/gaze/page", {"job": "j1", "alive": True}]])
        self.assertEqual(done["timers"], [400])
        self.assertEqual(done["beat"], 7)

    def test_a_hidden_page_says_nothing_and_starts_no_beat(self):
        done = self.run_it(hidden=True)
        self.assertEqual(done, {"sent": [], "timers": [], "beat": None})

    def test_a_beat_that_fires_after_the_page_was_hidden_says_nothing(self):
        stubs = ("const S = { job: 'j1' };\nconst sent = [];\n"
                 "const post = (path, body) => { sent.push(body); return Promise.resolve(); };\n"
                 "const document = { hidden: true };\n")
        self.assertEqual(run_function("sayAlive", "(sayAlive(), sent)", stubs, CALIBRATE_JS), [])


@unittest.skipUnless(NODE, "node isn't installed")
class JobWatchTest(unittest.TestCase):
    """The page watches its job as well as listening for events. A control center that has stopped must not leave it
    sitting on a dot for ever."""

    def look(self, status, job, missed):
        stubs = constants(CALIBRATE_JS, "MISSED_LOOKS", "STOPPED")
        return run_function("lookedAt", f"lookedAt({json.dumps(status)}, {json.dumps(job)}, {json.dumps(missed)})", stubs,
                            CALIBRATE_JS)

    def test_a_job_still_running_is_left_to_run(self):
        self.assertEqual(self.look(200, {"state": "running"}, 2), {"missed": 0, "over": None})

    def test_a_job_that_is_over_is_shown(self):
        job = {"state": "failed", "error": "No."}
        self.assertEqual(self.look(200, job, 0), {"missed": 0, "over": job})

    def test_a_control_center_that_no_longer_knows_the_job_has_restarted_and_that_is_the_end_of_it(self):
        over = self.look(404, None, 0)["over"]
        self.assertEqual(over["state"], "failed")
        self.assertIn("control center stopped", over["error"])
        self.assertNotIn("result", over)  # a run that stopped, not one that was refused

    def test_a_few_looks_in_a_row_with_no_answer_are_the_end_of_it_too(self):
        first = self.look(0, None, 0)
        self.assertEqual(first, {"missed": 1, "over": None})  # one bad look is not enough
        second = self.look(0, None, first["missed"])
        self.assertEqual(second, {"missed": 2, "over": None})
        third = self.look(0, None, second["missed"])
        self.assertEqual(third["missed"], 3)
        self.assertIn("control center stopped", third["over"]["error"])

    def test_an_error_page_or_a_reply_that_isnt_a_job_counts_as_no_answer(self):
        self.assertEqual(self.look(500, None, 0), {"missed": 1, "over": None})
        self.assertEqual(self.look(200, None, 1), {"missed": 2, "over": None})

    def test_an_answer_starts_the_count_again(self):
        self.assertEqual(self.look(200, {"state": "running"}, 2)["missed"], 0)


@unittest.skipUnless(NODE, "node isn't installed")
class GuideRingTest(unittest.TestCase):
    """Where the ring that stands for the head is drawn, from numbers alone: the page never gets a picture."""

    WIDTH, HEIGHT = 1710, 1074
    BOX = 0.7 * HEIGHT  # the outline is as tall as 70% of the page

    def setUp(self):
        source = CALIBRATE_JS.read_text()
        found = re.findall(r"^const (?:OUTLINE|GUIDE_AT|RING_SCALE) = .*$", source, re.MULTILINE)
        self.assertEqual(len(found), 3, "the constants the ring is drawn from are not where this test expects")
        self.constants = "\n".join(found)

    def ring(self, data, width=WIDTH, height=HEIGHT):
        return run_function("guideRing", f"guideRing({json.dumps(data)}, {width}, {height})", self.constants, CALIBRATE_JS)

    def reading(self, **kw):
        return {"found": True, "x": 0.5, "y": 0.45, "scale": 1.0, "fits": True, "advice": "Hold still", **kw}

    def test_a_head_in_the_target_place_and_size_sits_exactly_on_the_outlines_head(self):
        ring = self.ring(self.reading())
        self.assertAlmostEqual(ring["x"], self.WIDTH / 2)
        self.assertAlmostEqual(ring["y"], self.HEIGHT / 2 - (0.5 - 0.43) * self.BOX)  # where the outline's eyes are
        self.assertAlmostEqual(ring["w"], 0.36 * self.BOX)
        self.assertAlmostEqual(ring["h"], 0.48 * self.BOX)

    def test_the_ring_moves_with_the_head_across_the_whole_page(self):
        home, moved = self.ring(self.reading()), self.ring(self.reading(x=0.6, y=0.35))
        self.assertAlmostEqual(moved["x"] - home["x"], 0.1 * self.WIDTH)
        self.assertAlmostEqual(moved["y"] - home["y"], -0.1 * self.HEIGHT)

    def test_a_bigger_head_gets_a_bigger_ring(self):
        home = self.ring(self.reading())
        for scale in (0.5, 2.0):
            ring = self.ring(self.reading(scale=scale))
            self.assertAlmostEqual(ring["w"], home["w"] * scale)
            self.assertAlmostEqual(ring["h"], home["h"] * scale)

    def test_a_wild_size_is_held_to_something_that_can_be_drawn(self):
        home = self.ring(self.reading())
        self.assertLess(self.ring(self.reading(scale=1000))["w"], home["w"] * 3)
        self.assertGreater(self.ring(self.reading(scale=0.0001))["w"], home["w"] / 3)

    def test_the_ring_stays_on_the_page(self):
        for x, y in ((-5, 0.5), (6, 0.5), (0.5, -5), (0.5, 7)):
            ring = self.ring(self.reading(x=x, y=y))
            self.assertTrue(0 <= ring["x"] <= self.WIDTH and 0 <= ring["y"] <= self.HEIGHT, (x, y, ring))

    def test_with_no_face_there_is_no_ring(self):
        self.assertIsNone(self.ring({"found": False, "x": None, "y": None, "scale": None, "fits": False, "advice": "I can't see your face."}))

    def test_in_a_narrow_window_the_outline_and_the_ring_shrink_together(self):
        ring = self.ring(self.reading(), width=500, height=1000)  # the outline is held to 90% of the width: 450
        self.assertAlmostEqual(ring["w"], 0.36 * 450)
        self.assertAlmostEqual(ring["x"], 250)
        self.assertAlmostEqual(ring["y"], 500 - (0.5 - 0.43) * 450)


class GuidePageTest(unittest.TestCase):
    """The parts of the page the ring leans on, held to each other: the drawing in the page, its style and the script."""

    def test_the_numbers_the_ring_is_drawn_from_are_those_of_the_outline_in_the_page(self):
        html, source = CALIBRATE_HTML.read_text(), CALIBRATE_JS.read_text()
        head = re.search(r'<ellipse class="guide-head" cx="(\d+)" cy="(\d+)" rx="(\d+)" ry="(\d+)"', html)
        self.assertTrue(head, "the outline's head is not where this test expects")
        cx, cy, rx, ry = (int(n) for n in head.groups())
        self.assertIn('viewBox="0 0 100 100"', html)
        outline = json.loads(re.sub(r"(\w+):", r'"\1":', re.search(r"^const OUTLINE = (.*);$", source, re.MULTILINE).group(1)))
        self.assertEqual(cx, 50)
        self.assertAlmostEqual(outline["eyes"], cy / 100)
        self.assertAlmostEqual(outline["head"]["w"], 2 * rx / 100)
        self.assertAlmostEqual(outline["head"]["h"], 2 * ry / 100)
        css = CALIBRATE_CSS.read_text()
        self.assertIn(f"min({round(outline['height'] * 100)}vh, {round(outline['maxWidth'] * 100)}vw)", css)

    def test_the_target_the_page_centres_on_is_the_servers(self):
        from gazevoice.components.gaze import GUIDE_TARGET

        match = re.search(r"^const GUIDE_AT = \[([\d.]+), ([\d.]+)\];", CALIBRATE_JS.read_text(), re.MULTILINE)
        self.assertEqual((float(match.group(1)), float(match.group(2))), GUIDE_TARGET[:2])

    def test_the_page_says_how_many_dots_the_job_shows(self):
        from gazevoice.components.gaze import DOT_ORDER

        said = re.search(r"look at (\d+) dots", " ".join(CALIBRATE_HTML.read_text().split()))
        self.assertTrue(said, "the page no longer says how many dots there are")
        self.assertEqual(int(said.group(1)), len(DOT_ORDER))

    def test_the_page_draws_with_classes_and_never_touches_the_camera(self):
        html, source = CALIBRATE_HTML.read_text(), CALIBRATE_JS.read_text()
        self.assertNotRegex(html, r"\sstyle\s*=")
        for word in ("getUserMedia", "mediaDevices", "<canvas", "<video", "<img", "ImageCapture"):
            self.assertNotIn(word, html + source, word)

    def test_the_ring_glides_and_stops_gliding_for_those_who_ask_for_less_motion(self):
        css = CALIBRATE_CSS.read_text()
        self.assertRegex(css, r"\.guide-ring\s*\{[^}]*transition:[^;}]*120ms")
        reduced = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", css, re.DOTALL)
        self.assertTrue(reduced and ".guide-ring" in reduced.group(1) and "transition: none" in reduced.group(1))

    def test_the_cursor_stays_visible_in_the_guide_so_start_anyway_can_be_clicked(self):
        css = CALIBRATE_CSS.read_text()
        self.assertNotRegex(css, r'data-stage="guide"\][^{]*\{[^}]*cursor:\s*none')
        self.assertRegex(css, r'body\[data-stage="guide"\]')  # and it is as bright as the dots


@unittest.skipUnless(NODE, "node isn't installed")
class PrivacyNoteTest(unittest.TestCase):
    def test_it_names_the_camera_while_the_camera_is_on(self):
        note = privacy_note({"gaze": {"live": True, "source": "camera"}})
        self.assertTrue(note.startswith("The mic and camera are open while voice is on."), note)

    def test_it_names_only_the_mic_otherwise(self):
        for gaze in ({"live": False, "source": "camera"}, {"live": True, "source": "mouse"}, None):
            with self.subTest(gaze):
                voice = {} if gaze is None else {"gaze": gaze}
                self.assertTrue(privacy_note(voice).startswith("The mic is open while voice is on."))


@unittest.skipUnless(NODE, "node isn't installed")
class GazeProblemTest(unittest.TestCase):
    PROBLEM = "Gaze couldn't start (That camera isn't connected.), so voice uses your selection."

    def problem(self, voice):
        return run_function("gazeProblem", f"gazeProblem({json.dumps(voice)})")

    def test_the_reason_gaze_didnt_start_is_shown_while_voice_is_on(self):
        self.assertEqual(self.problem({"state": "ready", "gaze": {"problem": self.PROBLEM}}), self.PROBLEM)

    def test_nothing_is_shown_with_no_problem_or_once_voice_is_off(self):
        for voice in ({"state": "ready", "gaze": {"problem": ""}}, {"state": "off", "gaze": {"problem": self.PROBLEM}},
                      {"state": "ready"}):
            with self.subTest(voice):
                self.assertIsNone(self.problem(voice))

    def test_the_voice_panel_shows_it_as_a_warning(self):
        self.assertRegex(APP_JS.read_text(), r'h\("p", \{ class: "voice-note", "data-tone": "warn", text: gazeProblem\(v\) \}\)')


def target(word, name, score):
    return {"word": word, "at": 1.0, "candidates": [{"name": name, "score": score}, {"name": "Other", "score": 0.1}]}


@unittest.skipUnless(NODE, "node isn't installed")
class GazeLineTest(unittest.TestCase):
    def line(self, record):
        return run_function("gazeLine", f"gazeLine({json.dumps(record)})")

    def test_gaze_on_says_what_gaze_picked(self):
        gaze = {"status": "resolved", "targets": [target("there", "Eagle", 0.79)]}
        self.assertEqual(self.line({"gaze": gaze, "focus": {"gaze": gaze}}), "Gaze picked “there” → Eagle (79%)")

    def test_watch_only_says_what_gaze_would_have_picked(self):
        gaze = {"status": "resolved", "targets": [target("there", "Eagle", 0.79)]}
        self.assertEqual(self.line({"gaze": gaze, "focus": {"selected": ["Chair"]}}),
                         "Gaze would have picked “there” → Eagle (79%)")

    def test_a_question_nobody_answered_is_what_gaze_would_have_picked(self):
        gaze = {"status": "clarify", "asked": True, "targets": [target("this", "Eagle", 0.46)]}
        self.assertEqual(self.line({"gaze": gaze, "focus": None}), "Gaze would have picked “this” → Eagle (46%)")

    def test_every_pointing_word_is_shown_in_order(self):
        gaze = {"status": "resolved", "targets": [target("this", "Eagle", 0.8), target("there", "Perch", 0.6)]}
        self.assertEqual(self.line({"gaze": gaze, "focus": {"gaze": gaze}}),
                         "Gaze picked “this” → Eagle (80%) · “there” → Perch (60%)")

    def test_a_word_that_looked_at_empty_space_says_so_in_its_place(self):
        gaze = {"status": "resolved", "targets": [{**target("this", "Eagle", 0.8), "at": 0.4}],
                "misses": [{"word": "there", "at": 1.2}]}
        self.assertEqual(self.line({"gaze": gaze, "focus": {"gaze": gaze}}),
                         "Gaze picked “this” → Eagle (80%) · “there” → nothing")

    def test_an_empty_space_work_plane_point_is_shown(self):
        gaze = {"status": "resolved", "targets": [],
                "misses": [{"word": "there", "at": 1.2, "point": [2, 0, 3]}]}
        self.assertEqual(self.line({"gaze": gaze, "focus": {"gaze": gaze}}),
                         "Gaze picked “there” → work-plane point (2.00, 0.00, 3.00)")

    def test_nothing_to_show_is_no_line(self):
        for gaze in (None, {"status": "none", "targets": []},
                     {"status": "none", "targets": [], "radius": [], "not_on_screen": True},
                     {"status": "none", "targets": [], "misses": [{"word": "there", "at": 1.0}]}):
            with self.subTest(gaze):
                self.assertIsNone(self.line({"gaze": gaze, "focus": None}))

    def test_the_command_row_shows_it(self):
        self.assertIn('gazeLine(r) ? h("p", { class: "cmd-heard", text: gazeLine(r) }) : null', APP_JS.read_text())


@unittest.skipUnless(NODE, "node isn't installed")
class ShelfTest(unittest.TestCase):
    """Which units the dashboard puts on which shelf, read from the real NAMES and GROUPS in app.js."""

    @classmethod
    def setUpClass(cls):
        source = APP_JS.read_text()
        blocks = []
        for name, close in (("NAMES", r"\}"), ("GROUPS", r"\]")):
            match = re.search(rf"^const {name} = [\[{{].*?^{close};\n", source, re.DOTALL | re.MULTILINE)
            assert match, f"{name} is not where this test expects it"
            blocks.append(match.group(0))
        program = "\n".join(blocks) + "\nconsole.log(JSON.stringify({ NAMES, GROUPS }));\n"
        done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        cls.names, cls.groups = (lambda data: (data["NAMES"], data["GROUPS"]))(json.loads(done.stdout))

    def test_ml_agents_is_on_the_unity_shelf_after_the_bridge(self):
        unity = next(group for group in self.groups if group["label"] == "Unity")
        self.assertEqual(unity["ids"], ["unity", "unity_project", "unity_mcp", "unity_bridge", "mlagents"])

    def test_it_is_on_no_other_shelf(self):
        shelves = [group["label"] for group in self.groups if "mlagents" in group["ids"]]
        self.assertEqual(shelves, ["Unity"])

    def test_it_is_called_ml_agents(self):
        self.assertEqual(self.names["mlagents"], "ML-Agents")


@unittest.skipUnless(NODE, "node isn't installed")
class ResultLineTest(unittest.TestCase):
    def test_a_passed_picture_check_says_which_claude_code_it_was_for(self):
        self.assertEqual(result_line({"passed": True, "claude": "2.1.303", "seconds": 7.4}),
                         "Pictures are cleaned up with Claude Code 2.1.303")

    def test_a_passing_workflow_check_does_not_claim_anything_about_pictures(self):
        # "Check everything" returns how many stages passed, a number, under the same key.
        for passed in (1, 6, 12):
            line = result_line({"passed": passed})
            self.assertNotIn("Pictures", line)
            self.assertNotIn("undefined", line)
            self.assertEqual(line, "Done")

    def test_a_pass_without_a_version_is_not_taken_for_a_picture_check(self):
        self.assertEqual(result_line({"passed": True}), "Done")
        self.assertEqual(result_line({"passed": False, "claude": "2.1.303"}), "Done")

    def test_stopping_a_training_run_reads_as_what_training_says_not_as_a_project(self):
        status = {"state": "stopped", "run_id": "run1", "project": "/Users/me/Unity/Game", "behaviors": {},
                  "message": "Training run1 stopped. Its model was saved and is ready to use.", "models": ["Mover"]}
        self.assertEqual(result_line(status), "Training run1 stopped. Its model was saved and is ready to use.")

    def test_a_result_with_only_a_state_or_only_a_message_is_not_taken_for_a_training_status(self):
        self.assertEqual(result_line({"state": "on"}), "Done")
        self.assertEqual(result_line({"message": "hello"}), "Done")

    def test_the_other_results_read_as_they_did(self):
        self.assertEqual(result_line({"installed": True}), "Installed")
        self.assertEqual(result_line({"reply": "ready", "seconds": 1.5}), "Replied “ready” in 1.5 s")


@unittest.skipUnless(APP_JS.exists(), "no dashboard script")
class PicturesRowTest(unittest.TestCase):
    def test_the_pictures_switch_says_what_it_costs_and_the_note_says_how_they_are_kept(self):
        source = APP_JS.read_text()
        self.assertIn('"Pictures · each unnamed target uses a little of your plan", "gaze_pictures"', source)
        self.assertIn("GazeVoice never saves a picture. Claude Code gets it for one reply, and the check looks for any "
                      "copy left behind. Anthropic keeps what's sent under your account's terms.", source)
        self.assertIn("The dashboard keeps the last 20 pictures in memory so you can see what the AI was shown, and "
                      "drops them when it stops.", source)
        self.assertNotIn("deleted from this Mac", source)


FAKE_PAGE = """
class Node {}
class FakeElement extends Node {
  constructor(tag) { super(); this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {}; this.className = "";
    this.textContent = ""; this.dataset = {}; this.hidden = false; this.src = ""; this.alt = ""; this.opened = false; this.focused = false; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  append(...kids) { this.children.push(...kids); }
  fire(type) { (this.listeners[type] || []).forEach((handler) => handler({ target: this })); }
  showModal() { this.opened = true; }
  close() { this.opened = false; this.fire("close"); }
  focus() { this.focused = true; }
}
const document = {
  body: new FakeElement("body"),
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ text }),
  querySelector: (selector) => document.found[selector] || null,
  found: {},
};
const CSS = { escape: (text) => text };
const tree = (el) => (el instanceof Node
  ? { tag: el.tag, class: el.className || undefined, text: el.textContent || undefined, attrs: el.attrs,
      children: el.children.map(tree) }
  : el.text);
const S = { thumbs: new Map(), oldThumbs: new Map(), gonePictures: new Set(), pictureView: null };
let rendered = 0;
const renderVoice = () => { rendered += 1; };
let shown = [];
"""

RECORD = {
    "id": "20261108-224739-5",
    "gaze": {"targets": [{"word": "there", "picture": "the left wing tip"}, {"word": "this", "picture": None}],
             "pictures_sent": [0]},
}


ROW_FUNCTIONS = ["sentPictures", "pictureLine", "pictureThumb", "pictureGone", "pictureRows", "renderingPictures"]


@unittest.skipUnless(NODE, "node isn't installed")
class SentPictureTest(unittest.TestCase):
    """The command row's picture: a small one beside what the AI said about it, which opens full size."""

    def run_js(self, names, body, stubs=""):
        source = APP_JS.read_text()
        parts = []
        for name in ("h", *names):  # h builds every element, so it is always run for real
            match = re.search(rf"^function {name}\(.*?^\}}\n", source, re.DOTALL | re.MULTILINE)
            assert match, f"{name} is not where this test expects it"
            parts.append(match.group(0))
        program = f"{FAKE_PAGE}\n{stubs}\n{chr(10).join(parts)}\n{body}\n"
        done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    def items(self, record):
        return self.run_js(["sentPictures"], f"console.log(JSON.stringify(sentPictures({json.dumps(record)})));")

    def rows(self, record, extra="", names=()):
        body = f"{extra}\nconsole.log(JSON.stringify(pictureRows({json.dumps(record)}).map(tree)));"
        return self.run_js([*ROW_FUNCTIONS, *names], body)

    # --- which pictures a command had --------------------------------------------------------------------------
    def test_a_command_with_no_picture_has_nothing_new(self):
        for record in ({"id": "x"}, {"id": "x", "gaze": None}, {"id": "x", "gaze": {"targets": []}},
                       {"id": "x", "gaze": {"targets": [{"word": "there"}], "pictures_sent": []}},
                       {"id": "x", "gaze": {"targets": [{"word": "there"}]}},  # a record from before pictures were shown
                       {"id": "x", "gaze": {"targets": [{"word": "there"}], "pictures_sent": "0"}}):
            with self.subTest(record):
                self.assertEqual(self.items(record), [])
                self.assertEqual(self.rows(record), [])

    def test_each_picture_knows_its_word_what_the_ai_said_and_where_it_is(self):
        record = {"id": "20261108-224739-5", "gaze": {"targets": [{"word": "this", "picture": "the beak"},
                                                              {"word": "there", "picture": None}],
                                                      "pictures_sent": [0, 1]}}
        self.assertEqual(self.items(record), [
            {"number": 0, "command": "20261108-224739-5", "word": "this", "said": "the beak",
             "url": "/api/voice/pictures/20261108-224739-5/0"},
            {"number": 1, "command": "20261108-224739-5", "word": "there", "said": None,
             "url": "/api/voice/pictures/20261108-224739-5/1"},
        ])

    def test_a_note_that_points_at_no_pointing_word_is_ignored(self):
        record = {"id": "x-1", "gaze": {"targets": [{"word": "there", "picture": "a"}], "pictures_sent": [5, -1, 0.5, "0", None, 0]}}
        self.assertEqual([item["number"] for item in self.items(record)], [0])

    def test_the_address_is_made_of_the_commands_id_and_nothing_else(self):
        record = {"id": "a/../b?c#d", "gaze": {"targets": [{"word": "there"}], "pictures_sent": [0]}}
        self.assertEqual(self.items(record)[0]["url"], "/api/voice/pictures/a%2F..%2Fb%3Fc%23d/0")

    # --- what it says ------------------------------------------------------------------------------------------
    def line(self, item, gone):
        return self.run_js(["pictureLine"], f"console.log(JSON.stringify(pictureLine({json.dumps(item)}, {json.dumps(gone)})));")

    def test_the_line_says_what_the_ai_said_in_its_own_words(self):
        item = {"word": "there", "said": "the left wing tip"}
        self.assertEqual(self.line(item, False), "Picture sent for “there”. The AI said: “the left wing tip”.")

    def test_the_line_says_so_when_the_ai_couldnt_tell(self):
        self.assertEqual(self.line({"word": "this", "said": None}, False),
                         "Picture sent for “this”. The AI couldn't say what was in it.")

    def test_a_picture_that_is_gone_says_so_in_plain_words(self):
        item = {"word": "there", "said": "the left wing tip"}
        self.assertEqual(self.line(item, True),
                         "Picture sent for “there”. The AI said: “the left wing tip”. The picture isn't kept any more: "
                         "only the last 20 are, and none after the dashboard stops.")

    # --- the row -----------------------------------------------------------------------------------------------
    def test_a_picture_there_is_a_small_picture_you_can_press_and_the_sentence_beside_it(self):
        row, = self.rows(RECORD)
        self.assertEqual(row["tag"], "div")
        self.assertEqual(row["class"], "cmd-picture")
        button, line = row["children"]
        self.assertEqual((button["tag"], button["class"], button["attrs"]["type"]), ("button", "cmd-thumb", "button"))
        self.assertEqual(button["attrs"]["aria-label"], "Show the picture sent for “there” full size")
        picture, = button["children"]
        self.assertEqual((picture["tag"], picture["attrs"]["src"]), ("img", "/api/voice/pictures/20261108-224739-5/0"))
        self.assertIn("with the gaze bubble on it", picture["attrs"]["alt"])
        self.assertEqual((line["tag"], line["class"]), ("p", "cmd-heard"))
        self.assertEqual(line["text"], "Picture sent for “there”. The AI said: “the left wing tip”.")

    def test_a_gone_picture_leaves_the_words_and_no_picture(self):
        row, = self.rows(RECORD, extra="S.gonePictures.add('/api/voice/pictures/20261108-224739-5/0');")
        line, = row["children"]  # no button, no img
        self.assertEqual(line["class"], "cmd-heard")
        self.assertIn("The picture isn't kept any more", line["text"])
        self.assertIn("“the left wing tip”", line["text"])

    def test_a_picture_the_browser_cant_get_turns_into_the_words_for_a_gone_one(self):
        program = """
        const row = pictureRows(RECORD)[0];
        const img = row.children[0].children[0];
        img.fire("error");
        console.log(JSON.stringify({ rendered, gone: [...S.gonePictures], kept: S.thumbs.size,
                                     again: pictureRows(RECORD).map(tree) }));
        """.replace("RECORD", json.dumps(RECORD))
        result = self.run_js(ROW_FUNCTIONS, program)
        self.assertEqual(result["rendered"], 1)  # the page is drawn again, now without the picture
        self.assertEqual(result["gone"], ["/api/voice/pictures/20261108-224739-5/0"])
        self.assertEqual(result["kept"], 0)
        self.assertEqual(len(result["again"][0]["children"]), 1)

    def test_drawing_the_page_again_keeps_the_same_loaded_picture_instead_of_fetching_it_again(self):
        program = """
        const first = pictureRows(RECORD)[0].children[0].children[0];
        S.oldThumbs = S.thumbs; S.thumbs = new Map();   // what renderingPictures does at the start of a draw
        const second = pictureRows(RECORD)[0].children[0].children[0];
        console.log(JSON.stringify({ same: first === second, kept: S.thumbs.size }));
        """.replace("RECORD", json.dumps(RECORD))
        self.assertEqual(self.run_js(ROW_FUNCTIONS, program),
                         {"same": True, "kept": 1})

    def test_pictures_of_commands_no_longer_listed_are_let_go(self):
        program = """
        pictureRows(RECORD);
        renderingPictures(() => []);   // a draw whose list has no such command
        console.log(JSON.stringify({ kept: S.thumbs.size, old: S.oldThumbs.size }));
        """.replace("RECORD", json.dumps(RECORD))
        self.assertEqual(self.run_js(ROW_FUNCTIONS,
                                     program), {"kept": 0, "old": 0})

    def test_the_list_with_nothing_in_it_lets_go_of_every_picture_too(self):
        # After a dashboard restart or a rebuilt voice the list comes back empty, and its old pictures must not linger.
        program = """
        const commandRow = (r) => h("li", {}, pictureRows(r));   // the row itself is not what is tested here
        const full = commandList([RECORD], "nothing yet");
        const before = S.thumbs.size;
        const empty = commandList([], "nothing yet");
        console.log(JSON.stringify({ before, after: S.thumbs.size, old: S.oldThumbs.size, full: tree(full).class,
                                     empty: tree(empty) }));
        """.replace("RECORD", json.dumps(RECORD))
        result = self.run_js([*ROW_FUNCTIONS, "commandList"], program)
        self.assertEqual((result["before"], result["after"], result["old"]), (1, 0, 0))
        self.assertEqual(result["full"], "cmd-list")
        self.assertEqual((result["empty"]["class"], result["empty"]["text"]), ("cmd-empty", "nothing yet"))

    def test_every_command_the_service_keeps_is_listed_not_just_the_first_twelve(self):
        records = [{**RECORD, "id": f"20261108-224739-{n}"} for n in range(20)]
        program = """
        const commandRow = (r) => h("li", {}, r.id);   // the row itself is not what is tested here
        console.log(JSON.stringify(tree(commandList(RECORDS, "nothing yet")).children.length));
        """.replace("RECORDS", json.dumps(records))
        self.assertEqual(self.run_js([*ROW_FUNCTIONS, "commandList"], program), 20)

    def test_a_new_command_pushes_the_oldest_off_at_the_number_the_service_keeps(self):
        records = [{"id": f"c{n}", "created": 100 - n} for n in range(3)]  # newest first, as the service sends them
        program = """
        S.voice = { state: "ready", recent: RECORDS, recent_limit: LIMIT };
        const voice = () => S.voice;
        upsertCommand({ id: "new", created: 200 });
        const grown = S.voice.recent.map((r) => r.id);
        upsertCommand({ id: "c1", created: 100 - 1, stage: "done" });   // an update to one that is there changes nothing else
        console.log(JSON.stringify({ grown, updated: S.voice.recent.map((r) => r.id), limit: S.voice.recent_limit }));
        """
        def result(limit):
            return self.run_js(["upsertCommand"], program.replace("RECORDS", json.dumps(records)).replace("LIMIT", limit))
        self.assertEqual(result("3")["grown"], ["new", "c0", "c1"])  # the service's number, here 3
        self.assertEqual(result("3")["updated"], ["new", "c0", "c1"])
        self.assertEqual(result("3")["limit"], 3)
        self.assertEqual(result("undefined")["grown"], ["new", "c0", "c1", "c2"])  # none known yet: nothing is cut

    def test_pressing_the_small_picture_opens_it_full_size(self):
        program = """
        const opened = [];
        const showPicture = (item, opener) => opened.push([item.url, item.word, opener]);
        const button = pictureRows(RECORD)[0].children[0];
        button.fire("click");
        console.log(JSON.stringify({ opened, key: button.attrs["data-focus-key"] }));
        """.replace("RECORD", json.dumps(RECORD))
        result = self.run_js(ROW_FUNCTIONS, program)
        self.assertEqual(result["opened"], [["/api/voice/pictures/20261108-224739-5/0", "there", result["key"]]])
        self.assertTrue(result["key"].startswith("picture-"))

    # --- the full-size view --------------------------------------------------------------------------------------
    VIEW = ["pictureLine", "pictureGone", "commandKey", "pictureView", "showPicture"]

    def view(self, program):
        return self.run_js(self.VIEW, program)

    ITEM = ('{ number: 0, command: "20261108-224739-5", word: "there", said: "the left wing tip", '
            'url: "/api/voice/pictures/20261108-224739-5/0" }')

    def test_the_full_size_view_is_a_real_dialog_with_a_name_and_the_picture_in_it(self):
        result = self.view(f"""
        showPicture({self.ITEM}, "picture-x");
        const view = S.pictureView;
        console.log(JSON.stringify({{ opened: view.dialog.opened, tag: view.dialog.tag, attrs: view.dialog.attrs,
          src: view.img.src, alt: view.img.alt, caption: view.caption.textContent,
          inPage: document.body.children.length, kids: view.dialog.children.map((c) => c.tag) }}));
        """)
        self.assertEqual((result["opened"], result["tag"], result["inPage"]), (True, "dialog", 1))  # modal: Esc is built in
        self.assertEqual(result["attrs"]["aria-label"], "Picture sent to the AI")
        self.assertEqual(result["src"], "/api/voice/pictures/20261108-224739-5/0")
        self.assertIn("“there”", result["alt"])
        self.assertEqual(result["caption"], "Picture sent for “there”. The AI said: “the left wing tip”.")
        self.assertEqual(result["kids"], ["img", "p", "button"])

    def test_a_click_anywhere_on_it_closes_it_and_the_thumbnail_gets_the_focus_back(self):
        result = self.view(f"""
        const opener = new FakeElement("button");
        document.found['[data-focus-key="picture-x"]'] = opener;
        showPicture({self.ITEM}, "picture-x");
        const view = S.pictureView;
        view.dialog.fire("click");
        console.log(JSON.stringify({{ opened: view.dialog.opened, focused: opener.focused }}));
        """)
        self.assertEqual(result, {"opened": False, "focused": True})

    def test_when_the_picture_turns_out_to_be_gone_the_focus_goes_to_its_command_on_close(self):
        # The small picture is gone from the page by then, so there is nothing of it to give the focus back to.
        result = self.view(f"""
        const row = new FakeElement("li");
        document.found['[data-focus-key="command-20261108-224739-5"]'] = row;
        showPicture({self.ITEM}, "picture-x");
        S.pictureView.img.fire("error");
        S.pictureView.dialog.close();
        console.log(JSON.stringify({{ rowFocused: row.focused }}));
        """)
        self.assertEqual(result, {"rowFocused": True})

    def test_the_small_picture_is_still_the_first_choice_for_the_focus_and_nothing_breaks_without_either(self):
        result = self.view(f"""
        const thumb = new FakeElement("button"), row = new FakeElement("li");
        document.found['[data-focus-key="picture-x"]'] = thumb;
        document.found['[data-focus-key="command-20261108-224739-5"]'] = row;
        showPicture({self.ITEM}, "picture-x");
        S.pictureView.dialog.close();
        const both = {{ thumb: thumb.focused, row: row.focused }};
        document.found = {{}};
        showPicture({self.ITEM}, "picture-x");
        S.pictureView.dialog.close();   // neither is there any more: no error
        console.log(JSON.stringify(both));
        """)
        self.assertEqual(result, {"thumb": True, "row": False})

    def test_a_command_is_found_again_by_a_key_made_from_its_id(self):
        self.assertEqual(self.run_js(["commandKey"], 'console.log(JSON.stringify(commandKey("20261108-224739-5")));'),
                         "command-20261108-224739-5")

    def test_the_full_size_picture_stays_hidden_until_it_has_loaded_so_the_last_one_never_shows_in_its_place(self):
        result = self.view(f"""
        showPicture({self.ITEM}, "picture-x");
        const during = S.pictureView.img.hidden;
        S.pictureView.img.fire("load");
        const after = S.pictureView.img.hidden;
        S.pictureView.dialog.close();
        showPicture({{ ...{self.ITEM}, url: "/api/voice/pictures/20261108-224739-5/1" }}, "picture-y");
        console.log(JSON.stringify({{ during, after, next: S.pictureView.img.hidden }}));
        """)
        self.assertEqual(result, {"during": True, "after": False, "next": True})

    def test_the_second_picture_uses_the_same_dialog(self):
        result = self.view(f"""
        showPicture({self.ITEM}, "picture-x");
        S.pictureView.dialog.close();
        showPicture({{ ...{self.ITEM}, word: "this", url: "/api/voice/pictures/20261108-224739-5/1" }}, "picture-y");
        console.log(JSON.stringify({{ inPage: document.body.children.length, src: S.pictureView.img.src,
                                     caption: S.pictureView.caption.textContent }}));
        """)
        self.assertEqual(result["inPage"], 1)
        self.assertTrue(result["src"].endswith("/1"))
        self.assertIn("“this”", result["caption"])

    def test_a_picture_that_is_gone_by_the_time_it_is_opened_says_so(self):
        result = self.view(f"""
        showPicture({self.ITEM}, "picture-x");
        S.pictureView.img.fire("error");
        console.log(JSON.stringify({{ hidden: S.pictureView.img.hidden, caption: S.pictureView.caption.textContent,
                                     gone: [...S.gonePictures] }}));
        """)
        self.assertTrue(result["hidden"])
        self.assertIn("The picture isn't kept any more", result["caption"])
        self.assertEqual(result["gone"], ["/api/voice/pictures/20261108-224739-5/0"])

    # --- the page itself -----------------------------------------------------------------------------------------
    def test_the_command_row_puts_the_pictures_after_the_gaze_line(self):
        source = APP_JS.read_text()
        self.assertIn('gazeLine(r) ? h("p", { class: "cmd-heard", text: gazeLine(r) }) : null,\n      pictureRows(r),', source)

    def test_the_list_is_drawn_so_that_unlisted_pictures_are_let_go(self):
        source = APP_JS.read_text()
        self.assertIn("renderingPictures(() => recent.length", source)  # the whole list branch, the empty one too
        self.assertIn("box.append(commandList(recent,", source)

    def test_each_command_row_can_be_given_the_focus_by_its_key(self):
        source = APP_JS.read_text()
        self.assertIn('"data-focus-key": commandKey(r.id),', source)
        self.assertIn('tabindex: "-1"', source)  # focusable by the script, not a stop on the way round the page
        rule = re.search(r"^\.cmd:focus-visible \{(.*?)\}", (STATIC / "app.css").read_text(), re.MULTILINE).group(1)
        # The list scrolls and clips what pokes out of it, so the ring is drawn inside the row, not around it.
        self.assertIn("outline-offset: -2px", rule)

    def test_the_command_rows_light_is_written_as_it_always_was(self):
        self.assertIn('const led = { done: "live", failed: "fault", stopped: "off" }[r.stage]', APP_JS.read_text())

    def test_what_the_ai_said_is_only_ever_put_in_as_text(self):
        source = APP_JS.read_text()
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(unsafe, source)

    def test_the_styles_cover_the_small_picture_the_dialog_and_its_backdrop(self):
        css = (STATIC / "app.css").read_text()
        for selector in (".cmd-picture", ".cmd-thumb", ".cmd-thumb:focus-visible", ".picture-view", ".picture-view::backdrop",
                         ".picture-view-img"):
            self.assertIn(selector, css)
        self.assertNotIn("filter: blur", css)  # no floating glass (see DESIGN.md)


TERMINAL_PAGE = """
class Node {}
class FakeElement extends Node {
  constructor(tag, id = "") {
    super();
    this.tag = tag; this.id = id; this.attrs = {}; this.children = []; this.listeners = {}; this.className = "";
    this.textContent = ""; this.dataset = {}; this.hidden = false; this.disabled = false; this.parent = null;
    this.scrollTop = 0; this.clientHeight = 100; this.focused = 0;
  }
  get scrollHeight() { return this.children.length * 20; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  append(...kids) { for (const kid of kids) { if (kid instanceof FakeElement) kid.parent = this; this.children.push(kid); } }
  replaceChildren() { this.children = []; this.scrollTop = 0; }
  get firstChild() { return this.children[0] || null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((kid) => kid !== this); }
  fire(type) { (this.listeners[type] || []).forEach((handler) => handler({ target: this })); }
  focus() { this.focused += 1; }
}
const ids = ["terminal", "terminal-run", "terminal-status", "terminal-led", "terminal-stop", "terminal-close",
             "terminal-log", "terminal-jump", "announcer"];
const page = Object.fromEntries(ids.map((id) => [id, new FakeElement("x", id)]));
page.terminal.hidden = true;
page["terminal-stop"].hidden = true;
page["terminal-jump"].hidden = true;
const document = {
  getElementById: (id) => page[id] || null,
  createElement: (tag) => new FakeElement(tag),
  createTextNode: (text) => ({ text }),
  querySelector: () => null,
  activeElement: null,
};
const ran = [];
const runAction = (cid, aid) => { ran.push([cid, aid]); };
let busyNow = false;
const isBusy = () => busyNow;
const runningJob = () => (busyNow ? { action: "stop" } : null);
const asked = [];
let answers = [];
const getJSON = async (path) => { asked.push(path); const next = answers.shift(); if (next instanceof Error) throw next; return next; };
const el = (id) => page[id];
const texts = () => page["terminal-log"].children.map((line) => line.textContent);
// A run name has a serial of its own, so two runs with different names are different runs; pass a serial to make
// the same name another run (resume, overwrite).
const serials = {};
const serialFor = (run) => { if (!(run in serials)) serials[run] = 1000 + Object.keys(serials).length; return serials[run]; };
const say = (run, n, text, serial = serialFor(run)) => onTrainingOutput({ run_id: run, serial, n, text });
const answer = (run, lines, serial = serialFor(run)) => ({ run_id: run, serial, first: lines.length ? lines[0].n : 0, lines });
const trainer = (state, run = "run1", extra = {}) => ({
  id: "mlagents", name: "ML-Agents", health: "ready", summary: extra.summary || `Training ${run}: step 10000, mean reward 0.42.`,
  details: state === "idle" ? [] : [["Last run", `${run} · ${state}`]],
  actions: extra.actions || (["waiting", "training"].includes(state) ? [{ id: "stop", label: "Stop training" }] : []),
  run: state === "idle" ? null
    : { run_id: run, serial: extra.serial === undefined ? serialFor(run) : extra.serial, state, listening: extra.listening === undefined ? true : extra.listening },
});
const setTrainer = (comp) => { S.state = { components: [comp] }; };
const store = {};
let storageBroken = false;
const sessionStorage = {
  getItem: (key) => { if (storageBroken) throw new Error("no storage"); return key in store ? store[key] : null; },
  setItem: (key, value) => { if (storageBroken) throw new Error("no storage"); store[key] = String(value); },
};
const refreshed = [];
const refresh = () => { refreshed.push(1); };
"""

TERMINAL_CONSTS = ("TERM_MAX", "TERM_LIVE", "TERM_LEDS", "TERM_SAYS", "TERM_STARTING", "TERM_IDLE", "TERM_CLOSED_KEY",
                   "TERM_EXCEPTION")
TERMINAL_FUNCTIONS = ("h", "$", "component", "components", "announce", "termKind", "trainerRun", "terminalStatus",
                      "startTerminalRun", "termAppend", "onTrainingOutput", "takeOutput", "fillTerminal", "noteTrainer",
                      "renderTerminal", "rememberClosed", "recallClosed", "closeTerminal", "jumpToLatest", "onTerminalScroll",
                      "wireTerminal", "onComponentUpdated")


def block(source: str, name: str) -> str:
    """The `const NAME = …;` (on one line or several), `const $ = …` or `function NAME(…) {…}` of app.js, whole."""
    if name == "$" or name.startswith("TERM_"):
        start = re.search(rf"^const {re.escape(name)} = .*$", source, re.MULTILINE)
        assert start, f"{name} is not where this test expects it"
        if start.group(0).split(" // ")[0].rstrip().endswith(";"):
            return start.group(0) + "\n"
        match = re.compile(r"^.*?^[}\]];\n", re.DOTALL | re.MULTILINE).match(source, start.start())
    else:
        match = re.search(rf"^(?:async )?function {name}\(.*?^\}}\n", source, re.DOTALL | re.MULTILINE)
    assert match, f"{name} is not where this test expects it"
    return match.group(0)


def terminal(body: str, stubs: str = ""):
    """Run `body` against the real terminal-panel code in app.js and a small stand-in page. `body` sets `out`."""
    source = APP_JS.read_text()
    state = re.search(r"^const S = \{.*?^\};\n", source, re.DOTALL | re.MULTILINE)
    assert state, "S is not where this test expects it"
    parts = [state.group(0), *(block(source, name) for name in TERMINAL_CONSTS),
             *(block(source, name) for name in TERMINAL_FUNCTIONS)]
    program = (f"{TERMINAL_PAGE}\n{chr(10).join(parts)}\n{stubs}\nlet out = null;\n"
               f"(async () => {{\n{body}\n}})().then(() => console.log(JSON.stringify(out)));\n")
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


TERM_STARTING_TEXT = "ML-Agents is starting up. Press Play once it says it is waiting for Unity."


@unittest.skipUnless(NODE, "node isn't installed")
class TerminalPanelTest(unittest.TestCase):
    """The ML-Agents terminal panel: the real functions of app.js against a stand-in page."""

    def test_it_stays_hidden_while_there_is_no_run(self):
        out = terminal("""
          setTrainer(trainer("idle", null, { summary: "Ready to train" }));
          renderTerminal();
          out = { hidden: el("terminal").hidden };
        """)
        self.assertEqual(out, {"hidden": True})

    def test_the_first_event_shows_the_panel_with_the_run_and_the_line(self):
        out = terminal("""
          setTrainer(trainer("waiting", "Mover-1"));
          const before = el("terminal").hidden;
          say("Mover-1", 1, "[INFO] Listening on port 5004.");
          out = { before, hidden: el("terminal").hidden, run: el("terminal-run").textContent, lines: texts() };
        """)
        self.assertEqual(out, {"before": True, "hidden": False, "run": "Mover-1", "lines": ["[INFO] Listening on port 5004."]})

    def test_the_first_event_shows_it_even_before_the_page_knows_the_trainer(self):
        out = terminal("""
          S.state = null;
          say("Mover-1", 1, "hello");
          out = { hidden: el("terminal").hidden, run: el("terminal-run").textContent };
        """)
        self.assertEqual(out, {"hidden": False, "run": "Mover-1"})

    def test_lines_are_added_in_order_and_one_it_already_has_is_skipped(self):
        out = terminal("""
          for (const [n, text] of [[1, "a"], [2, "b"], [2, "b again"], [1, "a again"], [3, "c"], [3, "c"], [4, "d"]]) say("run1", n, text);
          out = texts();
        """)
        self.assertEqual(out, ["a", "b", "c", "d"])

    def test_a_blank_line_is_a_line(self):
        out = terminal("""
          say("run1", 1, ""); say("run1", 2, "   "); say("run1", 3, "x");
          out = texts();
        """)
        self.assertEqual(out, ["", "   ", "x"])

    def test_a_line_with_markup_in_it_shows_literally(self):
        out = terminal("""
          say("run1", 1, "<b>bold</b> & <img src=x onerror=alert(1)>");
          const line = el("terminal-log").children[0];
          out = { text: line.textContent, kids: line.children.length, tag: line.tag };
        """)
        self.assertEqual(out, {"text": "<b>bold</b> & <img src=x onerror=alert(1)>", "kids": 0, "tag": "div"})

    def test_only_the_last_1000_lines_stay_on_the_page(self):
        out = terminal("""
          for (let n = 1; n <= 1200; n += 1) say("run1", n, `line ${n}`);
          const all = texts();
          out = { count: all.length, first: all[0], last: all[all.length - 1], max: TERM_MAX };
        """)
        self.assertEqual(out, {"count": 1000, "first": "line 201", "last": "line 1200", "max": 1000})

    def test_a_batch_that_is_too_long_is_cut_to_the_last_1000_too(self):
        out = terminal("""
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: Array.from({ length: 1500 }, (_, i) => ({ n: i + 1, text: `line ${i + 1}` })) }];
          await fillTerminal();
          const all = texts();
          out = { count: all.length, first: all[0], last: all[all.length - 1] };
        """)
        self.assertEqual(out, {"count": 1000, "first": "line 501", "last": "line 1500"})

    def test_close_hides_it_until_another_run_starts(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          say("run1", 1, "a");
          el("terminal-close").fire("click");
          const closed = el("terminal").hidden;
          say("run1", 2, "b");
          say("run1", 3, "c");
          renderTerminal();
          const stillClosed = el("terminal").hidden;
          setTrainer(trainer("waiting", "run2"));
          say("run2", 1, "x");
          out = { closed, stillClosed, hidden: el("terminal").hidden, run: el("terminal-run").textContent, lines: texts() };
        """, "wireTerminal();")
        self.assertEqual(out, {"closed": True, "stillClosed": True, "hidden": False, "run": "run2", "lines": ["x"]})

    def test_a_closed_run_stays_closed_when_the_page_catches_up_or_the_trainer_state_changes(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          say("run1", 1, "a");
          closeTerminal();
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 1, text: "a" }, { n: 2, text: "b" }] }];
          await fillTerminal();
          setTrainer(trainer("finished", "run1"));
          renderTerminal();
          out = { hidden: el("terminal").hidden, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": True, "lines": ["a"]})

    def test_a_late_line_of_a_run_that_was_closed_does_not_take_the_panel_back_from_the_run_after_it(self):
        out = terminal("""
          say("run1", 1, "a");
          closeTerminal();
          say("run2", 1, "b");
          say("run1", 2, "late line of the closed run");
          out = { hidden: el("terminal").hidden, run: el("terminal-run").textContent, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": False, "run": "run2", "lines": ["b"]})

    def test_after_close_the_focus_goes_to_the_voice_keys_which_are_still_there(self):
        out = terminal("""
          const asked = [];
          const target = new FakeElement("button");
          document.querySelector = (selector) => { asked.push(selector); return target; };
          say("run1", 1, "a");
          closeTerminal();
          out = { focused: target.focused, asked };
        """)
        self.assertEqual(out["focused"], 1)
        self.assertEqual(len(out["asked"]), 1)
        self.assertIn("voice", out["asked"][0])

    def test_close_with_nothing_shown_does_nothing(self):
        out = terminal("""
          closeTerminal();
          say("run1", 1, "a");
          out = { hidden: el("terminal").hidden, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": False, "lines": ["a"]})

    def test_stop_is_there_only_while_the_trainer_offers_it(self):
        out = terminal("""
          const seen = [];
          for (const state of ["waiting", "training", "stopping", "stopped", "finished", "failed"]) {
            setTrainer(trainer(state));
            say("run1", seen.length + 1, state);
            seen.push([state, !el("terminal-stop").hidden]);
          }
          out = seen;
        """)
        self.assertEqual(out, [["waiting", True], ["training", True], ["stopping", False], ["stopped", False],
                               ["finished", False], ["failed", False]])

    def test_pressing_stop_runs_the_ml_agents_stop_action(self):
        out = terminal("""
          setTrainer(trainer("training"));
          say("run1", 1, "a");
          el("terminal-stop").fire("click");
          out = ran;
        """, "wireTerminal();")
        self.assertEqual(out, [["mlagents", "stop"]])

    def test_stop_waits_while_a_job_is_busy_and_says_so(self):
        out = terminal("""
          setTrainer(trainer("training"));
          say("run1", 1, "a");
          const idle = { disabled: el("terminal-stop").disabled, label: el("terminal-stop").textContent };
          busyNow = true;
          renderTerminal();
          out = { idle, busy: { disabled: el("terminal-stop").disabled, label: el("terminal-stop").textContent } };
        """)
        self.assertEqual(out, {"idle": {"disabled": False, "label": "Stop training"},
                               "busy": {"disabled": True, "label": "Stopping…"}})

    def test_the_status_line_is_the_units_summary_and_the_light_follows_the_state(self):
        out = terminal("""
          const rows = [];
          for (const state of ["waiting", "training", "stopping", "finished", "failed"]) {
            setTrainer(trainer(state, "run1", { summary: `summary of ${state}` }));
            say("run1", rows.length + 1, state);
            rows.push([state, el("terminal-status").textContent, el("terminal-led").dataset.led]);
          }
          out = rows;
        """)
        self.assertEqual(out, [["waiting", "summary of waiting", "standby"], ["training", "summary of training", "live"],
                               ["stopping", "summary of stopping", "busy"], ["finished", "summary of finished", "off"],
                               ["failed", "summary of failed", "fault"]])

    def test_the_status_line_says_starting_up_until_the_trainer_is_listening(self):
        out = terminal("""
          const rows = [];
          setTrainer(trainer("waiting", "run1", { listening: false, summary: "Waiting for Unity: press Play in the Editor to start training." }));
          say("run1", 1, "banner");
          rows.push([el("terminal-status").textContent, el("terminal-led").dataset.led]);
          setTrainer(trainer("waiting", "run1", { listening: true, summary: "Waiting for Unity: press Play in the Editor to start training." }));
          renderTerminal();
          rows.push([el("terminal-status").textContent, el("terminal-led").dataset.led]);
          // not listening any more, but not waiting either: the unit's own words
          setTrainer(trainer("training", "run1", { listening: false, summary: "Training run1: step 10000, mean reward 0.42." }));
          renderTerminal();
          rows.push([el("terminal-status").textContent, el("terminal-led").dataset.led]);
          out = rows;
        """)
        self.assertEqual(out, [[TERM_STARTING_TEXT, "busy"],
                               ["Waiting for Unity: press Play in the Editor to start training.", "standby"],
                               ["Training run1: step 10000, mean reward 0.42.", "live"]])

    def test_a_trainer_that_is_running_shows_the_panel_on_load_even_with_no_lines_yet(self):
        out = terminal("""
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 0, lines: [] }];
          await fillTerminal();
          const beforeState = el("terminal").hidden;
          setTrainer(trainer("waiting", "run1"));
          renderTerminal();
          out = { beforeState, hidden: el("terminal").hidden, run: el("terminal-run").textContent };
        """)
        self.assertEqual(out, {"beforeState": True, "hidden": False, "run": "run1"})

    def test_a_running_trainer_shows_the_panel_with_its_run_before_any_output_has_been_heard(self):
        out = terminal("""
          setTrainer(trainer("training", "run7"));
          renderTerminal();
          out = { hidden: el("terminal").hidden, run: el("terminal-run").textContent, stop: !el("terminal-stop").hidden };
        """)
        self.assertEqual(out, {"hidden": False, "run": "run7", "stop": True})

    def test_a_finished_run_with_nothing_kept_does_not_show_an_empty_panel(self):
        out = terminal("""
          setTrainer(trainer("finished", "run1"));
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 0, lines: [] }];
          await fillTerminal();
          out = { hidden: el("terminal").hidden };
        """)
        self.assertEqual(out, {"hidden": True})

    # --- catching up -------------------------------------------------------------------------------------------
    def test_on_load_it_asks_for_everything_and_shows_what_comes_back(self):
        out = terminal("""
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 1, text: "a" }, { n: 2, text: "b" }] }];
          await fillTerminal();
          out = { asked, lines: texts(), hidden: el("terminal").hidden, run: el("terminal-run").textContent };
        """)
        self.assertEqual(out, {"asked": ["/api/mlagents/output?after=0"], "lines": ["a", "b"], "hidden": False, "run": "run1"})

    def test_after_a_reconnect_it_asks_only_for_what_came_after_the_last_line_shown(self):
        out = terminal("""
          for (const n of [1, 2, 3, 4, 5, 6, 7]) say("run1", n, `l${n}`);
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 8, text: "l8" }, { n: 9, text: "l9" }] }];
          await fillTerminal();
          out = { asked, lines: texts() };
        """)
        self.assertEqual(out["asked"], ["/api/mlagents/output?after=7"])
        self.assertEqual(out["lines"], ["l1", "l2", "l3", "l4", "l5", "l6", "l7", "l8", "l9"])

    def test_lines_the_events_already_brought_are_not_shown_twice(self):
        out = terminal("""
          say("run1", 1, "l1");
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 1, text: "l1" }, { n: 2, text: "l2" }, { n: 3, text: "l3" }] }];
          const pending = fillTerminal();
          say("run1", 2, "l2");  // arrives while the answer is on its way
          await pending;
          out = texts();
        """)
        self.assertEqual(out, ["l1", "l2", "l3"])

    def test_a_different_run_clears_the_page_and_is_asked_for_again_from_the_start(self):
        out = terminal("""
          for (const n of [1, 2, 3, 4, 5]) say("old", n, `old ${n}`);
          answers = [
            { run_id: "new", serial: serialFor("new"), first: 1, lines: [] },
            { run_id: "new", serial: serialFor("new"), first: 1, lines: [{ n: 1, text: "new 1" }, { n: 2, text: "new 2" }] },
          ];
          await fillTerminal();
          out = { asked, lines: texts(), run: el("terminal-run").textContent };
        """)
        self.assertEqual(out, {"asked": ["/api/mlagents/output?after=5", "/api/mlagents/output?after=0"],
                               "lines": ["new 1", "new 2"], "run": "new"})

    def test_a_different_run_that_has_more_lines_than_were_shown_is_not_cut_short(self):
        out = terminal("""
          for (const n of [1, 2]) say("old", n, `old ${n}`);
          answers = [
            { run_id: "new", serial: serialFor("new"), first: 1, lines: [{ n: 3, text: "new 3" }, { n: 4, text: "new 4" }] },
            { run_id: "new", serial: serialFor("new"), first: 1, lines: [{ n: 1, text: "new 1" }, { n: 2, text: "new 2" }, { n: 3, text: "new 3" },
                                               { n: 4, text: "new 4" }] },
          ];
          await fillTerminal();
          out = texts();
        """)
        self.assertEqual(out, ["new 1", "new 2", "new 3", "new 4"])

    def test_a_failed_fetch_changes_nothing_and_can_be_tried_again(self):
        out = terminal("""
          say("run1", 1, "a");
          answers = [new Error("HTTP 500")];
          await fillTerminal();
          const after = texts();
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 2, text: "b" }] }];
          await fillTerminal();
          out = { after, lines: texts(), asked };
        """)
        self.assertEqual(out, {"after": ["a"], "lines": ["a", "b"],
                               "asked": ["/api/mlagents/output?after=1", "/api/mlagents/output?after=1"]})

    def test_no_run_at_all_shows_nothing(self):
        out = terminal("""
          answers = [{ run_id: null, first: 0, lines: [] }];
          await fillTerminal();
          out = { hidden: el("terminal").hidden, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": True, "lines": []})

    def test_a_missed_line_is_fetched_instead_of_leaving_a_hole(self):
        out = terminal("""
          say("run1", 1, "l1"); say("run1", 2, "l2");
          answers = [{ run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 3, text: "l3" }, { n: 4, text: "l4" }, { n: 5, text: "l5" }] }];
          say("run1", 5, "l5");  // 3 and 4 never came
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { asked, lines: texts() };
        """)
        self.assertEqual(out, {"asked": ["/api/mlagents/output?after=2"], "lines": ["l1", "l2", "l3", "l4", "l5"]})

    def test_a_run_started_again_under_the_same_name_clears_the_screen_and_shows_only_the_new_lines(self):
        """The reviewer's case: the page last saw the run training, and the same name is resumed before it saw "ended"."""
        out = terminal("""
          setTrainer(trainer("training", "run1", { serial: 1 }));
          renderTerminal();
          for (let n = 1; n <= 5; n += 1) say("run1", n, `old ${n}`, 1);
          await new Promise((resolve) => setTimeout(resolve, 20));
          asked.length = 0;  // what the page asked when it first saw the run is not what this is about
          setTrainer(trainer("waiting", "run1", { serial: 2 }));
          for (let n = 1; n <= 4; n += 1) say("run1", n, `NEW ${n}`, 2);  // numbers 1 to 4 are ones the page already has
          renderTerminal();
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { asked, lines: texts(), run: el("terminal-run").textContent, hidden: el("terminal").hidden };
        """)
        self.assertEqual(out, {"asked": [], "lines": ["NEW 1", "NEW 2", "NEW 3", "NEW 4"], "run": "run1", "hidden": False})

    def test_a_run_started_again_under_the_same_name_is_noticed_from_the_unit_alone_and_its_lines_are_fetched(self):
        out = terminal("""
          setTrainer(trainer("training", "run1", { serial: 1 }));
          renderTerminal();
          for (let n = 1; n <= 4; n += 1) say("run1", n, `old ${n}`, 1);
          await new Promise((resolve) => setTimeout(resolve, 20));
          asked.length = 0;
          answers = [answer("run1", [{ n: 1, text: "NEW 1" }, { n: 2, text: "NEW 2" }], 2)];
          setTrainer(trainer("waiting", "run1", { serial: 2 }));  // no ended state in between, and no line yet
          renderTerminal();
          const cleared = texts();
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { cleared, asked, lines: texts(), hidden: el("terminal").hidden };
        """)
        self.assertEqual(out, {"cleared": [], "asked": ["/api/mlagents/output?after=0"], "lines": ["NEW 1", "NEW 2"],
                               "hidden": False})

    def test_an_answer_about_the_run_the_page_has_already_left_is_not_taken_back(self):
        out = terminal("""
          answers = [answer("run1", [{ n: 1, text: "old 1" }], 1)];
          setTrainer(trainer("training", "run1", { serial: 1 }));
          renderTerminal();  // the first sighting asks for the run's lines; the answer will be about serial 1
          setTrainer(trainer("waiting", "run1", { serial: 2 }));
          say("run1", 1, "NEW 1", 2);  // before the answer is in, the page has moved on to the next run
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { lines: texts(), run: el("terminal-run").textContent, serial: S.term.serial };
        """)
        self.assertEqual(out, {"lines": ["NEW 1"], "run": "run1", "serial": 2})

    def test_a_run_going_back_to_waiting_in_the_middle_is_not_a_new_run(self):
        # Unity lost its connection and training waits for Play again: the same run, so its lines stay.
        out = terminal("""
          setTrainer(trainer("training"));
          renderTerminal();
          for (const n of [1, 2, 3]) say("run1", n, `l${n}`);
          await new Promise((resolve) => setTimeout(resolve, 20));
          asked.length = 0;
          setTrainer(trainer("waiting", "run1", { listening: false }));
          renderTerminal();
          say("run1", 4, "l4");
          out = { asked, lines: texts() };
        """)
        self.assertEqual(out, {"asked": [], "lines": ["l1", "l2", "l3", "l4"]})

    def test_a_closed_run_that_is_resumed_under_the_same_name_opens_the_panel_again(self):
        out = terminal("""
          setTrainer(trainer("stopped", "run1", { serial: 1 }));
          renderTerminal();
          say("run1", 1, "a", 1);
          closeTerminal();
          const closed = el("terminal").hidden;
          say("run1", 2, "late line of the closed run", 1);  // still the same run: stays closed
          const stillClosed = el("terminal").hidden;
          setTrainer(trainer("waiting", "run1", { serial: 2 }));
          say("run1", 1, "b", 2);
          out = { closed, stillClosed, hidden: el("terminal").hidden, lines: texts(), run: el("terminal-run").textContent };
        """)
        self.assertEqual(out, {"closed": True, "stillClosed": True, "hidden": False, "lines": ["b"], "run": "run1"})

    def test_a_closed_run_that_is_resumed_opens_the_panel_from_the_unit_alone_too(self):
        out = terminal("""
          setTrainer(trainer("stopped", "run1", { serial: 1 }));
          say("run1", 1, "a", 1);
          closeTerminal();
          answers = [answer("run1", [{ n: 1, text: "b" }], 2)];
          setTrainer(trainer("waiting", "run1", { serial: 2 }));
          renderTerminal();
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { hidden: el("terminal").hidden, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": False, "lines": ["b"]})

    def test_closing_remembers_the_serial_and_not_the_run_name(self):
        out = terminal("""
          say("run1", 1, "a", 41);
          closeTerminal();
          out = { dismissed: S.term.dismissed, serial: S.term.serial, stored: store[TERM_CLOSED_KEY] };
        """)
        self.assertEqual(out, {"dismissed": 41, "serial": 41, "stored": "41"})

    def test_a_reload_keeps_the_panel_closed_for_that_run_and_opens_it_for_the_next(self):
        out = terminal("""
          say("run1", 1, "a", 7);
          closeTerminal();
          // the page is loaded again: nothing is in memory but the browser's storage
          S.term.runId = null; S.term.serial = null; S.term.last = 0; S.term.shown = false; S.term.dismissed = null; S.term.seen = null;
          el("terminal-log").replaceChildren();
          S.term.dismissed = recallClosed();
          const recalled = S.term.dismissed;
          setTrainer(trainer("training", "run1", { serial: 7 }));
          answers = [answer("run1", [{ n: 1, text: "a" }, { n: 2, text: "b" }], 7)];
          renderTerminal();
          await fillTerminal();
          const sameRun = { hidden: el("terminal").hidden, lines: texts() };
          setTrainer(trainer("waiting", "run1", { serial: 8 }));  // the same name resumed
          say("run1", 1, "fresh", 8);
          out = { recalled, sameRun, resumed: { hidden: el("terminal").hidden, lines: texts() } };
        """)
        self.assertEqual(out, {"recalled": 7, "sameRun": {"hidden": True, "lines": []},
                               "resumed": {"hidden": False, "lines": ["fresh"]}})

    def test_a_browser_that_will_not_keep_the_choice_still_closes_the_panel_and_loads(self):
        out = terminal("""
          storageBroken = true;
          say("run1", 1, "a");
          closeTerminal();
          out = { hidden: el("terminal").hidden, recalled: recallClosed() };
        """)
        self.assertEqual(out, {"hidden": True, "recalled": None})

    def test_nothing_or_junk_in_storage_is_no_closed_run(self):
        out = terminal("""
          const empty = recallClosed();
          const kept = [];
          for (const junk of ["", "abc", "-1", "1.5", "1e3", "null", "7;8"]) { store[TERM_CLOSED_KEY] = junk; kept.push(recallClosed()); }
          out = { empty, kept };
        """)
        self.assertEqual(out, {"empty": None, "kept": [None] * 7})

    def test_only_one_fetch_is_out_at_a_time_and_one_more_follows_if_asked_meanwhile(self):
        out = terminal("""
          answers = [
            { run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 1, text: "l1" }] },
            { run_id: "run1", serial: serialFor("run1"), first: 1, lines: [{ n: 2, text: "l2" }] },
          ];
          const a = fillTerminal();
          const b = fillTerminal();
          await a; await b;
          out = { asked, lines: texts() };
        """)
        self.assertEqual(out, {"asked": ["/api/mlagents/output?after=0", "/api/mlagents/output?after=1"],
                               "lines": ["l1", "l2"]})

    def test_events_that_are_not_lines_are_ignored(self):
        out = terminal("""
          for (const bad of [null, {}, { run_id: "r", n: "1", text: "x" }, { run_id: "r", n: 1 }, { run_id: 4, n: 1, text: "x" }]) {
            onTrainingOutput(bad);
          }
          out = { hidden: el("terminal").hidden, lines: texts() };
        """)
        self.assertEqual(out, {"hidden": True, "lines": []})

    # --- the unit's own news -----------------------------------------------------------------------------------
    def test_a_component_update_for_the_trainer_reaches_the_panel_at_once(self):
        out = terminal("""
          setTrainer(trainer("training", "run1", { summary: "Training run1: step 10000, mean reward 0.42." }));
          say("run1", 1, "[INFO] a line");
          const before = { status: el("terminal-status").textContent, stop: !el("terminal-stop").hidden };
          // the payload of a component.updated event; nothing has been fetched, and no timer has run
          onComponentUpdated(trainer("stopping", "run1", { summary: "Stopping training run1. Waiting for it to save the model." }));
          const during = { status: el("terminal-status").textContent, stop: !el("terminal-stop").hidden, led: el("terminal-led").dataset.led,
                           stored: component("mlagents").summary, asked };
          onComponentUpdated(trainer("stopped", "run1", { summary: "Training run1 stopped. Its model was saved and is ready to use." }));
          out = { before, during, after: { status: el("terminal-status").textContent, led: el("terminal-led").dataset.led, announced: el("announcer").textContent } };
        """)
        self.assertEqual(out, {
            "before": {"status": "Training run1: step 10000, mean reward 0.42.", "stop": True},
            "during": {"status": "Stopping training run1. Waiting for it to save the model.", "stop": False, "led": "busy",
                       "stored": "Stopping training run1. Waiting for it to save the model.", "asked": []},
            "after": {"status": "Training run1 stopped. Its model was saved and is ready to use.", "led": "off",
                      "announced": "ML-Agents training stopped."},
        })

    def test_a_component_update_with_a_new_run_clears_and_fills_the_panel_without_waiting_for_a_refresh(self):
        out = terminal("""
          setTrainer(trainer("training", "run1", { serial: 1 }));
          say("run1", 1, "old", 1);
          answers = [answer("run1", [{ n: 1, text: "new" }], 2)];
          onComponentUpdated(trainer("waiting", "run1", { serial: 2, listening: false }));
          const cleared = { lines: texts(), status: el("terminal-status").textContent };
          await new Promise((resolve) => setTimeout(resolve, 20));
          out = { cleared, lines: texts(), asked, refreshed: refreshed.length };
        """)
        self.assertEqual(out["cleared"], {"lines": [], "status": TERM_STARTING_TEXT})
        self.assertEqual(out["lines"], ["new"])
        self.assertEqual(out["asked"], ["/api/mlagents/output?after=0"])
        self.assertEqual(out["refreshed"], 0)  # the refresh is a debounce: it has not run yet

    def test_the_general_refresh_still_follows_a_changed_component_a_moment_later(self):
        out = terminal("""
          setTrainer(trainer("training", "run1", { summary: "one" }));
          onComponentUpdated(trainer("training", "run1", { summary: "two" }));
          onComponentUpdated(trainer("training", "run1", { summary: "three" }));
          const early = refreshed.length;
          await new Promise((resolve) => setTimeout(resolve, 500));
          out = { early, late: refreshed.length };
        """)
        self.assertEqual(out, {"early": 0, "late": 1})  # one refresh for both

    def test_an_update_that_changes_nothing_the_page_shows_asks_for_no_refresh(self):
        out = terminal("""
          setTrainer(trainer("training", "run1", { summary: "same" }));
          onComponentUpdated(trainer("training", "run1", { summary: "same" }));
          await new Promise((resolve) => setTimeout(resolve, 500));
          out = refreshed.length;
        """)
        self.assertEqual(out, 0)

    def test_an_update_for_another_unit_is_left_to_the_refresh(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          const other = { id: "voice", name: "Voice", health: "ready", summary: "new", details: [], actions: [] };
          onComponentUpdated(other);
          out = { components: components().map((c) => c.id), refreshAsked: true };
          await new Promise((resolve) => setTimeout(resolve, 500));
          out.refreshed = refreshed.length;
        """)
        self.assertEqual(out, {"components": ["mlagents"], "refreshAsked": True, "refreshed": 1})

    def test_an_update_before_the_page_has_its_first_state_is_left_to_the_refresh(self):
        out = terminal("""
          S.state = null;
          onComponentUpdated(trainer("training", "run1"));
          await new Promise((resolve) => setTimeout(resolve, 500));
          out = { state: S.state, hidden: el("terminal").hidden, refreshed: refreshed.length };
        """)
        self.assertEqual(out, {"state": None, "hidden": True, "refreshed": 1})

    # --- focus ---------------------------------------------------------------------------------------------------
    def test_the_focus_moves_to_the_log_when_stop_goes_away_under_it(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          say("run1", 1, "a");
          document.activeElement = el("terminal-stop");
          setTrainer(trainer("stopping", "run1"));  // the unit stops offering Stop
          renderTerminal();
          out = { stopHidden: el("terminal-stop").hidden, logFocus: el("terminal-log").focused };
        """)
        self.assertEqual(out, {"stopHidden": True, "logFocus": 1})

    def test_the_focus_is_left_alone_when_it_was_not_on_stop(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          say("run1", 1, "a");
          document.activeElement = el("terminal-close");
          setTrainer(trainer("stopping", "run1"));
          renderTerminal();
          document.activeElement = null;
          setTrainer(trainer("stopped", "run1"));
          renderTerminal();
          out = el("terminal-log").focused;
        """)
        self.assertEqual(out, 0)

    def test_the_focus_stays_on_stop_while_it_is_still_offered(self):
        out = terminal("""
          setTrainer(trainer("training", "run1"));
          say("run1", 1, "a");
          document.activeElement = el("terminal-stop");
          for (let n = 2; n <= 6; n += 1) say("run1", n, `l${n}`);
          setTrainer(trainer("waiting", "run1"));
          renderTerminal();
          out = { logFocus: el("terminal-log").focused, stopHidden: el("terminal-stop").hidden };
        """)
        self.assertEqual(out, {"logFocus": 0, "stopHidden": False})

    # --- looking at it -----------------------------------------------------------------------------------------
    def test_lines_are_coloured_by_what_they_are(self):
        warn = ["[WARNING] Restarting worker[0] after 'The Unity environment took too long to respond.'"]
        error = [
            "[ERROR] UnityEnvironment worker 0: environment raised an unexpected exception.",
            "Traceback (most recent call last):",
            "mlagents_envs.exception.UnityTimeOutException: The Unity environment took too long to respond.",
            "OSError: [Errno 48] Address already in use",
            "ValueError: something unexpected went wrong",
            "mlagents.trainers.exception.UnityTrainerException: Previous data from this run ID was found.",
        ]
        progress = ["[INFO] Mover. Step: 10000. Time Elapsed: 20.100 s. Mean Reward: 0.500. Std of Reward: 0.200. Training."]
        plain = [
            "[INFO] Listening on port 5004. Start training by pressing the Play button in the Unity Editor.",
            "[INFO] Connected new brain: Mover?team=0", "", "            ┐  ╖",
            '  File "/x/venv/bin/mlagents-learn", line 12, in <module>', "    sys.exit(main())",
            "\t The environment does not need user interaction to launch",
            "[INFO] Exported results/run1/Mover/Mover-50000.onnx", "An Error happened here",
            "  OSError: indented lines are not exception lines", "During handling of the above exception, another exception occurred:",
        ]
        cases = [(text, "warn") for text in warn] + [(text, "error") for text in error] + \
                [(text, "progress") for text in progress] + [(text, None) for text in plain]
        out = terminal(f"const cases = {json.dumps(cases)};\nout = cases.map(([text]) => termKind(text));")
        self.assertEqual(out, [kind for _, kind in cases])

    def test_each_line_carries_its_colour_as_data_the_styles_pick_up(self):
        out = terminal("""
          say("run1", 1, "[WARNING] careful"); say("run1", 2, "plain"); say("run1", 3, "[ERROR] bad");
          say("run1", 4, "[INFO] Mover. Step: 1. Time Elapsed: 2.000 s. Training.");
          out = page["terminal-log"].children.map((line) => [line.className, line.attrs["data-kind"] || null]);
        """)
        self.assertEqual(out, [["term-line", "warn"], ["term-line", None], ["term-line", "error"], ["term-line", "progress"]])

    # --- following the newest line -----------------------------------------------------------------------------
    def test_it_follows_the_newest_line(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("run1", n, `l${n}`);
          const log = el("terminal-log");
          out = { atBottom: log.scrollTop >= log.scrollHeight - log.clientHeight, jump: el("terminal-jump").hidden };
        """)
        self.assertEqual(out, {"atBottom": True, "jump": True})

    def test_the_panel_opening_after_lines_arrived_still_starts_at_the_newest(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("run1", n, `l${n}`);
          const log = el("terminal-log");
          el("terminal").hidden = true;   // a hidden box can't be scrolled, so its position is lost
          log.scrollTop = 0;
          renderTerminal();
          out = { hidden: el("terminal").hidden, atBottom: log.scrollTop >= log.scrollHeight - log.clientHeight };
        """)
        self.assertEqual(out, {"hidden": False, "atBottom": True})

    def test_scrolling_up_stops_following_and_offers_jump_to_latest(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("run1", n, `l${n}`);
          const log = el("terminal-log");
          log.scrollTop = 40;  // the reader scrolled up
          log.fire("scroll");
          const top = log.scrollTop;
          say("run1", 31, "l31");
          out = { stayed: log.scrollTop === top, jump: el("terminal-jump").hidden, count: texts().length };
        """, "wireTerminal();")
        self.assertEqual(out, {"stayed": True, "jump": False, "count": 31})

    def test_jump_to_latest_goes_to_the_end_and_follows_again(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("run1", n, `l${n}`);
          const log = el("terminal-log");
          log.scrollTop = 40; log.fire("scroll");
          say("run1", 31, "l31");
          el("terminal-jump").fire("click");
          const jumped = { atBottom: log.scrollTop >= log.scrollHeight - log.clientHeight, jump: el("terminal-jump").hidden,
                           focus: log.focused };
          say("run1", 32, "l32");
          out = { jumped, followsAgain: log.scrollTop >= log.scrollHeight - log.clientHeight };
        """, "wireTerminal();")
        self.assertEqual(out, {"jumped": {"atBottom": True, "jump": True, "focus": 1}, "followsAgain": True})

    def test_scrolling_back_down_by_hand_follows_again(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("run1", n, `l${n}`);
          const log = el("terminal-log");
          log.scrollTop = 40; log.fire("scroll");
          const away = !el("terminal-jump").hidden;
          log.scrollTop = log.scrollHeight - log.clientHeight; log.fire("scroll");
          say("run1", 31, "l31");
          out = { away, jump: el("terminal-jump").hidden, atBottom: log.scrollTop >= log.scrollHeight - log.clientHeight };
        """, "wireTerminal();")
        self.assertEqual(out, {"away": True, "jump": True, "atBottom": True})

    def test_a_new_run_starts_at_the_bottom_with_no_jump_key(self):
        out = terminal("""
          for (let n = 1; n <= 30; n += 1) say("old", n, `l${n}`);
          el("terminal-log").scrollTop = 40; el("terminal-log").fire("scroll");
          say("new", 1, "first");
          out = { jump: el("terminal-jump").hidden, lines: texts() };
        """, "wireTerminal();")
        self.assertEqual(out, {"jump": True, "lines": ["first"]})

    # --- what is said aloud ------------------------------------------------------------------------------------
    def test_only_these_state_changes_are_announced(self):
        out = terminal("""
          const said = [], counts = {};
          setTrainer(trainer("idle", null, { summary: "Ready to train" }));
          renderTerminal();
          const steps = [["a", "waiting"], ["a", "training"], ["a", "stopping"], ["a", "stopped"],
                         ["b", "waiting"], ["b", "training"], ["b", "finished"], ["c", "waiting"], ["c", "failed"]];
          for (const [run, state] of steps) {
            el("announcer").textContent = "";
            setTrainer(trainer(state, run, { summary: `Because ${state}.` }));
            counts[run] = (counts[run] || 0) + 1;
            say(run, counts[run], state);
            said.push([state, el("announcer").textContent]);
          }
          out = said;
        """)
        ready = "ML-Agents is ready. Press Play in Unity to start training."
        self.assertEqual(out, [
            ["waiting", ready],
            ["training", "ML-Agents training started."],
            ["stopping", ""],
            ["stopped", "ML-Agents training stopped."],
            ["waiting", ready],
            ["training", "ML-Agents training started."],
            ["finished", "ML-Agents training finished."],
            ["waiting", ready],
            ["failed", "ML-Agents training failed. Because failed."],
        ])

    STARTING = "ML-Agents is starting up. It will say when it is ready."
    READY = "ML-Agents is ready. Press Play in Unity to start training."

    def test_waiting_is_announced_as_starting_up_until_the_trainer_is_listening_and_as_ready_only_then(self):
        out = terminal("""
          const said = [];
          setTrainer(trainer("idle", null, { summary: "Ready to train" }));
          renderTerminal();
          const step = (comp) => { el("announcer").textContent = ""; setTrainer(comp); renderTerminal(); said.push(el("announcer").textContent); };
          step(trainer("waiting", "run1", { listening: false }));
          step(trainer("waiting", "run1", { listening: false, summary: "still starting up" }));  // nothing new
          step(trainer("waiting", "run1", { listening: true }));
          step(trainer("waiting", "run1", { listening: true, summary: "waiting a bit more" }));  // nothing new
          out = said;
        """)
        self.assertEqual(out, [self.STARTING, "", self.READY, ""])

    def test_ready_is_not_said_before_listening_even_when_the_summary_says_to_press_play(self):
        out = terminal("""
          setTrainer(trainer("idle", null, { summary: "Ready to train" }));
          renderTerminal();
          el("announcer").textContent = "";
          setTrainer(trainer("waiting", "run1", { listening: false, summary: "Waiting for Unity: press Play in the Editor." }));
          say("run1", 1, "banner");
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, self.STARTING)
        self.assertNotIn("Press Play", out)

    def test_after_a_lost_connection_it_is_starting_up_again_and_then_ready_again(self):
        out = terminal("""
          const said = [];
          setTrainer(trainer("training", "run1"));
          renderTerminal();
          const step = (comp) => { el("announcer").textContent = ""; setTrainer(comp); renderTerminal(); said.push(el("announcer").textContent); };
          step(trainer("waiting", "run1", { listening: false }));  // Unity stopped answering: the worker restarts
          step(trainer("waiting", "run1", { listening: true }));   // and listens again
          step(trainer("training", "run1"));
          out = said;
        """)
        self.assertEqual(out, [self.STARTING, self.READY, "ML-Agents training started."])

    def test_a_page_opened_while_the_trainer_is_starting_says_nothing_until_it_is_ready(self):
        out = terminal("""
          setTrainer(trainer("waiting", "run1", { listening: false }));
          renderTerminal();
          const atLoad = el("announcer").textContent;
          setTrainer(trainer("waiting", "run1", { listening: true }));
          renderTerminal();
          out = { atLoad, later: el("announcer").textContent };
        """)
        self.assertEqual(out, {"atLoad": "", "later": self.READY})

    def test_a_resumed_run_with_the_same_name_is_announced_again(self):
        out = terminal("""
          setTrainer(trainer("waiting", "run1", { serial: 1 }));
          renderTerminal();
          setTrainer(trainer("finished", "run1", { serial: 1 }));
          renderTerminal();
          el("announcer").textContent = "";
          setTrainer(trainer("waiting", "run1", { serial: 2, listening: false }));
          renderTerminal();
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, self.STARTING)

    def test_a_failure_is_announced_without_the_path_of_the_log(self):
        out = terminal("""
          const said = [];
          setTrainer(trainer("waiting", "run1"));
          renderTerminal();
          const summaries = [
            "Training stopped with an error. The log is at /Users/me/Library/Application Support/GazeVoice/logs/mlagents-run1.log.",
            "Unity stopped answering for 2 minutes during training, so training ended. The log is at /Users/me/logs/mlagents-run2.log.",
            "Another training run is already using ML-Agents' connection to Unity. Stop it first.",
          ];
          summaries.forEach((summary, i) => {
            setTrainer(trainer("waiting", `run${i + 1}`));
            say(`run${i + 1}`, 1, "[INFO] banner");
            el("announcer").textContent = "";
            setTrainer(trainer("failed", `run${i + 1}`, { summary }));
            renderTerminal();
            said.push(el("announcer").textContent);
          });
          out = { said, panel: el("terminal-status").textContent };
        """)
        self.assertEqual(out["said"], [
            "ML-Agents training failed. Training stopped with an error.",
            "ML-Agents training failed. Unity stopped answering for 2 minutes during training, so training ended.",
            "ML-Agents training failed. Another training run is already using ML-Agents' connection to Unity. Stop it first.",
        ])
        self.assertTrue(all("/" not in said and "log is at" not in said for said in out["said"]))

    def test_the_same_state_is_not_announced_again_and_a_line_is_never_announced(self):
        out = terminal("""
          setTrainer(trainer("training"));
          renderTerminal();
          el("announcer").textContent = "";
          for (let n = 1; n <= 5; n += 1) { say("run1", n, `[WARNING] line ${n}`); renderTerminal(); }
          setTrainer(trainer("training", "run1", { summary: "Training run1: step 20000, mean reward 0.9." }));
          renderTerminal();
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, "")

    def test_a_page_that_loads_in_the_middle_of_a_run_announces_nothing(self):
        out = terminal("""
          setTrainer(trainer("training"));
          renderTerminal();
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, "")

    def test_a_unit_that_is_still_checking_is_not_taken_for_a_change(self):
        out = terminal("""
          setTrainer(trainer("training"));
          renderTerminal();
          S.state = { components: [{ id: "mlagents", health: "checking", summary: "Still checking…", details: [], actions: [] }] };
          renderTerminal();
          setTrainer(trainer("training"));
          renderTerminal();
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, "")

    def test_state_changes_are_not_announced_by_a_panel_that_was_closed(self):
        out = terminal("""
          setTrainer(trainer("training"));
          say("run1", 1, "a");
          closeTerminal();
          el("announcer").textContent = "";
          setTrainer(trainer("finished"));
          renderTerminal();
          out = el("announcer").textContent;
        """)
        self.assertEqual(out, "")

    def test_the_trainers_run_is_read_from_the_units_run_field(self):
        out = terminal("""
          const live = trainer("training", "Mover-1", { serial: 5, listening: false });
          live.details = [["Last run", "something else · finished"]];  // the words for people are not what the panel reads
          out = [
            trainerRun(live),
            trainerRun(trainer("idle")),
            trainerRun({ id: "mlagents", health: "checking", details: [], actions: [], run: null }),
            trainerRun({ id: "mlagents", health: "missing", details: [], actions: [], run: null }),
            trainerRun({ id: "mlagents", health: "ready", details: [], actions: [] }),
            trainerRun(null),
          ];
        """)
        self.assertEqual(out, [{"run_id": "Mover-1", "serial": 5, "state": "training", "listening": False}, None,
                               None, None, None, None])

    def test_a_unit_that_is_still_being_checked_is_not_the_same_as_one_with_no_run(self):
        out = terminal("""
          out = [trainerRun({ id: "mlagents", health: "checking" }) === undefined, trainerRun(null) === undefined,
                 trainerRun(trainer("idle")) === null];
        """)
        self.assertEqual(out, [True, True, True])

    def test_the_panel_follows_the_units_run_and_ignores_the_last_run_text(self):
        out = terminal("""
          const comp = trainer("training", "run1", { serial: 5 });
          comp.details = [["Last run", "another · stopped"]];
          setTrainer(comp);
          renderTerminal();
          out = { hidden: el("terminal").hidden, run: el("terminal-run").textContent, led: el("terminal-led").dataset.led };
        """)
        self.assertEqual(out, {"hidden": False, "run": "run1", "led": "live"})


class TerminalWiringTest(unittest.TestCase):
    """The parts of the page that are not functions: the markup, the listeners and the styles."""

    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text()
        cls.js = APP_JS.read_text()
        cls.css = (STATIC / "app.css").read_text()

    def tag(self, element_id):
        return re.search(rf"<[a-z0-9]+[^>]*id=\"{element_id}\"[^>]*>", self.html).group(0)

    def test_the_panel_sits_directly_under_voice_and_starts_hidden(self):
        voice = self.html.index('<section class="voice"')
        terminal_at = self.html.index('id="terminal"')
        desk = self.html.index('<div class="desk">')
        self.assertLess(self.html.index("</section>", voice), terminal_at)
        self.assertLess(terminal_at, desk)
        self.assertEqual(self.html[self.html.index("</section>", voice):terminal_at].count("<section"), 1)  # nothing between
        tag = self.tag("terminal")
        self.assertTrue(tag.startswith("<section"))
        self.assertIn(" hidden", tag)
        self.assertIn('aria-labelledby="terminal-h"', tag)
        self.assertIn('<h2 id="terminal-h">ML-Agents terminal</h2>', self.html)

    def test_the_output_area_is_a_quiet_log_the_keyboard_can_scroll(self):
        tag = self.tag("terminal-log")
        self.assertIn('role="log"', tag)
        self.assertIn('aria-live="off"', tag)
        self.assertIn('tabindex="0"', tag)
        self.assertIn("aria-label=", tag)

    def test_the_buttons_have_names_and_focus_keys(self):
        stop = re.search(r"<button[^>]*id=\"terminal-stop\"[^>]*>(.*?)</button>", self.html, re.DOTALL)
        self.assertIn(" hidden", stop.group(0))
        self.assertEqual(stop.group(1).strip(), "Stop training")
        jump = re.search(r"<button[^>]*id=\"terminal-jump\"[^>]*>(.*?)</button>", self.html, re.DOTALL)
        self.assertIn(" hidden", jump.group(0))
        self.assertEqual(jump.group(1).strip(), "Jump to latest")
        self.assertIn("aria-label=", self.tag("terminal-close"))
        for button in ("terminal-stop", "terminal-close", "terminal-jump"):
            tag = self.tag(button)
            self.assertIn('type="button"', tag)
            self.assertIn(f'data-focus-key="{button}"', tag)

    def test_the_status_line_is_not_a_live_region(self):
        tag = self.tag("terminal-status")
        self.assertNotIn("aria-live", tag)
        self.assertNotIn("role=", tag)

    def test_events_and_page_loads_feed_the_panel(self):
        self.assertRegex(self.js, r'source\.addEventListener\("training\.output", \(event\) => \{\s*'
                                  r'onTrainingOutput\(JSON\.parse\(event\.data\)\);')
        opened = re.search(r'source\.addEventListener\("open", \(\) => \{.*?\n  \}\);', self.js, re.DOTALL).group(0)
        self.assertIn("fillTerminal();", opened)  # on a reconnect
        started = re.search(r"^function start\(\) \{.*?^\}\n", self.js, re.DOTALL | re.MULTILINE).group(0)
        self.assertIn("wireTerminal();", started)
        self.assertLess(started.index("fillTerminal();"), started.index("if (SNAPSHOT) return;"))  # on load
        render_all = re.search(r"^function renderAll\(\) \{.*?^\}\n", self.js, re.DOTALL | re.MULTILINE).group(0)
        self.assertIn("renderTerminal();", render_all)

    def test_lines_are_only_ever_put_in_as_text(self):
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(unsafe, self.js)
        self.assertIn("text: record.text", self.js)

    def test_the_run_comes_from_the_units_run_field_and_not_from_its_text(self):
        for gone in ("trainerNow", "renewTerminal", "terminalRun", "Last run", "TERM_ENDED"):
            self.assertNotIn(gone, self.js)
        self.assertIn("comp.run", self.js)

    def test_component_updates_are_taken_in_straight_away(self):
        self.assertRegex(self.js, r'source\.addEventListener\("component\.updated", \(event\) => \{\s*'
                                  r'onComponentUpdated\(JSON\.parse\(event\.data\)\);')
        update = re.search(r"^function onComponentUpdated\(late\) \{.*?^\}\n", self.js, re.DOTALL | re.MULTILINE).group(0)
        self.assertLess(update.index("renderTerminal();"), update.index("setTimeout(refresh, 400)"))  # the panel does not wait
        self.assertNotIn("connectEvents.lateTimer", self.js)

    def test_a_closed_panel_is_read_back_before_the_first_draw_and_the_storage_is_always_guarded(self):
        started = re.search(r"^function start\(\) \{.*?^\}\n", self.js, re.DOTALL | re.MULTILINE).group(0)
        recalled = started.index("S.term.dismissed = recallClosed();")
        self.assertLess(recalled, started.index("  wireTerminal();\n  renderAll();"))  # before the first draw
        self.assertLess(recalled, started.index("fillTerminal();"))
        for call in ("sessionStorage.getItem", "sessionStorage.setItem"):
            at = self.js.index(call)
            self.assertIn("try {", self.js[at - 80:at])  # never outside a try
        self.assertEqual(self.js.count("sessionStorage."), 2)
        self.assertNotIn("localStorage", self.js[self.js.index("// ---------- ML-Agents terminal"):])

    def test_a_serial_is_what_a_line_is_checked_against(self):
        taken = re.search(r"^function onTrainingOutput\(data\) \{.*?^\}\n", self.js, re.DOTALL | re.MULTILINE).group(0)
        self.assertIn("data.serial", taken)
        self.assertNotIn("data.run_id === t.", taken)
        self.assertNotIn("data.run_id !== t.", taken)

    def test_the_styles_make_a_dark_scrolling_monospace_screen(self):
        rule = re.search(r"^\.term-screen \{(.*?)\}", self.css, re.DOTALL | re.MULTILINE).group(1)
        self.assertIn("max-height: 22rem", rule)
        self.assertIn("overflow: auto", rule)
        self.assertIn("var(--font-mono)", rule)
        self.assertIn("background: var(--readout)", rule)

    def test_the_screen_gets_the_same_focus_ring_as_the_other_controls(self):
        self.assertIn(".term-screen:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }", self.css)
        self.assertIn(".key:focus-visible { outline: 2px solid var(--focus)", self.css)  # the buttons are .key

    def test_lines_that_warn_fail_or_report_progress_have_their_own_colours(self):
        for kind in ("warn", "error", "progress"):
            self.assertRegex(self.css, rf'\.term-line\[data-kind="{kind}"\] \{{[^}}]*color: var\(--term-{kind}\)')
            self.assertRegex(self.css, rf"--term-{kind}: #[0-9a-fA-F]{{6}};")
        self.assertRegex(self.css, r'\.term-line\[data-kind="progress"\] \{[^}]*font-weight: 500')

    def test_the_panel_never_scrolls_smoothly(self):
        # Following the newest line and Jump to latest move at once, so there is no motion to take away from anyone.
        self.assertNotIn("scroll-behavior: smooth", self.css)
        section = re.search(r"^// ---------- ML-Agents terminal ----------\n.*?(?=^// ----------)", self.js,
                            re.DOTALL | re.MULTILINE).group(0)
        self.assertNotIn("smooth", section)
        self.assertNotIn("scrollTo(", section)
        self.assertNotIn("scrollIntoView", section)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_no_new_script_font_or_stylesheet_is_loaded(self):
        self.assertEqual(re.findall(r"<script[^>]*>", self.html), ['<script src="/static/app.js" defer>'])
        self.assertEqual(re.findall(r"<link[^>]*>", self.html), ['<link rel="stylesheet" href="/static/app.css">'])
        self.assertNotIn("@import", self.css)
        self.assertNotIn("url(", self.css)


if __name__ == "__main__":
    unittest.main()
