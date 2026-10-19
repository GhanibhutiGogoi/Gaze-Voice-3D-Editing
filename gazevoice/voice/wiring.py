"""Turning the control center's units into a running voice loop."""

from __future__ import annotations

from typing import Mapping

from ..blender_socket import BridgeClient
from ..components.base import ComponentState, Context, Health
from ..events import EventBus
from .audio import MicError, MicStream, mic_argv
from .brains import ClaudeSession, CodexSession, claude_session_argv
from .commands import EDITOR_PROMPT, REFINER_PROMPT
from .service import EDITOR_WARMUP, REFINER_WARMUP, Factories, VoiceService
from .stt import SpeechService

# unit id, the healths that are good enough, what to call it
_NEEDED = (
    ("microphone", {Health.READY, Health.INSTALLED}, "Microphone"),  # untested is fine: starting voice tests it
    ("speech", {Health.READY}, "Speech-to-text"),
    ("blender", {Health.READY}, "Blender"),
    ("mcp_addon", {Health.READY}, "Blender MCP add-on"),
    ("mcp_server", {Health.READY, Health.INSTALLED}, "Blender MCP server"),
    ("bridge", {Health.READY}, "GazeVoice Bridge"),
)


def voice_readiness(states: Mapping[str, ComponentState], brain: str | None) -> list[str]:
    """What has to be fixed before voice can start, in plain words. Empty means ready."""
    missing: list[str] = []
    for cid, good, label in _NEEDED:
        state = states.get(cid)
        if state is None:
            missing.append(f"{label} is missing.")
        elif state.health is Health.CHECKING:
            missing.append(f"{label} is still being checked.")
        elif state.health not in good:
            missing.append(f"{label}: {state.summary}")
        if cid == "speech" and brain is None:
            missing.append("No brain is logged in: log into Claude Code or Codex.")
    return missing


def build_voice(ctx: Context, bus: EventBus, brain: str) -> VoiceService:
    """A VoiceService wired to the real mic, speech engine, brain CLI and Blender."""
    from ..components.blender import find_blender
    from ..components.blender_mcp import server_command
    from ..components.brains import write_mcp_config
    from ..components.microphone import Microphone, pick_device
    from ..components.speech import ENGINES, WORKER, Speech

    paths, settings = ctx.paths, ctx.settings
    microphone = Microphone(ctx)

    def mic() -> MicStream:
        # Picked at each start: plugging in a device can move the one you chose to another number.
        picked = pick_device(microphone.devices(), settings.mic_device)
        if picked is None:
            raise MicError("That microphone isn't connected. Pick another one in Routing.")
        return MicStream(mic_argv(picked[0]))

    speech = Speech(ctx)
    engine = ENGINES[settings.stt_engine]
    server_cmd = server_command(paths, find_blender(ctx.runner))
    mcp_config = write_mcp_config(paths, server_cmd)
    paths.work.mkdir(parents=True, exist_ok=True)

    def claude(prompt, model, mcp, max_turns, **start_kwargs) -> ClaudeSession:
        session = ClaudeSession(claude_session_argv(prompt, model, mcp, max_turns=max_turns), cwd=paths.work)
        session.start_kwargs = start_kwargs
        return session

    if brain == "codex":
        def editor():
            return CodexSession(paths.work, EDITOR_PROMPT.read_text(), mcp_cmd=server_cmd)

        def refiner():
            return CodexSession(paths.work, REFINER_PROMPT.read_text())
    else:
        def editor():
            return claude(EDITOR_PROMPT, settings.editor_model, mcp_config, 12,
                          warmup=EDITOR_WARMUP, require_mcp="blender", timeout=150)

        def refiner():
            return claude(REFINER_PROMPT, settings.refiner_model, None, 1, warmup=REFINER_WARMUP, timeout=90)

    factories = Factories(
        bridge=BridgeClient,
        mic=mic,
        # The model was downloaded when speech-to-text was installed; loading it never needs the network.
        stt=lambda: SpeechService(speech.python, WORKER, engine.key, engine.model, env={"HF_HUB_OFFLINE": "1"}),
        refiner=refiner,
        editor=editor,
    )
    return VoiceService(paths.logs, bus, factories, engine=engine.key, brain=brain)
