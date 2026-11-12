"""Unity ML-Agents' Python side on this Mac: the program that trains agents, in its own Python environment.

Only the Install button sets it up. The training itself (starting, following and stopping a run) is gazevoice/mlagents.py.
"""

from __future__ import annotations

from pathlib import Path

from ..jobs import Job, JobError
from ..mlagents import RESULTS_DIR
from ..paths import tilde
from .base import ActionSpec, Component, ComponentState, Health

VENV_PYTHON = "3.10.12"  # the newest Python ML-Agents accepts (>=3.10.1,<=3.10.12)
ML_AGENTS_VERSION = "4.2.0"
# PyPI's ML-Agents release doesn't work on Apple silicon, so this installs the release/4.2.0 source from GitHub.
ML_AGENTS_COMMIT = "82ce4998f18bbe05eb3de834cace09fc6564ba8e"
_TARBALL = f"https://github.com/Unity-Technologies/ml-agents/archive/{ML_AGENTS_COMMIT}.tar.gz"
PACKAGES = (
    "torch>=2.1.1,<=2.8.0",
    f"mlagents-envs @ {_TARBALL}#subdirectory=ml-agents-envs",
    f"mlagents @ {_TARBALL}#subdirectory=ml-agents",
)
IMPORT_CHECK = "import mlagents.trainers"
LIVE_STATES = ("waiting", "training", "stopping")  # a training run is going
INSTALL_SECONDS = 3600
CHECK_SECONDS = 60  # the first import of PyTorch can take a while


class MLAgents(Component):
    id = "mlagents"
    name = "ML-Agents"
    role = "Trains Unity agents on this Mac"

    @property
    def venv(self) -> Path:
        return self.paths.venvs / "mlagents"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def mlagents_learn(self) -> Path:
        return self.venv / "bin" / "mlagents-learn"

    def _imports(self) -> bool:
        """Whether ML-Agents imports. Shared and cached briefly; raises ProbeTimedOut if the Mac is too busy to say."""
        argv = [str(self.python), "-c", IMPORT_CHECK]
        return self.probe(argv, timeout=CHECK_SECONDS, ttl=60).ok

    def _run_is_live(self) -> bool:
        """A run that is waiting, training or stopping proves the install, so nothing needs probing."""
        return self.ctx.training.status()["state"] in LIVE_STATES

    def installed(self) -> bool:
        """True when the training program is there and ML-Agents imports. Raises ProbeTimedOut when it can't tell."""
        if self._run_is_live():
            return True
        return self.python.exists() and self.mlagents_learn.is_file() and self._imports()

    def _install_action(self) -> ActionSpec:
        return ActionSpec(
            "install",
            "Install",
            confirm="Install ML-Agents' Python side? This sets up its own Python 3.10 environment and downloads "
                    "ML-Agents and PyTorch (about 1.2 GB, about 5 minutes).",
        )

    def check(self) -> ComponentState:
        run = self.ctx.training.status()
        if run["state"] in LIVE_STATES:
            # The run is the proof. Asking Python to import PyTorch now could time out under training's load, and
            # "Still checking…" would take Stop away in the middle of a run.
            return self._ready(run)
        if not self.runner.which("uv"):
            return self.state(Health.ERROR, "uv is needed to set up ML-Agents. Install uv (astral.sh/uv), then refresh.")
        if not self.python.exists() and self.venv.exists():
            return self.state(
                Health.MISSING,
                "ML-Agents' Python environment doesn't work any more, usually because the Python it was made from was "
                "removed. Press Install to set it up again.",
                actions=[self._install_action()],
            )
        if not self.python.exists():
            return self.state(
                Health.MISSING,
                "Not installed. ML-Agents trains your Unity agents on this Mac.",
                actions=[self._install_action()],
            )
        if not self.mlagents_learn.is_file() or not self._imports():
            return self.state(Health.MISSING, "ML-Agents isn't installed yet.", actions=[self._install_action()])
        return self._ready(self.ctx.training.status())  # the probe may have taken a minute; a run may have started

    def _ready(self, run: dict) -> ComponentState:
        details = [("Python side", f"ML-Agents {ML_AGENTS_VERSION} · Python {VENV_PYTHON}")]
        if self.settings.unity_project:
            details.append(("Runs folder", tilde(Path(self.settings.unity_project) / RESULTS_DIR)))
        if run["state"] != "idle":
            details.append(("Last run", f"{run['run_id']} · {run['state']}"))
        actions = [ActionSpec("stop", "Stop training")] if run["state"] in ("waiting", "training") else []
        summary = "Ready to train" if run["state"] == "idle" else run["message"]
        return self.state(Health.READY, summary, details, actions)

    def action_install(self, job: Job) -> dict:
        if not self.runner.which("uv"):
            raise JobError("uv isn't installed. Install it from astral.sh/uv, then try again.")
        steps = []
        if not self.python.exists():
            if self.venv.exists():  # a folder whose Python is gone (an uninstalled Anaconda, say); it holds only packages
                # uv's --clear deletes whatever folder it is given, so only an environment or an empty folder is cleared.
                if not ((self.venv / "pyvenv.cfg").is_file() or (self.venv.is_dir() and not any(self.venv.iterdir()))):
                    raise JobError(f"{self.venv} isn't a Python environment, so it's left alone. "
                                   "Move or delete that folder, then press Install again.")
                job.log("The Python environment from before has no working Python, so it's being made again.")
            steps.append(("Creating a Python 3.10 environment…",
                          ["uv", "venv", "--clear", "--python", VENV_PYTHON, str(self.venv)]))
        steps.append(("Installing ML-Agents and PyTorch…",
                      ["uv", "pip", "install", "--python", str(self.python), *PACKAGES]))
        for message, argv in steps:
            job.log(message)
            code = self.runner.stream(argv, job.log, timeout=INSTALL_SECONDS)
            if code != 0:
                raise JobError(f"{message.rstrip('…')} failed (exit {code}). See the log above.")
        return {"installed": True}

    def action_stop(self, job: Job) -> dict:
        job.log("Stopping training. The model is saved first, which can take a moment…")
        return self.ctx.training.stop()
