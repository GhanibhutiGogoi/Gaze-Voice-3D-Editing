"""The five-stage strip at the top of the dashboard, and "Check everything"."""

from __future__ import annotations

from typing import Mapping

from .components.base import ActionSpec, Component, ComponentState, Health
from .components.brains import BRAIN_ORDER, pick_brain
from .jobs import Job, JobError
from .probes import ProbeTimedOut

SEVERITY = {Health.READY: 0, Health.CHECKING: 1, Health.INSTALLED: 2, Health.MISSING: 3, Health.ERROR: 4}
BRAIN = "@brain"

STAGES = (
    ("listen", "Microphone", ("microphone",)),
    ("transcribe", "Speech-to-text", ("speech",)),
    ("refine", "Refiner", (BRAIN,)),
    ("edit", "Editor", (BRAIN, "mcp_server")),
    ("blender", "Blender", ("blender", "mcp_addon", "bridge")),
)


def workflow_stages(states: Mapping[str, ComponentState], brain: str | None) -> list[dict]:
    """Each stage is as healthy as its least healthy part. No ready brain counts as missing."""
    stages = []
    for stage_id, label, parts in STAGES:
        resolved = [brain if part == BRAIN else part for part in parts]
        healths = [states[cid].health if cid in states else Health.MISSING for cid in resolved if cid]
        if None in resolved:
            healths.append(Health.MISSING)
        worst = max(healths, key=SEVERITY.__getitem__)
        stages.append(
            {
                "id": stage_id,
                "label": label,
                "health": worst.value,
                "parts": [cid if cid else "brain" for cid in resolved],
            }
        )
    return stages


# "Check everything" runs these left to right. The dashboard lights each stage
# as the log reports "▶ id.action", then "✓ id.action: …" or "✗ id.action: …".
STEPS = (
    ("microphone", "test"),
    ("speech", "test"),
    ("mcp_server", "test"),
    ("bridge", "test"),
    (BRAIN, "connect_blender"),
)
CHECK_CONFIRM = (
    "Run every connection test now? This records 3 seconds from your microphone and asks "
    "your brain (Claude Code or Codex) one small question."
)


def _brief(result: dict | None) -> str:
    if not result:
        return "ok"
    return ", ".join(f"{key}={value}" for key, value in result.items())[:160]


class WorkflowCheck(Component):
    id = "workflow"
    name = "Workflow check"
    role = "Runs every connection test in order"

    def __init__(self, ctx, components: Mapping[str, Component]):
        super().__init__(ctx)
        self.components = components

    def check(self) -> ComponentState:
        return self.state(
            Health.INSTALLED,
            "Runs every connection test, left to right.",
            actions=[ActionSpec("check", "Check everything", confirm=CHECK_CONFIRM, uses_plan=True)],
        )

    def _state_of(self, component: Component) -> ComponentState:
        try:
            return component.check()
        except ProbeTimedOut:
            return component.state(Health.CHECKING, "Still checking…")

    def _brain(self) -> str | None:
        states = {cid: self._state_of(self.components[cid]) for cid in BRAIN_ORDER if cid in self.components}
        return pick_brain(states, self.settings.brain)

    def action_check(self, job: Job) -> dict:
        passed = 0
        for component_id, action in STEPS:
            if component_id == BRAIN:
                component_id = self._brain()
                if component_id is None:
                    job.log(f"✗ brain.{action}: no brain logged in")
                    raise JobError("No brain is logged in. Log into Claude Code or Codex first.")
            component = self.components.get(component_id)
            if component is None:
                raise JobError(f"{component_id} isn't part of this setup.")
            state = self._state_of(component)
            if state.health is Health.CHECKING:
                job.log(f"✗ {component_id}.{action}: still being checked")
                raise JobError(f"{component.name} is still being checked. Try again in a moment.")
            if not state.offers(action):
                job.log(f"✗ {component_id}.{action}: not available: {state.summary}")
                raise JobError(f"Stopped at {component.name}: {state.summary}")
            job.log(f"▶ {component_id}.{action}")
            try:
                result = component.run_action(action, job)
            except JobError as exc:
                job.log(f"✗ {component_id}.{action}: {exc}")
                raise JobError(f"{component.name} {action.replace('_', ' ')} failed: {exc}") from exc
            job.log(f"✓ {component_id}.{action}: {_brief(result)}")
            passed += 1
        job.log("Every stage passed. The workflow is ready.")
        return {"passed": passed}
