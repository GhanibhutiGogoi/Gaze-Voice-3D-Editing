"""Put the GazeVoice package into a Unity project.

It goes in the project's own Packages folder, which Unity picks up as an embedded package. That
leaves manifest.json alone, and Unity never has to read this checkout: macOS guards ~/Documents, so
pointing a project here would make Unity ask for access, and refusing would break the package.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .jobs import JobError

PACKAGE_NAME = "com.gazevoice.bridge"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "unity_package"
_STAGING = ".gazevoice-new~"  # Unity ignores folders ending in ~
_OLD = ".gazevoice-old~"
_TRIES = 20
_WAIT = 0.25


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
    old = target.with_name(_OLD)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    try:
        shutil.copytree(PACKAGE_DIR, staging, ignore=shutil.ignore_patterns(".DS_Store"))
        if _swap(staging, target, old):
            shutil.rmtree(old, ignore_errors=True)
        else:
            _sync(staging, target)
    except OSError as exc:
        raise JobError(f"Couldn't copy the GazeVoice package into the project: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _swap(staging: Path, target: Path, old: Path) -> bool:
    """Move the old copy aside, then the new one in. Windows refuses while Unity is reading the folder, so this
    tries for a few seconds. A deleted folder that Unity still watches keeps its name until Unity lets go, which
    is why the old copy is moved rather than deleted first."""
    for attempt in range(_TRIES):
        try:
            if target.exists():
                target.rename(old)
            staging.rename(target)
            return True
        except PermissionError:
            if attempt + 1 < _TRIES:
                time.sleep(_WAIT)
    return False


def _sync(source: Path, target: Path) -> None:
    """Bring the copy in place up to date, file by file: the fallback when the folder can't be swapped."""
    wanted = _contents(source)
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(target.rglob("*"), reverse=True):
        relative = path.relative_to(target).as_posix()
        if path.is_file() and relative not in wanted:
            path.unlink()
        elif path.is_dir() and not any(name == relative or name.startswith(relative + "/") for name in wanted):
            shutil.rmtree(path, ignore_errors=True)
    for relative, data in wanted.items():
        destination = target / relative
        if destination.is_file() and destination.read_bytes() == data:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def remove_bridge(project: Path) -> None:
    shutil.rmtree(_target(project), ignore_errors=True)
