"""Set up uv in an app-owned environment, only when Install is pressed."""
from __future__ import annotations

import sys
import threading

from .jobs import JobError
from .platforms import venv_executable

_install_lock = threading.Lock()


def find_uv(runner, paths) -> str | None:
    folder = paths.root / "tools" / "uv"
    binary = venv_executable(folder, "uv")
    if binary.is_file() and (folder / ".gazevoice-ready").is_file():
        return str(binary)
    return "uv" if runner.which("uv") else None


def ensure_uv(runner, paths, job) -> str:
    """Serialize shared setup; return an immediately usable command, without changing PATH."""
    with _install_lock:
        existing = find_uv(runner, paths)
        if existing:
            return existing
        folder = paths.root / "tools" / "uv"
        python = venv_executable(folder)
        if folder.exists() and not ((folder / "pyvenv.cfg").is_file() or
                                    (folder.is_dir() and not any(folder.iterdir()))):
            raise JobError(f"{folder} isn't a Python environment, so it's left alone. Move it and retry Install.")
        folder.parent.mkdir(parents=True, exist_ok=True)
        if not python.is_file():
            job.log("Setting up uv in GazeVoice's own tools folder…")
            code = runner.stream([sys.executable, "-m", "venv", "--clear", str(folder)], job.log, timeout=180)
            if code != 0:
                raise JobError(f"Could not create uv's Python environment (exit {code}). See the log above, then retry Install.")
        job.log("Installing uv from PyPI for GazeVoice…")
        code = runner.stream([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                              "--no-input", "--only-binary=:all:", "uv"], job.log, timeout=600)
        if code != 0:
            raise JobError(f"Could not install uv (exit {code}). Check your connection and the log, then retry Install.")
        binary = venv_executable(folder, "uv")
        result = runner.run([str(binary), "--version"], timeout=15)
        if not binary.is_file() or not result.ok or not result.stdout.strip().startswith("uv "):
            raise JobError("uv was downloaded but could not run. See the log and retry Install.")
        (folder / ".gazevoice-ready").write_text(result.stdout.strip(), encoding="utf-8")
        job.log("uv is ready. Continuing this installation…")
        return str(binary)
