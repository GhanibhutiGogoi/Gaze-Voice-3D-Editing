import contextlib
import errno
import inspect
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gazevoice import mlagents
from gazevoice.mlagents import (
    CONFIGS_DIR,
    MODELS_DIR,
    RESULTS_DIR,
    RUNS_LISTED,
    Training,
    TrainingError,
    parse_line,
    recent_runs,
)

from .helpers import fake_unity_project

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mlagents_learn.py"
WRAPPER: Path  # one executable for the whole module: macOS vets every new executable on its first run, about a second
_wrapper_dir: tempfile.TemporaryDirectory


def setUpModule():
    global WRAPPER, _wrapper_dir
    _wrapper_dir = tempfile.TemporaryDirectory()
    WRAPPER = Path(_wrapper_dir.name) / "mlagents-learn"
    WRAPPER.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FIXTURE))} \"$@\"\n")
    WRAPPER.chmod(0o755)
    subprocess.run([str(WRAPPER)], env={"FAKE_ML_SCRIPT": "crash"}, capture_output=True, check=False)  # the vetting


def tearDownModule():
    _wrapper_dir.cleanup()

ALREADY_GOING = "A training run is already going. Stop it first."
TIMED_OUT = ("Unity didn't connect within 2 minutes, so training gave up. If Unity was already in Play mode, "
             "stop Play first. Then start a new run and press Play.")
LOST_CONNECTION = ("Unity lost its connection to training. Leave Play mode in Unity, then press Play again to carry on, "
                   "or stop training.")
STARTING_UP = "ML-Agents is starting up. Press Play once it says it is waiting for Unity."
WAITING = "Waiting for Unity: press Play in the Editor to start training."


class ParseLineTest(unittest.TestCase):
    def test_waiting(self):
        line = "[INFO] Listening on port 5004. Start training by pressing the Play button in the Unity Editor."
        self.assertEqual(parse_line(line), {"event": "waiting"})

    def test_progress_lines(self):
        cases = {
            "plain": (
                "[INFO] Mover. Step: 50000. Time Elapsed: 120.543 s. Mean Reward: 1.234. Std of Reward: 0.500. Training.",
                {"behavior": "Mover", "step": 50000, "seconds": 120.543, "mean_reward": 1.234, "training": True}),
            "a name with spaces and dots": (
                "[INFO] Robot Arm v1.2. Step: 10000. Time Elapsed: 20.100 s. Mean Reward: 0.500. Std of Reward: 0.200. Training.",
                {"behavior": "Robot Arm v1.2", "step": 10000, "seconds": 20.1, "mean_reward": 0.5, "training": True}),
            "a negative reward": (
                "[INFO] Mover. Step: 1000. Time Elapsed: 3.250 s. Mean Reward: -2.500. Std of Reward: 0.100. Training.",
                {"behavior": "Mover", "step": 1000, "seconds": 3.25, "mean_reward": -2.5, "training": True}),
            "a group reward": (
                "[INFO] Mover. Step: 2000. Time Elapsed: 5.000 s. Mean Reward: 0.750. Mean Group Reward: 0.300. Training.",
                {"behavior": "Mover", "step": 2000, "seconds": 5.0, "mean_reward": 0.75, "training": True}),
            "only a group reward": (
                "[INFO] Mover. Step: 2000. Time Elapsed: 5.000 s. Mean Group Reward: 0.300. Training.",
                {"behavior": "Mover", "step": 2000, "seconds": 5.0, "mean_reward": None, "training": True}),
            "no finished episode": (
                "[INFO] Mover. Step: 3000. Time Elapsed: 7.500 s. No episode was completed since last summary. Training.",
                {"behavior": "Mover", "step": 3000, "seconds": 7.5, "mean_reward": None, "training": True}),
            "not training": (
                "[INFO] Mover. Step: 4000. Time Elapsed: 9.000 s. Mean Reward: 1.000. Std of Reward: 0.000. Not Training.",
                {"behavior": "Mover", "step": 4000, "seconds": 9.0, "mean_reward": 1.0, "training": False}),
            "not training and no episode": (
                "[INFO] Mover. Step: 4000. Time Elapsed: 9.000 s. No episode was completed since last summary. Not Training.",
                {"behavior": "Mover", "step": 4000, "seconds": 9.0, "mean_reward": None, "training": False}),
        }
        for name, (line, fields) in cases.items():
            with self.subTest(name):
                self.assertEqual(parse_line(line), {"event": "progress", **fields})

    def test_a_trailing_newline_is_ignored(self):
        self.assertEqual(parse_line("[INFO] Listening on port 5004. Start training by pressing the Play button.\n"),
                         {"event": "waiting"})

    def test_connected(self):
        for line in ("[INFO] Connected to Unity environment with package version 4.2.0 and communication version 1.5.0",
                     "[INFO] Connected new brain: Mover?team=0"):
            with self.subTest(line):
                self.assertEqual(parse_line(line), {"event": "connected"})

    def test_exported(self):
        self.assertEqual(parse_line("[INFO] Exported results/run1/Mover/Mover-50000.onnx"),
                         {"event": "exported", "path": "results/run1/Mover/Mover-50000.onnx"})

    def test_a_worker_restart(self):
        # Printed about 120 s after the last step, when Unity stops answering mid-run.
        for line in (
            "[WARNING] Restarting worker[0] after 'The Unity environment took too long to respond. Make sure that :'",
            "[WARNING] Restarting worker[0] after 'The Unity environment took too long to respond. Make sure that :\n",
            "[WARNING] Restarting worker[3] after 'something else'",
        ):
            with self.subTest(line):
                self.assertEqual(parse_line(line), {"event": "restarting"})

    def test_the_lines_around_a_restart_are_not_one(self):
        for line in ("\t The environment does not need user interaction to launch",
                     "[WARNING] Something odd happened.", "[INFO] Restarting worker[0] after 'x'",
                     "Restarting worker[0] after 'x'"):
            with self.subTest(line):
                self.assertIsNone(parse_line(line))

    def test_the_copied_line_is_not_an_export(self):
        self.assertIsNone(parse_line("[INFO] Copied results/run1/Mover/Mover-50000.onnx to results/run1/Mover.onnx."))

    def test_interrupted(self):
        self.assertEqual(parse_line("[INFO] Learning was interrupted. Please wait while the graph is generated."),
                         {"event": "interrupted"})

    def test_errors(self):
        cases = {
            "timeout": "mlagents_envs.exception.UnityTimeOutException: The Unity environment took too long to respond. "
                       "Make sure that :",
            "previous_run": "mlagents.trainers.exception.UnityTrainerException: Previous data from this run ID was "
                            "found. Either specify a new run ID, use --resume to resume this run, or use the --force "
                            "parameter to overwrite existing data.",
            "port_busy": "OSError: [Errno 48] Address already in use",
        }
        for kind, line in cases.items():
            with self.subTest(kind):
                self.assertEqual(parse_line(line), {"event": "error", "kind": kind})
        self.assertEqual(
            parse_line("mlagents_envs.exception.UnityWorkerInUseException: Couldn't start socket communication because "
                       "worker number 0 is still in use."),
            {"event": "error", "kind": "port_busy"})

    def test_everything_else_is_ignored(self):
        for line in (
            "", "   ", "            ┐  ╖", " ╬╬╬╜       ╬╬╣╣      ╙╝╣╣╬      ╙╣╣╣╗╖╓╗╣╣╣╜ ╟╣╣╬   ╣╣╣  ╣╣╣  ╟╣╣╦╓    ╣╣╣╣╣",
            " Version information:", "  ml-agents: 1.2.0.dev0,", "  Communicator API: 1.5.0,", "  PyTorch: 2.8.0",
            "Traceback (most recent call last):", "    sys.exit(main())",
            "[INFO] Hyperparameters for behavior name Mover:",
            "[INFO] Starting training from step 0 and saving to results/run1/Mover.",
            "[WARNING] Something odd happened.",
        ):
            with self.subTest(line):
                self.assertIsNone(parse_line(line))


