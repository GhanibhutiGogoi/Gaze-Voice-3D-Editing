import threading
import unittest

from gazevoice.events import EventBus
from gazevoice.jobs import JobAlreadyRunning, JobError, JobManager, JobState


class JobManagerTest(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.mgr = JobManager(self.bus)

    def test_job_success_records_result_and_lines(self):
        def work(job):
            job.log("step 1")
            return {"ok": 1}

        job = self.mgr.start("blender", "install", work)
        done = self.mgr.wait(job.id, 2)
        self.assertEqual(done.state, JobState.SUCCEEDED)
        self.assertEqual(done.result, {"ok": 1})
        self.assertEqual(done.lines, ["step 1"])
        self.assertIsNotNone(done.finished_at)

    def test_job_error_message_is_user_facing(self):
        def work(job):
            raise JobError("Homebrew not found")

        done = self.mgr.wait(self.mgr.start("blender", "install", work).id, 2)
        self.assertEqual(done.state, JobState.FAILED)
        self.assertEqual(done.error, "Homebrew not found")

    def test_unexpected_exception_fails_with_traceback_lines(self):
        def work(job):
            raise ValueError("boom")

        done = self.mgr.wait(self.mgr.start("speech", "install", work).id, 2)
        self.assertEqual(done.state, JobState.FAILED)
        self.assertTrue(done.error.startswith("Unexpected error"))
        self.assertTrue(any("Traceback" in line for line in done.lines))

    def test_second_job_for_same_component_rejected(self):
        release = threading.Event()
        first = self.mgr.start("blender", "install", lambda job: release.wait(2))
        with self.assertRaises(JobAlreadyRunning):
            self.mgr.start("blender", "launch", lambda job: None)
        release.set()
        self.mgr.wait(first.id, 2)
        # Once finished, the component accepts a new job.
        again = self.mgr.start("blender", "launch", lambda job: None)
        self.assertEqual(self.mgr.wait(again.id, 2).state, JobState.SUCCEEDED)

    def test_other_components_can_run_in_parallel(self):
        release = threading.Event()
        a = self.mgr.start("blender", "install", lambda job: release.wait(2))
        b = self.mgr.start("speech", "install", lambda job: None)
        self.assertEqual(self.mgr.wait(b.id, 2).state, JobState.SUCCEEDED)
        release.set()
        self.mgr.wait(a.id, 2)

    def test_events_published(self):
        sub = self.bus.subscribe()

        def work(job):
            job.log("hello")

        self.mgr.wait(self.mgr.start("codex", "test", work).id, 2)
        kinds = [sub.get(timeout=1).kind for _ in range(3)]
        self.assertEqual(kinds, ["job.started", "job.log", "job.finished"])

    def test_recent_lists_newest_first_and_to_dict(self):
        a = self.mgr.wait(self.mgr.start("a", "x", lambda job: None).id, 2)
        b = self.mgr.wait(self.mgr.start("b", "x", lambda job: None).id, 2)
        self.assertEqual([j.id for j in self.mgr.recent()], [b.id, a.id])
        d = b.to_dict()
        self.assertEqual(d["component_id"], "b")
        self.assertEqual(d["state"], "succeeded")

    def test_history_is_bounded(self):
        mgr = JobManager(self.bus, max_history=3)
        for i in range(5):
            mgr.wait(mgr.start(f"c{i}", "x", lambda job: None).id, 2)
        self.assertEqual(len(mgr.recent(limit=10)), 3)


if __name__ == "__main__":
    unittest.main()
