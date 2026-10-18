"""Where GazeVoice keeps its files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "GAZEVOICE_HOME"


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    @property
    def venvs(self) -> Path:
        return self.root / "venvs"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def work(self) -> Path:
        """Scratch space, and a neutral working directory for CLI agents."""
        return self.root / "work"

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def mcp_server_dir(self) -> Path:
        return self.root / "blender-mcp"

    def ensure(self) -> None:
        for directory in (self.root, self.downloads, self.venvs, self.logs, self.work):
            directory.mkdir(parents=True, exist_ok=True)


def tilde(path: Path | str) -> str:
    """Show a path with the home folder as ~, the way it reads in Finder and Terminal."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def default_paths() -> AppPaths:
    override = os.environ.get(HOME_ENV)
    if override:
        return AppPaths(Path(override).expanduser())
    return AppPaths(Path.home() / "Library" / "Application Support" / "GazeVoice")
