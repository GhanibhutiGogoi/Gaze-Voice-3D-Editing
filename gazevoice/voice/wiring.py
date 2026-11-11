"""Turning the control center's units into a running voice loop."""

from __future__ import annotations

from typing import Mapping

from ..blender_socket import BridgeClient
from ..components.base import ComponentState, Context, Health
from ..components.brains import BRAIN_ORDER, brain_pending
from ..components.mlagents import LIVE_STATES as TRAINING_LIVE
from ..events import EventBus
from ..providers import ChatClient, KeyStore
from ..settings import Settings
from .api_brain import ApiSession, host_mcp
from .audio import MicError, MicStream, mic_argv
from .brains import BrainError, ClaudeSession, CodexSession, claude_session_argv
from .commands import (
    EDITOR_PROMPT,
    PLAY_MODE_SAYING,
    REFINER_PROMPT,
    UNITY_EDITOR_PROMPT,
    UNITY_REFINER_PROMPT,
)
from .gaze_tracker import GazeError, GazeTracker
from .picture import Describer, clear_leftover_images
from .service import EDITOR_WARMUP, REFINER_WARMUP, Factories, PointerTracker, VoiceService
from .stt import SpeechService

# unit id, the healths that are good enough, what to call it
_COMMON_NEEDED = (
    ("microphone", {Health.READY, Health.INSTALLED}, "Microphone"),  # untested is fine: starting voice tests it
    ("speech", {Health.READY}, "Speech-to-text"),
)
_BLENDER_NEEDED = (
    ("blender", {Health.READY}, "Blender"),
    ("mcp_addon", {Health.READY}, "Blender MCP add-on"),
    ("mcp_server", {Health.READY, Health.INSTALLED}, "Blender MCP server"),
    ("bridge", {Health.READY}, "GazeVoice Bridge"),
)


MCP_SECONDS = {"blender": 180.0, "unity": 330.0}  # how long one tool call may take; Unity's C# can run for 300 s
STEP_LABELS = (("refiner", "Cleanup"), ("editor", "Editing"))
EDITOR_STEPS = 150  # model and tool turns per editing command; detailed builds take many


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
    needed = list(_COMMON_NEEDED)
    if settings.voice_host == "blender":
        needed.extend(_BLENDER_NEEDED)
    else:
        from ..blender_socket import port_open

        if not port_open("127.0.0.1", 9878):
            missing.append("Unity Editor bridge isn't connected. Press Set up Unity in the rack, or open your project in Unity.")
    for cid, good, label in needed:
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