def wait_for(test, check, timeout=10.0, what="the expected change"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    test.fail(f"Timed out waiting for {what}")


def process_gone(pid: int, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


class TrainingCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = fake_unity_project(self.root / "Game")
        self.logs = self.root / "app" / "logs"
        self.fake = self.root / "fake"
        self.fake.mkdir()
        self.training = Training(self.logs)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        # Let the fixture get as far as listening first. A suite run in the background has SIGINT ignored, and a SIGINT
        # sent while a fresh process is still starting up is then lost (see test_a_run_does_not_inherit_an_ignored_sigint).
        deadline = time.monotonic() + 5
        while self.state() in ("waiting", "training") and not (self.fake / "ready").exists() \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.training.shutdown()

    def begin(self, script: str, run_id="run1", env=None, **kwargs) -> dict:
        """Start the fixture acting out `script`. The run inherits `env` from this process, as it would any setting."""
        exports = {"FAKE_ML_SCRIPT": script, "FAKE_ML_DIR": str(self.fake), **(env or {})}
        with mock.patch.dict(os.environ, exports):
            return self.training.start(WRAPPER, self.project, run_id, **kwargs)

    def state(self) -> str:
        return self.training.status()["state"]

    def wait_state(self, *states, timeout=10.0):
        return wait_for(self, lambda: (s := self.training.status())["state"] in states and s, timeout,
                        f"state {states}")

    def wait_ready(self):
        """The fixture is listening and its signal handling is in place."""
        wait_for(self, (self.fake / "ready").exists, what="the fake mlagents-learn to listen")

    def pid_of(self, name: str) -> int:
        return int(wait_for(self, lambda: (self.fake / name).exists() and (self.fake / name).read_text(),
                            what=name))

    def recorded(self) -> dict:
        return json.loads(wait_for(self, lambda: (self.fake / "argv.json").exists()
                                   and (self.fake / "argv.json").read_text(), what="argv.json"))


class StatusShapeTest(TrainingCase):
    KEYS = {"state", "run_id", "serial", "project", "behaviors", "message", "log", "log_tail", "models", "exit_code",
            "listening"}

    def test_idle_before_any_run(self):
        status = self.training.status()
        self.assertEqual(set(status), self.KEYS)
        self.assertEqual(status["state"], "idle")
        self.assertIsNone(status["run_id"])
        self.assertIsNone(status["serial"])
        self.assertEqual((status["behaviors"], status["log_tail"], status["models"]), ({}, [], []))
        self.assertIsNone(status["exit_code"])
        self.assertIs(status["listening"], False)
        self.assertTrue(status["message"].endswith("."))

    def test_stop_and_shutdown_do_nothing_when_idle(self):
        self.assertEqual(self.training.stop()["state"], "idle")
        self.training.shutdown()
        self.assertEqual(self.state(), "idle")

    def test_a_started_run_reports_its_names(self):
        status = self.begin("wait_interrupt")
        self.assertEqual(set(status), self.KEYS)
        self.assertEqual(status["run_id"], "run1")
        self.assertEqual(status["project"], str(self.project))
        self.assertEqual(status["log"], str(self.logs / "mlagents-run1.log"))

    def test_the_log_tail_keeps_the_last_15_lines_without_blanks(self):
        self.begin("progress")
        status = wait_for(self, lambda: (s := self.training.status())["behaviors"].get("Mover", {}).get("step") == 20000
                          and s, what="the second progress line")
        self.assertEqual(len(status["log_tail"]), 15)
        self.assertIn("Step: 20000", status["log_tail"][-1])
        self.assertNotIn("", [line.strip() for line in status["log_tail"]])
        written = (self.logs / "mlagents-run1.log").read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(written), 15)


class StartAndWaitTest(TrainingCase):
    def test_starts_in_waiting_with_the_play_message(self):
        status = self.begin("wait_interrupt")
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["message"], WAITING)
        self.assertIsNone(status["exit_code"])
        self.assertIs(status["listening"], True)

    def test_the_log_is_written_in_utf8_under_the_logs_folder(self):
        with self.assertRaises(TrainingError):
            self.begin("previous_run")
        self.wait_state("failed")
        log = self.logs / "mlagents-run1.log"
        text = log.read_text(encoding="utf-8")
        self.assertIn("╬╬╬╜", text)
        self.assertIn("UnityTrainerException", text)

    def test_the_run_command_has_the_documented_shape(self):
        self.begin("wait_interrupt")
        recorded = self.recorded()
        self.assertEqual(recorded["argv"], [
            "--run-id", "run1", "--results-dir", str(self.project / RESULTS_DIR), "--timeout-wait", "120"])
        self.assertEqual(os.path.realpath(recorded["cwd"]), os.path.realpath(self.project))

    def test_a_run_does_not_inherit_an_ignored_sigint(self):
        # Started in the background by a shell, the control center has SIGINT ignored. Its run must still hear Stop.
        before = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self.begin("wait_interrupt")
        finally:
            signal.signal(signal.SIGINT, before)
        recorded = self.recorded()
        self.assertEqual(recorded["sigint_at_start"], "handled")
        self.assertEqual(recorded["argv"][:2], ["--run-id", "run1"])  # the same command, just reached through a reset
        self.wait_ready()
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")
        self.assertEqual(self.training.status()["models"], ["Mover"])  # SIGINT was heard, so the model was saved

    def test_a_signal_py_in_the_project_cant_shadow_the_reset(self):
        (self.project / "signal.py").write_text("raise RuntimeError('this file must not be imported')\n")
        before = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self.begin("wait_interrupt")
        finally:
            signal.signal(signal.SIGINT, before)
        self.assertEqual(self.recorded()["sigint_at_start"], "handled")

    def test_a_reused_run_name_is_refused_before_anything_is_touched(self):
        (self.project / RESULTS_DIR / "run1").mkdir(parents=True)
        config = self.project / CONFIGS_DIR / "run1.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("old: config\n")
        self.logs.mkdir(parents=True)
        log = self.logs / "mlagents-run1.log"
        log.write_text("old log\n")
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", config_yaml="new: config\n")
        self.assertEqual(str(caught.exception),
                         "A run called run1 already exists. Resume it, overwrite it, or use a new name.")
        self.assertEqual((config.read_text(), log.read_text()), ("old: config\n", "old log\n"))
        self.assertFalse((self.fake / "argv.json").exists(), "a process was started")
        self.assertEqual(self.state(), "idle")

    def test_resume_and_overwrite_are_the_ways_back_into_a_used_run_name(self):
        (self.project / RESULTS_DIR / "run1").mkdir(parents=True)
        for kwargs in ({"resume": True}, {"overwrite": True}):
            with self.subTest(kwargs):
                self.assertEqual(self.begin("wait_interrupt", **kwargs)["state"], "waiting")
                self.wait_ready()
                self.training.stop(timeout=10)
                (self.fake / "ready").unlink()

    def test_nothing_starts_once_the_service_is_shutting_down(self):
        self.training.shutdown()
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt")
        self.assertEqual(str(caught.exception), "GazeVoice is shutting down, so training can't start.")
        self.assertEqual(self.state(), "idle")
        self.assertFalse((self.fake / "argv.json").exists())

    def test_the_wait_can_be_shortened(self):
        self.begin("wait_interrupt", timeout_wait=7)
        argv = self.recorded()["argv"]
        self.assertEqual(argv[argv.index("--timeout-wait") + 1], "7")

    def test_resume_and_overwrite_are_passed_through(self):
        for kwargs, flag in (({"resume": True}, "--resume"), ({"overwrite": True}, "--force")):
            with self.subTest(flag):
                (self.fake / "argv.json").unlink(missing_ok=True)
                self.begin("wait_interrupt", **kwargs)
                argv = self.recorded()["argv"]
                self.assertEqual(argv[-1], flag)
                self.assertEqual(argv.count("--resume") + argv.count("--force"), 1)
                self.wait_ready()
                self.training.stop(timeout=10)
                (self.fake / "ready").unlink()

    def test_resume_together_with_overwrite_is_refused(self):
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", resume=True, overwrite=True)
        self.assertEqual(str(caught.exception), "Choose either resume or overwrite, not both.")
        self.assertEqual(self.state(), "idle")

    def test_the_config_is_written_where_the_run_can_find_it(self):
        yaml = "behaviors:\n  Mover:\n    trainer_type: ppo\n    max_steps: 5000\n"
        self.begin("wait_interrupt", config_yaml=yaml)
        path = self.project / CONFIGS_DIR / "run1.yaml"
        self.assertEqual(path.read_text(), yaml)
        self.assertEqual(self.recorded()["argv"][0], str(path))

    def test_without_a_config_nothing_is_written(self):
        self.begin("wait_interrupt")
        self.assertFalse((self.project / CONFIGS_DIR).exists())

    def test_a_config_over_64_kb_is_refused_and_nothing_starts(self):
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", config_yaml="x" * (64 * 1024 + 1))
        self.assertIn("64 KB", str(caught.exception))
        self.assertFalse((self.project / CONFIGS_DIR / "run1.yaml").exists())
        self.assertEqual(self.state(), "idle")
        self.assertFalse((self.logs / "mlagents-run1.log").exists())

    def test_a_bad_run_id_is_refused_and_nothing_starts(self):
        for bad in ("", "-first", ".hidden", "has space", "a/b", "../x", "x" * 65, "tab\t", "end\n", "naïve"):
            with self.subTest(bad), self.assertRaises(TrainingError) as caught:
                self.begin("wait_interrupt", run_id=bad)
            self.assertIn("run name", str(caught.exception))
        self.assertEqual(self.state(), "idle")
        self.assertFalse(self.logs.exists())
        self.assertFalse(self.project.joinpath("MLAgents").exists())

    def test_a_run_id_of_64_characters_is_fine(self):
        self.begin("wait_interrupt", run_id="a" + "b" * 63)
        self.assertEqual(self.state(), "waiting")

    def test_a_folder_without_assets_is_refused(self):
        bare = self.root / "NotUnity"
        bare.mkdir()
        for folder in (bare, self.root / "missing"):
            with self.subTest(folder.name), self.assertRaises(TrainingError) as caught:
                self.training.start(WRAPPER, folder, "run1")
            self.assertIn("Unity project", str(caught.exception))
        self.assertEqual(self.state(), "idle")
        self.assertFalse((bare / "MLAgents").exists())

    def test_a_missing_mlagents_learn_says_to_install_it(self):
        with self.assertRaises(TrainingError) as caught:
            self.training.start(self.root / "nowhere" / "mlagents-learn", self.project, "run1")
        self.assertEqual(str(caught.exception), "ML-Agents isn't installed. Press Install on the ML-Agents unit.")
        self.assertEqual(self.state(), "idle")

    def test_a_second_start_while_waiting_is_refused_and_changes_nothing(self):
        self.begin("wait_interrupt")
        self.wait_ready()
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", run_id="run2", config_yaml="a: 1\n")
        self.assertEqual(str(caught.exception), ALREADY_GOING)
        status = self.training.status()
        self.assertEqual((status["run_id"], status["state"]), ("run1", "waiting"))
        self.assertFalse((self.logs / "mlagents-run2.log").exists())
        self.assertFalse((self.project / CONFIGS_DIR / "run2.yaml").exists())

    def test_a_second_start_while_training_is_refused(self):
        self.begin("progress")
        self.wait_state("training")
        with self.assertRaises(TrainingError) as caught:
            self.begin("progress", run_id="run2")
        self.assertEqual(str(caught.exception), ALREADY_GOING)

    def test_a_new_run_can_start_once_the_last_one_is_over(self):
        self.begin("finish", run_id="first")
        self.wait_state("finished")
        (self.fake / "ready").unlink()
        self.begin("wait_interrupt", run_id="second")
        self.wait_ready()
        status = self.training.status()
        self.assertEqual((status["run_id"], status["state"], status["behaviors"], status["exit_code"]),
                         ("second", "waiting", {}, None))


    def test_a_results_folder_that_cant_be_read_is_a_plain_sentence_and_starts_nothing(self):
        results = self.project / RESULTS_DIR
        (results / "run1").mkdir(parents=True)
        results.chmod(0)  # the run's folder can't even be looked at
        self.addCleanup(results.chmod, 0o700)
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", config_yaml="new: config\n")
        self.assertEqual(str(caught.exception), "Couldn't read the training results folder in your Unity project.")
        self.assertFalse((self.project / CONFIGS_DIR / "run1.yaml").exists(), "the config was written")
        self.assertFalse((self.logs / "mlagents-run1.log").exists(), "a log was opened")
        self.assertFalse((self.fake / "argv.json").exists(), "a process was started")
        self.assertEqual(self.training.status()["state"], "idle")


