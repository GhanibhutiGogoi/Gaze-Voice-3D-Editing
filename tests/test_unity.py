import json
import unittest
from pathlib import Path

from gazevoice.components.base import Health
from gazevoice.components.unity import (
    CLI_CASK,
    HUB_CASK,
    MIN_UNITY,
    Unity,
    UnityEditor,
    UnityProject,
    best_editor,
    find_editors,
    next_project_path,
    parse_unity_version,
    project_problem,
    project_version,
)
from gazevoice.jobs import JobError
from gazevoice.runner import CommandResult, FakeRunner
from gazevoice.settings import load_settings, update_settings
from gazevoice.unity_install import add_bridge, bridge_current

from .helpers import TempCtx, fake_unity_editor, fake_unity_project, make_job

UNITY_CLI = "/opt/homebrew/bin/unity"
BREW = {"brew": "/opt/homebrew/bin/brew"}


class SetupRunner(FakeRunner):
    """FakeRunner whose installs leave their results on disk, the way the real ones do."""

    def __init__(self, env, which=None):
        super().__init__(which=which)
        self.env = env
        self.create_code = 0
        self.create_log = ""
        self.install_code = 0
        self.install_leaves_editor = True

    def run(self, argv, *, timeout=30.0, env=None, cwd=None, input_text=None):
        args = tuple(str(a) for a in argv)
        if "-createProject" in args:
            with self._lock:
                self.calls.append(args)
                self.call_kwargs.append({"timeout": timeout})
            project = Path(args[args.index("-createProject") + 1])
            log = Path(args[args.index("-logFile") + 1])
            log.write_text(self.create_log)
            if self.create_code == 0:
                fake_unity_project(project, version=args[0].split("/")[-5])
            else:
                project.mkdir(parents=True, exist_ok=True)  # a failed run can leave a stub behind
            return CommandResult(args, self.create_code, "", "")
        return super().run(argv, timeout=timeout, env=env, cwd=cwd, input_text=input_text)

    def stream(self, argv, on_line, *, timeout=None, env=None, cwd=None):
        args = tuple(str(a) for a in argv)
        if args[:2] == (UNITY_CLI, "install"):
            with self._lock:
                self.calls.append(args)
                self.call_kwargs.append({"timeout": timeout})
            if self.install_code == 0 and self.install_leaves_editor:
                fake_unity_editor(self.env.editors, "6000.3.7f1")
            return self.install_code
        if args == ("brew", "install", "--cask", HUB_CASK):
            with self._lock:
                self.calls.append(args)
            self.env.hub.mkdir(parents=True)
            return 0
        if args == ("brew", "install", "--cask", CLI_CASK):
            with self._lock:
                self.calls.append(args)
            self._which["unity"] = UNITY_CLI
            return 0
        return super().stream(argv, on_line, timeout=timeout, env=env, cwd=cwd)


class UnityEnv:
    """A throwaway Mac: nothing of Unity's on it until a test adds it."""

    def __init__(self, test, which=BREW):
        self.t = TempCtx(which=which)
        test.addCleanup(self.t.cleanup)
        self.editors = self.t.root / "Editors"
        self.editors.mkdir()
        self.hub = self.t.root / "Unity Hub.app"
        self.hub_config = self.t.root / "UnityHub"
        self.projects = self.t.root / "Projects"
        self.runner = SetupRunner(self, which=which)
        self.t.ctx.runner = self.runner
        self.ctx = self.t.ctx
        self.runner.add(["pgrep", "-x", "Unity"], returncode=1)

    def unit(self, cls):
        unit = cls(self.ctx)
        unit.roots = (self.editors,)
        unit.hub_app = self.hub
        unit.hub_config = self.hub_config
        unit.projects_dir = self.projects
        unit.cli_fallback = self.t.root / "no-unity-cli"
        return unit

    def choose(self, project):
        self.ctx.settings = update_settings(self.ctx.settings, {"unity_project": str(project)})

    def running(self, yes=True):
        self.runner.add(["pgrep", "-x", "Unity"], returncode=0 if yes else 1)
        self.ctx.probes.clear()


