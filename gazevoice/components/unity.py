"""Unity itself: find it, set it up with one key, make or pick a project."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from xml.parsers.expat import ExpatError

from ..jobs import Job, JobError
from ..paths import tilde
from ..probes import ProbeCache
from ..runner import CommandRunner
from ..platforms import app_data, install_command, is_windows, launch_app, process_running
from ..settings import project_key, save_settings, unity_project_of, update_settings
from ..unity_install import add_bridge, bridge_current
from .base import ActionSpec, Component, Health

HUB_APP = Path("/Applications/Unity Hub.app")
EDITOR_ROOTS = (Path("/Applications/Unity/Hub/Editor"),)
HUB_CONFIG = Path.home() / "Library" / "Application Support" / "UnityHub"
PROJECTS_DIR = Path.home() / "Unity"
PROJECT_NAME = "GazeVoice"
VR_PROJECT_NAME = "GazeVoice VR"  # Unity VR's project: its own, so the Unity one is never touched
VR_TEMPLATE = "com.unity.template.vr"  # Unity's VR template: the XR Origin rig with controllers, moving and teleporting
VR_SETUP = "GazeVoiceVRSetup.Batch"  # unity_package/Editor/VR/GazeVoiceVRSetup.cs
NO_VR_TEMPLATE = ("Unity's VR template isn't downloaded yet. In Unity Hub, press New project, pick VR and press "
                  "Download template, then try again.")
CLI_FALLBACK = Path("/opt/homebrew/bin/unity")  # a GUI-started app may not have Homebrew on its PATH
HUB_CASK, CLI_CASK = "unity-hub", "unity-cli"
MIN_UNITY = (2022, 3)  # matches "unity" in unity_package/package.json
SPOTLIGHT_QUERY = "kMDItemFSName == 'Unity.app'"
BREW_SECONDS = 1800
INSTALL_SECONDS = 7200  # the editor is several GB
CREATE_SECONDS = 900
AUTH_EXIT = 3  # the Unity CLI's exit code for "sign in first"
NO_HOMEBREW = "Install Homebrew (brew.sh) so GazeVoice can set Unity up, or get Unity Hub from unity.com/download."
SIGN_IN = ("Unity needs a license before it can make a project. Sign in to Unity Hub and add the free Personal "
           "license, then press Set up Unity again.")
PICK_FOLDER = 'POSIX path of (choose folder with prompt "Pick your Unity project")'
if is_windows():
    HUB_APP = Path(os.environ.get("UNITY_HUB_PATH", str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Unity Hub" / "Unity Hub.exe")))
    EDITOR_ROOTS = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Unity" / "Hub" / "Editor",)
    HUB_CONFIG = app_data("UnityHub", roaming=True)
    CLI_FALLBACK = Path("__no_unity_cli__")  # Windows uses Unity Hub's headless commands.
    NO_HOMEBREW = "Install App Installer (WinGet), or install Unity Hub from unity.com/download."
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)([abfp]\d+)")
_LICENSE_PROBLEM = re.compile(
    r"no valid (?:unity editor )?licen[sc]e|failed to activate|unable to activate"
    r"|access token is unavailable|not signed in", re.IGNORECASE)


@dataclass(frozen=True)
class UnityEditor:
    version: str
    app: Path

    @property
    def binary(self) -> Path:
        if self.app.suffix.lower() == ".exe":
            return self.app
        if self.app.name != "Unity.app":
            return self.app / "Editor" / "Unity.exe"
        return self.app / "Contents" / "MacOS" / "Unity"


def parse_unity_version(text: str) -> tuple[int, int, int, str] | None:
    match = _VERSION.match(text.strip())
    if not match:
        return None
    major, minor, patch, build = match.groups()
    return int(major), int(minor), int(patch), build


def _sort_key(version: str) -> tuple[int, int, int, int]:
    major, minor, patch, build = parse_unity_version(version) or (0, 0, 0, "f0")
    return major, minor, patch, int(build[1:])


def _editor_version(app: Path) -> str | None:
    """The version in the app's Info.plist, or else its folder's name, which the Hub sets to the version."""
    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleVersion")
    except (OSError, ValueError, ExpatError):
        value = None
    for text in (value, app.parent.parent.name if app.suffix.lower() == ".exe" else app.name,
                 app.parent.name):
        if isinstance(text, str) and parse_unity_version(text):
            return text
    return None


def _hub_install_folder(hub_config: Path | None) -> Path | None:
    """The custom folder set in Unity Hub's preferences, if any."""
    if hub_config is None:
        return None
    try:
        value = json.loads((hub_config / "secondaryInstallPath.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Path(value) if isinstance(value, str) and value else None


def find_editors(
    roots: Iterable[Path], hub_config: Path | None = None, apps: Iterable[Path] = ()
) -> list[UnityEditor]:
    """Every Unity editor in these folders (plus any apps Spotlight found), newest first."""
    folders = [*roots, *filter(None, [_hub_install_folder(hub_config)])]
    candidates = [app for folder in folders for pattern in ("*/Unity.app", "*/Editor/Unity.exe")
                  for app in sorted(Path(folder).glob(pattern))]
    if is_windows() and hub_config is not None:
        try:
            saved = json.loads((hub_config / "editors-v2.json").read_text(encoding="utf-8"))
            items = saved.get("data", []) if isinstance(saved, dict) else saved
            if isinstance(items, list):
                for item in items:
                    locations = item.get("location", []) if isinstance(item, dict) else []
                    if isinstance(locations, str):
                        locations = [locations]
                    if isinstance(locations, list):
                        for location in locations:
                            if isinstance(location, str) and location:
                                app = Path(location)
                                candidates.append(app / "Unity.exe" if app.name.lower() == "editor" else app)
        except (OSError, ValueError):
            pass
    configured = os.environ.get("UNITY_EDITOR_PATH") if is_windows() else None
    if configured:
        candidates.append(Path(configured))
    found: dict[str, UnityEditor] = {}
    for app in (*candidates, *apps):
        version = _editor_version(app)
        if version and version not in found and UnityEditor(version, app).binary.is_file():
            found[version] = UnityEditor(version, app)
    return sorted(found.values(), key=lambda editor: _sort_key(editor.version), reverse=True)


def best_editor(editors: Sequence[UnityEditor]) -> UnityEditor | None:
    """The newest full release our package can run on."""
    usable = [e for e in editors if (parsed := parse_unity_version(e.version))
              and parsed[:2] >= MIN_UNITY and parsed[3].startswith("f")]
    return max(usable, key=lambda editor: _sort_key(editor.version), default=None)


def project_version(project: Path) -> str | None:
    try:
        text = (Path(project) / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^m_EditorVersion:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def project_problem(project: Path) -> str | None:
    """Why this folder can't be used as a Unity project, in plain words; None when it can."""
    project = Path(project)
    if not project.is_dir():
        return f"{tilde(project)} doesn't exist."
    if not ((project / "Assets").is_dir() and (project / "Packages" / "manifest.json").is_file()
            and project_version(project)):
        return f"{tilde(project)} isn't a Unity project."
    return None


def next_project_path(parent: Path, name: str = PROJECT_NAME) -> Path:
    """A folder for a new project. It never exists yet, so a failed attempt can safely be cleaned up."""
    number = 1
    while True:
        path = Path(parent) / (name if number == 1 else f"{name}{number}")
        if not path.exists():
            return path
        number += 1


def template_folders(hub_config: Path | None, editor: UnityEditor) -> list[Path]:
    """Where project templates are: the ones Unity Hub downloaded, then the ones that came with the editor."""
    built_in = (editor.app / "Contents" / "Resources" if editor.app.name == "Unity.app"
                else editor.binary.parent / "Data" / "Resources")
    return [*([hub_config / "Templates"] if hub_config else []), built_in / "PackageManager" / "ProjectTemplates"]


def find_template(name: str, folders: Iterable[Path]) -> Path | None:
    """The newest copy of the project template `name` (such as com.unity.template.vr-6.1.1.tgz) in `folders`."""
    found = []
    for folder in folders:
        for path in sorted(folder.glob(f"{name}-*.tgz")) if folder.is_dir() else ():
            version = tuple(int(part) for part in re.findall(r"\d+", path.name[len(name):]))
            found.append((version, path))
    return max(found, key=lambda item: item[0])[1] if found else None


def is_running(runner: CommandRunner, probes: ProbeCache | None = None) -> bool:
    return process_running(runner, "Unity", probes, windows=is_windows())


def project_is_open(project: Path, running: bool) -> bool:
    """Unity keeps this lockfile while it has the project open; a crash can leave one behind."""
    return running and (Path(project) / "Temp" / "UnityLockfile").exists()


def find_cli(runner: CommandRunner, fallback: Path = CLI_FALLBACK) -> str | None:
    return runner.which("unity") or (str(fallback) if fallback.exists() else None)


class _UnityUnit(Component):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.roots: Sequence[Path] = EDITOR_ROOTS  # tests point these at temp folders
        self.hub_app = HUB_APP
        self.hub_config: Path | None = HUB_CONFIG
        self.projects_dir = PROJECTS_DIR
        self.cli_fallback = CLI_FALLBACK

    @property
    def vr(self) -> bool:
        """Unity VR has a project of its own, made from Unity's VR template."""
        return self.settings.voice_host == "unity_vr"

    @property
    def project_name(self) -> str:
        return VR_PROJECT_NAME if self.vr else PROJECT_NAME

    @property
    def project(self) -> Path | None:
        project = unity_project_of(self.settings)
        return Path(project) if project else None

    def editors(self, fresh: bool = False) -> list[UnityEditor]:
        """`fresh` skips the shared cache, for actions that have just installed something."""
        if is_windows():
            return find_editors(self.roots, self.hub_config)
        argv = ["mdfind", SPOTLIGHT_QUERY]
        result = (self.runner.run(argv, timeout=5) if fresh
                  else self.ctx.probes.run(self.runner, argv, timeout=5, ttl=15, raise_on_timeout=True))
        apps = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        return find_editors(self.roots, self.hub_config, apps)

    def editor_for(self, project: Path, editors: Sequence[UnityEditor]) -> UnityEditor | None:
        version = project_version(project)
        return next((editor for editor in editors if editor.version == version), None)

    def remember(self, project: Path) -> None:
        self.ctx.settings = update_settings(self.ctx.settings, {project_key(self.settings.voice_host): str(project)})
        save_settings(self.paths.settings_file, self.ctx.settings)

    def open_hub(self) -> None:
        if is_windows():
            try:
                launch_app(self.runner, self.hub_app)
            except OSError as exc:
                raise JobError(f"Couldn't open Unity Hub: {exc}") from exc
            return
        result = self.runner.run(["open", "-a", self.hub_app], timeout=30)
        if not result.ok:
            raise JobError(f"Couldn't open Unity Hub: {result.stderr.strip() or result.returncode}")

    def make_project(self, job: Job, editor: UnityEditor) -> Path:
        project = next_project_path(self.projects_dir, self.project_name)
        argv = [editor.binary, "-batchmode", "-quit", "-createProject", project]
        if self.vr:
            template = find_template(VR_TEMPLATE, template_folders(self.hub_config, editor))
            if template is None:
                if self.hub_app.exists():
                    self.open_hub()
                raise JobError(NO_VR_TEMPLATE)
            argv += ["-cloneFromTemplate", template]
            job.log(f"Making a new VR project at {tilde(project)} from Unity's VR template ({template.name}) with "
                    f"Unity {editor.version}. This takes a few minutes…")
        else:
            job.log(f"Making a new project at {tilde(project)} with Unity {editor.version}. This takes a few minutes…")
        project.parent.mkdir(parents=True, exist_ok=True)
        log = self.paths.logs / ("unity-new-vr-project.log" if self.vr else "unity-new-project.log")
        result = self.runner.run([*argv, "-logFile", log], timeout=CREATE_SECONDS)
        if result.ok and project_problem(project) is None:
            self.remember(project)
            if self.vr:
                self._prepare_vr(job, editor, project)
            job.log(f"Made {tilde(project)}")
            return project
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = result.stderr
        shutil.rmtree(project, ignore_errors=True)  # next_project_path chose a new folder, so this is only ours
        if _LICENSE_PROBLEM.search(text):
            if self.hub_app.exists():
                self.open_hub()
            raise JobError(SIGN_IN)
        tail = " ".join(text.split("\n")[-6:]).strip()
        raise JobError(f"Unity couldn't make the project (exit {result.returncode}). {tail} Full log: {tilde(log)}")


    def _prepare_vr(self, job: Job, editor: UnityEditor, project: Path) -> None:
        """Add the GazeVoice package, then let it build the VR scene: a flat plane with the template's XR Origin rig,
        and OpenXR set up for a Meta Quest Pro with eye gaze."""
        add_bridge(project)
        log = self.paths.logs / "unity-vr-setup.log"
        job.log("Building the VR scene: a plane to work on, the template's player rig, and eye tracking…")
        result = self.runner.run([editor.binary, "-batchmode", "-quit", "-projectPath", project,
                                  "-executeMethod", VR_SETUP, "-logFile", log], timeout=CREATE_SECONDS)
        if not result.ok:
            raise JobError(f"Unity made {project.name}, but couldn't build its VR scene (exit {result.returncode}). "
                           f"Open it in Unity and press Prepare VR scene on the VR headset unit. Full log: {tilde(log)}")


class Unity(_UnityUnit):
    id = "unity"
    name = "Unity"
    role = "Game engine"

    def _can_brew(self, editor: UnityEditor | None) -> bool:
        if is_windows():
            return self.hub_app.exists() or bool(self.runner.which("winget"))
        needs_brew = not self.hub_app.exists() or (
            editor is None and find_cli(self.runner, self.cli_fallback) is None)
        return not needs_brew or bool(self.runner.which("brew"))

    def setup_steps(self, editor: UnityEditor | None) -> list[str]:
        """What Set up Unity still has to do, in order, in plain words."""
        steps = []
        if not self.hub_app.exists():
            steps.append("install Unity Hub with WinGet" if is_windows() else "install Unity Hub with Homebrew")
        if editor is None:
            if not is_windows() and find_cli(self.runner, self.cli_fallback) is None:
                steps.append("install the Unity CLI with Homebrew")
            steps.append("download the latest Unity LTS for Windows (several GB)" if is_windows() else
                         "download the latest Unity LTS for Apple silicon (several GB, so this can take a while)")
        project = self.project
        if project is None or project_problem(project):
            default = self.projects_dir / self.project_name
            if project_problem(default) is None:
                steps.append(f"use the project at {tilde(default)}")
                steps.append("add the GazeVoice package to it")
            elif self.vr:
                steps.append(f"make a new VR project from Unity's VR template at "
                             f"{tilde(next_project_path(self.projects_dir, self.project_name))}")
                steps.append("add the GazeVoice package and build its VR scene")
            else:
                steps.append(f"make a new project at {tilde(next_project_path(self.projects_dir))}")
                steps.append("add the GazeVoice package to it")
        elif not bridge_current(project):
            steps.append(f"add the GazeVoice package to {project.name}")
        return steps

    def _setup_action(self, steps: Sequence[str]) -> ActionSpec:
        confirm = f"Set up Unity now? GazeVoice will: {'; '.join(steps)}; then open the project in Unity."
        if not self.hub_app.exists():
            confirm += " You'll need to sign in to Unity Hub once, for the free Personal license."
        return ActionSpec("setup", "Set up Unity", confirm=confirm)

    def check(self):
        editors = self.editors()
        editor = best_editor(editors)
        steps = self.setup_steps(editor)
        actions = []
        if steps and self._can_brew(editor):
            actions.append(self._setup_action(steps))
        if self.hub_app.exists():
            actions.append(ActionSpec("open_hub", "Open Unity Hub"))
        details = [("Unity Hub", "installed" if self.hub_app.exists() else "not installed")]
        if editor is None:
            if not self._can_brew(editor):
                return self.state(Health.MISSING, f"Not installed. {NO_HOMEBREW}", details, actions)
            if editors:
                return self.state(Health.MISSING, f"Unity {editors[0].version} is too old: GazeVoice needs "
                                  f"{MIN_UNITY[0]}.{MIN_UNITY[1]} or newer. Set up Unity adds the latest LTS.",
                                  details, actions)
            return self.state(Health.MISSING, "Not installed. Set up Unity installs it, makes a project and "
                              "connects it. You sign in once, in Unity Hub.", details, actions)
        running = is_running(self.runner, self.ctx.probes)
        details = [("Version", editor.version), ("Location", tilde(editor.app)), *details,
                   ("Running", "yes" if running else "no")]
        project = self.project
        if not steps and not project_is_open(project, running) and self.editor_for(project, editors):
            actions.append(ActionSpec("launch", "Open project"))
        return self.state(Health.READY, f"Unity {editor.version} · {'running' if running else 'not running'}",
                          details, actions)

    # --- one key, every step. Each step is skipped once it's done, so pressing it again carries on. --------
    def _brew_install(self, job: Job, cask: str, name: str) -> None:
        if is_windows():
            if not self.runner.which("winget"):
                raise JobError(NO_HOMEBREW)
            code = self.runner.stream(install_command(cask, cask=True), job.log, timeout=BREW_SECONDS)
            if code != 0:
                raise JobError(f"WinGet couldn't install {name} (exit {code}). See the log above.")
            return
        if not self.runner.which("brew"):
            raise JobError(NO_HOMEBREW)
        job.log(f"Installing {name} with Homebrew (brew install --cask {cask})…")
        code = self.runner.stream(["brew", "install", "--cask", cask], job.log, timeout=BREW_SECONDS)
        if code != 0:
            raise JobError(f"Homebrew couldn't install {name} (exit {code}). See the log above.")

    def _install_editor(self, job: Job) -> UnityEditor:
        if is_windows():
            from urllib.request import urlopen
            url = ("https://services.api.unity.com/unity/editor/release/v1/releases?"
                   "limit=1&platform=WINDOWS&architecture=X86_64&stream=LTS&order=RELEASE_DATE_DESC")
            try:
                with urlopen(url, timeout=30) as response:
                    release = json.load(response)["results"][0]
                version, revision = release["version"], release["shortRevision"]
                if not parse_unity_version(version) or not re.fullmatch(r"[a-fA-F0-9]{12,40}", revision):
                    raise ValueError("invalid release metadata")
            except (OSError, ValueError, KeyError, IndexError) as exc:
                raise JobError("Could not read Unity's Windows LTS release. Install an LTS editor in Unity Hub and retry.") from exc
            job.log(f"Installing Unity {version} for Windows through Unity Hub. Sign in and activate a license in the Hub.")
            code = self.runner.stream([str(self.hub_app), "--", "--headless", "install", "--version", version,
                                       "--changeset", revision], job.log, timeout=INSTALL_SECONDS)
            if code != 0:
                raise JobError(f"Unity Hub install failed (exit {code}). Sign in, install the LTS editor in Hub and retry.")
            editor = best_editor(self.editors(fresh=True))
            if editor is None:
                raise JobError("Unity Hub returned, but the editor is not installed yet. Finish its installation in Hub and retry.")
            return editor
        cli = find_cli(self.runner, self.cli_fallback)
        if cli is None:
            self._brew_install(job, CLI_CASK, "the Unity CLI")
            cli = find_cli(self.runner, self.cli_fallback)
            if cli is None:
                raise JobError("The Unity CLI installed, but GazeVoice can't find its `unity` command.")
        job.log("Downloading the latest Unity LTS for Apple silicon. It's several GB, so this can take a while…")
        code = self.runner.stream([cli, "install", "lts", "-a", "arm64"], job.log, timeout=INSTALL_SECONDS)
        if code == AUTH_EXIT:
            raise JobError("Unity wants you to sign in first. Run `unity auth login` in Terminal, "
                           "then press Set up Unity again.")
        if code != 0:
            raise JobError(f"Unity couldn't be installed (exit {code}). See the log above.")
        editor = best_editor(self.editors(fresh=True))
        if editor is None:
            raise JobError("The Unity install finished, but GazeVoice can't find it. "
                           "Check that it shows up in Unity Hub, then press Set up Unity again.")
        return editor

    def _pick_project(self, job: Job, editor: UnityEditor) -> Path:
        project = self.project
        if project is not None and project_problem(project) is None:
            return project
        default = self.projects_dir / self.project_name
        if project_problem(default) is None:
            self.remember(default)
            job.log(f"Using the project at {tilde(default)}")
            return default
        return self.make_project(job, editor)

    def _open_project(self, job: Job, project: Path, editor: UnityEditor) -> None:
        if project_is_open(project, is_running(self.runner)):
            job.log(f"{project.name} is already open in Unity.")
            return
        job.log(f"Opening {project.name} in Unity {editor.version}…")
        if is_windows():
            try:
                launch_app(self.runner, editor.binary, ["-projectPath", str(project)])
            except OSError as exc:
                raise JobError(f"Couldn't open Unity: {exc}") from exc
            return
        result = self.runner.run(["open", "-n", "-a", editor.app, "--args", "-projectPath", project], timeout=30)
        if not result.ok:
            raise JobError(f"Couldn't open Unity: {result.stderr.strip() or result.returncode}")

    def action_setup(self, job: Job) -> dict:
        if not self.hub_app.exists():
            self._brew_install(job, HUB_CASK, "Unity Hub")
            self.open_hub()
            job.log("Unity Hub is open: sign in there and add the free Personal license while the rest installs.")
        editor = best_editor(self.editors(fresh=True)) or self._install_editor(job)
        project = self._pick_project(job, editor)
        editor = self.editor_for(project, self.editors(fresh=True))
        if editor is None:
            raise JobError(f"{project.name} uses Unity {project_version(project)}, which isn't installed. "
                           "Install that version in Unity Hub, then press Set up Unity again.")
        if not bridge_current(project):
            add_bridge(project)
            job.log(f"Added the GazeVoice package to {project.name}.")
        self._open_project(job, project, editor)
        job.log("Done. Unity's first import takes a few minutes; the bridge connects when it finishes.")
        return {"project": str(project), "unity": editor.version}

    def action_launch(self, job: Job) -> dict:
        project = self.project
        editor = self.editor_for(project, self.editors(fresh=True)) if project else None
        if editor is None:
            raise JobError("There's no project to open. Press Set up Unity first.")
        self._open_project(job, project, editor)
        return {"project": str(project), "unity": editor.version}

    def action_open_hub(self, job: Job) -> dict:
        self.open_hub()
        return {"opened": "Unity Hub"}


class UnityProject(_UnityUnit):
    id = "unity_project"
    name = "Unity project"
    role = "The project GazeVoice edits"

    def check(self):
        actions = [
            ActionSpec("create", "New project",
                       confirm=f"Make a new {'Unity VR' if self.vr else 'Unity'} project at "
                               f"{tilde(next_project_path(self.projects_dir, self.project_name))}?"),
            ActionSpec("choose", "Choose…"),
        ]
        project = self.project
        if project is None:
            return self.state(Health.MISSING, "No project yet. Set up Unity makes one, or make or pick one here.",
                              actions=actions)
        problem = project_problem(project)
        if problem:
            return self.state(Health.ERROR, problem, actions=actions)
        version = project_version(project)
        details = [("Path", tilde(project)), ("Unity", version)]
        if self.editor_for(project, self.editors()) is None:
            return self.state(Health.ERROR, f"This project uses Unity {version}, which isn't installed. "
                              "Install that version in Unity Hub.", details, actions)
        return self.state(Health.READY, f"{project.name} · Unity {version}", details, actions)

    def action_create(self, job: Job) -> dict:
        editor = best_editor(self.editors(fresh=True))
        if editor is None:
            raise JobError("Unity isn't installed yet. Press Set up Unity first.")
        return {"project": str(self.make_project(job, editor))}

    def action_choose(self, job: Job) -> dict:
        job.log("Pick your Unity project in the window that just opened…")
        if is_windows():
            script = ("Add-Type -AssemblyName System.Windows.Forms; "
                      "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                      "$dialog.Description = 'Pick your Unity project'; "
                      "if ($dialog.ShowDialog() -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                      "[Console]::WriteLine($dialog.SelectedPath) }; $dialog.Dispose()")
            result = self.runner.run(["powershell.exe", "-NoProfile", "-STA", "-Command", script], timeout=600)
        else:
            result = self.runner.run(["osascript", "-e", PICK_FOLDER], timeout=600)
        if not result.ok or not result.stdout.strip():
            raise JobError("No folder was picked.")
        project = Path(result.stdout.strip())
        problem = project_problem(project)
        if problem:
            raise JobError(problem)
        self.remember(project)
        job.log(f"Using {tilde(project)}")
        return {"project": str(project)}
