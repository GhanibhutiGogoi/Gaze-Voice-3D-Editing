"""Turning the control center's units into a running voice loop."""

from __future__ import annotations

from typing import Mapping

from ..blender_socket import BridgeClient
from ..components.base import ComponentState, Context, Health
from ..components.brains import BRAIN_ORDER, brain_pending
from ..events import EventBus
from ..providers import ChatClient, KeyStore
from ..settings import Settings
from .api_brain import ApiSession, blender_mcp
from .audio import MicError, MicStream, mic_argv
from .brains import BrainError, ClaudeSession, CodexSession, claude_session_argv
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


STEP_LABELS = (("refiner", "Cleanup"), ("editor", "Editing"))


def _step_missing(states, step: str, label: str, source: str | None, settings: Settings) -> str | None:
    if source is None:
        preference = getattr(settings, f"{step}_source")
        if brain_pending(states, preference if preference in ("auto", *BRAIN_ORDER) else "auto"):
            return f"{label}: the brain is still being checked."
        return f"{label}: no brain is logged in. Log into Claude Code or Codex, or pick an API provider in Routing."
    if source in BRAIN_ORDER:
        return None
    state = states.get(source)
    if state is None:
        return f"{label}: the provider {source} isn't set up."
    if state.health is not Health.READY:
        return f"{label}: {state.name} has no API key yet. Add it in the rack."
    if not getattr(settings, f"{step}_api_model"):
        return f"{label}: pick a {state.name} model in Routing."
    return None


def voice_readiness(
    states: Mapping[str, ComponentState], sources: Mapping[str, str | None] | str | None, settings: Settings | None = None
) -> list[str]:
    """What has to be fixed before voice can start, in plain words. Empty means ready."""
    from ..workflow import as_sources

    sources, settings = as_sources(sources), settings or Settings()
    missing: list[str] = []
    for cid, good, label in _NEEDED:
        state = states.get(cid)
        if state is None:
            missing.append(f"{label} is missing.")
        elif state.health is Health.CHECKING:
            missing.append(f"{label} is still being checked.")
        elif state.health not in good:
            missing.append(f"{label}: {state.summary}")
        if cid == "speech":
            for step, step_label in STEP_LABELS:
                line = _step_missing(states, step, step_label, sources[step], settings)
                if line:
                    missing.append(line)
    return missing


def build_voice(ctx: Context, bus: EventBus, sources: Mapping[str, str], keys: KeyStore | None = None) -> VoiceService:
    """A VoiceService wired to the real mic, speech engine, each step's brain and Blender."""
    from ..components.providers import config_for

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

    def claude(prompt, model, effort, mcp, max_turns, **start_kwargs) -> ClaudeSession:
        argv = claude_session_argv(prompt, model, mcp, max_turns=max_turns, effort=effort)
        session = ClaudeSession(argv, cwd=paths.work)
        session.start_kwargs = start_kwargs
        session.effort = effort  # shown with each command, next to the model that answered
        return session

    keys = keys or KeyStore(ctx.runner)

    def provider(step: str, source: str) -> ApiSession:
        config = config_for(settings, source)
        if config is None:
            raise BrainError(f"The provider {source} isn't set up. Add it in the rack.")
        key = keys.get(source)
        if not key:
            raise BrainError(f"{config.name} has no API key. Add it in the rack.")
        editing = step == "editor"
        return ApiSession(
            ChatClient(config, key), getattr(settings, f"{step}_api_model"),
            (EDITOR_PROMPT if editing else REFINER_PROMPT).read_text(), kind=config.kind,
            thinking=getattr(settings, f"{step}_api_thinking"),
            mcp_factory=blender_mcp(server_cmd) if editing else None, provider=config.name,
        )

    def editor():
        source = sources["editor"]
        if source == "codex":
            return CodexSession(paths.work, EDITOR_PROMPT.read_text(), mcp_cmd=server_cmd)
        if source == "claude":
            return claude(EDITOR_PROMPT, settings.editor_model, settings.editor_effort, mcp_config, 12,
                          warmup=EDITOR_WARMUP, require_mcp="blender", timeout=150)
        return provider("editor", source)

    def refiner():
        source = sources["refiner"]
        if source == "codex":
            return CodexSession(paths.work, REFINER_PROMPT.read_text())
        if source == "claude":
            return claude(REFINER_PROMPT, settings.refiner_model, settings.refiner_effort, None, 1,
                          warmup=REFINER_WARMUP, timeout=90)
        return provider("refiner", source)

    factories = Factories(
        bridge=BridgeClient,
        mic=mic,
        # The model was downloaded when speech-to-text was installed; loading it never needs the network.
        stt=lambda: SpeechService(speech.python, WORKER, engine.key, engine.model, env={"HF_HUB_OFFLINE": "1"}),
        refiner=refiner,
        editor=editor,
    )
    return VoiceService(paths.logs, bus, factories, engine=engine.key, brain=sources["editor"])
