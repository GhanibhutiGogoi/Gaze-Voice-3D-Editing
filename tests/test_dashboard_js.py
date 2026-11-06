"""The dashboard's script has no test harness of its own, so the one function whose text is shown to the person after a
job is pulled out of app.js and run with node. Skipped where node isn't installed."""

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
        self.assertIn("Pictures are deleted from this Mac right after use. Anthropic keeps what's sent under your "
                      "account's terms.", source)


if __name__ == "__main__":
    unittest.main()
