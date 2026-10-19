"""The five-stage strip at the top of the dashboard, and "Check everything"."""

from __future__ import annotations

from typing import Mapping

from .components.base import ActionSpec, Component, ComponentState, Health
from .components.brains import BRAIN_ORDER, STEPS as SOURCE_STEPS, resolve_sources
from .jobs import Job, JobError
from .probes import ProbeTimedOut

SEVERITY = {Health.READY: 0, Health.CHECKING: 1, Health.INSTALLED: 2, Health.MISSING: 3, Health.ERROR: 4}
REFINER, EDITOR = "@refiner", "@editor"  # whatever brain or provider each step uses

STAGES = (
    ("listen", "Microphone", ("microphone",)),
    ("transcribe", "Speech-to-text", ("speech",)),
    ("refine", "Refiner", (REFINER,)),
    ("edit", "Editor", (EDITOR, "mcp_server")),
    ("blender", "Blender", ("blender", "mcp_addon", "bridge")),
)


def as_sources(sources: Mapping[str, str | None] | str | None) -> dict[str, str | None]:
    """Per-step sources; a single brain name (or None) means both steps."""
    if isinstance(sources, Mapping):
        return {step: sources.get(step) for step in SOURCE_STEPS}
    return {step: sources for step in SOURCE_STEPS}


def workflow_stages(states: Mapping[str, ComponentState], sources: Mapping[str, str | None] | str | None) -> list[dict]:
    """Each stage is as healthy as its least healthy part. A step with no brain counts as missing."""
    sources = as_sources(sources)
    marker = {REFINER: sources["refiner"], EDITOR: sources["editor"]}
    stages = []
    for stage_id, label, parts in STAGES:
        resolved = [marker[part] if part in marker else part for part in parts]
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
)
CHECK_CONFIRM = (
    "Run every connection test now? This records 3 seconds from your microphone and asks "
    "your brain (Claude Code, Codex or your API provider) one small question."
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

    def _brain_steps(self) -> list[tuple[str, str]]:
        """The last steps: Claude Code or Codex reaching Blender through MCP, and each API provider's test."""
        states = {cid: self._state_of(self.components[cid]) for cid in BRAIN_ORDER if cid in self.components}
        sources = resolve_sources(states, self.settings)
        if None in sources.values():
            raise JobError("No brain is logged in. Log into Claude Code or Codex, or pick an API provider in Routing.")
        steps = []
        for step in ("refiner", "editor"):
            source = sources[step]
            if source in BRAIN_ORDER:
                if step == "editor":
                    steps.append((source, "connect_blender"))
            elif (source, "test") not in steps:
                steps.append((source, "test"))
        return steps

    def action_check(self, job: Job) -> dict:
        passed = 0
        try:
            brain_steps = self._brain_steps()
        except JobError as exc:
            job.log(f"✗ brain: {exc}")
            raise
        for component_id, action in (*STEPS, *brain_steps):
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