class ListeningTest(TrainingCase):
    """Unity's Academy asks for a trainer once, when Play starts, so Play must start after the trainer is listening."""

    def test_the_wait_is_45_seconds_unless_a_test_shortens_it(self):
        self.assertEqual(mlagents.LISTEN_SECONDS, 45)
        self.assertEqual(inspect.signature(Training.start).parameters["listen_seconds"].default, mlagents.LISTEN_SECONDS)

    def test_start_waits_for_the_listening_line_and_says_so(self):
        began = time.monotonic()
        status = self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "0.6"})
        self.assertGreaterEqual(time.monotonic() - began, 0.5)
        self.assertIs(status["listening"], True)
        self.assertEqual(status["state"], "waiting")
        self.assertEqual(status["message"], WAITING)
        self.assertIs(self.training.status()["listening"], True)

    def test_listening_is_not_reported_before_the_line_is_printed(self):
        status = self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "3"}, listen_seconds=0.3)
        self.assertIs(status["listening"], False)
        self.assertFalse((self.fake / "ready").exists())

    def test_no_listening_line_in_time_is_not_a_failure(self):
        began = time.monotonic()
        status = self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "3"}, listen_seconds=0.3)
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertEqual(status["state"], "waiting")
        self.assertIs(status["listening"], False)
        self.assertEqual(status["message"], STARTING_UP)
        self.assertIsNone(status["exit_code"])
        self.assertEqual(self.training.status()["message"], STARTING_UP)

    def test_the_message_changes_when_the_line_arrives_later(self):
        seen = []
        self.training.subscribe(lambda: seen.append(self.training.status()["listening"]))
        self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "1"}, listen_seconds=0.2)
        later = wait_for(self, lambda: (s := self.training.status())["listening"] and s, what="the Listening line")
        self.assertEqual((later["state"], later["message"]), ("waiting", WAITING))
        wait_for(self, lambda: True in seen, what="a callback after the Listening line")

    def test_a_connection_with_no_listening_line_counts_as_listening(self):
        # The line may be missed or reworded in another ML-Agents version; a run that has connected is listening.
        for shown in ("connected", "progress"):
            with self.subTest(shown):
                (self.fake / "ready").unlink(missing_ok=True)
                began = time.monotonic()
                status = self.begin("no_listening", run_id=f"run-{shown}", env={"FAKE_ML_SHOW": shown}, listen_seconds=5)
                self.assertLess(time.monotonic() - began, 4.0, "start() waited out the whole time")
                self.assertIs(status["listening"], True)
                self.assertEqual(status["state"], "training")
                self.assertNotIn("Listening on port", " ".join(status["log_tail"]))
                self.wait_ready()
                self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")

    def test_a_run_that_listens_and_then_ends_quickly_is_not_refused(self):
        status = self.begin("finish")
        self.assertIs(status["listening"], True)
        self.assertEqual(self.wait_state("finished")["exit_code"], 0)

    def test_a_stop_while_start_is_still_waiting_ends_the_wait_without_an_error(self):
        result = {}

        def starter():
            try:
                result["status"] = self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "5"}, listen_seconds=20)
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                result["error"] = exc

        thread = threading.Thread(target=starter)
        began = time.monotonic()
        thread.start()
        wait_for(self, (self.fake / "armed").exists, what="the fake mlagents-learn to be ready for a stop")
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 4.0)
        self.assertNotIn("error", result)
        self.assertIn(result["status"]["state"], ("stopping", "stopped"))  # it was stopped, not refused


