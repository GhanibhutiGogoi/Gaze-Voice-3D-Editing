"""The headset for Unity VR: a PC VR runtime such as Meta Quest Link, and a Unity project that can play to it.

In Unity VR, Unity plays the scene to the headset. The GazeVoice package listens to the controllers' B and Y buttons
for hold-to-talk and follows where the eyes look (the Quest Pro's eye tracking), or where the head points without
it, so the person never needs the keyboard.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..blender_socket import bridge_request, port_open
from ..jobs import Job, JobError
from ..platforms import is_windows, process_running
from .base import ActionSpec, Component, Health
from .unity_bridge import BRIDGE_PORT

OPENXR_KEY = r"SOFTWARE\Khronos\OpenXR\1"
LINK_PROCESS = "OVRServer_x64"  # Meta Quest Link's runtime; it runs while the Link app is open
TALK_BUTTONS = "B or Y"  # matches TalkButtons in unity_package/Editor/GazeVoiceVR.cs
NO_RUNTIME = ("No OpenXR runtime is set. Install Meta Quest Link (the Meta Horizon Link app) and make it the active "
              "OpenXR runtime in its settings.")
NO_LINK = "Meta Quest Link isn't running. Open the Meta Horizon Link app and connect the headset."
NO_LOADER = "Turn on OpenXR for Windows in Unity: Edit \u203a Project Settings \u203a XR Plug-in Management."
NO_XR_MODULE = "The project has no XR module. Add XR Plug-in Management and OpenXR in Unity's Package Manager."
OLD_BRIDGE = "Update the GazeVoice Unity Bridge on its unit to use VR."
NO_EYES = ("Eye tracking is off, so gaze follows the head. To use the eyes: on the Quest Pro, turn on Settings \u203a "
           "Movement tracking \u203a Eye tracking; in the Meta Horizon Link app, turn on Settings \u203a Beta \u203a "
           "Developer runtime features, then Eye tracking over Meta Quest Link. Then leave Play mode and press Play again.")
NO_EYE_FEATURE = "Eye gaze is off in the project. Press Prepare VR scene to turn it on."


def active_openxr_runtime() -> tuple[str, Path] | None:
    """The OpenXR runtime that apps start, as (name, manifest): XR_RUNTIME_JSON, or the one Windows has active."""
    path = os.environ.get("XR_RUNTIME_JSON")
    if not path and is_windows():
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, OPENXR_KEY) as key:
                path, _ = winreg.QueryValueEx(key, "ActiveRuntime")
        except OSError:
            return None
    if not path:
        return None
    manifest = Path(path)
    try:
        name = json.loads(manifest.read_text(encoding="utf-8"))["runtime"]["name"]
    except (OSError, ValueError, KeyError, TypeError):
        name = None
    return (name if isinstance(name, str) and name.strip() else manifest.stem), manifest


def is_meta_runtime(name: str) -> bool:
    return any(word in name.casefold() for word in ("oculus", "meta"))


class UnityVR(Component):
    id = "unity_vr"
    name = "VR headset"
    role = "Unity plays to it \u00b7 B or Y talks \u00b7 the eyes point"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.port = BRIDGE_PORT

    def _vr_status(self) -> dict | None:
        """What the GazeVoice package says about XR in Unity; None if Unity isn't connected."""
        if not port_open("127.0.0.1", self.port):
            return None
        try:
            return bridge_request({"cmd": "vr_status"}, port=self.port, timeout=1.0)
        except (OSError, ValueError):
            return None

    def _runtime(self) -> tuple[Health | None, str, list]:
        """(None, runtime name, details) when the headset's runtime is ready, else (health, what to do, details)."""
        if not is_windows():
            return Health.MISSING, ("Unity VR needs Windows, where Unity plays to a PC VR headset such as a "
                                    "Meta Quest over Link."), []
        runtime = active_openxr_runtime()
        if runtime is None:
            return Health.MISSING, NO_RUNTIME, []
        details = [("OpenXR runtime", runtime[0])]
        if is_meta_runtime(runtime[0]) and not process_running(self.runner, LINK_PROCESS, self.ctx.probes):
            return Health.INSTALLED, NO_LINK, details
        return None, runtime[0], details

    def check(self):
        if self.settings.voice_host != "unity_vr":
            return self.state(Health.INSTALLED, "Used in Unity VR. Pick Unity VR as the 3D host in Routing.")
        health, runtime, details = self._runtime()
        if health is not None:
            return self.state(health, runtime, details)
        status = self._vr_status()
        if status is None:
            return self.state(Health.INSTALLED, f"{runtime} is ready. Open the project in Unity with the GazeVoice "
                              "Bridge to check its XR setup.", details)
        if not status.get("ok"):
            return self.state(Health.INSTALLED, OLD_BRIDGE, details)
        if not status.get("xr"):
            return self.state(Health.MISSING, NO_XR_MODULE, details)
        loaders = [str(name) for name in status.get("loaders") or []]
        if not loaders:
            return self.state(Health.MISSING, NO_LOADER, details)
        details.append(("XR plug-in", ", ".join(loaders)))
        details.append(("Scene", status.get("scene") or "none"))
        details.append(("Gaze", self._gaze_words(status)))
        key = status.get("key") or TALK_BUTTONS
        test = ActionSpec("test", "Test")
        if status.get("active"):
            headset = status.get("headset") or "the headset"
            details.append(("Headset", headset))
            looks = "where you look" if status.get("eyes") else "where your head points"
            return self.state(Health.READY, f"In VR on {headset}. Hold {key} to talk: {looks} is what \u201chere\u201d "
                              "and \u201cthis\u201d mean.", details, [test, ActionSpec("leave_vr", "Leave VR")])
        if status.get("playing"):
            return self.state(Health.INSTALLED, "Unity is in Play mode, but not in the headset. Check that the headset "
                              "is connected over Link, then press Play again.", details,
                              [test, ActionSpec("leave_vr", "Leave VR")])
        actions = [ActionSpec("enter_vr", "Enter VR"), test, ActionSpec("prepare", "Prepare VR scene")]
        if not status.get("vr_scene"):
            return self.state(Health.INSTALLED, "The project has no GazeVoice VR scene yet. Press Prepare VR scene: it "
                              "adds a plane to build on and the template's player rig, and turns on eye gaze.", details, actions)
        return self.state(Health.READY, "Ready. Press Enter VR (or Play in Unity), put the headset on, and hold "
                          f"{key} to talk. Edits made while playing are kept.", details, actions)

    @staticmethod
    def _gaze_words(status: dict) -> str:
        """Where gaze comes from, and what to do when it can't be the eyes."""
        if not status.get("eye_feature"):
            return NO_EYE_FEATURE
        if status.get("eyes"):
            return "Eyes (Quest Pro eye tracking)"
        if status.get("eye_runtime") is False:
            return NO_EYES
        if status.get("active"):
            return "Head direction, until the eyes are tracked"
        return "Eyes when Link sends eye tracking, otherwise head direction"

    def _request(self, request: dict, timeout: float = 5.0) -> dict:
        try:
            reply = bridge_request(request, port=self.port, timeout=timeout)
        except (OSError, ValueError) as exc:
            raise JobError(f"Unity didn't answer: {exc}. Open the project with the GazeVoice Bridge.") from exc
        if not reply.get("ok"):
            raise JobError(OLD_BRIDGE if reply.get("error") == "unknown command" else str(reply.get("error")))
        return reply

    def action_test(self, job: Job) -> dict:
        health, runtime, _ = self._runtime()
        if health is not None:
            raise JobError(runtime)
        job.log(f"OpenXR runtime: {runtime}")
        status = self._request({"cmd": "vr_status"})
        if not status.get("xr"):
            raise JobError(NO_XR_MODULE)
        loaders = status.get("loaders") or []
        if not loaders:
            raise JobError(NO_LOADER)
        job.log(f"Unity's XR plug-in for Windows: {', '.join(loaders)}")
        job.log(f"Gaze: {self._gaze_words(status)}")
        if status.get("active"):
            job.log(f"In VR on {status.get('headset') or 'the headset'}; controllers: "
                    f"{', '.join(status.get('controllers') or []) or 'none tracked'}")
            if not status.get("eyes"):
                gaze = self._request({"cmd": "vr_gaze"})
                looked = [sample for sample in gaze.get("samples") or [] if sample.get("uid") is not None]
                job.log(f"Head gaze: {len(looked)} of the last {len(gaze.get('samples') or [])} samples looked at "
                        "something in the scene.")
        else:
            job.log("Unity isn't playing to the headset now. Enter VR when you're ready.")
        return {"runtime": runtime, "loaders": len(loaders), "in_vr": bool(status.get("active")),
                "eyes": bool(status.get("eyes"))}

    def action_prepare(self, job: Job) -> dict:
        """Build (or open) the GazeVoice VR scene and set OpenXR up for the Quest Pro, in the open project."""
        job.log("Preparing the VR scene: a plane, the template's player rig, and OpenXR with eye gaze…")
        reply = self._request({"cmd": "vr_setup"}, timeout=120.0)
        for note in reply.get("notes") or []:
            job.log(str(note))
        xr = reply.get("xr") or {}
        if xr.get("features"):
            job.log(f"OpenXR features on: {', '.join(xr['features'])}")
        if xr.get("repaired"):
            job.log(f"Repaired {xr['repaired']} broken OpenXR settings entries.")
        job.log(f"{'Made' if reply.get('made') else 'Opened'} {reply.get('scene')}.")
        return {"scene": reply.get("scene"), "made": bool(reply.get("made"))}

    def action_enter_vr(self, job: Job) -> dict:
        self._request({"cmd": "vr_play"})
        job.log(f"Unity is entering Play mode. Put the headset on, look where you mean, and hold {TALK_BUTTONS} to talk.")
        return {"playing": True}

    def action_leave_vr(self, job: Job) -> dict:
        self._request({"cmd": "vr_stop"})
        job.log("Unity is leaving Play mode.")
        return {"playing": False}
