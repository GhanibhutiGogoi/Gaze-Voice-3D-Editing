"""Operating-system boundaries. Importing this module never installs or opens anything."""
from __future__ import annotations

import csv
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def is_apple_silicon() -> bool:
    if sys.platform != "darwin":
        return False
    if platform.machine().lower() in ("arm64", "aarch64"):
        return True
    try:
        result = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
                                capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and result.stdout.strip() == "1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def system_info() -> dict:
    return {"id": "windows" if is_windows() else "macos" if sys.platform == "darwin" else sys.platform,
            "name": "Windows" if is_windows() else "macOS" if sys.platform == "darwin" else platform.system(),
            "architecture": platform.machine(), "key_store": "Windows Credential Manager" if is_windows() else "macOS Keychain",
            "talk_key": "Ctrl+Alt+Space" if is_windows() else "⌥ Space"}


def app_data(*parts: str, roaming: bool = False) -> Path:
    if is_windows():
        key = "APPDATA" if roaming else "LOCALAPPDATA"
        base = Path(os.environ.get(key, Path.home() / "AppData" / ("Roaming" if roaming else "Local")))
    else:
        base = Path.home() / "Library" / "Application Support"
    return base.joinpath(*parts)


def venv_executable(folder: Path, name: str = "python") -> Path:
    return folder / ("Scripts" if is_windows() else "bin") / (name + ".exe" if is_windows() else name)


def process_options() -> dict:
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if is_windows() else {"start_new_session": True}


def child_env(extra=None) -> dict:
    return {
        **os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **(extra or {})}


def spawn(argv, **kwargs):
    """Start an owned worker; all its descendants are reclaimed when it exits."""
    kwargs["env"] = child_env(kwargs.get("env"))
    if is_windows():
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 4  # CREATE_SUSPENDED
    proc = subprocess.Popen(argv, **kwargs)
    if is_windows():
        try:
            from .windows_process import own_and_resume
            own_and_resume(proc)
        except BaseException:
            proc.kill()
            proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            raise
    return proc


def command_argv(argv) -> list[str]:
    args = [str(item) for item in argv]
    if is_windows() and args:
        resolved = find_executable(args[0]) or args[0]
        if Path(resolved).suffix.lower() in (".cmd", ".bat"):
            # npm CLIs ship a JS entry point next to their Windows shim. Avoid cmd.exe quoting.
            prefix = Path(resolved).parent / "node_modules"
            entries = {"codex": prefix / "@openai" / "codex" / "bin" / "codex.js",
                       "claude": prefix / "@anthropic-ai" / "claude-code" / "cli.js"}
            entry = entries.get(Path(resolved).stem.lower())
            if entry is not None and entry.is_file():
                return [shutil.which("node") or "node", str(entry), *args[1:]]
            raise OSError(f"Cannot safely launch Windows command shim {resolved}. Install the native executable or npm package with its Node entry point.")
        args[0] = resolved
    return args


def find_executable(name: str) -> str | None:
    """Resolve installs made after this process inherited its PATH."""
    found = shutil.which(name)
    if found or not is_windows() or Path(name).name != name:
        return found
    links = app_data("Microsoft", "WinGet", "Links")
    return shutil.which(name, path=str(links))


KILL = getattr(signal, "SIGKILL", 9)


def signal_process_tree(proc: subprocess.Popen, sig: int) -> None:
    try:
        if is_windows():
            if sig == signal.SIGINT:
                if proc.poll() is None:
                    try:
                        proc.send_signal(signal.CTRL_BREAK_EVENT)
                        return
                    except OSError:
                        pass  # No console is attached; still reclaim the owned job.
            if hasattr(proc, "close_tree"):
                proc.close_tree()
            else:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if proc.poll() is None:
                    proc.kill()
        else:
            os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def process_running(runner, name: str, probes=None, *, windows: bool | None = None) -> bool:
    windows = is_windows() if windows is None else windows
    argv = ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"] if windows else ["pgrep", "-x", name]
    result = probes.run(runner, argv, timeout=5, ttl=3, raise_on_timeout=True) if probes else runner.run(argv, timeout=5)
    if not windows:
        return result.ok
    return result.ok and any(row and row[0].casefold() == f"{name}.exe".casefold() for row in csv.reader(result.stdout.splitlines()))


def audio_input(device: str, *, windows: bool | None = None) -> list[str]:
    windows = is_windows() if windows is None else windows
    return ["-f", "dshow", "-i", f"audio={device}"] if windows else ["-f", "avfoundation", "-i", f":{device}"]


def install_command(package: str, *, cask: bool = False) -> list[str]:
    if is_windows():
        ids = {"blender": "BlenderFoundation.Blender", "ffmpeg": "Gyan.FFmpeg", "unity-hub": "Unity.UnityHub"}
        return ["winget", "install", "--exact", "--id", ids[package], "--source", "winget",
                "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"]
    return ["brew", "install", *(["--cask"] if cask else []), package]


def launch_app(runner, binary: Path, args=()) -> None:
    if is_windows():
        subprocess.Popen(command_argv([binary, *args]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    else:
        result = runner.run(["open", "-a", binary, *(["--args", *args] if args else [])], timeout=30)
        if not result.ok:
            raise OSError(result.stderr.strip() or str(result.returncode))