class RestartTest(TrainingCase):
    """Unity stops answering mid-run: mlagents-learn restarts its worker and waits for Play again, for up to 2 minutes."""

    def go(self, name):
        (self.fake / name).write_text(name)

    def test_a_worker_restart_puts_the_run_back_to_waiting_and_a_new_connection_to_training(self):
        self.begin("restart")
        trained = wait_for(self, lambda: (s := self.training.status())["behaviors"].get("Mover", {}).get("step") == 10000
                           and s, what="the first progress line")
        self.assertEqual((trained["state"], trained["listening"]), ("training", True))

        self.go("restart")
        lost = self.wait_state("waiting")
        self.assertIs(lost["listening"], False)  # until it says it is listening again
        self.assertEqual(lost["message"], LOST_CONNECTION)
        self.assertEqual(lost["behaviors"]["Mover"]["step"], 10000)  # what it had learnt so far is still shown

        self.go("listen")
        again = wait_for(self, lambda: (s := self.training.status())["listening"] and s, what="the second Listening line")
        self.assertEqual((again["state"], again["message"]), ("waiting", LOST_CONNECTION))

        self.go("reconnect")
        back = wait_for(self, lambda: (s := self.training.status())["behaviors"]["Mover"]["step"] == 30000 and s,
                        what="progress after the new connection")
        self.assertEqual(back["state"], "training")
        self.assertEqual(back["message"], "Training run1: step 30000, mean reward 2.500.")
        self.assertIs(back["listening"], True)

    def test_a_connection_alone_is_enough_to_be_training_again(self):
        self.begin("restart")
        self.wait_state("training")
        self.go("restart")
        self.wait_state("waiting")
        self.go("listen")
        self.go("reconnect")
        self.assertEqual(self.wait_state("training")["state"], "training")

    def test_the_run_can_still_be_stopped_while_it_waits_to_be_connected_again(self):
        self.begin("restart")
        self.wait_state("training")
        self.go("restart")
        self.wait_state("waiting")
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")

    def test_a_restart_before_unity_ever_connected_changes_nothing(self):
        self.begin("wait_interrupt")
        self.wait_ready()
        with self.training._lock:  # the fake can't print it before connecting; the rule is the service's
            run = self.training._run
            self.training._apply(run, {"event": "restarting"})
            self.assertEqual((run.state, run.listening), ("waiting", True))
        self.assertEqual(self.training.status()["message"], WAITING)

    def test_subscribers_are_told_when_the_run_goes_back_to_waiting(self):
        states = []
        self.training.subscribe(lambda: states.append(self.training.status()["state"]))
        self.begin("restart")
        self.wait_state("training")
        self.go("restart")
        wait_for(self, lambda: states[-1:] == ["waiting"] and "training" in states, what="a callback for the restart")


class FailureTest(TrainingCase):
    def test_play_never_pressed_ends_as_failed_with_the_timeout_sentence(self):
        started = time.monotonic()
        status = self.begin("timeout", timeout_wait=1)
        self.assertEqual(status["state"], "waiting")
        failed = self.wait_state("failed")
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(failed["message"], TIMED_OUT)
        self.assertEqual(failed["exit_code"], 1)
        self.assertIn("UnityTimeOutException", "".join(failed["log_tail"]))

    def test_a_timeout_after_training_began_does_not_blame_the_play_button(self):
        self.begin("timeout", timeout_wait=1, env={"FAKE_ML_CONNECT": "1"})
        failed = self.wait_state("failed")
        self.assertEqual(failed["behaviors"]["Mover"]["step"], 10000)
        self.assertEqual(failed["message"], "Unity stopped answering for 2 minutes during training, so training ended. "
                                            f"The log is at {self.logs / 'mlagents-run1.log'}.")

    def refused(self, script, **kwargs):
        """The sentence start() raises when the run ends before it is listening; the run itself stays, as failed."""
        with self.assertRaises(TrainingError) as caught:
            self.begin(script, **kwargs)
        failed = self.training.status()
        self.assertEqual(failed["state"], "failed")
        self.assertIs(failed["listening"], False)
        self.assertEqual(str(caught.exception), failed["message"])
        return failed

    def test_a_reused_run_name_fails_with_its_sentence(self):
        failed = self.refused("previous_run", run_id="old.run")
        self.assertEqual(failed["message"],
                         "A run called old.run already exists. Resume it, overwrite it, or use a new name.")
        self.assertEqual(failed["exit_code"], 1)

    def test_a_busy_port_fails_with_its_sentence(self):
        failed = self.refused("port_busy")
        self.assertEqual(failed["message"], "Another training run is already using ML-Agents' connection to Unity. "
                                            "Stop it first.")

    def test_any_other_error_points_to_the_log(self):
        failed = self.refused("crash")
        self.assertEqual(failed["exit_code"], 2)
        self.assertEqual(failed["message"],
                         f"Training stopped with an error. The log is at {self.logs / 'mlagents-run1.log'}.")

    def test_a_run_that_failed_to_start_does_not_block_the_next_one(self):
        self.refused("previous_run")
        (self.fake / "ready").unlink(missing_ok=True)
        self.assertEqual(self.begin("wait_interrupt", run_id="run2")["state"], "waiting")


class ProgressTest(TrainingCase):
    def test_progress_moves_it_to_training_with_step_and_reward(self):
        self.begin("progress")
        status = wait_for(self, lambda: (s := self.training.status())["behaviors"].get("Mover", {}).get("step") == 20000
                          and s, what="the second progress line")
        self.assertEqual(status["state"], "training")
        self.assertEqual(status["behaviors"], {"Mover": {"step": 20000, "mean_reward": 1.234, "seconds": 41.25}})
        self.assertEqual(status["message"], "Training run1: step 20000, mean reward 1.234.")

    def test_a_run_with_no_finished_episode_says_so(self):
        self.begin("progress", env={"FAKE_ML_PROGRESS": "no_episode"})
        status = wait_for(self, lambda: (s := self.training.status())["behaviors"] and s, what="a progress line")
        self.assertEqual(status["behaviors"]["Mover"]["mean_reward"], None)
        self.assertEqual(status["message"], "Training run1: step 10000, no finished episode yet.")

    def test_connecting_alone_is_enough_to_be_training(self):
        self.begin("progress", env={"FAKE_ML_PROGRESS": "none"})
        status = self.wait_state("training")
        self.assertEqual(status["behaviors"], {})
        self.assertEqual(status["message"], "Training run1: Unity is connected, and no steps are reported yet.")

    def test_it_finishes_by_itself(self):
        self.begin("finish")
        status = self.wait_state("finished")
        self.assertEqual(status["message"], "Training run1 finished. Its model is ready to use.")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["models"], ["Mover"])
        self.assertEqual(status["behaviors"]["Mover"]["step"], 50000)

    def test_finishing_without_a_model_does_not_claim_one(self):
        self.begin("finish", env={"FAKE_ML_SAVE": "0"})
        status = self.wait_state("finished")
        self.assertEqual(status["message"], "Training run1 finished, but it saved no model.")
        self.assertEqual(status["models"], [])

    def test_a_log_that_cant_be_written_is_closed_and_the_run_carries_on(self):
        class BrokenLog:
            close_calls = 0

            def write(self, text):
                raise OSError(errno.ENOSPC, "No space left on device")

            def flush(self):
                pass

            def close(self):
                BrokenLog.close_calls += 1

        with mock.patch.object(mlagents, "open", return_value=BrokenLog(), create=True):
            self.begin("finish")
        status = self.wait_state("finished")  # the pipe was still read, or the run would have frozen
        self.assertEqual(status["models"], ["Mover"])
        self.assertEqual(BrokenLog.close_calls, 1)

    def test_status_can_be_read_from_other_threads_while_it_runs(self):
        errors, stop = [], threading.Event()

        def poll():
            try:
                while not stop.is_set():
                    self.training.status()
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        threads = [threading.Thread(target=poll) for _ in range(4)]
        for thread in threads:
            thread.start()
        try:
            self.begin("finish")
            self.wait_state("finished")
        finally:
            stop.set()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])


