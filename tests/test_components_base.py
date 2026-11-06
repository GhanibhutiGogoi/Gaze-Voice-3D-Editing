import unittest
from pathlib import Path

from gazevoice.components.base import ActionSpec, CameraClaim, Component, Context, Health
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


class CameraClaimTest(unittest.TestCase):
    def test_one_user_at_a_time(self):
        camera = CameraClaim()
        self.assertIsNone(camera.holder)
        self.assertIsNone(camera.take("voice"))  # None means it is yours
        self.assertEqual(camera.holder, "voice")
        self.assertEqual(camera.take("gaze"), "voice")  # and who has it, when it isn't
        self.assertEqual(camera.take("voice"), "voice")  # taking it twice is not holding it twice
        camera.release("voice")
        self.assertIsNone(camera.holder)
        self.assertIsNone(camera.take("gaze"))

    def test_only_the_holder_can_give_it_back(self):
        camera = CameraClaim()
        camera.take("voice")
        camera.release("gaze")  # a refused user cleaning up must not take voice's camera away
        self.assertEqual(camera.holder, "voice")
        camera.release("voice")
        camera.release("voice")  # twice is fine
        self.assertIsNone(camera.holder)

    def test_a_context_has_one_by_default_and_each_context_its_own(self):
        a, b = make_ctx(), make_ctx()
        a.camera.take("voice")
        self.assertEqual(a.camera.holder, "voice")
        self.assertIsNone(b.camera.holder)
