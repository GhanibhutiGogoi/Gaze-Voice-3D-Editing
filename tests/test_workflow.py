import unittest

from gazevoice.components.base import ComponentState, Health
from gazevoice.providers import preset
from gazevoice.settings import Settings, update_settings
from gazevoice.workflow import workflow_stages


def st(cid, health):
    return ComponentState(cid, cid, "", health, "")


class WorkflowStagesTest(unittest.TestCase):
    def test_stage_health_is_worst_of_its_parts(self):
        states = {
            "microphone": st("microphone", Health.READY),
            "speech": st("speech", Health.INSTALLED),
            "claude": st("claude", Health.READY),
            "mcp_server": st("mcp_server", Health.MISSING),
            "blender": st("blender", Health.READY),
            "mcp_addon": st("mcp_addon", Health.READY),
            "bridge": st("bridge", Health.ERROR),
        }
        stages = workflow_stages(states, "claude")
        self.assertEqual([s["id"] for s in stages], ["listen", "transcribe", "refine", "edit", "blender"])
        self.assertEqual(
            {s["id"]: s["health"] for s in stages},
            {"listen": "ready", "transcribe": "installed", "refine": "ready", "edit": "missing", "blender": "error"},
        )

    def test_absent_components_and_no_brain_count_as_missing(self):
        stages = {s["id"]: s for s in workflow_stages({"microphone": st("microphone", Health.READY)}, None)}
        self.assertEqual(stages["listen"]["health"], "ready")
        self.assertEqual(stages["transcribe"]["health"], "missing")
        self.assertEqual(stages["refine"]["health"], "missing")

    def test_checking_ranks_between_ready_and_installed(self):
        states = {"blender": st("blender", Health.READY), "mcp_addon": st("mcp_addon", Health.CHECKING),
                  "bridge": st("bridge", Health.READY)}
        stages = {s["id"]: s for s in workflow_stages(states, None)}
        self.assertEqual(stages["blender"]["health"], "checking")
        states["bridge"] = st("bridge", Health.INSTALLED)
        stages = {s["id"]: s for s in workflow_stages(states, None)}
        self.assertEqual(stages["blender"]["health"], "installed")

    def test_stages_follow_each_step(self):
        stages = {s["id"]: s for s in workflow_stages({}, {"refiner": "claude", "editor": "deepseek"})}
        self.assertEqual(stages["refine"]["parts"], ["claude"])
        self.assertEqual(stages["edit"]["parts"], ["deepseek", "mcp_server"])

    def test_a_provider_step_without_a_model_isnt_ready(self):
        states = {"deepseek": st("deepseek", Health.READY), "mcp_server": st("mcp_server", Health.READY)}
        settings = update_settings(Settings(), {"providers": [preset("deepseek").to_dict()], "editor_source": "deepseek"})
        sources = {"refiner": None, "editor": "deepseek"}
        self.assertEqual({s["id"]: s for s in workflow_stages(states, sources, settings)}["edit"]["health"], "installed")
        settings = update_settings(settings, {"editor_api_model": "deepseek-v4-pro"})
        self.assertEqual({s["id"]: s for s in workflow_stages(states, sources, settings)}["edit"]["health"], "ready")

    def test_brain_part_is_named(self):
        stages = {s["id"]: s for s in workflow_stages({}, "codex")}
        self.assertEqual(stages["edit"]["parts"], ["codex", "mcp_server"])


if __name__ == "__main__":
    unittest.main()
