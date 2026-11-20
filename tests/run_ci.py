"""Run regressions with enough diagnostics to locate a stalled CI test."""

import faulthandler
import sys
import unittest
from pathlib import Path


class DiagnosticResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        # Bound a single test, rather than the whole suite, and report every thread
        # before exiting so a deadlock cannot consume the entire Actions job.
        faulthandler.dump_traceback_later(120, exit=True)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2, resultclass=DiagnosticResult).run(suite)
    sys.exit(not result.wasSuccessful())
