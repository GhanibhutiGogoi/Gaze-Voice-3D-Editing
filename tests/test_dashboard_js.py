"""The dashboard's script has no test harness of its own, so the functions that decide what the person reads are pulled
out of app.js and run with node. Skipped where node isn't installed."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "gazevoice" / "server" / "static" / "app.js"
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


def run_function(name: str, call: str, stubs: str = ""):
    """What the real function `name` in app.js returns for the JavaScript expression `call`."""
    match = re.search(rf"^function {name}\(.*?^\}}\n", APP_JS.read_text(), re.DOTALL | re.MULTILINE)
    assert match, f"{name} is not where this test expects it"
    program = f"{stubs}\n{match.group(0)}\nconsole.log(JSON.stringify({call}));\n"
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def privacy_note(voice: dict, sources=None) -> str:
    stubs = (f"const S = {{ state: {{ sources: {json.dumps(sources or {'editor': 'claude'})} }} }};\n"
             "const providerNames = () => [];\n")
    return run_function("privacyNote", f"privacyNote({json.dumps(voice)})", stubs)


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
                     {"status": "none", "targets": [], "radius": [], "viewport_changed": True},
                     {"status": "none", "targets": [], "misses": [{"word": "there", "at": 1.0}]}):
            with self.subTest(gaze):
                self.assertIsNone(self.line({"gaze": gaze, "focus": None}))

    def test_the_command_row_shows_it(self):
        self.assertIn('gazeLine(r) ? h("p", { class: "cmd-heard", text: gazeLine(r) }) : null', APP_JS.read_text())


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
        self.assertNotIn("deleted from this Mac", source)


if __name__ == "__main__":
    unittest.main()