class StopTest(TrainingCase):
    def test_stop_signals_the_whole_group_and_keeps_the_model(self):
        self.begin("wait_interrupt", env={"FAKE_ML_HELPER": "sigint"})
        self.wait_ready()
        helper = self.pid_of("helper.pid")
        status = self.training.stop(timeout=10)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["models"], ["Mover"])
        self.assertEqual(status["message"], "Training run1 stopped. Its model was saved and is ready to use.")
        self.assertIn("interrupted", " ".join(status["log_tail"]))
        wait_for(self, (self.fake / "helper.sigint").exists, what="the helper to get the SIGINT")
        self.assertTrue(process_gone(helper))
        self.assertTrue(process_gone(self.pid_of("main.pid")))

    def test_stop_says_when_no_model_was_saved(self):
        self.begin("wait_interrupt", env={"FAKE_ML_SAVE": "0"})
        self.wait_ready()
        status = self.training.stop(timeout=10)
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["message"], "Training run1 stopped before a model was saved.")

    def test_stop_while_training(self):
        self.begin("progress")
        self.wait_state("training")
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")

    def test_a_run_that_ignores_sigint_is_killed_after_the_wait_and_so_is_its_helper(self):
        self.begin("deaf", env={"FAKE_ML_HELPER": "deaf"})
        self.wait_ready()
        helper, main = self.pid_of("helper.pid"), self.pid_of("main.pid")
        result = {}
        stopper = threading.Thread(target=lambda: result.update(self.training.stop(timeout=3.0)))
        started = time.monotonic()
        stopper.start()
        stopping = self.wait_state("stopping")
        self.assertEqual(stopping["message"], "Stopping training run1. Waiting for it to save the model.")
        with self.assertRaises(TrainingError) as caught:
            self.begin("wait_interrupt", run_id="run2")
        self.assertEqual(str(caught.exception), ALREADY_GOING)
        stopper.join(timeout=10)
        self.assertFalse(stopper.is_alive())
        self.assertGreaterEqual(time.monotonic() - started, 2.5)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["exit_code"], -9)
        self.assertEqual(result["message"], "Training run1 stopped before a model was saved.")
        self.assertTrue(process_gone(main))
        self.assertTrue(process_gone(helper))

    def test_stop_twice_is_harmless(self):
        self.begin("wait_interrupt")
        self.wait_ready()
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")
        self.assertEqual(self.training.stop(timeout=10)["state"], "stopped")

    def test_shutdown_stops_a_running_run(self):
        self.begin("wait_interrupt", env={"FAKE_ML_HELPER": "sigint"})
        self.wait_ready()
        main, helper = self.pid_of("main.pid"), self.pid_of("helper.pid")
        self.training.shutdown()
        self.assertEqual(self.state(), "stopped")
        self.assertTrue(process_gone(main))
        self.assertTrue(process_gone(helper))

    def test_a_helper_still_holding_the_pipe_after_a_natural_finish_is_cleaned_up(self):
        with mock.patch.object(mlagents, "_DRAIN_SECONDS", 0.3):
            self.begin("finish", env={"FAKE_ML_HELPER": "deaf"})
            helper = self.pid_of("helper.pid")
            self.assertEqual(self.wait_state("finished")["exit_code"], 0)
            self.assertTrue(process_gone(helper), "a helper was left running")


class DrainTest(TrainingCase):
    def test_a_stop_after_the_process_has_ended_leaves_the_real_failure_alone(self):
        with mock.patch.object(mlagents, "_DRAIN_SECONDS", 1.5):
            self.begin("timeout", timeout_wait=1, env={"FAKE_ML_HELPER": "deaf"})
            main = self.pid_of("main.pid")
            self.assertTrue(process_gone(main))  # it timed out; its helper still holds the pipe, so it is draining
            time.sleep(0.1)
            self.assertEqual(self.state(), "waiting")
            status = self.training.stop(timeout=10)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["message"], TIMED_OUT)
        self.assertEqual(status["exit_code"], 1)

    def test_a_helper_that_let_go_of_the_pipe_is_still_cleaned_up_after_a_finish(self):
        with mock.patch.object(mlagents, "_DRAIN_SECONDS", 0.3):
            self.begin("finish", env={"FAKE_ML_HELPER": "quiet"})
            helper = self.pid_of("helper.pid")
            self.assertEqual(self.wait_state("finished")["exit_code"], 0)
            self.assertTrue(process_gone(helper), "a helper was left running")


class SubscribeTest(TrainingCase):
    def test_callbacks_run_after_changes_and_can_read_the_status(self):
        seen, calls = set(), []

        def on_change():
            calls.append(1)
            seen.add(self.training.status()["state"])  # would deadlock if it ran inside the lock

        self.training.subscribe(on_change)
        self.begin("finish")
        self.wait_state("finished")
        wait_for(self, lambda: "finished" in seen, what="a callback after the finish")
        self.assertGreaterEqual(len(calls), 3)

    def test_a_callback_that_fails_does_not_stop_the_others_or_the_run(self):
        later, reported = [], io.StringIO()
        self.training.subscribe(lambda: 1 / 0)
        self.training.subscribe(lambda: later.append(self.training.status()["state"]))
        with contextlib.redirect_stderr(reported):
            self.begin("finish")
            self.wait_state("finished")
            wait_for(self, lambda: "finished" in later, what="the second callback")
        self.assertIn("ZeroDivisionError", reported.getvalue())  # reported, not swallowed

    def test_stop_notifies_about_stopping_and_stopped(self):
        states = []
        self.training.subscribe(lambda: states.append(self.training.status()["state"]))
        self.begin("deaf")  # ignores SIGINT, so "stopping" lasts until the kill and can't be missed
        self.wait_ready()
        self.training.stop(timeout=0.5)
        wait_for(self, lambda: "stopped" in states, what="the stopped callback")
        self.assertIn("stopping", states)
        self.assertLess(states.index("stopping"), states.index("stopped"))

    def test_a_listener_that_raises_cant_leave_a_run_stopping_with_no_signal_sent(self):
        class Boom(BaseException):  # not an Exception, so the service doesn't swallow it
            pass

        caller = threading.get_ident()

        def listener():
            if threading.get_ident() == caller and self.training.status()["state"] == "stopping":
                raise Boom

        self.begin("wait_interrupt")
        self.wait_ready()
        self.training.subscribe(listener)
        with self.assertRaises(Boom):
            self.training.stop(timeout=10)
        status = self.wait_state("stopped", timeout=5)  # the SIGINT went out before the listeners were told
        self.assertEqual(status["models"], ["Mover"])


