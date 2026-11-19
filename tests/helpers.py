"""Shared test helpers."""

import plistlib
import tempfile
from pathlib import Path
import ctypes
import os
import sys
import time
from contextlib import ExitStack
from unittest.mock import patch

from gazevoice.components.base import Context
from gazevoice.events import EventBus
from gazevoice.jobs import Job
from gazevoice.paths import AppPaths
from gazevoice.runner import FakeRunner
from gazevoice.settings import Settings


def macos_adapters(*modules):
    """Exercise the existing macOS adapter contracts on either test host."""
    stack = ExitStack()
    for module in modules:
        if hasattr(module, "is_windows"):
            stack.enter_context(patch.object(module, "is_windows", return_value=False))
        if hasattr(module, "is_apple_silicon"):
            stack.enter_context(patch.object(module, "is_apple_silicon", return_value=True))
        if hasattr(module, "install_command"):
            stack.enter_context(patch.object(module, "install_command", side_effect=lambda package, cask=False:
                ["brew", "install", *(["--cask"] if cask else []), package]))
    return stack


def process_alive(pid):
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87
    try:
        return api.WaitForSingleObject(handle, 0) == 258
    finally:
        api.CloseHandle(handle)


class TempCtx:
    """A Context with a FakeRunner and a throwaway app folder."""

    def __init__(self, which=None, settings=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runner = FakeRunner(which=which)
        self.paths = AppPaths(self.root / "app")
        self.paths.ensure()
        self.ctx = Context(runner=self.runner, paths=self.paths, settings=settings or Settings())

    def cleanup(self):
        self._tmp.cleanup()


def make_job(component_id="test", action="test"):
    return Job(id="job1", component_id=component_id, action=action, _bus=EventBus())


def fake_app(root: Path, name="Blender.app") -> Path:
    """A fake Blender.app; returns the path of its binary."""
    binary = root / name / "Contents" / "MacOS" / "Blender"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    return binary


def with_blender(t: TempCtx, version_line="Blender 5.2.2 LTS", running=False) -> Path:
    """Put a fake Blender into a TempCtx: app on disk, `--version` and `pgrep` faked."""
    binary = fake_app(t.root)
    t.runner.add([str(binary), "--version"], stdout=f"{version_line}\n")
    t.runner.add(["pgrep", "-x", "Blender"], returncode=0 if running else 1)
    return binary


def fake_unity_editor(root: Path, version="6000.3.7f1") -> Path:
    """A fake Unity.app under an editors folder; returns the app."""
    app = root / version / "Unity.app"
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "MacOS" / "Unity").write_text("#!/bin/sh\n")
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleVersion": version}, handle)
    return app


def fake_unity_project(path: Path, version="6000.3.7f1") -> Path:
    """The files that make a folder a Unity project."""
    (path / "Assets").mkdir(parents=True, exist_ok=True)
    (path / "ProjectSettings").mkdir(exist_ok=True)
    (path / "Packages").mkdir(exist_ok=True)
    (path / "ProjectSettings" / "ProjectVersion.txt").write_text(
        f"m_EditorVersion: {version}\nm_EditorVersionWithRevision: {version} (abc123)\n")
    (path / "Packages" / "manifest.json").write_text(
        '{\n  "dependencies": {\n    "com.unity.ugui": "2.0.0",\n    "com.unity.modules.physics": "1.0.0"\n  }\n}\n')
    return path
