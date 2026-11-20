"""Real gaze worker with test-only progress reports for calibration synchronization."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gazevoice import gaze_worker


class ReportingSession(gaze_worker.Session):
    def see(self, t, seen, light=None):
        super().see(t, seen, light)
        with self._lock:
            collecting = self._collecting
            if collecting is None:
                return
            u, v, check = collecting
            count = sum(k.target == (u, v) and k.check == check for k in self._kept)
        self.emit({"collected": count, "target": [u, v], "check": check})


gaze_worker.Session = ReportingSession
raise SystemExit(gaze_worker.main())