def build_voice(ctx: Context, bus: EventBus, sources: Mapping[str, str], keys: KeyStore | None = None,
                control_url: str | None = None) -> VoiceService:
    """A VoiceService wired to the selected editor host, mic, speech engine and brains.

    `control_url` is where the dashboard is. Unity's built-in MCP server uses it to reach ML-Agents training.
    """
    from ..components.providers import config_for

    from ..components.brains import write_mcp_config
    from ..components.gaze import WORKER as GAZE_WORKER, Gaze, camera_gaze_installed, load_calibration
    from ..components.microphone import Microphone, pick_camera, pick_device
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
    host = settings.voice_host
    mcp_server = host
    if host == "blender":
        from ..components.blender import find_blender
        from ..components.blender_mcp import server_command

        server_cmd = server_command(paths, find_blender(ctx.runner))
        bridge_factory = BridgeClient
    else:
        from ..unity_mcp import server_command

        server_cmd = server_command(settings, control_url)
        bridge_factory = lambda: BridgeClient(port=9878)
    mcp_config = write_mcp_config(paths, server_cmd, mcp_server)
    editor_prompt = EDITOR_PROMPT if host == "blender" else UNITY_EDITOR_PROMPT
    refiner_prompt = REFINER_PROMPT if host == "blender" else UNITY_REFINER_PROMPT
    paths.work.mkdir(parents=True, exist_ok=True)

    def claude(prompt, model, effort, mcp, max_turns, **start_kwargs) -> ClaudeSession:
        argv = claude_session_argv(prompt, model, mcp, max_turns=max_turns, effort=effort, mcp_server=mcp_server)
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
            (editor_prompt if editing else refiner_prompt).read_text(), kind=config.kind,
            thinking=getattr(settings, f"{step}_api_thinking"),
            mcp_factory=host_mcp(server_cmd, timeout=MCP_SECONDS[host]) if editing else None, provider=config.name,
            max_steps=EDITOR_STEPS if editing else 1, host="Unity" if host == "unity" else "Blender",
        )

    def editor():
        source = sources["editor"]
        if source == "codex":
            return CodexSession(paths.work, editor_prompt.read_text(), mcp_cmd=server_cmd, mcp_server=mcp_server)
        if source == "claude":
            return claude(editor_prompt, settings.editor_model, settings.editor_effort, mcp_config, EDITOR_STEPS,
                          warmup=EDITOR_WARMUP, require_mcp=mcp_server, timeout=150)
        return provider("editor", source)

    def refiner():
        source = sources["refiner"]
        if source == "codex":
            return CodexSession(paths.work, refiner_prompt.read_text())
        if source == "claude":
            return claude(refiner_prompt, settings.refiner_model, settings.refiner_effort, None, 1,
                          warmup=REFINER_WARMUP, timeout=90)
        return provider("refiner", source)

    def camera_gaze() -> GazeTracker:
        # Picked at each start from a fresh listing: the camera's number moves when a phone or a webcam is plugged in,
        # and one plugged in just now has to count.
        picked = pick_camera(microphone.cameras(fresh=True), settings.camera_device)
        if picked is None:
            raise GazeError("That camera isn't connected. Pick another one in Routing.")
        calibration = load_calibration(paths.gaze_calibration)
        if calibration is None:
            raise GazeError("The saved gaze calibration is out of date. Calibrate gaze again.")
        return GazeTracker(Gaze(ctx).python, GAZE_WORKER, picked[0], calibration, model_file=paths.gaze_model_file)

    # Gaze is for Blender. The mouse needs no setup. The camera needs gaze installed and calibrated; without those
    # there is no gaze part, and voice uses the selection. A missing camera is found at each start, not here: this
    # service is used again the next time voice starts, and by then the camera may be plugged in.
    gaze_mode = settings.gaze_mode if host == "blender" else "off"
    gaze = None
    if gaze_mode != "off":
        if settings.gaze_source == "mouse":
            gaze = PointerTracker
        elif camera_gaze_installed(paths):
            gaze = camera_gaze

    def pictures() -> Describer | None:
        clear_leftover_images(paths)  # whatever a crash left goes at every start, with pictures on or not
        return Describer(paths, ctx.runner) if settings.gaze_pictures and gaze_mode == "on" else None

    def training_notice() -> str | None:
        """While a training run is going, the editing AI is told so with every command.

        A run that is waiting for Play leaves Unity in Edit mode, where scene edits survive. Once it is running,
        Unity is in Play mode for it, and the scene must be left alone.
        """
        training = ctx.training
        run = training.status() if training is not None else None
        if run is None or run["state"] not in TRAINING_LIVE:
            return None
        if run["state"] == "waiting":
            return f"Training: {run['run_id']} is waiting for Unity to press Play."
        return f"Training: {run['run_id']} is running, so {PLAY_MODE_SAYING} for it."

    factories = Factories(
        bridge=bridge_factory,
        mic=mic,
        # The model was downloaded when speech-to-text was installed; loading it never needs the network.
        stt=lambda: SpeechService(speech.python, WORKER, engine.key, engine.model, env={"HF_HUB_OFFLINE": "1"}),
        refiner=refiner,
        editor=editor,
        gaze=gaze,
        pictures=pictures if host == "blender" else None,
        editor_notice=training_notice if host == "unity" else None,
    )
    return VoiceService(paths.logs, bus, factories, engine=engine.key, brain=sources["editor"], host=host,
                        gaze_mode=gaze_mode, gaze_source=settings.gaze_source,
                        camera=ctx.camera if gaze is camera_gaze else None, overlay=ctx.camera)