class FindingUnityTest(unittest.TestCase):
    def setUp(self):
        self.env = UnityEnv(self)

    def test_parse_unity_version(self):
        self.assertEqual(parse_unity_version("6000.3.7f1"), (6000, 3, 7, "f1"))
        self.assertEqual(parse_unity_version("2022.3.62f3"), (2022, 3, 62, "f3"))
        self.assertIsNone(parse_unity_version("not a version"))

    def test_editors_are_found_newest_first(self):
        fake_unity_editor(self.env.editors, "2022.3.62f3")
        fake_unity_editor(self.env.editors, "6000.3.7f1")
        self.assertEqual([e.version for e in find_editors((self.env.editors,))], ["6000.3.7f1", "2022.3.62f3"])

    def test_the_folder_name_gives_the_version_when_the_plist_cant(self):
        app = fake_unity_editor(self.env.editors, "6000.0.1f1")
        (app / "Contents" / "Info.plist").write_text("not a plist")
        self.assertEqual([e.version for e in find_editors((self.env.editors,))], ["6000.0.1f1"])

    def test_the_hubs_custom_install_folder_is_searched(self):
        elsewhere = self.env.t.root / "Elsewhere"
        fake_unity_editor(elsewhere, "6000.3.7f1")
        self.env.hub_config.mkdir()
        (self.env.hub_config / "secondaryInstallPath.json").write_text(json.dumps(str(elsewhere)))
        found = find_editors((self.env.editors,), self.env.hub_config)
        self.assertEqual([e.version for e in found], ["6000.3.7f1"])

    def test_the_best_editor_is_the_newest_full_release(self):
        editors = [UnityEditor(v, Path("/x")) for v in ("6000.6.0b3", "6000.3.7f1", "2021.3.1f1")]
        self.assertEqual(best_editor(editors).version, "6000.3.7f1")

    def test_an_editor_older_than_the_package_needs_doesnt_count(self):
        self.assertEqual(MIN_UNITY, (2022, 3))
        self.assertIsNone(best_editor([UnityEditor("2021.3.1f1", Path("/x"))]))

    def test_a_project_folder_is_recognized_and_its_version_read(self):
        project = fake_unity_project(self.env.projects / "Game")
        self.assertIsNone(project_problem(project))
        self.assertEqual(project_version(project), "6000.3.7f1")

    def test_other_folders_are_not_projects(self):
        self.env.projects.mkdir()
        self.assertIn("isn't a Unity project", project_problem(self.env.projects))
        self.assertIn("doesn't exist", project_problem(self.env.projects / "Nope"))

    def test_a_new_project_never_lands_on_an_existing_folder(self):
        self.assertEqual(next_project_path(self.env.projects), self.env.projects / "GazeVoice")
        (self.env.projects / "GazeVoice").mkdir(parents=True)
        self.assertEqual(next_project_path(self.env.projects), self.env.projects / "GazeVoice2")


