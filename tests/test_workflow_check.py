import unittest

from gazevoice.components.base import ActionSpec, Component, Health
from gazevoice.jobs import JobError
from gazevoice.probes import ProbeTimedOut
from gazevoice.workflow import STEPS, WorkflowCheck

from gazevoice.providers import preset
from gazevoice.settings import update_settings

from .helpers import TempCtx, make_job


def fake(cid, offers=True, fails=False, health=Health.READY, calls=None):
    class Fake(Component):
        id = cid
        name = cid.title()
        role = "fake"

        def check(self):
            actions = [ActionSpec(a, a) for a in ("test", "connect_blender")] if offers else []
            return self.state(health, f"{cid} summary", actions=actions)

        def action_test(self, job):
            calls.append((cid, "test"))
            if fails:
                raise JobError(f"{cid} broke")
            return {"ok": True}

        def action_connect_blender(self, job):
            calls.append((cid, "connect_blender"))
            return {"reply": "3", "seconds": 1.0}

    return Fake


class WorkflowCheckTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.calls = []

    def tearDown(self):
        self.t.cleanup()

    def build(self, extra=(), **overrides):
        ids = ["microphone", "speech", "claude", "codex", "mcp_server", "bridge", *extra]
        components = {}
        for cid in ids:
            kwargs = {"calls": self.calls, **overrides.get(cid, {})}
            components[cid] = fake(cid, **kwargs)(self.t.ctx)
        return WorkflowCheck(self.t.ctx, components)

    def test_offers_check_with_consent_and_plan_tag(self):
        state = self.build().check()
        spec = next(a for a in state.actions if a.id == "check")
        self.assertTrue(spec.uses_plan)
        self.assertIn("record", spec.confirm.lower())

    def test_runs_every_step_in_order_with_markers(self):
        job = make_job("workflow", "check")
        result = self.build().run_action("check", job)
        self.assertEqual(
            self.calls,
            [("microphone", "test"), ("speech", "test"), ("mcp_server", "test"), ("bridge", "test"),
             ("claude", "connect_blender")],
        )
        self.assertIn("▶ microphone.test", job.lines)
        self.assertTrue(any(line.startswith("✓ claude.connect_blender") for line in job.lines))
        self.assertEqual(result["passed"], 5)

    def test_stops_at_first_failure_and_names_it(self):
        job = make_job("workflow", "check")
        with self.assertRaises(JobError) as caught:
            self.build(speech={"fails": True}).run_action("check", job)
        self.assertIn("Speech", str(caught.exception))
        self.assertEqual(self.calls, [("microphone", "test"), ("speech", "test")])
        self.assertTrue(any(line.startswith("✗ speech.test") for line in job.lines))

    def test_unavailable_step_fails_with_its_summary(self):
        job = make_job("workflow", "check")
        with self.assertRaises(JobError) as caught:
            self.build(mcp_server={"offers": False, "health": Health.MISSING}).run_action("check", job)
        self.assertIn("mcp_server summary", str(caught.exception))

    def test_a_still_checking_unit_stops_the_run_politely(self):
        workflow = self.build()

        def stalled():
            raise ProbeTimedOut(("ffmpeg",))

        workflow.components["speech"].check = stalled
        with self.assertRaises(JobError) as caught:
            workflow.run_action("check", make_job("workflow", "check"))
        self.assertIn("still being checked", str(caught.exception))

    def test_uses_codex_when_claude_not_ready(self):
        self.build(claude={"health": Health.INSTALLED}).run_action("check", make_job("workflow", "check"))
        self.assertEqual(self.calls[-1], ("codex", "connect_blender"))

    def test_steps_constant(self):
        # The brain's step is added per run: it depends on which brain or provider each step uses.
        self.assertEqual([s[0] for s in STEPS], ["microphone", "speech", "mcp_server", "bridge"])

    def test_an_api_provider_runs_its_own_test(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {
            "providers": [preset("deepseek").to_dict()], "editor_source": "deepseek", "refiner_source": "deepseek"})
        result = self.build(extra=["deepseek"]).run_action("check", make_job("workflow", "check"))
        self.assertEqual(self.calls[-1], ("deepseek", "test"))
        self.assertNotIn(("claude", "connect_blender"), self.calls)
        self.assertEqual(result["passed"], 5)  # one test, though both steps use DeepSeek

    def test_no_brain_at_all_stops_with_a_plain_message(self):
        workflow = self.build(claude={"health": Health.MISSING}, codex={"health": Health.MISSING})
        with self.assertRaises(JobError) as caught:
            workflow.run_action("check", make_job("workflow", "check"))
        self.assertIn("API provider", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