class OutputTest(TrainingCase):
    """What mlagents-learn printed, kept line by line for the dashboard's terminal panel."""

    def written(self) -> list[str]:
        """The lines of the run's log file: every line the reader saw, as it saw it."""
        return (self.logs / "mlagents-run1.log").read_text(encoding="utf-8").split("\n")[:-1]

    def test_the_cap_is_1000_lines(self):
        self.assertEqual(mlagents.TERMINAL_LINES, 1000)

    def test_with_no_run_there_is_nothing(self):
        self.assertEqual(self.training.output(), {"run_id": None, "serial": None, "first": 0, "lines": []})
        self.assertEqual(self.training.output(after=5), {"run_id": None, "serial": None, "first": 0, "lines": []})

    def test_every_line_is_kept_exactly_as_read_with_its_number_starting_at_1(self):
        self.begin("finish")
        self.wait_state("finished")
        out = self.training.output()
        self.assertEqual(set(out), {"run_id", "serial", "first", "lines"})
        self.assertEqual(out["run_id"], "run1")
        self.assertEqual(out["first"], 1)
        self.assertEqual([line["n"] for line in out["lines"]], list(range(1, len(out["lines"]) + 1)))
        self.assertEqual([line["text"] for line in out["lines"]], self.written())  # banner, blanks and spaces too
        texts = [line["text"] for line in out["lines"]]
        self.assertEqual(texts[0], "")  # the banner begins with an empty line, and it is kept
        self.assertIn(" ╬╬╬╜       ╬╬╣╣      ╙╝╣╣╬      ╙╣╣╣╗╖╓╗╣╣╣╜ ╟╣╣╬   ╣╣╣  ╣╣╣  ╟╣╣╦╓    ╣╣╣╣╣", texts)
        self.assertIn("        ", texts)  # a line of spaces is not trimmed
        self.assertIn("[INFO] Listening on port 5004. Start training by pressing the Play button in the Unity Editor.", texts)
        self.assertTrue(any(". Step: 50000. " in text for text in texts))
        self.assertEqual(set(out["lines"][0]), {"n", "text"})

    def test_after_gives_only_the_lines_after_that_number(self):
        self.begin("finish")
        self.wait_state("finished")
        everything = self.training.output()["lines"]
        total = len(everything)
        self.assertGreater(total, 12)
        later = self.training.output(after=10)
        self.assertEqual(later["lines"], everything[10:])
        self.assertEqual(later["lines"][0]["n"], 11)
        self.assertEqual(later["first"], 1)  # first is about what is kept, not about what was asked for
        self.assertEqual(later["run_id"], "run1")
        self.assertEqual(self.training.output(after=total - 1)["lines"], everything[-1:])
        self.assertEqual(self.training.output(after=total)["lines"], [])
        self.assertEqual(self.training.output(after=total + 50)["lines"], [])
        self.assertEqual(self.training.output(after=0)["lines"], everything)
        self.assertEqual(self.training.output(after=-3)["lines"], everything)
        self.assertEqual(inspect.signature(Training.output).parameters["after"].default, 0)

    def test_the_banner_is_there_while_it_is_still_starting_up(self):
        self.begin("wait_interrupt", env={"FAKE_ML_LISTEN_DELAY": "3"}, listen_seconds=0.1)
        out = wait_for(self, lambda: (o := self.training.output())["lines"] and o, what="the banner")
        self.assertEqual(out["run_id"], "run1")
        self.assertFalse(any("Listening on port" in line["text"] for line in out["lines"]))

    def test_only_the_last_lines_are_kept_and_first_says_where_they_begin(self):
        with mock.patch.object(mlagents, "TERMINAL_LINES", 10):
            self.begin("finish")
            self.wait_state("finished")
            out = self.training.output()
        total = len(self.written())
        self.assertGreater(total, 10)
        self.assertEqual([line["n"] for line in out["lines"]], list(range(total - 9, total + 1)))
        self.assertEqual(out["first"], total - 9)
        self.assertEqual([line["text"] for line in out["lines"]], self.written()[-10:])
        after = self.training.output(after=total - 3)
        self.assertEqual([line["n"] for line in after["lines"]], [total - 2, total - 1, total])
        self.assertEqual(after["first"], total - 9)
        self.assertEqual(self.training.output(after=2)["lines"], out["lines"])  # lines already dropped stay dropped

    def test_a_thousand_and_a_half_lines_leave_the_last_thousand(self):
        text = "".join(f"[INFO] line {n}\n" for n in range(1, 1501))
        run = mlagents._Run("big", self.project, self.logs / "big.log", SimpleNamespace(stdout=io.StringIO(text)))
        self.training._run = run
        self.training._pump(run, None)
        run.state = "finished"  # there is no process behind this run, so nothing is left to stop
        out = self.training.output()
        self.assertEqual(len(out["lines"]), 1000)
        self.assertEqual((out["first"], out["lines"][0]), (501, {"n": 501, "text": "[INFO] line 501"}))
        self.assertEqual(out["lines"][-1], {"n": 1500, "text": "[INFO] line 1500"})
        self.assertEqual(self.training.output(after=1498)["lines"], [{"n": 1499, "text": "[INFO] line 1499"},
                                                                       {"n": 1500, "text": "[INFO] line 1500"}])

    def test_a_new_run_starts_counting_again_and_forgets_the_old_lines(self):
        self.begin("finish")
        self.wait_state("finished")
        first = self.training.output()
        self.begin("finish", run_id="run2")
        self.wait_state("finished")
        second = self.training.output()
        self.assertEqual(second["run_id"], "run2")
        self.assertEqual((second["first"], second["lines"][0]["n"]), (1, 1))
        self.assertEqual([line["n"] for line in second["lines"]], [line["n"] for line in first["lines"]])  # from 1 again
        self.assertEqual(second["lines"][:14], first["lines"][:14])  # the same banner, and none of run1's lines after it
        self.assertFalse(any("run1" in line["text"] for line in second["lines"]))

    def test_a_new_run_with_the_same_name_starts_counting_again_too(self):
        self.begin("finish")
        self.wait_state("finished")
        before = len(self.training.output()["lines"])
        self.begin("finish", overwrite=True)
        self.wait_state("finished")
        again = self.training.output()
        self.assertEqual((again["run_id"], again["first"], len(again["lines"])), ("run1", 1, before))

    def test_the_lines_of_a_run_that_failed_are_there_to_read(self):
        with self.assertRaises(TrainingError):
            self.begin("previous_run")
        self.wait_state("failed")
        texts = [line["text"] for line in self.training.output()["lines"]]
        self.assertTrue(any(text.startswith("Traceback (most recent call last)") for text in texts))
        self.assertTrue(any("UnityTrainerException: Previous data from this run ID was found" in text for text in texts))

    def test_the_log_tail_is_not_changed_by_keeping_the_lines(self):
        self.begin("finish")
        status = self.wait_state("finished")
        self.assertEqual(len(status["log_tail"]), 15)
        self.assertNotIn("", [line.strip() for line in status["log_tail"]])
        self.assertEqual(set(status), StatusShapeTest.KEYS)


