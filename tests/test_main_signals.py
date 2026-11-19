import contextlib
import io
import json
import subprocess
import sys
import tempfile
import signal
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gazevoice import __main__ as entry
from gazevoice.components.base import Component, Health
from gazevoice.server.app import ControlCenter

from .helpers import TempCtx


class Quiet(Component):
    id = "quiet"
    name = "Quiet"
    role = "fake"

    def check(self):
        return self.state(Health.READY, "fine")


class CountingTraining:
    def __init__(self, shutdown_seconds=0.0):
        self.shutdowns = 0
        self.shutdown_seconds = shutdown_seconds  # how long saving a model takes
        self.shutting_down = threading.Event()
        self.finished = False

    def subscribe(self, callback):
        pass

    def status(self):
        return {"state": "idle", "message": ""}

    def shutdown(self):
        self.shutdowns += 1
        self.shutting_down.set()
        time.sleep(self.shutdown_seconds)
        self.finished = True


@unittest.skipUnless(hasattr(signal, "SIGHUP"), "POSIX terminal signals; Windows lifecycle is tested separately")
class StopSignalTest(unittest.TestCase):
    """Send real OS signals to an isolated control-center process."""

    def run_main(self, signum, *, ignored_hangup=False, repeated=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = Path(__file__).parent / "fixtures" / "signal_control_center.py"
            proc = subprocess.Popen(
                [sys.executable, "-u", str(fixture), str(root),
                 "ignored" if ignored_hangup else "handled", "repeat" if repeated else "once"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", start_new_session=True,
            )

            def wait_for_file(name):
                deadline = time.monotonic() + 20
                while not (root / name).exists():
                    if proc.poll() is not None:
                        out, err = proc.communicate(timeout=5)
                        self.fail(f"Control center exited before {name}: {proc.returncode} {out} {err}")
                    if time.monotonic() >= deadline:
                        proc.kill()
                        out, err = proc.communicate(timeout=5)
                        self.fail(f"Control center did not report {name}: {out} {err}")
                    time.sleep(0.02)

            try:
                wait_for_file("ready.json")
                ready = json.loads((root / "ready.json").read_text(encoding="utf-8"))
                self.assertTrue(ready["terminate_handled"])
                self.assertEqual(ready["hangup_ignored"], ignored_hangup)
                proc.send_signal(signum)
                if repeated:
                    wait_for_file("saving")
                    proc.send_signal(signal.SIGHUP)
                    proc.send_signal(signal.SIGTERM)
                    (root / "release").touch()
                out, err = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, out + err)
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result, {"code": 0, "shutdowns": 1, "finished": True})
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def test_terminate_closes_the_control_center(self):
        self.run_main(signal.SIGTERM)

    def test_closing_the_terminal_closes_the_control_center(self):
        self.run_main(signal.SIGHUP)

    def test_a_hangup_that_starts_ignored_is_left_ignored(self):
        self.run_main(signal.SIGTERM, ignored_hangup=True)

    def test_a_second_signal_while_the_model_is_saving_does_not_cut_the_wait_short(self):
        self.run_main(signal.SIGTERM, repeated=True)


class StartupSignalTest(unittest.TestCase):
    def test_a_signal_during_handler_registration_still_closes_the_server(self):
        center, server = mock.Mock(), mock.Mock()
        server.server_address = ("127.0.0.1", 8977)

        def register(signum, handler):
            if signum == signal.SIGTERM and callable(handler):
                handler(signum, None)

        with (mock.patch.object(entry, "build_center", return_value=center),
              mock.patch.object(entry, "make_server", return_value=server),
              mock.patch.object(entry.signal, "signal", side_effect=register),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(entry.main(["--port", "0", "--no-browser"]), 0)
        center.shutdown.assert_called_once_with()
        server.server_close.assert_called_once_with()
        server.serve_forever.assert_not_called()


if __name__ == "__main__":
    unittest.main()