class UnityUnitTest(unittest.TestCase):
    def setUp(self):
        self.env = UnityEnv(self)

    def actions(self, state):
        return {a.id: a for a in state.actions}

    def test_nothing_installed_offers_setup_with_every_step_in_the_confirm(self):
        state = self.env.unit(Unity).check()
        self.assertEqual(state.health, Health.MISSING)
        confirm = self.actions(state)["setup"].confirm
        for words in ("Unity Hub", "Unity CLI", "Unity LTS", "GazeVoice package", "sign in", "several GB"):
            self.assertIn(words, confirm)

    def test_without_homebrew_it_says_how_to_get_unity(self):
        env = UnityEnv(self, which={})
        state = env.unit(Unity).check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertFalse(state.offers("setup"))
        self.assertIn("brew.sh", state.summary)

    def test_the_confirm_lists_only_whats_left(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        state = self.env.unit(Unity).check()
        self.assertEqual(state.health, Health.READY)
        confirm = self.actions(state)["setup"].confirm
        self.assertNotIn("Unity Hub", confirm)
        self.assertNotIn("several GB", confirm)
        self.assertIn("new project", confirm)

    def test_the_key_keeps_its_name_so_the_instructions_match(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.assertEqual(self.actions(self.env.unit(Unity).check())["setup"].label, "Set up Unity")

    def test_a_too_old_editor_is_not_enough(self):
        fake_unity_editor(self.env.editors, "2021.3.1f1")
        state = self.env.unit(Unity).check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("2022.3", state.summary)
        self.assertTrue(state.offers("setup"))

    def done(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        project = fake_unity_project(self.env.projects / "Game")
        self.env.choose(project)
        add_bridge(project)
        return project

    def test_when_everything_is_set_up_it_offers_to_open_the_project(self):
        self.done()
        state = self.env.unit(Unity).check()
        self.assertEqual(state.health, Health.READY)
        self.assertFalse(state.offers("setup"))
        self.assertEqual(self.actions(state)["launch"].label, "Open project")
        self.assertEqual(state.detail("Version"), "6000.3.7f1")

    def test_open_starts_the_projects_own_editor(self):
        project = self.done()
        app = self.env.editors / "6000.3.7f1" / "Unity.app"
        self.env.runner.add(["open", "-n", "-a", app, "--args", "-projectPath", project])
        self.env.unit(Unity).run_action("launch", make_job("unity", "launch"))
        self.assertIn(("open", "-n", "-a", str(app), "--args", "-projectPath", str(project)), self.env.runner.calls)

    def test_a_project_already_open_isnt_offered_again(self):
        project = self.done()
        self.env.running()
        (project / "Temp").mkdir()
        (project / "Temp" / "UnityLockfile").write_text("")
        self.assertFalse(self.env.unit(Unity).check().offers("launch"))

    def test_a_stale_lockfile_with_unity_closed_doesnt_block_opening(self):
        project = self.done()
        (project / "Temp").mkdir()
        (project / "Temp" / "UnityLockfile").write_text("")
        self.assertTrue(self.env.unit(Unity).check().offers("launch"))

    def test_an_out_of_date_package_is_a_setup_step(self):
        project = self.done()
        (project / "Packages" / "com.gazevoice.bridge" / "package.json").write_text("{}")
        state = self.env.unit(Unity).check()
        self.assertIn("GazeVoice package", self.actions(state)["setup"].confirm)

    def test_the_hub_can_be_opened_for_signing_in(self):
        self.env.hub.mkdir()
        self.env.runner.add(["open", "-a", self.env.hub])
        unit = self.env.unit(Unity)
        self.assertTrue(unit.check().offers("open_hub"))
        unit.run_action("open_hub", make_job("unity", "open_hub"))
        self.assertIn(("open", "-a", str(self.env.hub)), self.env.runner.calls)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.env = UnityEnv(self)
        self.runner = self.env.runner

    def setup_unity(self):
        self.runner.add(["open", "-a", self.env.hub])
        return self.env.unit(Unity).run_action("setup", make_job("unity", "setup"))

    def open_argv(self, project):
        return ("open", "-n", "-a", str(self.env.editors / "6000.3.7f1" / "Unity.app"),
                "--args", "-projectPath", str(project))

    def add_open(self, project):
        self.runner.add(self.open_argv(project))

    def steps(self):
        names = []
        for call in self.runner.calls:
            if call[:4] == ("brew", "install", "--cask", HUB_CASK):
                names.append("hub")
            elif call[:4] == ("brew", "install", "--cask", CLI_CASK):
                names.append("cli")
            elif call[:2] == (UNITY_CLI, "install"):
                names.append("editor")
            elif "-createProject" in call:
                names.append("project")
            elif call[:3] == ("open", "-n", "-a"):
                names.append("open")
            elif call[:2] == ("open", "-a"):
                names.append("hub_opened")
        return names

    def test_from_nothing_it_does_every_step_in_order(self):
        project = self.env.projects / "GazeVoice"
        self.add_open(project)
        result = self.setup_unity()
        self.assertEqual(self.steps(), ["hub", "hub_opened", "cli", "editor", "project", "open"])
        self.assertEqual(result["project"], str(project))
        self.assertTrue(bridge_current(project))

    def test_the_hub_opens_as_soon_as_its_installed_so_you_can_sign_in_during_the_download(self):
        job = make_job("unity", "setup")
        self.add_open(self.env.projects / "GazeVoice")
        self.runner.add(["open", "-a", self.env.hub])
        self.env.unit(Unity).run_action("setup", job)
        self.assertTrue(any("sign in" in line for line in job.lines))

    def test_an_already_installed_hub_isnt_opened(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.add_open(self.env.projects / "GazeVoice")
        self.setup_unity()
        self.assertNotIn("hub_opened", self.steps())

    def test_the_editor_is_the_latest_lts_for_apple_silicon(self):
        self.add_open(self.env.projects / "GazeVoice")
        self.setup_unity()
        self.assertIn((UNITY_CLI, "install", "lts", "-a", "arm64"), self.runner.calls)

    def test_the_project_is_made_by_unity_in_the_background(self):
        self.add_open(self.env.projects / "GazeVoice")
        self.setup_unity()
        create = next(c for c in self.runner.calls if "-createProject" in c)
        self.assertEqual(create[0], str(self.env.editors / "6000.3.7f1" / "Unity.app/Contents/MacOS/Unity"))
        self.assertIn("-batchmode", create)
        self.assertIn("-quit", create)

    def test_the_new_project_is_remembered(self):
        project = self.env.projects / "GazeVoice"
        self.add_open(project)
        self.setup_unity()
        self.assertEqual(self.env.ctx.settings.unity_project, str(project))
        self.assertEqual(load_settings(self.env.ctx.paths.settings_file).unity_project, str(project))

    def test_what_is_already_there_is_skipped(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.add_open(self.env.projects / "GazeVoice")
        self.setup_unity()
        self.assertEqual(self.steps(), ["project", "open"])

    def test_the_cli_is_skipped_when_an_editor_is_there(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.add_open(self.env.projects / "GazeVoice")
        self.setup_unity()
        self.assertNotIn("cli", self.steps())

    def test_a_project_you_picked_is_kept_and_gets_the_package(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        project = fake_unity_project(self.env.projects / "Mine")
        self.env.choose(project)
        self.add_open(project)
        self.setup_unity()
        self.assertEqual(self.steps(), ["open"])
        self.assertTrue(bridge_current(project))

    def test_a_finished_default_project_is_picked_up_again(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        project = fake_unity_project(self.env.projects / "GazeVoice")
        self.add_open(project)
        self.setup_unity()
        self.assertEqual(self.steps(), ["open"])
        self.assertEqual(self.env.ctx.settings.unity_project, str(project))

    def test_a_missing_license_stops_for_sign_in_and_opens_the_hub(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.runner.create_code = 1
        self.runner.create_log = "No valid Unity Editor license found. Please activate your license.\n"
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("Sign in", str(caught.exception))
        self.assertIn("Set up Unity again", str(caught.exception))
        self.assertEqual(self.steps(), ["project", "hub_opened"])
        self.assertEqual(self.env.ctx.settings.unity_project, "")

    def test_a_failed_create_leaves_no_half_made_folder(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.runner.create_code = 1
        self.runner.create_log = "No valid Unity Editor license found.\n"
        with self.assertRaises(JobError):
            self.setup_unity()
        self.assertFalse((self.env.projects / "GazeVoice").exists())

    def test_pressing_it_again_after_signing_in_finishes_the_job(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.runner.create_code = 1
        self.runner.create_log = "No valid Unity Editor license found.\n"
        with self.assertRaises(JobError):
            self.setup_unity()
        self.runner.create_code = 0
        project = self.env.projects / "GazeVoice"
        self.add_open(project)
        result = self.setup_unity()
        self.assertEqual(result["project"], str(project))

    def test_any_other_create_failure_shows_unitys_own_words(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.runner.create_code = 1
        self.runner.create_log = "Starting...\nCompiler is broken today\n"
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("Compiler is broken today", str(caught.exception))
        self.assertNotIn("hub_opened", self.steps())

    def test_a_cli_that_wants_a_sign_in_says_how(self):
        self.env.hub.mkdir()
        self.runner.install_code = 3
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("unity auth login", str(caught.exception))

    def test_a_failed_download_stops_with_its_exit_code(self):
        self.env.hub.mkdir()
        self.runner.install_code = 7
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("exit 7", str(caught.exception))
        self.assertNotIn("project", self.steps())

    def test_an_install_that_cant_be_found_afterwards_is_an_error(self):
        self.env.hub.mkdir()
        self.runner.install_leaves_editor = False
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("can't find it", str(caught.exception))

    def test_a_picked_project_on_another_unity_version_names_it(self):
        self.env.hub.mkdir()
        fake_unity_editor(self.env.editors)
        self.env.choose(fake_unity_project(self.env.projects / "Old", version="2022.3.62f3"))
        with self.assertRaises(JobError) as caught:
            self.setup_unity()
        self.assertIn("2022.3.62f3", str(caught.exception))


class ProjectUnitTest(unittest.TestCase):
    def setUp(self):
        self.env = UnityEnv(self)
        self.unit = self.env.unit(UnityProject)

    def test_no_project_offers_new_and_choose(self):
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("create"))
        self.assertTrue(state.offers("choose"))

    def test_new_needs_unity_first(self):
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("create", make_job("unity_project", "create"))
        self.assertIn("Set up Unity", str(caught.exception))

    def test_new_makes_a_project_and_uses_it(self):
        fake_unity_editor(self.env.editors)
        (self.env.projects / "GazeVoice").mkdir(parents=True)
        result = self.unit.run_action("create", make_job("unity_project", "create"))
        project = self.env.projects / "GazeVoice2"
        self.assertEqual(result["project"], str(project))
        self.assertEqual(self.env.ctx.settings.unity_project, str(project))

    def osascript(self, **kwargs):
        self.env.runner.add(["osascript", "-e", 'POSIX path of (choose folder with prompt "Pick your Unity project")'],
                            **kwargs)

    def test_choose_uses_the_folder_you_picked(self):
        project = fake_unity_project(self.env.projects / "Game")
        self.osascript(stdout=f"{project}/\n")
        self.unit.run_action("choose", make_job("unity_project", "choose"))
        self.assertEqual(self.env.ctx.settings.unity_project, str(project))

    def test_cancelling_the_picker_changes_nothing(self):
        self.osascript(returncode=1, stderr="execution error: User canceled. (-128)\n")
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("choose", make_job("unity_project", "choose"))
        self.assertIn("No folder was picked", str(caught.exception))
        self.assertEqual(self.env.ctx.settings.unity_project, "")

    def test_picking_a_folder_that_isnt_a_project_is_refused(self):
        self.env.projects.mkdir()
        self.osascript(stdout=f"{self.env.projects}/\n")
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("choose", make_job("unity_project", "choose"))
        self.assertIn("isn't a Unity project", str(caught.exception))
        self.assertEqual(self.env.ctx.settings.unity_project, "")

    def test_a_good_project_is_ready(self):
        fake_unity_editor(self.env.editors)
        project = fake_unity_project(self.env.projects / "Game")
        self.env.choose(project)
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertEqual(state.detail("Unity"), "6000.3.7f1")

    def test_a_project_whose_unity_isnt_installed_names_the_version(self):
        self.env.choose(fake_unity_project(self.env.projects / "Game", version="2022.3.62f3"))
        state = self.unit.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("2022.3.62f3", state.summary)

    def test_a_project_that_moved_says_so_and_offers_another(self):
        self.env.choose(self.env.projects / "Gone")
        state = self.unit.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("doesn't exist", state.summary)
        self.assertTrue(state.offers("choose"))


if __name__ == "__main__":
    unittest.main()