class SerialTest(TrainingCase):
    """Every run has its own serial, so a run that reuses a name (resume, overwrite) can still be told from the one before."""

    def test_a_run_gets_a_serial_from_the_clock_in_microseconds_when_it_starts(self):
        before = time.time_ns() // 1000
        status = self.begin("finish")
        after = time.time_ns() // 1000
        self.assertIsInstance(status["serial"], int)
        self.assertTrue(before <= status["serial"] <= after)

    def test_the_serial_is_exact_in_javascript(self):
        """The page parses it as a double, which holds whole numbers exactly only below 2**53 (raw nanoseconds don't fit)."""
        status = self.begin("finish")
        self.assertLess(status["serial"], 2 ** 53)
        self.assertEqual(float(status["serial"]), status["serial"])
        self.assertGreater(status["serial"], 10 ** 15)  # still the clock, not a small counter

    def test_two_runs_started_back_to_back_get_different_serials_even_in_the_same_microsecond(self):
        with mock.patch.object(mlagents.time, "time_ns", return_value=1_778_000_000_000_000_000):  # the clock stands still
            first = self.begin("finish")
            self.wait_state("finished")
            second = self.begin("finish", run_id="run2")
            self.wait_state("finished")
            third = self.begin("finish", run_id="run3")
        self.assertEqual(first["serial"], 1_778_000_000_000_000)
        self.assertEqual(second["serial"], first["serial"] + 1)
        self.assertEqual(third["serial"], second["serial"] + 1)
        self.wait_state("finished")

    def test_the_serial_stays_the_same_for_the_whole_run(self):
        status = self.begin("wait_interrupt")
        serial = status["serial"]
        self.wait_ready()
        self.assertEqual(self.training.status()["serial"], serial)
        self.assertEqual(self.training.stop(timeout=10)["serial"], serial)  # stopping is not a new run
        self.assertEqual(self.training.output()["serial"], serial)

    def test_the_output_names_the_same_serial_as_the_status(self):
        self.begin("finish")
        status = self.wait_state("finished")
        out = self.training.output()
        self.assertEqual((out["run_id"], out["serial"]), ("run1", status["serial"]))
        self.assertEqual(self.training.output(after=3)["serial"], status["serial"])

    def test_a_run_started_again_under_the_same_name_by_overwrite_has_a_new_serial(self):
        first = self.begin("finish")
        self.wait_state("finished")
        second = self.begin("finish", overwrite=True)
        self.wait_state("finished")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["serial"], second["serial"])
        self.assertGreater(second["serial"], first["serial"])
        self.assertEqual(self.training.output()["serial"], second["serial"])

    def test_a_run_started_again_under_the_same_name_by_resume_has_a_new_serial(self):
        first = self.begin("finish")
        self.wait_state("finished")
        second = self.begin("finish", resume=True)
        self.wait_state("finished")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["serial"], second["serial"])

    def test_a_run_with_another_name_has_another_serial_too(self):
        first = self.begin("finish")
        self.wait_state("finished")
        second = self.begin("finish", run_id="run2")
        self.wait_state("finished")
        self.assertNotEqual(first["serial"], second["serial"])

    def test_a_run_that_fails_to_start_leaves_the_serial_of_the_one_before(self):
        first = self.begin("finish")
        self.wait_state("finished")
        with self.assertRaises(TrainingError):
            self.begin("finish")  # the name is taken, so it is refused before anything starts
        self.assertEqual(self.training.status()["serial"], first["serial"])

    def test_the_serial_is_not_a_counter_that_a_restart_would_repeat(self):
        """Two services (two dashboard runs) never start counting at the same number."""
        run = mlagents._Run("a", self.project, self.logs / "a.log", SimpleNamespace(stdout=io.StringIO("")))
        self.assertGreater(run.serial, 10 ** 15)  # microseconds since 1970
        self.assertTrue(abs(run.serial - time.time_ns() // 1000) < 10 ** 7)


class SubscribeOutputTest(TrainingCase):
    def test_every_line_arrives_with_the_run_id_and_its_number_in_order(self):
        seen = []
        self.training.subscribe_output(lambda run_id, serial, n, text: seen.append((run_id, serial, n, text)))
        self.begin("finish")
        status = self.wait_state("finished")
        kept = self.training.output()["lines"]
        wait_for(self, lambda: len(seen) >= len(kept), what="every line to be passed on")
        self.assertEqual(seen, [("run1", status["serial"], line["n"], line["text"]) for line in kept])
        self.assertEqual([n for _, _, n, _ in seen], list(range(1, len(seen) + 1)))

    def test_a_second_run_is_passed_on_under_its_own_id_from_1(self):
        seen = []
        self.training.subscribe_output(lambda run_id, serial, n, text: seen.append((run_id, serial, n)))
        self.begin("finish")
        first = self.wait_state("finished")
        self.begin("finish", run_id="run2")
        second = self.wait_state("finished")
        total = len(self.training.output()["lines"])
        wait_for(self, lambda: len([1 for run_id, _, _ in seen if run_id == "run2"]) >= total, what="the second run's lines")
        self.assertEqual({run_id for run_id, _, _ in seen}, {"run1", "run2"})
        self.assertEqual([n for run_id, _, n in seen if run_id == "run2"], list(range(1, total + 1)))
        self.assertEqual({serial for run_id, serial, _ in seen if run_id == "run1"}, {first["serial"]})
        self.assertEqual({serial for run_id, serial, _ in seen if run_id == "run2"}, {second["serial"]})

    def test_the_callback_runs_outside_the_lock_so_it_can_ask_for_the_status_and_the_lines(self):
        answers = []

        def on_line(run_id, serial, n, text):
            status = self.training.status()  # would deadlock if the lock were held
            kept = self.training.output(after=n - 1)
            answers.append((status["run_id"], [line["n"] for line in kept["lines"]][:1]))

        self.training.subscribe_output(on_line)
        self.begin("finish")
        self.wait_state("finished")
        total = len(self.training.output()["lines"])
        wait_for(self, lambda: len(answers) >= total, what="every callback to finish")
        self.assertEqual(answers[0], ("run1", [1]))

    def test_a_callback_that_raises_is_reported_and_does_not_stop_the_reader_or_the_others(self):
        later, reported = [], io.StringIO()
        self.training.subscribe_output(lambda run_id, serial, n, text: 1 / 0)
        self.training.subscribe_output(lambda run_id, serial, n, text: later.append(n))
        with contextlib.redirect_stderr(reported):
            self.begin("finish")
            status = self.wait_state("finished")
            total = len(self.training.output()["lines"])
            wait_for(self, lambda: len(later) >= total, what="the second callback to get every line")
        self.assertEqual(later, list(range(1, total + 1)))
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["behaviors"]["Mover"]["step"], 50000)  # the reader went on applying what it read
        self.assertIn("ZeroDivisionError", reported.getvalue())

    def test_state_listeners_and_output_listeners_are_separate(self):
        states, lines = [], []
        self.training.subscribe(lambda: states.append(1))
        self.training.subscribe_output(lambda run_id, serial, n, text: lines.append(n))
        self.begin("finish")
        self.wait_state("finished")
        wait_for(self, lambda: len(lines) >= len(self.training.output()["lines"]), what="the lines")
        self.assertGreaterEqual(len(states), 3)
        self.assertGreater(len(lines), len(states))  # a callback per line, not per change

    def test_a_line_is_passed_on_after_it_can_be_read_back(self):
        readable = []
        self.training.subscribe_output(
            lambda run_id, serial, n, text: readable.append(
                self.training.output(after=n - 1)["lines"][:1] == [{"n": n, "text": text}]))
        self.begin("finish")
        self.wait_state("finished")
        wait_for(self, lambda: len(readable) >= len(self.training.output()["lines"]), what="the lines")
        self.assertTrue(all(readable))


class ModelsTest(TrainingCase):
    def save(self, run_id: str, *names: str, content=b"model") -> Path:
        folder = self.project / RESULTS_DIR / run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / f"{name}.onnx").write_bytes(content + name.encode())
        return folder

    def test_models_lists_the_behaviors_with_a_saved_model(self):
        folder = self.save("run1", "Zed", "Mover", "My.Agent v2")
        (folder / "Mover").mkdir()
        (folder / "Mover" / "Mover-5000.onnx").write_bytes(b"checkpoint")
        (folder / "notes.txt").write_text("x")
        (folder / "Other.pt").write_text("x")
        self.assertEqual(self.training.models(self.project, "run1"), ["Mover", "My.Agent v2", "Zed"])

    def test_models_is_empty_for_an_unknown_run(self):
        self.assertEqual(self.training.models(self.project, "never-ran"), [])

    def test_models_refuses_a_bad_run_id(self):
        with self.assertRaises(TrainingError):
            self.training.models(self.project, "../run1")

    def test_import_copies_the_only_model_into_the_project(self):
        self.save("run1", "Mover")
        result = self.training.import_model(self.project, "run1")
        self.assertEqual(result, {"asset_path": "Assets/GazeVoice/MLAgents/run1/Mover.onnx", "behavior": "Mover"})
        copy = self.project / MODELS_DIR / "run1" / "Mover.onnx"
        self.assertEqual(copy.read_bytes(), b"modelMover")

    def test_import_overwrites_an_older_copy(self):
        self.save("run1", "Mover", content=b"old")
        self.training.import_model(self.project, "run1")
        self.save("run1", "Mover", content=b"new")
        self.training.import_model(self.project, "run1")
        self.assertEqual((self.project / MODELS_DIR / "run1" / "Mover.onnx").read_bytes(), b"newMover")

    def test_import_copies_through_a_temporary_file_beside_the_target(self):
        self.save("run1", "Mover")
        copies, real = [], shutil.copyfile

        def spy(source, target, **kwargs):
            copies.append(Path(target))
            return real(source, target, **kwargs)

        folder = self.project / MODELS_DIR / "run1"
        with mock.patch.object(mlagents.shutil, "copyfile", spy):
            self.training.import_model(self.project, "run1")
        self.assertEqual(copies, [folder / ".Mover.onnx.tmp"])
        self.assertEqual([path.name for path in folder.iterdir()], ["Mover.onnx"])

    def test_a_failed_copy_leaves_the_older_model_and_no_temporary_file(self):
        self.save("run1", "Mover", content=b"old")
        self.training.import_model(self.project, "run1")

        def breaks(source, target, **kwargs):
            Path(target).write_bytes(b"half a model")
            raise OSError(errno.ENOSPC, "No space left on device")

        folder = self.project / MODELS_DIR / "run1"
        with mock.patch.object(mlagents.shutil, "copyfile", breaks), self.assertRaises(TrainingError) as caught:
            self.training.import_model(self.project, "run1")
        self.assertIn("Couldn't copy the model", str(caught.exception))
        self.assertEqual((folder / "Mover.onnx").read_bytes(), b"oldMover")
        self.assertEqual([path.name for path in folder.iterdir()], ["Mover.onnx"])

    def test_import_names_the_choice_when_there_are_several(self):
        self.save("run1", "Mover", "Jumper")
        with self.assertRaises(TrainingError) as caught:
            self.training.import_model(self.project, "run1")
        self.assertIn("Jumper", str(caught.exception))
        self.assertIn("Mover", str(caught.exception))
        self.assertFalse((self.project / MODELS_DIR).exists())

    def test_import_takes_the_named_behavior_when_there_are_several(self):
        self.save("run1", "Mover", "Jumper")
        result = self.training.import_model(self.project, "run1", "Jumper")
        self.assertEqual(result["asset_path"], "Assets/GazeVoice/MLAgents/run1/Jumper.onnx")
        self.assertTrue((self.project / MODELS_DIR / "run1" / "Jumper.onnx").is_file())
        self.assertFalse((self.project / MODELS_DIR / "run1" / "Mover.onnx").exists())

    def test_import_refuses_a_behavior_that_has_no_model(self):
        self.save("run1", "Mover")
        for bad in ("Jumper", "../Mover", "Mover/../../x"):
            with self.subTest(bad), self.assertRaises(TrainingError) as caught:
                self.training.import_model(self.project, "run1", bad)
            self.assertIn("Mover", str(caught.exception))
        self.assertFalse((self.project / MODELS_DIR).exists())

    def test_import_with_no_model_says_so(self):
        for run_id in ("run1", "never-ran"):
            if run_id == "run1":
                (self.project / RESULTS_DIR / "run1").mkdir(parents=True)
            with self.subTest(run_id), self.assertRaises(TrainingError) as caught:
                self.training.import_model(self.project, run_id)
            self.assertEqual(str(caught.exception), f"Run {run_id} hasn't saved a model yet.")

    def test_import_refuses_a_bad_run_id_and_a_folder_that_is_not_a_project(self):
        with self.assertRaises(TrainingError):
            self.training.import_model(self.project, "../run1")
        bare = self.root / "NotUnity"
        bare.mkdir()
        with self.assertRaises(TrainingError) as caught:
            self.training.import_model(bare, "run1")
        self.assertIn("Unity project", str(caught.exception))

    def test_a_model_saved_by_a_real_run_is_found_and_imported(self):
        self.begin("finish")
        self.wait_state("finished")
        result = self.training.import_model(self.project, "run1")
        self.assertEqual(result["behavior"], "Mover")
        self.assertEqual((self.project / MODELS_DIR / "run1" / "Mover.onnx").read_bytes(), b"onnx for Mover")


