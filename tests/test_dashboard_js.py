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


if __name__ == "__main__":
    unittest.main()
