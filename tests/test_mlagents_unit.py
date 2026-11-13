import os
import unittest
from pathlib import Path

from gazevoice.components.base import Context, Health
from gazevoice.components.mlagents import ML_AGENTS_COMMIT, VENV_PYTHON, MLAgents
from gazevoice.jobs import JobError
from gazevoice.mlagents import RESULTS_DIR, Training
from gazevoice.probes import ProbeTimedOut
from gazevoice.settings import Settings

from .helpers import TempCtx, make_job

UV = {"uv": "/Users/me/.local/bin/uv"}
COMMIT = "82ce4998f18bbe05eb3de834cace09fc6564ba8e"
TARBALL = f"https://github.com/Unity-Technologies/ml-agents/archive/{COMMIT}.tar.gz"
CONFIRM = ("Install ML-Agents' Python side? This sets up its own Python 3.10 environment and downloads ML-Agents and "
           "PyTorch (about 1.2 GB, about 5 minutes).")


class StubTraining:
    """Stands in for the training service: says what it is told to, and records what it is asked."""

    def __init__(self, state="idle", message="No training run has started yet.", run_id=None, serial=None,
                 listening=False):
        self.state, self.message, self.run_id = state, message, run_id
        self.serial = serial if serial is not None or run_id is None else 1778000000000000
        self.listening = listening
        self.stop_calls = 0

    def status(self):
        return {"state": self.state, "run_id": self.run_id, "serial": self.serial, "project": None, "behaviors": {},
                "message": self.message, "log": None, "log_tail": [], "models": [], "exit_code": None,
                "listening": self.listening}

    def stop(self):
        self.stop_calls += 1
        self.state, self.message = "stopped", "Training run1 stopped. Its model was saved and is ready to use."
        return self.status()


class ContextTest(unittest.TestCase):
    def test_every_context_has_a_training_service_that_logs_into_the_app_folder(self):
        t = TempCtx()
        try:
            self.assertIsInstance(t.ctx.training, Training)
            self.assertEqual(t.ctx.training.status()["state"], "idle")
            self.assertEqual(t.ctx.training._logs, t.paths.logs)
        finally:
            t.cleanup()

    def test_a_training_service_that_is_given_is_kept(self):
        t = TempCtx()
        try:
            stub = StubTraining()
            ctx = Context(runner=t.runner, paths=t.paths, settings=Settings(), training=stub)
            self.assertIs(ctx.training, stub)
        finally:
            t.cleanup()


class MLAgentsTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which=UV)
        self.t.ctx.training = StubTraining()
        self.unit = MLAgents(self.t.ctx)
        self.venv = self.t.paths.venvs / "mlagents"
        self.py = self.venv / "bin" / "python"
        self.learn = self.venv / "bin" / "mlagents-learn"
        self.probe = [str(self.py), "-c", "import mlagents.trainers"]

    def tearDown(self):
        self.t.cleanup()

    def make_venv(self, importable=True, script=True):
        self.py.parent.mkdir(parents=True, exist_ok=True)
        self.py.write_text("")
        if script:
            self.learn.write_text("")
        self.t.runner.add(self.probe, returncode=0 if importable else 1)

    def make_broken_venv(self):
        """The folder is there, but its python links to a Python that was removed (an uninstalled Anaconda, say)."""
        self.py.parent.mkdir(parents=True)
        os.symlink(self.t.root / "removed" / "bin" / "python3", self.py)
        (self.venv / "pyvenv.cfg").write_text("home = /opt/anaconda3/bin\n")

    def pip_argv(self):
        return ["uv", "pip", "install", "--python", str(self.py), "torch>=2.1.1,<=2.8.0",
                f"mlagents-envs @ {TARBALL}#subdirectory=ml-agents-envs",
                f"mlagents @ {TARBALL}#subdirectory=ml-agents"]

    def install_steps(self):
        steps = [["uv", "venv", "--clear", "--python", "3.10.12", str(self.venv)], self.pip_argv()]
        for argv in steps:
            self.t.runner.add(argv, stdout="ok\n")
        return steps

    # --- what the unit is -------------------------------------------------------------------------------------

    def test_it_is_the_ml_agents_unit(self):
        self.assertEqual((MLAgents.id, MLAgents.name, MLAgents.role), ("mlagents", "ML-Agents", "Trains Unity agents on this Mac"))
        self.assertEqual((VENV_PYTHON, ML_AGENTS_COMMIT), ("3.10.12", COMMIT))
        self.assertEqual((self.unit.venv, self.unit.python, self.unit.mlagents_learn),
                         (self.venv, self.py, self.learn))

    # --- check() ------------------------------------------------------------------------------------------------

    def test_needs_uv(self):
        self.t.runner.set_which("uv", None)
        state = self.unit.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertEqual(state.summary, "uv is needed to set up ML-Agents. Install uv (astral.sh/uv), then refresh.")

    def test_no_venv_is_not_installed_and_offers_install_with_consent(self):
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertEqual(state.summary, "Not installed. ML-Agents trains your Unity agents on this Mac.")
        self.assertEqual(next(a for a in state.actions if a.id == "install").confirm, CONFIRM)
        self.assertEqual(self.t.runner.calls, [])

    def test_a_venv_whose_python_was_removed_says_so_and_offers_install(self):
        self.make_broken_venv()
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertEqual(state.summary, "ML-Agents' Python environment doesn't work any more, usually because the "
                                        "Python it was made from was removed. Press Install to set it up again.")
        self.assertEqual(next(a for a in state.actions if a.id == "install").confirm, CONFIRM)

    def test_the_package_missing_is_not_installed(self):
        self.make_venv(importable=False)
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertEqual(state.summary, "ML-Agents isn't installed yet.")
        self.assertTrue(state.offers("install"))

    def test_a_package_without_its_training_command_is_not_installed(self):
        self.make_venv(script=False)
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_a_timed_out_import_check_is_not_reported_as_missing(self):
        self.make_venv()
        self.t.runner.add(self.probe, returncode=124, stderr="timed out")
        with self.assertRaises(ProbeTimedOut):
            self.unit.check()

    def test_ready_when_nothing_is_running(self):
        self.make_venv()
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Ready to train")
        self.assertEqual(state.detail("Python side"), "ML-Agents 4.2.0 · Python 3.10.12")
        self.assertIsNone(state.detail("Runs folder"))  # no Unity project yet
        self.assertIsNone(state.detail("Last run"))
        self.assertEqual(state.actions, ())

    def test_ready_shows_where_the_runs_go_when_a_project_is_set(self):
        self.make_venv()
        project = self.t.root / "Game"
        self.t.ctx.settings = Settings(unity_project=str(project))
        state = self.unit.check()
        self.assertEqual(state.detail("Runs folder"), str(project / RESULTS_DIR))

    def test_the_summary_follows_the_training_message_and_the_last_run_is_listed(self):
        self.make_venv()
        self.t.ctx.training = StubTraining("training", "Training run1: step 20000, mean reward 1.234.", "run1")
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Training run1: step 20000, mean reward 1.234.")
        self.assertEqual(state.detail("Last run"), "run1 · training")

    # --- the run, for the dashboard's terminal panel ------------------------------------------------------------

    def test_the_state_carries_the_live_run_and_its_serial_apart_from_the_details_text(self):
        self.make_venv()
        self.t.ctx.training = StubTraining("waiting", "Waiting for Unity: press Play in the Editor to start training.",
                                           "run1", serial=1778000000000123, listening=True)
        state = self.unit.check()
        run = {"run_id": "run1", "serial": 1778000000000123, "state": "waiting", "listening": True}
        self.assertEqual(state.run, run)
        self.assertEqual(state.to_dict()["run"], run)
        self.assertEqual(state.detail("Last run"), "run1 · waiting")  # the words for people are as they were

    def test_the_run_says_when_the_trainer_is_not_listening_yet(self):
        self.make_venv()
        self.t.ctx.training = StubTraining("waiting", "ML-Agents is starting up.", "run1", serial=5, listening=False)
        self.assertEqual(self.unit.check().to_dict()["run"],
                         {"run_id": "run1", "serial": 5, "state": "waiting", "listening": False})

    def test_a_run_that_has_ended_is_still_the_run_the_state_names(self):
        self.make_venv()
        for run_state in ("finished", "stopped", "failed", "stopping", "training"):
            with self.subTest(run_state):
                self.t.ctx.training = StubTraining(run_state, "x", "run1", serial=9, listening=True)
                self.assertEqual(self.unit.check().to_dict()["run"],
                                 {"run_id": "run1", "serial": 9, "state": run_state, "listening": True})

    def test_the_run_is_none_when_nothing_has_run_and_the_key_is_still_there(self):
        self.make_venv()
        state = self.unit.check()
        self.assertIsNone(state.run)
        self.assertIn("run", state.to_dict())
        self.assertIsNone(state.to_dict()["run"])

    def test_the_run_key_is_there_while_the_unit_is_missing_or_still_being_checked_too(self):
        missing = self.unit.check()  # no environment at all
        self.assertEqual(missing.health, Health.MISSING)
        checking = self.unit.state(Health.CHECKING, "Still checking…")  # what the control center shows while a probe is slow
        for state in (missing, checking):
            self.assertIn("run", state.to_dict())
            self.assertIsNone(state.to_dict()["run"])

    def test_a_finished_run_is_ready_to_train_again(self):
        self.make_venv()
        self.t.ctx.training = StubTraining("failed", "Training run1 stopped with an error.", "run1")
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.detail("Last run"), "run1 · failed")

    def test_stop_is_offered_only_while_a_run_is_waiting_or_training(self):
        self.make_venv()
        for run_state, offered in (("idle", False), ("waiting", True), ("training", True), ("stopping", False),
                                   ("finished", False), ("stopped", False), ("failed", False)):
            with self.subTest(run_state):
                self.t.ctx.training = StubTraining(run_state, "x", "run1")
                state = self.unit.check()
                self.assertEqual(state.offers("stop"), offered)
                if offered:
                    action = next(a for a in state.actions if a.id == "stop")
                    self.assertEqual((action.label, action.confirm), ("Stop training", None))

    def test_the_stop_key_calls_through_to_training_and_returns_its_status(self):
        self.make_venv()
        stub = self.t.ctx.training = StubTraining("training", "x", "run1")
        result = self.unit.run_action("stop", make_job("mlagents", "stop"))
        self.assertEqual(stub.stop_calls, 1)
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["message"], "Training run1 stopped. Its model was saved and is ready to use.")

    def test_a_run_that_is_live_proves_the_install_so_a_slow_probe_cannot_hide_stop(self):
        self.make_venv()
        self.t.runner.add(self.probe, returncode=124, stderr="timed out")  # a busy Mac: the import check can't answer
        for run_state, offered in (("waiting", True), ("training", True), ("stopping", False)):
            with self.subTest(run_state):
                self.t.ctx.training = StubTraining(run_state, f"the {run_state} message", "run1")
                state = self.unit.check()
                self.assertEqual(state.health, Health.READY)
                self.assertEqual(state.summary, f"the {run_state} message")
                self.assertEqual(state.detail("Last run"), f"run1 · {run_state}")
                self.assertEqual(state.offers("stop"), offered)
        self.assertEqual(self.t.runner.calls, [])  # nothing was run to find out

    def test_a_run_that_starts_while_the_probe_is_slow_is_shown_with_stop(self):
        self.make_venv()
        stub = self.t.ctx.training = StubTraining()  # idle when the check begins
        probing = self.t.runner.run

        def run_and_start_a_run(argv, **kwargs):
            result = probing(argv, **kwargs)
            if list(argv) == self.probe:  # Play was pressed during the slow import check
                stub.state, stub.message, stub.run_id = "training", "Training run1: step 100, mean reward 0.500.", "run1"
            return result

        self.t.runner.run = run_and_start_a_run
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.summary, "Training run1: step 100, mean reward 0.500.")
        self.assertEqual(state.detail("Last run"), "run1 · training")
        self.assertTrue(state.offers("stop"))

    def test_a_live_run_is_ready_even_if_uv_has_gone_since(self):
        self.make_venv()
        self.t.runner.set_which("uv", None)
        self.t.ctx.training = StubTraining("training", "Training run1: step 100, no finished episode yet.", "run1")
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertTrue(state.offers("stop"))

    def test_the_stop_key_is_not_refused_while_the_probe_is_timing_out(self):
        self.make_venv()
        self.t.runner.add(self.probe, returncode=124, stderr="timed out")
        stub = self.t.ctx.training = StubTraining("training", "x", "run1")
        self.assertTrue(self.unit.check().offers("stop"))
        self.unit.run_action("stop", make_job("mlagents", "stop"))
        self.assertEqual(stub.stop_calls, 1)

    # --- installed() --------------------------------------------------------------------------------------------

    def test_installed_is_true_only_when_the_package_imports(self):
        self.assertFalse(self.unit.installed())  # no environment
        self.make_venv(importable=False)
        self.assertFalse(self.unit.installed())
        self.t.ctx.probes.clear()
        self.t.runner.add(self.probe, returncode=0)
        self.assertTrue(self.unit.installed())

    def test_installed_needs_the_training_command_too(self):
        self.make_venv(script=False)
        self.assertFalse(self.unit.installed())

    def test_installed_lets_a_timed_out_probe_raise_instead_of_calling_a_healthy_install_missing(self):
        self.make_venv()
        self.t.runner.add(self.probe, returncode=124, stderr="timed out")
        with self.assertRaises(ProbeTimedOut):
            self.unit.installed()

    def test_installed_shares_the_probe_with_check(self):
        self.make_venv()
        self.assertTrue(self.unit.installed())
        self.assertEqual(self.unit.check().health, Health.READY)
        self.assertEqual(self.t.runner.calls.count(tuple(self.probe)), 1)  # one probe answered both

    def test_installed_is_true_without_probing_while_a_run_is_live(self):
        self.make_venv()
        self.t.runner.add(self.probe, returncode=124, stderr="timed out")
        for run_state in ("waiting", "training", "stopping"):
            with self.subTest(run_state):
                self.t.ctx.training = StubTraining(run_state, "x", "run1")
                self.assertTrue(self.unit.installed())
        self.assertEqual(self.t.runner.calls, [])
        self.t.ctx.training = StubTraining("finished", "x", "run1")  # an ended run proves nothing
        with self.assertRaises(ProbeTimedOut):
            self.unit.installed()

    # --- install ------------------------------------------------------------------------------------------------

    def test_install_runs_exactly_these_two_commands_with_an_hour_each(self):
        steps = self.install_steps()
        result = self.unit.run_action("install", make_job("mlagents", "install"))
        self.assertEqual(result, {"installed": True})
        self.assertEqual(self.t.runner.calls, [tuple(step) for step in steps])
        self.assertEqual([kw["timeout"] for kw in self.t.runner.call_kwargs], [3600, 3600])

    def test_install_without_uv_says_so_and_runs_nothing(self):
        self.t.runner.set_which("uv", None)
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("install", make_job("mlagents", "install"))
        self.assertEqual(str(caught.exception), "uv isn't installed. Install it from astral.sh/uv, then try again.")
        self.assertEqual(self.t.runner.calls, [])

    def test_a_failing_step_stops_the_install_and_names_itself(self):
        steps = self.install_steps()
        self.t.runner.add(steps[1], returncode=2, stderr="no matching distribution")
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("install", make_job("mlagents", "install"))
        self.assertEqual(str(caught.exception), "Installing ML-Agents and PyTorch failed (exit 2). See the log above.")

    def test_install_makes_a_venv_with_no_working_python_again(self):
        self.make_broken_venv()
        steps = self.install_steps()
        job = make_job("mlagents", "install")
        self.unit.run_action("install", job)
        self.assertEqual(self.t.runner.calls[0], tuple(steps[0]))
        self.assertNotIn("--force", self.t.runner.calls[0])
        made_again = "The Python environment from before has no working Python, so it's being made again."
        self.assertIn(made_again, job.lines)
        self.assertLess(job.lines.index(made_again), job.lines.index("Creating a Python 3.10 environment…"))

    def test_install_leaves_a_folder_that_is_not_a_python_environment_alone(self):
        self.venv.mkdir(parents=True)
        precious = self.venv / "notes.txt"
        precious.write_text("not an environment")
        self.install_steps()
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("install", make_job("mlagents", "install"))
        self.assertEqual(str(caught.exception), f"{self.venv} isn't a Python environment, so it's left alone. "
                                                "Move or delete that folder, then press Install again.")
        self.assertEqual(self.t.runner.calls, [])  # uv was never asked to clear anything
        self.assertEqual(precious.read_text(), "not an environment")

    def test_install_goes_ahead_over_an_empty_folder(self):
        self.venv.mkdir(parents=True)
        steps = self.install_steps()
        self.unit.run_action("install", make_job("mlagents", "install"))
        self.assertEqual(self.t.runner.calls, [tuple(step) for step in steps])

    def test_install_leaves_a_working_environment_alone(self):
        self.make_venv()
        (self.venv / "pyvenv.cfg").write_text("home = /opt/homebrew/opt/python@3.10/bin\n")
        self.t.runner.add(["uv", "venv", "--clear", "--python", "3.10.12", str(self.venv)])  # answers if wrongly asked
        self.t.runner.add(self.pip_argv(), stdout="ok\n")
        job = make_job("mlagents", "install")
        self.unit.run_action("install", job)
        self.assertEqual(self.t.runner.calls, [tuple(self.pip_argv())])
        self.assertEqual([line for line in job.lines if "being made again" in line], [])

    def test_with_no_venv_folder_install_says_nothing_about_making_it_again(self):
        steps = self.install_steps()
        job = make_job("mlagents", "install")
        self.unit.run_action("install", job)
        self.assertEqual(self.t.runner.calls[0], tuple(steps[0]))
        self.assertEqual(job.lines[0], "Creating a Python 3.10 environment…")
        self.assertEqual([line for line in job.lines if "being made again" in line], [])


if __name__ == "__main__":
    unittest.main()
