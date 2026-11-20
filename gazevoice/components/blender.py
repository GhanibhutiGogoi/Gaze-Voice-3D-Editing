"""Blender itself: find it, install it with Homebrew, launch it."""

from __future__ import annotations

import re
import os
import threading
from pathlib import Path
from typing import Iterable, Sequence

from ..jobs import Job, JobError
from ..paths import tilde
from ..probes import ProbeCache
from ..runner import CommandRunner
from ..platforms import app_data, install_command, is_windows, launch_app, process_running
from .base import ActionSpec, Component, ComponentState, Health

MIN_BLENDER = (5, 1)
APP_CANDIDATES = (Path("/Applications/Blender.app"), Path.home() / "Applications" / "Blender.app")
SPOTLIGHT_QUERY = "kMDItemCFBundleIdentifier == 'org.blenderfoundation.blender'"
EXTENSION_REPO = "user_default"
_VERSION = re.compile(r"Blender\s+(\d+)\.(\d+)(?:\.(\d+))?")

_version_cache: dict[tuple[str, float], tuple[int, int, int] | None] = {}
_version_lock = threading.Lock()


def binary_in(app: Path) -> Path:
    if app.suffix.lower() == ".exe":
        return app
    if is_windows() and app.suffix != ".app":
        return app / "blender.exe"
    return app / "Contents" / "MacOS" / "Blender"


def app_path(binary: Path) -> Path:
    return binary.parent if binary.suffix.lower() == ".exe" else binary.parents[2]


def find_blender(
    runner: CommandRunner, candidates: Iterable[Path] | None = None, probes: ProbeCache | None = None
) -> Path | None:
    """Path to the Blender binary, or None. Checks pass `probes` to share one Spotlight query."""
    if is_windows():
        if candidates is None:
            configured = os.environ.get("BLENDER_PATH")
            if configured and binary_in(Path(configured)).is_file():
                return binary_in(Path(configured))
            path_binary = runner.which("blender")
            roots = [Path(os.environ.get(key, default)) / "Blender Foundation" for key, default in
                     (("ProgramFiles", r"C:\Program Files"), ("ProgramFiles(x86)", r"C:\Program Files (x86)"))]
            candidates = [*(Path(p) for p in (configured, path_binary) if p),
                          *(p for root in roots for p in sorted(root.glob("Blender*"), reverse=True)),
                          *sorted(app_data("Programs", "Blender Foundation").glob("Blender*"), reverse=True)]
        binaries = list(dict.fromkeys(binary_in(Path(app)) for app in candidates if binary_in(Path(app)).is_file()))
        if len(binaries) < 2:
            return next(iter(binaries), None)
        versions = [(blender_version(runner, binary, probes) or (0, 0, 0), binary) for binary in binaries]
        return max(versions, key=lambda item: item[0])[1]
    for app in candidates if candidates is not None else APP_CANDIDATES:
        if binary_in(Path(app)).is_file():
            return binary_in(Path(app))
    argv = ["mdfind", SPOTLIGHT_QUERY]
    found = probes.run(runner, argv, timeout=5, ttl=15, raise_on_timeout=True) if probes else runner.run(argv, timeout=5)
    for line in found.stdout.splitlines():
        line = line.strip()
        if line and binary_in(Path(line)).is_file():
            return binary_in(Path(line))
    return None


def parse_blender_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(text)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def blender_version(
    runner: CommandRunner, binary: Path, probes: ProbeCache | None = None
) -> tuple[int, int, int] | None:
    """`Blender --version` takes a second or two, so results are cached per binary (and mtime,
    so a reinstall is noticed). Checks pass `probes` so units asking at once share one run."""
    try:
        key = (str(binary), binary.stat().st_mtime)
    except OSError:
        return None
    with _version_lock:
        if key in _version_cache:
            return _version_cache[key]
    argv = [str(binary), "--version"]
    result = (
        probes.run(runner, argv, timeout=60, ttl=600, raise_on_timeout=True)
        if probes
        else runner.run(argv, timeout=60)
    )
    version = parse_blender_version(result.stdout)
    if version is not None:
        with _version_lock:
            _version_cache[key] = version
    return version


def format_version(version: Sequence[int]) -> str:
    return ".".join(str(n) for n in version)


def extensions_dir(version: Sequence[int], home: Path | None = None) -> Path:
    if is_windows():
        base = (home / "AppData" / "Roaming") if home is not None else app_data(roaming=True)
        return base / "Blender Foundation" / "Blender" / f"{version[0]}.{version[1]}" / "extensions" / EXTENSION_REPO
    home = home if home is not None else Path.home()
    return (
        home / "Library" / "Application Support" / "Blender"
        / f"{version[0]}.{version[1]}" / "extensions" / EXTENSION_REPO
    )


