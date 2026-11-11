"""Training for Unity ML-Agents: runs `mlagents-learn` in the background and follows what it prints.

One run at a time. The process gets its own session, so Stop can signal the whole group: SIGINT makes it save a model
and exit; SIGINT to the main process alone leaves a helper hanging, and SIGTERM to it alone orphans a helper that
keeps holding port 5004. Whatever the helpers do, a run that has ended leaves nothing behind.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

RESULTS_DIR = Path("MLAgents") / "results"
CONFIGS_DIR = Path("MLAgents") / "configs"
MODELS_DIR = Path("Assets") / "GazeVoice" / "MLAgents"
TIMEOUT_WAIT = 120  # seconds mlagents-learn waits for Play before it gives up
# Unity's Academy asks for a trainer once, when Play starts, and never again. So start() waits for the Listening line
# before it returns, and Play is pressed after that. mlagents-learn prints it after about 3 s, or about 25-30 s the first
# time after an install, while PyTorch loads.
LISTEN_SECONDS = 45
STOP_SECONDS = 60.0  # how long Stop waits for the model to be saved before it ends the run by force
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

CONFIG_MAX_BYTES = 64 * 1024
LOG_TAIL_LINES = 15
RUNS_LISTED = 10  # how many earlier runs recent_runs() lists
_ACTIVE = ("waiting", "training", "stopping")
_DRAIN_SECONDS = 2.0  # after the process exits: how long to let the last lines arrive before ending its helpers
# A program started in the background by a shell has SIGINT ignored, and so would everything it starts: Stop would then
# do nothing. This puts SIGINT back to its default for the one command that is about to replace this small program.
# It runs with `-I` so a signal.py (or os.py) in the Unity project can't shadow what it imports. A Stop within about
# 30 ms of the start can still be lost while the wrapper starts; stop() then falls back to SIGKILL after its wait.
_RESTORE_SIGINT = "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])"


class TrainingError(Exception):
    """Something the user can fix; the message is a plain sentence."""


LOST_CONNECTION = ("Unity lost its connection to training. Leave Play mode in Unity, then press Play again to carry on, "
                   "or stop training.")


def _previous_run_sentence(run_id: str) -> str:
    return f"A run called {run_id} already exists. Resume it, overwrite it, or use a new name."


# --- reading what mlagents-learn prints -------------------------------------------------------------------------

_LISTENING = re.compile(r"^\[INFO\] Listening on port \d+\.")
# Printed about 120 s after Unity stops answering mid-run; about 2 s later mlagents-learn listens again for another 120 s.
_RESTARTING = "[WARNING] Restarting worker"
# The behavior name can hold spaces and dots, so it is whatever comes before ". Step: <n>. Time Elapsed: <s> s.".
_PROGRESS = re.compile(
    r"^\[INFO\] (?P<behavior>.+?)\. Step: (?P<step>\d+)\. Time Elapsed: (?P<seconds>\d+(?:\.\d+)?) s\.(?P<rest>.*)$")
_MEAN_REWARD = re.compile(r"\bMean Reward: (-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")  # not "Mean Group Reward"
_EXPORTED = re.compile(r"^\[INFO\] Exported (?P<path>.+\.onnx)\.?\s*$")
_ERRORS = (  # (kind, text that only appears when it happened)
    ("timeout", "UnityTimeOutException"),
    ("previous_run", "Previous data from this run ID was found"),
    ("port_busy", "UnityWorkerInUseException"),
    ("port_busy", "Address already in use"),
)


def parse_line(line: str) -> dict | None:
    """Turn one line of mlagents-learn output into an event, or None for everything we don't need."""
    line = line.rstrip("\r\n")
    match = _PROGRESS.match(line)
    if match:
        rest = match["rest"]
        reward = _MEAN_REWARD.search(rest)
        return {
            "event": "progress",
            "behavior": match["behavior"],
            "step": int(match["step"]),
            "seconds": float(match["seconds"]),
            "mean_reward": float(reward[1]) if reward else None,
            "training": "Not Training" not in rest,
        }
    if _LISTENING.match(line):
        return {"event": "waiting"}
    if line.startswith(_RESTARTING):
        return {"event": "restarting"}
    if "Connected to Unity environment" in line or "Connected new brain" in line:
        return {"event": "connected"}
    exported = _EXPORTED.match(line)
    if exported:
        return {"event": "exported", "path": exported["path"]}
    if "Learning was interrupted" in line:
        return {"event": "interrupted"}
    for kind, text in _ERRORS:
        if text in line:
            return {"event": "error", "kind": kind}
    return None


