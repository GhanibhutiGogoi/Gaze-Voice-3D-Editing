"""Control-center subprocess for POSIX lifecycle integration tests."""

import faulthandler
import json
import signal
import sys
import time
from pathlib import Path
from unittest import mock

faulthandler.dump_traceback_later(15, exit=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gazevoice import __main__ as entry
from gazevoice.server.app import ControlCenter
from tests.helpers import TempCtx
from tests.test_main_signals import CountingTraining, Quiet

root = Path(sys.argv[1])
repeated = sys.argv[3] == "repeat"
signal.signal(signal.SIGHUP, signal.SIG_IGN if sys.argv[2] == "ignored" else signal.SIG_DFL)


class Training(CountingTraining):
    def shutdown(self):
        self.shutdowns += 1
        (root / "saving").touch()
        if repeated:
            deadline = time.monotonic() + 15
            while not (root / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Parent did not release the model save")
                time.sleep(0.02)
        self.finished = True


context = TempCtx()
training = context.ctx.training = Training()
center = ControlCenter(context.ctx, [Quiet(context.ctx)])
server = entry.make_server(center, port=0)


def ready():
    path = root / "ready.json"
    if path.exists():
        return
    faulthandler.cancel_dump_traceback_later()
    payload = {"terminate_handled": callable(signal.getsignal(signal.SIGTERM)),
               "hangup_ignored": signal.getsignal(signal.SIGHUP) == signal.SIG_IGN}
    pending = root / "ready.tmp"
    pending.write_text(json.dumps(payload), encoding="utf-8")
    pending.replace(path)


server.service_actions = ready
try:
    with (mock.patch.object(entry, "build_center", return_value=center),
          mock.patch.object(entry, "make_server", return_value=server)):
        code = entry.main(["--port", "0", "--no-browser"])
    (root / "result.json").write_text(
        json.dumps({"code": code, "shutdowns": training.shutdowns, "finished": training.finished}),
        encoding="utf-8",
    )
finally:
    server.server_close()
    context.cleanup()
