import unittest
from pathlib import Path

from gazevoice.components.base import ActionSpec, Component, Context, Health
from gazevoice.events import EventBus
from gazevoice.jobs import Job, JobError
from gazevoice.paths import AppPaths
from gazevoice.runner import FakeRunner
from gazevoice.settings import Settings


class Dummy(Component):
    id = "dummy"
    name = "Dummy"
    role = "Test component"

    def check(self):
        return self.state(
            Health.READY,
            "All good",
            details=[("Version", "5.2.2")],
            actions=[ActionSpec("install", "Install")],
        )

    def action_install(self, job):
        job.log("installing")
        return {"done": True}


def make_ctx():
    return Context(runner=FakeRunner(), paths=AppPaths(Path("/tmp/gv-test")), settings=Settings())


class ComponentBaseTest(unittest.TestCase):
    def test_state_to_dict(self):
        state = Dummy(make_ctx()).check()
        self.assertEqual(
            state.to_dict(),
            {
                "id": "dummy",
                "name": "Dummy",
                "role": "Test component",
                "health": "ready",
                "summary": "All good",
                "details": [["Version", "5.2.2"]],
                "actions": [{"id": "install", "label": "Install", "confirm": None, "uses_plan": False}],
            },
        )

    def test_offers(self):
        state = Dummy(make_ctx()).check()
        self.assertTrue(state.offers("install"))
        self.assertFalse(state.offers("launch"))

    def test_run_action_dispatches(self):
        job = Job(id="j1", component_id="dummy", action="install", _bus=EventBus())
        self.assertEqual(Dummy(make_ctx()).run_action("install", job), {"done": True})
        self.assertEqual(job.lines, ["installing"])

    def test_run_action_unknown_raises_job_error(self):
        job = Job(id="j1", component_id="dummy", action="nope")
        with self.assertRaises(JobError):
            Dummy(make_ctx()).run_action("nope", job)


if __name__ == "__main__":
    unittest.main()