# --- the service ------------------------------------------------------------------------------------------------

def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _check_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise TrainingError(
            "A run name can use letters, numbers, dots, dashes and underscores (up to 64 characters), "
            "and must start with a letter or a number.")


def _check_project(project: Path) -> Path:
    project = Path(os.path.abspath(project))
    if not (project / "Assets").is_dir():
        raise TrainingError(f"{project} isn't a Unity project: it has no Assets folder.")
    return project


def _exists(path: Path) -> bool:
    """Whether the run's folder is there. Path.exists() hides a folder it isn't allowed to look at, so this asks the OS."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise TrainingError("Couldn't read the training results folder in your Unity project.") from exc
    return True


def _find_models(project: Path, run_id: str, since: float | None = None) -> list[str]:
    """Behaviors with a saved `<results>/<run_id>/<name>.onnx`; with `since`, only those written after that time."""
    folder = project / RESULTS_DIR / run_id
    names = []
    try:
        for path in folder.glob("*.onnx"):
            if path.is_file() and (since is None or path.stat().st_mtime >= since):
                names.append(path.stem)
    except OSError:
        return []
    return sorted(names)


def recent_runs(project: Path | str | None, limit: int = RUNS_LISTED) -> list[dict]:
    """The earlier runs in a project that saved a model, newest first: [{"run_id", "behaviors", "modified"}].

    Meant for after the control center restarts, when there is no current run to take a run id from. A run is as new as
    its newest model. Nothing is created or changed; a folder that can't be read counts as having no runs.
    """
    if not project:
        return []
    results = Path(os.path.abspath(project)) / RESULTS_DIR
    runs = []
    try:
        with os.scandir(results) as entries:
            folders = [entry.name for entry in entries if entry.is_dir() and RUN_ID.fullmatch(entry.name)]
    except OSError:
        return []
    for run_id in folders:
        newest, names = None, []
        try:
            for path in (results / run_id).glob("*.onnx"):
                if path.is_file():
                    names.append(path.stem)
                    modified = path.stat().st_mtime
                    newest = modified if newest is None else max(newest, modified)
        except OSError:
            continue
        if names:
            runs.append((newest, run_id, sorted(names)))
    runs.sort(key=lambda run: (run[0], run[1]), reverse=True)
    return [{"run_id": run_id, "behaviors": names,
             "modified": datetime.fromtimestamp(newest).astimezone().isoformat(timespec="seconds")}
            for newest, run_id, names in runs[:limit]]


class _Run:
    """Everything about one run. A new run gets a new one, so a late line from an old run can't touch it."""

    def __init__(self, run_id: str, project: Path, log_path: Path, proc: subprocess.Popen):
        self.run_id = run_id
        self.project = project
        self.log_path = log_path
        self.proc = proc
        self.started = time.time()
        self.state = "waiting"
        self.behaviors: dict[str, dict] = {}
        self.last_behavior: str | None = None
        self.tail: collections.deque[str] = collections.deque(maxlen=LOG_TAIL_LINES)
        self.error_kind: str | None = None
        self.reached_training = False  # Unity connected, so a later time-out is not about the Play button
        self.listening = False  # it has printed that it is listening for Unity, so Play can be pressed
        self.lost_unity = False  # Unity stopped answering mid-run and training waits for a new Play
        self.ready = threading.Event()  # it is listening, or it has ended: start() can stop waiting
        self.stop_asked = False
        self.sigint_sent = False
        self.exit_code: int | None = None
        self.exited = threading.Event()  # the process has ended
        self.done = threading.Event()  # and the state is final


