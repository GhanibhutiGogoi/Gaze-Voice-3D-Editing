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
