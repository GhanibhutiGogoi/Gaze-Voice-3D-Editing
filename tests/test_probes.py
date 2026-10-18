import threading
import time
import unittest

from gazevoice.probes import ProbeCache, ProbeTimedOut
from gazevoice.runner import FakeRunner


class ProbeCacheTest(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.runner.add(["claude", "auth", "status"], stdout='{"authMethod": "claude.ai"}')
        self.cache = ProbeCache()

    def test_reuses_result_within_ttl(self):
        a = self.cache.run(self.runner, ["claude", "auth", "status"], ttl=30)
        b = self.cache.run(self.runner, ["claude", "auth", "status"], ttl=30)
        self.assertEqual(a, b)
        self.assertEqual(self.runner.calls.count(("claude", "auth", "status")), 1)

    def test_expires_after_ttl(self):
        self.cache.run(self.runner, ["claude", "auth", "status"], ttl=0.05)
        time.sleep(0.1)
        self.cache.run(self.runner, ["claude", "auth", "status"], ttl=0.05)
        self.assertEqual(self.runner.calls.count(("claude", "auth", "status")), 2)

    def test_clear_forces_a_fresh_probe(self):
        self.cache.run(self.runner, ["claude", "auth", "status"])
        self.cache.clear()
        self.cache.run(self.runner, ["claude", "auth", "status"])
        self.assertEqual(self.runner.calls.count(("claude", "auth", "status")), 2)

    def test_concurrent_callers_share_one_run(self):
        self.runner.add(["mdfind", "q"], stdout="/Applications/Blender.app\n", delay=0.3)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.cache.run(self.runner, ["mdfind", "q"])))
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 3)
        self.assertEqual(self.runner.calls.count(("mdfind", "q")), 1)

    def test_timeout_can_raise_instead_of_answering(self):
        self.runner.add(["claude", "auth", "status"], returncode=124, stderr="claude: timed out after 15s")
        with self.assertRaises(ProbeTimedOut):
            self.cache.run(self.runner, ["claude", "auth", "status"], raise_on_timeout=True)

    def test_failed_probes_are_not_cached(self):
        self.runner.add(["codex", "login", "status"], returncode=124, stderr="codex: timed out after 15s")
        self.cache.run(self.runner, ["codex", "login", "status"])
        self.cache.run(self.runner, ["codex", "login", "status"])
        self.assertEqual(self.runner.calls.count(("codex", "login", "status")), 2)


if __name__ == "__main__":
    unittest.main()