class RecentRunsTest(TrainingCase):
    """After the dashboard restarts there is no current run, so the earlier runs that have a model are listed."""

    def save(self, run_id: str, *names: str, at: float | None = None) -> Path:
        folder = self.project / RESULTS_DIR / run_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            model = folder / f"{name}.onnx"
            model.write_bytes(b"model")
            if at is not None:
                os.utime(model, (at, at))
        return folder

    def test_the_list_is_at_most_10(self):
        self.assertEqual(RUNS_LISTED, 10)

    def test_no_project_or_no_results_folder_is_an_empty_list(self):
        for project in (None, "", self.root / "missing"):
            with self.subTest(project):
                self.assertEqual(recent_runs(project), [])
        self.assertEqual(recent_runs(self.project), [])
        self.assertFalse((self.project / "MLAgents").exists())  # looking never creates anything

    def test_a_run_is_listed_with_its_models_and_when_the_newest_was_saved(self):
        when = 1_760_000_000.0
        self.save("Mover-1", "Zed", "Mover", at=when)
        (self.project / RESULTS_DIR / "Mover-1" / "Mover").mkdir()
        (self.project / RESULTS_DIR / "Mover-1" / "Mover" / "Mover-5000.onnx").write_bytes(b"checkpoint")
        runs = recent_runs(self.project)
        self.assertEqual(len(runs), 1)
        self.assertEqual(set(runs[0]), {"run_id", "behaviors", "modified"})
        self.assertEqual((runs[0]["run_id"], runs[0]["behaviors"]), ("Mover-1", ["Mover", "Zed"]))
        from datetime import datetime
        self.assertEqual(datetime.fromisoformat(runs[0]["modified"]).timestamp(), when)

    def test_the_newest_run_comes_first(self):
        self.save("old", "A", at=1_760_000_000.0)
        self.save("newest", "A", at=1_760_002_000.0)
        self.save("middle", "A", at=1_760_001_000.0)
        self.assertEqual([run["run_id"] for run in recent_runs(self.project)], ["newest", "middle", "old"])

    def test_a_run_is_as_new_as_its_newest_model(self):
        folder = self.save("a-run", "Early", "Late", at=1_760_000_000.0)
        os.utime(folder / "Late.onnx", (1_760_005_000.0, 1_760_005_000.0))
        self.save("b-run", "A", at=1_760_001_000.0)
        self.assertEqual([run["run_id"] for run in recent_runs(self.project)], ["a-run", "b-run"])

    def test_only_the_10_newest_are_listed(self):
        for number in range(12):
            self.save(f"run{number:02d}", "A", at=1_760_000_000.0 + number)
        runs = recent_runs(self.project)
        self.assertEqual([run["run_id"] for run in runs], [f"run{n:02d}" for n in range(11, 1, -1)])

    def test_runs_without_a_model_are_left_out(self):
        results = self.project / RESULTS_DIR
        (results / "empty").mkdir(parents=True)
        (results / "checkpoints-only" / "Mover").mkdir(parents=True)
        (results / "checkpoints-only" / "Mover" / "Mover-5000.onnx").write_bytes(b"checkpoint")
        (results / "other-files").mkdir()
        (results / "other-files" / "Mover.pt").write_bytes(b"x")
        (results / "stray-file").write_text("not a run")
        self.save("has-one", "Mover")
        self.assertEqual([run["run_id"] for run in recent_runs(self.project)], ["has-one"])

    def test_a_folder_that_could_not_be_a_run_name_is_left_out(self):
        for name in (".hidden", "has space", "-dash"):
            self.save(name, "A")
        self.save("fine", "A")
        self.assertEqual([run["run_id"] for run in recent_runs(self.project)], ["fine"])

    def test_a_results_folder_that_cant_be_read_is_an_empty_list(self):
        self.save("run1", "A")
        results = self.project / RESULTS_DIR
        results.chmod(0)
        self.addCleanup(results.chmod, 0o700)
        self.assertEqual(recent_runs(self.project), [])

    def test_a_real_run_that_saved_a_model_shows_up(self):
        self.begin("finish")
        self.wait_state("finished")
        self.assertEqual([(run["run_id"], run["behaviors"]) for run in recent_runs(self.project)],
                         [("run1", ["Mover"])])


if __name__ == "__main__":
    unittest.main()
