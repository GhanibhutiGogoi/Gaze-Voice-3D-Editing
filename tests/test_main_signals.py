import contextlib
import io
import os
import signal
import threading
import time
import unittest
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


class StopSignalTest(unittest.TestCase):
    """Closing the Terminal window (SIGHUP) or `kill` (SIGTERM) must close the control center the way Ctrl+C does,
    so a training run is stopped and its model saved instead of being left holding port 5004."""

    def run_main(self, send, training=None, hangup=None):
        """Run the real main() with `training`, sending signals from `send(handlers_ready)`. `hangup` is what SIGHUP
        is set to before it starts (SIG_IGN is how `nohup` starts a program). Returns (exit code, what send returned)."""
        t = TempCtx()
        self.addCleanup(t.cleanup)
        training = t.ctx.training = training or CountingTraining()
        center = ControlCenter(t.ctx, [Quiet(t.ctx)])

        def stand_in(number, frame):  # keeps a signal main() doesn't handle from ending this test process
            raise KeyboardInterrupt

        for name in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, name, signal.getsignal(name))
            signal.signal(name, stand_in)
        if hangup is not None:
            signal.signal(signal.SIGHUP, hangup)
        seen = []

        def sender():
            deadline = time.monotonic() + 3
            while signal.getsignal(signal.SIGTERM) is stand_in and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.3)  # let the server start serving
            seen.append(send(stand_in))

        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        with mock.patch.object(entry, "build_center", return_value=center), contextlib.redirect_stdout(io.StringIO()):
            try:
                code = entry.main(["--port", "0", "--no-browser"])
            except KeyboardInterrupt:  # a signal that cut the shutdown short
                code = "interrupted"
        thread.join(5)
        return code, seen

    def stop_with(self, signum):
        def send(stand_in):
            handled = signal.getsignal(signum) is not stand_in
            os.kill(os.getpid(), signum)
            return handled

        training = CountingTraining()
        code, seen = self.run_main(send, training)
        return code, seen, training

    def test_terminate_closes_the_control_center(self):
        code, handled, training = self.stop_with(signal.SIGTERM)
        self.assertEqual((code, handled, training.shutdowns), (0, [True], 1))

    def test_closing_the_terminal_closes_the_control_center(self):
        code, handled, training = self.stop_with(signal.SIGHUP)
        self.assertEqual((code, handled, training.shutdowns), (0, [True], 1))

    def test_a_hangup_that_starts_ignored_is_left_ignored(self):
        # `nohup` starts a program with SIGHUP ignored, so that closing the Terminal doesn't end it.
        def send(stand_in):
            still_ignored = signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGTERM)
            return still_ignored

        code, seen = self.run_main(send, hangup=signal.SIG_IGN)
        self.assertEqual((code, seen), (0, [True]))

    def test_a_second_signal_while_the_model_is_saving_does_not_cut_the_wait_short(self):
        training = CountingTraining(shutdown_seconds=0.8)

        def send(stand_in):
            os.kill(os.getpid(), signal.SIGTERM)
            training.shutting_down.wait(5)
            time.sleep(0.1)  # the control center is now inside shutdown, waiting for the save
            os.kill(os.getpid(), signal.SIGHUP)
            os.kill(os.getpid(), signal.SIGTERM)

        code, _ = self.run_main(send, training)
        self.assertEqual((code, training.shutdowns, training.finished), (0, 1, True))


if __name__ == "__main__":
    unittest.main()