def install_extension(runner: CommandRunner, binary: Path, zip_path: Path, job: Job) -> None:
    """Install and enable an extension .zip into Blender's local repository."""
    argv = [str(binary), "--command", "extension", "install-file", "-r", EXTENSION_REPO, "-e", str(zip_path)]
    job.log(f"Installing {zip_path.name} into Blender…")
    code = runner.stream(argv, job.log, timeout=300)
    if code != 0:
        raise JobError(f"Blender couldn't install {zip_path.name} (exit {code}). See the log above.")


def is_running(runner: CommandRunner, probes: ProbeCache | None = None) -> bool:
    return process_running(runner, "Blender", probes, windows=is_windows())


class Blender(Component):
    id = "blender"
    name = "Blender"
    role = "3D app"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.candidates: Sequence[Path] | None = None  # tests point this at a temp folder

    def locate(self) -> tuple[Path | None, tuple[int, int, int] | None]:
        binary = find_blender(self.runner, self.candidates, self.ctx.probes)
        if binary is None:
            return None, None
        return binary, blender_version(self.runner, binary, self.ctx.probes)

    def check(self) -> ComponentState:
        binary, version = self.locate()
        if binary is None:
            if is_windows():
                actions = [ActionSpec("install", "Install Blender", confirm="Download and install Blender with WinGet?")] if self.runner.which("winget") else []
                return self.state(Health.MISSING,
                                  "Blender not found. Install with WinGet, or install from blender.org and set BLENDER_PATH for a portable installation.",
                                  actions=actions)
            if not self.runner.which("brew"):
                return self.state(
                    Health.MISSING,
                    "Not installed. Install Homebrew (brew.sh) or download Blender 5.2 from blender.org.",
                )
            return self.state(
                Health.MISSING,
                "Not installed yet. Install adds the latest Blender to Applications using Homebrew.",
                actions=[
                    ActionSpec(
                        "install",
                        "Install Blender",
                        confirm="Download and install Blender with Homebrew? It's about 400 MB.",
                    )
                ],
            )
        details = [("Location", tilde(app_path(binary)))]
        if version is None:
            return self.state(Health.ERROR, "Found Blender, but couldn't read its version.", details)
        details.insert(0, ("Version", format_version(version)))
        if version[:2] < MIN_BLENDER:
            return self.state(
                Health.ERROR,
                f"Blender {format_version(version)} is too old: the MCP add-on needs "
                f"{format_version(MIN_BLENDER)} or newer. Update Blender, then refresh.",
                details,
            )
        running = is_running(self.runner, self.ctx.probes)
        details.append(("Running", "yes" if running else "no"))
        actions = [] if running else [ActionSpec("launch", "Launch")]
        return self.state(
            Health.READY,
            f"Blender {format_version(version)} · {'running' if running else 'not running'}",
            details,
            actions,
        )

    def action_install(self, job: Job) -> dict:
        if is_windows():
            if not self.runner.which("winget"):
                raise JobError("WinGet is missing. Install App Installer from Microsoft Store, or download Blender from blender.org.")
            code = self.runner.stream(install_command("blender"), job.log, timeout=1800)
            if code != 0:
                raise JobError(f"WinGet couldn't install Blender (exit {code}). See the log above.")
            return {"installed": True}
        if not self.runner.which("brew"):
            raise JobError("Homebrew isn't installed. Get it from brew.sh, or install Blender from blender.org.")
        job.log("Installing Blender with Homebrew (brew install --cask blender)…")
        code = self.runner.stream(["brew", "install", "--cask", "blender"], job.log, timeout=1800)
        if code != 0:
            raise JobError(f"Homebrew couldn't install Blender (exit {code}). See the log above.")
        return {"installed": True}

    def action_launch(self, job: Job) -> dict:
        binary = find_blender(self.runner, self.candidates)
        if binary is None:
            raise JobError("Blender isn't installed.")
        if is_windows():
            try:
                launch_app(self.runner, binary, ["--online-mode"])
            except OSError as exc:
                raise JobError(f"Couldn't launch Blender: {exc}") from exc
            return {"launched": str(binary)}
        app = str(app_path(binary))
        job.log("Launching Blender with online access on, so the MCP add-on can start…")
        result = self.runner.run(["open", "-a", app, "--args", "--online-mode"], timeout=30)
        if not result.ok:
            raise JobError(f"Couldn't launch Blender: {result.stderr.strip() or result.returncode}")
        return {"launched": app}