class Training:
    def __init__(self, logs: Path):
        self._logs = Path(logs)
        self._lock = threading.Lock()  # guards _run, its fields and _subscribers; callbacks run outside it
        self._run: _Run | None = None
        self._subscribers: list[Callable[[], None]] = []
        self._closed = False  # set by shutdown(): nothing starts after it

    def subscribe(self, callback: Callable[[], None]) -> None:
        """`callback()` runs after every state change, from whichever thread caused it. It may call status()."""
        with self._lock:
            self._subscribers.append(callback)

    def _notify(self) -> None:
        with self._lock:
            callbacks = list(self._subscribers)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 - one bad listener must not stop the run or the other listeners
                traceback.print_exc(file=sys.stderr)

    # -- start ----

    def start(
        self, mlagents_learn: Path, project: Path, run_id: str, config_yaml: str | None = None,
        resume: bool = False, overwrite: bool = False, timeout_wait: int = TIMEOUT_WAIT,
        listen_seconds: float = LISTEN_SECONDS,
    ) -> dict:
        """Start a run and wait, up to `listen_seconds`, until it is listening for Unity.

        A run that ends first raises TrainingError with the sentence for why (its state stays "failed"). One that is
        still starting up when the time is over is not a failure: the status says `listening: False`, and it is
        asked again with status().
        """
        with self._lock:
            if self._closed:
                raise TrainingError("GazeVoice is shutting down, so training can't start.")
            if self._run is not None and self._run.state in _ACTIVE:
                raise TrainingError("A training run is already going. Stop it first.")
            _check_run_id(run_id)
            project = _check_project(project)
            if resume and overwrite:
                raise TrainingError("Choose either resume or overwrite, not both.")
            if config_yaml and len(config_yaml.encode("utf-8")) > CONFIG_MAX_BYTES:
                raise TrainingError("The training settings are too big: the limit is 64 KB.")
            exe = os.path.abspath(mlagents_learn)
            if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
                raise TrainingError("ML-Agents isn't installed. Press Install on the ML-Agents unit.")
            if not (resume or overwrite) and _exists(project / RESULTS_DIR / run_id):
                raise TrainingError(_previous_run_sentence(run_id))  # before the config or the log is touched

            argv = [exe]
            if config_yaml:
                config = project / CONFIGS_DIR / f"{run_id}.yaml"
                try:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    config.write_text(config_yaml, encoding="utf-8")
                except OSError as exc:
                    raise TrainingError(f"Couldn't save the training settings to {config}: {exc.strerror or exc}.") from exc
                argv.append(str(config))
            argv += ["--run-id", run_id, "--results-dir", str(project / RESULTS_DIR),
                     "--timeout-wait", str(int(timeout_wait))]
            if resume:
                argv.append("--resume")
            elif overwrite:
                argv.append("--force")

            if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
                argv = [sys.executable, "-I", "-c", _RESTORE_SIGINT, *argv]

            log_path = self._logs / f"mlagents-{run_id}.log"
            try:
                self._logs.mkdir(parents=True, exist_ok=True)
                log = open(log_path, "w", encoding="utf-8", errors="replace")
            except OSError as exc:
                raise TrainingError(f"Couldn't write the training log at {log_path}: {exc.strerror or exc}.") from exc
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")  # lines arrive as they happen
            try:
                proc = subprocess.Popen(
                    argv, cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                log.close()
                log_path.unlink(missing_ok=True)
                raise TrainingError(f"Couldn't start ML-Agents: {exc.strerror or exc}.") from exc

            run = self._run = _Run(run_id, project, log_path, proc)
            pump = threading.Thread(target=self._pump, args=(run, log), daemon=True, name=f"mlagents-out-{run_id}")
            watcher = threading.Thread(target=self._watch, args=(run, pump), daemon=True, name=f"mlagents-{run_id}")
            pump.start()
            watcher.start()
        self._notify()
        return self._await_listening(run, listen_seconds)

    def _await_listening(self, run: _Run, seconds: float) -> dict:
        """Called without the lock, so the output keeps being read while this waits."""
        run.ready.wait(seconds)
        if run.exited.is_set():
            run.done.wait(_DRAIN_SECONDS * 2 + 5.0)  # its last lines have been read, so what it ended as is final
            with self._lock:
                snapshot = self._snapshot(run)
            if snapshot["state"] == "failed" and not snapshot["listening"]:
                raise TrainingError(self._message(snapshot))
        return self.status()

    def _pump(self, run: _Run, log) -> None:
        """Copy every line to the log, keep the tail, and apply what the line means."""
        try:
            for raw in run.proc.stdout:
                line = raw.rstrip("\r\n")
                if log is not None:
                    try:
                        log.write(line + "\n")
                        log.flush()
                    except (OSError, ValueError):
                        try:
                            log.close()
                        except OSError:
                            pass
                        log = None  # a full disk mustn't block the pipe, or the run would freeze
                event = parse_line(line)
                with self._lock:
                    if line.strip():
                        run.tail.append(line)
                    if event is not None:
                        self._apply(run, event)
                if event is not None:
                    self._notify()
        except (OSError, ValueError):
            pass  # the pipe was closed under us
        finally:
            for stream in (log, run.proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    @staticmethod
    def _apply(run: _Run, event: dict) -> None:
        """Called with the lock held."""
        kind = event["event"]
        if kind == "waiting":
            run.listening = True
            run.ready.set()
        elif kind == "restarting":
            if run.state == "training":
                run.state = "waiting"
                run.listening = False  # until it says so again
                run.lost_unity = True
        elif kind in ("connected", "progress"):
            run.reached_training = True
            run.listening = True  # it can't have connected without listening
            run.lost_unity = False
            run.ready.set()
            if run.state == "waiting":
                run.state = "training"
        if kind == "progress":
            run.behaviors[event["behavior"]] = {
                "step": event["step"], "mean_reward": event["mean_reward"], "seconds": event["seconds"]}
            run.last_behavior = event["behavior"]
        elif kind == "error" and run.error_kind is None:
            run.error_kind = event["kind"]

    def _watch(self, run: _Run, pump: threading.Thread) -> None:
        """Wait for the process, let its last lines arrive, make sure nothing it started is left, then settle the state."""
        code = run.proc.wait()
        run.exited.set()
        run.ready.set()
        try:
            pump.join(_DRAIN_SECONDS)
            # Nothing it started may outlive it: not a helper that still holds the pipe, nor one that let go of it,
            # and with it port 5004.
            _signal_group(run.proc, signal.SIGKILL)
            if pump.is_alive():
                pump.join(_DRAIN_SECONDS)
        finally:
            with self._lock:
                run.exit_code = code
                run.state = "stopped" if run.stop_asked else "finished" if code == 0 else "failed"
            run.done.set()
            self._notify()

    # -- stop ----

    def stop(self, timeout: float = STOP_SECONDS) -> dict:
        """SIGINT to the run's whole group so it saves a model; after `timeout` seconds, SIGKILL to the group."""
        asked = False
        with self._lock:
            run = self._run
            if run is None or run.state not in _ACTIVE:
                run = None
            elif not run.exited.is_set():  # if it has already ended, what it ended as stays as it is
                run.stop_asked = True
                run.state = "stopping"
                asked = True
                if not run.sigint_sent:
                    run.sigint_sent = True
                    _signal_group(run.proc, signal.SIGINT)  # sent before any listener runs, so none can prevent it
        if run is None:
            return self.status()
        if asked:
            self._notify()
            if not run.exited.wait(timeout):
                _signal_group(run.proc, signal.SIGKILL)
                run.exited.wait(5.0)
        run.done.wait(_DRAIN_SECONDS * 2 + 5.0)
        return self.status()

    def shutdown(self) -> None:
        """The control center is closing: end any run, saving its model first, and start no more."""
        with self._lock:
            self._closed = True
        self.stop()

    # -- reading it back ----

    @staticmethod
    def _snapshot(run: _Run) -> dict:
        """Called with the lock held."""
        return {
            "state": run.state, "run_id": run.run_id, "project": run.project, "log": run.log_path,
            "behaviors": {name: dict(values) for name, values in run.behaviors.items()},
            "last_behavior": run.last_behavior, "tail": list(run.tail), "error_kind": run.error_kind,
            "reached_training": run.reached_training, "exit_code": run.exit_code, "started": run.started,
            "listening": run.listening, "lost_unity": run.lost_unity,
        }

    def status(self) -> dict:
        with self._lock:
            run = self._run
            snapshot = None if run is None else self._snapshot(run)
        if snapshot is None:
            return {
                "state": "idle", "run_id": None, "project": None, "behaviors": {},
                "message": "No training run has started yet.", "log": None, "log_tail": [], "models": [],
                "exit_code": None, "listening": False,
            }
        models = _find_models(snapshot["project"], snapshot["run_id"])
        return {
            "state": snapshot["state"], "run_id": snapshot["run_id"], "project": str(snapshot["project"]),
            "behaviors": snapshot["behaviors"], "message": self._message(snapshot),
            "log": str(snapshot["log"]), "log_tail": snapshot["tail"], "models": models,
            "exit_code": snapshot["exit_code"], "listening": snapshot["listening"],
        }

    @staticmethod
    def _message(snap: dict) -> str:
        run_id, state = snap["run_id"], snap["state"]
        if state == "waiting":
            if snap["lost_unity"]:
                return LOST_CONNECTION
            if not snap["listening"]:
                return "ML-Agents is starting up. Press Play once it says it is waiting for Unity."
            return "Waiting for Unity: press Play in the Editor to start training."
        if state == "training":
            latest = snap["behaviors"].get(snap["last_behavior"])
            if latest is None:
                return f"Training {run_id}: Unity is connected, and no steps are reported yet."
            reward = "no finished episode yet" if latest["mean_reward"] is None else (
                f"mean reward {latest['mean_reward']:.3f}")
            return f"Training {run_id}: step {latest['step']}, {reward}."
        if state == "stopping":
            return f"Stopping training {run_id}. Waiting for it to save the model."
        if state in ("finished", "stopped"):
            saved = bool(_find_models(snap["project"], run_id, since=snap["started"] - 2.0))
            if state == "finished":
                return (f"Training {run_id} finished. Its model is ready to use." if saved
                        else f"Training {run_id} finished, but it saved no model.")
            return (f"Training {run_id} stopped. Its model was saved and is ready to use." if saved
                    else f"Training {run_id} stopped before a model was saved.")
        kind = snap["error_kind"]
        if kind == "timeout":
            if snap["reached_training"]:
                return ("Unity stopped answering for 2 minutes during training, so training ended. "
                        f"The log is at {snap['log']}.")
            return ("Unity didn't connect within 2 minutes, so training gave up. If Unity was already in Play mode, "
                    "stop Play first. Then start a new run and press Play.")
        if kind == "previous_run":
            return _previous_run_sentence(run_id)
        if kind == "port_busy":
            return "Another training run is already using ML-Agents' connection to Unity. Stop it first."
        return f"Training stopped with an error. The log is at {snap['log']}."

    # -- models ----

    def models(self, project: Path, run_id: str) -> list[str]:
        """The behaviors that have a saved model in this run."""
        _check_run_id(run_id)
        return _find_models(Path(os.path.abspath(project)), run_id)

    def import_model(self, project: Path, run_id: str, behavior: str | None = None) -> dict:
        """Copy a trained model into the Unity project's Assets, over any older copy."""
        _check_run_id(run_id)
        project = _check_project(project)
        names = _find_models(project, run_id)
        if not names:
            raise TrainingError(f"Run {run_id} hasn't saved a model yet.")
        if behavior is None:
            if len(names) > 1:
                raise TrainingError(f"Run {run_id} saved several models ({', '.join(names)}). Say which one to use.")
            behavior = names[0]
        elif behavior not in names:
            raise TrainingError(f"Run {run_id} has no model for {behavior}. It has: {', '.join(names)}.")
        target = project / MODELS_DIR / run_id / f"{behavior}.onnx"
        temporary = target.with_name(f".{behavior}.onnx.tmp")  # Unity ignores dot files, so it never sees a half copy
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(project / RESULTS_DIR / run_id / f"{behavior}.onnx", temporary)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise TrainingError(f"Couldn't copy the model into the project: {exc.strerror or exc}.") from exc
        return {"asset_path": target.relative_to(project).as_posix(), "behavior": behavior}
