import sys
import time
import unittest

from gazevoice.runner import CommandRunner, FakeRunner


class CommandRunnerTest(unittest.TestCase):
    def test_run_captures_output(self):
        r = CommandRunner().run([sys.executable, "-c", "print('hi')"])
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout.strip(), "hi")

    def test_run_missing_executable_returns_127(self):
        r = CommandRunner().run(["definitely-not-a-command-xyz"])
        self.assertEqual(r.returncode, 127)
        self.assertIn("not found", r.stderr)

    def test_run_timeout_returns_124(self):
        r = CommandRunner().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertEqual(r.returncode, 124)
        self.assertIn("timed out", r.stderr)

    def test_run_passes_env_and_keeps_path(self):
        r = CommandRunner().run(
            [sys.executable, "-c", "import os; print(os.environ['GV_TEST'], bool(os.environ.get('PATH')))"],
            env={"GV_TEST": "yes"},
        )
        self.assertEqual(r.stdout.split(), ["yes", "True"])

    def test_stream_emits_lines_and_returns_code(self):
        lines = []
        code = CommandRunner().stream(
            [sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"], lines.append
        )
        self.assertEqual(lines, ["a", "b"])
        self.assertEqual(code, 3)

    def test_stream_missing_executable(self):
        lines = []
        code = CommandRunner().stream(["definitely-not-a-command-xyz"], lines.append)
        self.assertEqual(code, 127)
        self.assertIn("not found", lines[0])

    def test_stream_timeout_kills_process_and_its_children(self):
        # `sleep` is a grandchild holding the output pipe open; it must die too.
        lines = []
        started = time.monotonic()
        code = CommandRunner().stream(["/bin/sh", "-c", "echo start; sleep 30"], lines.append, timeout=1.0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 124)
        self.assertEqual(lines[0], "start")
        self.assertIn("timed out", lines[-1])

    def test_run_timeout_kills_children(self):
        started = time.monotonic()
        r = CommandRunner().run(["/bin/sh", "-c", "sleep 30 & wait"], timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(r.returncode, 124)

    def test_stdin_is_empty_so_commands_never_wait_for_input(self):
        r = CommandRunner().run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=10)
        self.assertEqual(r.stdout.strip(), "''")

    def test_run_passes_input_text(self):
        r = CommandRunner().run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input_text="abc")
        self.assertEqual(r.stdout.strip(), "ABC")


class FakeRunnerTest(unittest.TestCase):
    def test_fake_runner_replays_and_records(self):
        f = FakeRunner(which={"brew": "/opt/homebrew/bin/brew"})
        f.add(["brew", "--version"], stdout="Homebrew 4.6\n")
        self.assertEqual(f.run(["brew", "--version"]).stdout, "Homebrew 4.6\n")
        self.assertEqual(f.run(["nope"]).returncode, 127)
        self.assertEqual(f.calls, [("brew", "--version"), ("nope",)])
        self.assertEqual(f.which("brew"), "/opt/homebrew/bin/brew")
        self.assertIsNone(f.which("uv"))

    def test_fake_stream_emits_stdout_lines(self):
        f = FakeRunner()
        f.add(["brew", "install", "--cask", "blender"], stdout="one\ntwo\n", returncode=0)
        lines = []
        self.assertEqual(f.stream(["brew", "install", "--cask", "blender"], lines.append), 0)
        self.assertEqual(lines, ["one", "two"])


if __name__ == "__main__":
    unittest.main()
