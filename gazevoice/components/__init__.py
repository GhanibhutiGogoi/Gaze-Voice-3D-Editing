"""The components shown on the dashboard, in pipeline order."""

from __future__ import annotations

from .base import ActionSpec, Component, ComponentState, Context, Health

__all__ = ["ActionSpec", "Component", "ComponentState", "Context", "Health", "build_components"]


def build_components(ctx: Context) -> list[Component]:
    from ..workflow import WorkflowCheck
    from .providers import provider_units

    components = [cls(ctx) for cls in _component_classes()] + provider_units(ctx)
    return [*components, WorkflowCheck(ctx, {c.id: c for c in components})]


def _component_classes() -> list[type[Component]]:
    from .blender import Blender
    from .blender_mcp import McpAddon, McpServer
    from .brains import ClaudeCode, Codex
    from .bridge import Bridge
    from .gaze import Gaze
    from .microphone import Microphone
    from .mlagents import MLAgents
    from .speech import Speech
    from .unity import Unity, UnityProject
    from .unity_bridge import UnityBridge, UnityMcp

    return [Microphone, Speech, Gaze, ClaudeCode, Codex, Blender, McpAddon, McpServer, Bridge,
            Unity, UnityProject, UnityMcp, UnityBridge, MLAgents]
