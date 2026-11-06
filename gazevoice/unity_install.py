"""Put the GazeVoice package into a Unity project.

It goes in the project's own Packages folder, which Unity picks up as an embedded package. That
leaves manifest.json alone, and Unity never has to read this checkout: macOS guards ~/Documents, so
pointing a project here would make Unity ask for access, and refusing would break the package.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .jobs import JobError

PACKAGE_NAME = "com.gazevoice.bridge"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "unity_package"
_STAGING = ".gazevoice-new~"  # Unity ignores folders ending in ~


def _target(project: Path) -> Path:
    return Path(project) / "Packages" / PACKAGE_NAME


def _contents(folder: Path) -> dict[str, bytes]:
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob("*") if path.is_file() and path.name != ".DS_Store"}


def bridge_installed(project: Path) -> bool:
    return (_target(project) / "package.json").is_file()


def bridge_current(project: Path) -> bool:
    """Whether the project's copy is the same as this checkout's."""
    return bridge_installed(project) and _contents(_target(project)) == _contents(PACKAGE_DIR)


def add_bridge(project: Path) -> None:
    """Copy the package in, replacing any older copy in one step."""
    target = _target(project)
    staging = target.with_name(_STAGING)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(PACKAGE_DIR, staging, ignore=shutil.ignore_patterns(".DS_Store"))
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise JobError(f"Couldn't copy the GazeVoice package into the project: {exc}") from exc


def remove_bridge(project: Path) -> None:
    shutil.rmtree(_target(project), ignore_errors=True)
