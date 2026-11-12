"""Checks on the C# package that run without Unity: a package Unity can't compile breaks the whole project."""

import json
import re
import unittest
from pathlib import Path

from gazevoice.components.unity_bridge import BRIDGE_PORT
from gazevoice.unity_install import PACKAGE_DIR, PACKAGE_NAME
from gazevoice.unity_mcp import LOCAL_TOOLS, TOOLS
from gazevoice.voice.commands import UNITY_EDITOR_PROMPT, UNITY_REFINER_PROMPT

SOURCE = PACKAGE_DIR / "Editor" / "GazeVoiceBridge.cs"
ASMDEF = PACKAGE_DIR / "Editor" / "GazeVoice.Bridge.Editor.asmdef"
GUID = re.compile(r"^guid: ([0-9a-f]{32})$", re.MULTILINE)


def csharp_without_strings() -> str:
    """The source with comments and string and char literals blanked, so only real code is counted."""
    pattern = r'//[^\n]*|/\*.*?\*/|@"(?:[^"]|"")*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])\''
    return re.sub(pattern, " ", SOURCE.read_text(), flags=re.DOTALL)


class PackageTest(unittest.TestCase):
    def test_the_scripts_are_in_an_assembly_definition_or_unity_wont_compile_them(self):
        definition = json.loads(ASMDEF.read_text())
        self.assertEqual(definition["includePlatforms"], ["Editor"])

    def test_the_assembly_can_see_newtonsoft(self):
        definition = json.loads(ASMDEF.read_text())
        self.assertTrue(definition["overrideReferences"])
        self.assertIn("Newtonsoft.Json.dll", definition["precompiledReferences"])
        self.assertIn("com.unity.nuget.newtonsoft-json", json.loads((PACKAGE_DIR / "package.json").read_text())["dependencies"])

    def test_every_file_has_a_meta_with_its_own_guid(self):
        files = [p for p in PACKAGE_DIR.rglob("*") if p.suffix != ".meta" and p.name != ".DS_Store"]
        guids = []
        for path in files:
            meta = path.with_name(path.name + ".meta")
            self.assertTrue(meta.is_file(), f"{path.name} has no .meta")
            guids.append(GUID.search(meta.read_text()).group(1))
        self.assertEqual(len(guids), len(set(guids)))

    def test_python_and_csharp_agree_on_the_version_and_port(self):
        package = json.loads((PACKAGE_DIR / "package.json").read_text())
        self.assertEqual(package["name"], PACKAGE_NAME)
        source = SOURCE.read_text()
        self.assertIn(f'const string Version = "{package["version"]}"', source)
        self.assertIn(f"const int Port = {BRIDGE_PORT}", source)

    def test_every_tool_is_handled_in_the_bridge(self):
        """Except the ML-Agents tools, which the MCP server answers itself and Unity never sees."""
        source = SOURCE.read_text()
        local = {tool["name"] for tool in LOCAL_TOOLS}
        self.assertTrue(local)
        for tool in TOOLS:
            if tool["name"] not in local:
                self.assertIn(f'"{tool["name"]}"', source)

    def test_the_ml_agents_tools_are_not_in_the_bridge(self):
        source = SOURCE.read_text()
        for tool in LOCAL_TOOLS:
            self.assertNotIn(f'"{tool["name"]}"', source)

    def test_the_brackets_balance(self):
        code = csharp_without_strings()
        for opening, closing in ("{}", "()", "[]"):
            self.assertEqual(code.count(opening), code.count(closing), opening + closing)

    def test_names_that_clash_in_unity_are_spelled_out(self):
        """System.Diagnostics.Debug, System.Reflection.Assembly and UnityEditor.Compilation.Assembly all clash."""
        source = SOURCE.read_text()
        for clashing in ("using System.Diagnostics;", "using System.Reflection;", "using UnityEditor.Compilation;",
                         "using UnityEditor.PackageManager;"):
            self.assertNotIn(clashing, source)

    def test_there_is_a_hold_to_talk_key_and_the_bridge_reports_it(self):
        source = SOURCE.read_text()
        self.assertIn("ClutchShortcut", source)
        self.assertNotIn('key = "Talk button"', source)

    def test_losing_focus_cancels_a_hold_instead_of_sending_half_a_command(self):
        """Unity ends every held shortcut when it loses focus, so the key's own release handler sees that too."""
        self.assertIn('PushTalkEvent(EditorApplication.isFocused ? "up" : "cancel")', SOURCE.read_text())

    def test_each_editor_window_has_a_file_named_after_it(self):
        """Unity finds an EditorWindow by its script's file name, which matters across script reloads."""
        for path in PACKAGE_DIR.rglob("*.cs"):
            for name in re.findall(r"class (\w+)\s*:\s*EditorWindow", path.read_text()):
                self.assertEqual(path.stem, name)

    def test_calls_removed_in_newer_unity_only_compile_on_older_ones(self):
        """Unity 6000.5 turned these into compile errors, which blocks the whole project."""
        source = SOURCE.read_text()
        newer, older = source.split("#else", 1)[0], source.split("#else", 1)[1].split("#endif", 1)[0]
        self.assertEqual(source.count("#if UNITY_6000_5_OR_NEWER"), 1)
        for removed in ("GetInstanceID", "InstanceIDToObject", "Assembly.Load("):
            self.assertNotIn(removed, newer)
            self.assertNotIn(removed, source.split("#endif", 1)[1])
            self.assertIn(removed, older)

    def test_references_come_from_unitys_own_compile_list(self):
        source = SOURCE.read_text()
        self.assertIn("CompilationPipeline.GetAssemblies", source)
        self.assertNotIn("GetAssemblies()", source)
        self.assertNotIn(".Location", source)

    def test_it_never_listens_in_batch_mode(self):
        self.assertIn("Application.isBatchMode", SOURCE.read_text())


class PromptTest(unittest.TestCase):
    def test_the_editor_prompt_covers_the_tools_and_the_rules(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        for words in ("execute_csharp", "write_script", "capture_scene_view", "read_console", "add_package",
                      "Undo.RegisterCreatedObjectUndo", "Undo.DestroyObjectImmediate", "compile error",
                      "Assets/GazeVoice/", "Play mode", "Blender"):
            self.assertIn(words, prompt)

    def test_the_editor_prompt_has_an_ml_agents_section_for_training_by_voice(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("\nML-Agents\n", prompt)
        for words in ("mlagents_status", "mlagents_train", "mlagents_stop", "mlagents_import_model",
                      "com.unity.ml-agents", "Unity.MLAgents.Agent", "Heuristic", "DecisionRequester", "Behavior Name",
                      "trainer_type: ppo", "max_steps", "200000", "EditorApplication.isPlaying = true;",
                      "EditorApplication.isPlaying = false;", "AssetDatabase.Refresh();",
                      "Unity.MLAgents.Policies.BehaviorParameters", 'Undo.RecordObject(bp, "Use trained model");',
                      "bp.Model = model;"):
            self.assertIn(words, prompt)

    def test_training_that_cannot_start_changes_nothing_and_tells_the_user_what_to_press(self):
        """Nothing installs by voice: only the Install button does."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("Couldn't start training: press Install on the ML-Agents unit in the dashboard first.", prompt)
        self.assertNotIn("uv pip", prompt)

    def test_a_trained_model_is_assigned_as_the_inference_engine_model_asset(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("var model = AssetDatabase.LoadAssetAtPath<Unity.InferenceEngine.ModelAsset>(path);", prompt)

    def test_no_scene_edits_while_a_training_line_says_unity_is_in_play_mode(self):
        """Play mode throws scene changes away. What tells the editor is the line each command carries, not its memory."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn('- Training: only while a training run is going', prompt)  # in the list of what each message has
        self.assertIn('When a message\'s "Training:" line says Unity is in Play mode, don\'t change the scene', prompt)
        self.assertIn("Couldn't: training is running. Stop it first.", prompt)
        self.assertIn("throws", prompt)
        self.assertIn("status, stop or using the trained model", prompt)  # those commands are still fine
        self.assertNotIn("If you started training earlier", prompt)  # it must not depend on remembering
        self.assertNotIn("before a scene change", prompt)

    def test_the_line_the_prompt_describes_is_the_line_the_wiring_sends(self):
        from gazevoice.voice.commands import PLAY_MODE_SAYING, editor_message

        line = editor_message("x", "x", None, "Training: run1 is running, so Unity is in Play mode for it.").splitlines()[-1]
        self.assertTrue(line.startswith("Training: "))
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn(PLAY_MODE_SAYING, prompt)  # the words the running line carries, and the rule keys on
        self.assertIn("is waiting for Unity to press Play", prompt)  # the words the waiting line carries

    def test_scene_edits_are_fine_while_the_run_is_only_waiting_for_play(self):
        """Unity is in Edit mode then, so an edit survives. Only the line that says Play mode refuses."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn('If the line says the run is waiting for Unity to press Play, Unity is in Edit mode: '
                      'make scene changes as usual', prompt)
        self.assertNotIn('When a message has a "Training:" line, don\'t change the scene', prompt)

    def test_unity_leaves_play_mode_before_training_starts_because_the_trainer_hears_play_only_once(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        check = section.index("`EditorApplication.isPlaying` with execute_csharp")
        self.assertIn("If it is true, set `EditorApplication.isPlaying = false;` first", section[check:])
        self.assertIn("because Play must start after the trainer is listening", section[check:])
        self.assertLess(check, section.index("call mlagents_train with a new run id"))
        self.assertLess(check, section.index("`EditorApplication.isPlaying = true;`"))

    def test_play_is_pressed_only_when_asked_and_only_once_the_trainer_is_listening(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        asked = next(line for line in section.splitlines() if line.startswith("- If the command asks you to press Play"))
        for words in ('Press Play only when the state is "waiting" and `listening` is true: then enter Play mode at once',
                      "If `listening` is false, call mlagents_status, up to a few times, until `listening` is true, then enter Play mode",
                      "If it never becomes true, reply that ML-Agents is still starting, and that they can press Play "
                      "themselves once the dashboard says it is waiting for Unity",
                      "Training gives up if Play isn't pressed within 2 minutes.",
                      "Reply that training started and is shown on the dashboard."):
            self.assertIn(words, asked)
        self.assertNotIn("Then enter Play mode at once", section)
        self.assertLess(section.index("Press Play only when"), section.index("`EditorApplication.isPlaying = true;`"))

    DEFINE_CODE = """  ```
  var target = UnityEditor.Build.NamedBuildTarget.FromBuildTargetGroup(EditorUserBuildSettings.selectedBuildTargetGroup);
  var defines = PlayerSettings.GetScriptingDefineSymbols(target);
  if (!defines.Split(';').Contains("APP_UI_EDITOR_ONLY"))
      PlayerSettings.SetScriptingDefineSymbols(target, string.IsNullOrEmpty(defines) ? "APP_UI_EDITOR_ONLY" : defines + ";APP_UI_EDITOR_ONLY");
  return PlayerSettings.GetScriptingDefineSymbols(target);
  ```"""

    def test_the_scripting_define_is_set_and_compiled_before_training_starts(self):
        """The first Play adds APP_UI_EDITOR_ONLY, Unity recompiles about 8 s in, and that breaks the trainer's connection."""
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        self.assertIn(self.DEFINE_CODE, section)  # the verified C#, word for word
        self.assertIn("execute_csharp", section[section.index(self.DEFINE_CODE) - 400:section.index(self.DEFINE_CODE)])
        self.assertIn("ML-Agents' UI package adds this on the first Play, and the recompile would break training.",
                      section)
        after = section[section.index(self.DEFINE_CODE) + len(self.DEFINE_CODE):]
        self.assertIn("If it added the define, Unity recompiles: wait with get_compile_status until compiling is done "
                      "before mlagents_train and Play.", after.split("\n- ", 1)[0])
        # the order: out of Play mode, then the define, then the run
        check = section.index("`EditorApplication.isPlaying` with execute_csharp")
        self.assertLess(check, section.index(self.DEFINE_CODE))
        self.assertLess(section.index(self.DEFINE_CODE), section.index("call mlagents_train with a new run id"))
        self.assertLess(section.index(self.DEFINE_CODE), section.index("`EditorApplication.isPlaying = true;`"))

    def test_no_scripts_or_packages_change_while_unity_is_in_play_mode_for_training(self):
        """A recompile or a package change reloads Unity in Play mode and breaks the trainer's connection too."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn('says Unity is in Play mode, don\'t change the scene, scripts or packages (write_script, add_package)',
                      prompt)
        self.assertIn("Couldn't: training is running. Stop it first.", prompt)

    def test_a_run_that_lost_unity_leaves_play_mode_and_tells_the_user_unless_asked_to_press_play(self):
        from gazevoice.mlagents import LOST_CONNECTION

        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        line = next(line for line in section.splitlines() if LOST_CONNECTION in line)
        self.assertTrue(line.startswith("- If mlagents_status says"))
        off, on = "`EditorApplication.isPlaying = false;`", "`EditorApplication.isPlaying = true;`"
        words = ('reply that Unity lost its connection: "press Play again once the dashboard says it is waiting for '
                 'Unity".')
        self.assertIn(words, line)
        self.assertIn("Re-press Play only if the command asked you to press Play", line)
        # out of Play mode first, then the reply, and only then (when asked) in again once the trainer listens
        self.assertLess(line.index(off), line.index(words))
        self.assertLess(line.index(words), line.index("Re-press Play only if"))
        self.assertLess(line.index("Re-press Play only if"), line.index("`listening` is true"))
        self.assertLess(line.index("`listening` is true"), line.index(on))
        self.assertEqual(line.count(on), 1)  # setting isPlaying = true is no longer what it does by default
        reply = "Unity lost its connection: press Play again once the dashboard says it is waiting for Unity."
        self.assertLessEqual(len(reply.split()), 20)  # the reply this line asks for fits the cap

    def test_training_runs_from_the_dashboard_and_the_user_is_never_sent_to_a_terminal(self):
        """mlagents-learn lives in GazeVoice's own environment, not on the user's PATH, so their terminal can't run it."""
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1].split("\nLimits\n", 1)[0]
        for words in ("Training runs from the dashboard, never from a terminal: mlagents-learn lives in GazeVoice's own "
                      "environment, not on the user's PATH.",
                      "So a request to set up, start or run the terminal side, the Python side, the trainer or "
                      "mlagents-learn means: start a run with mlagents_train.",
                      "Starting the trainer isn't installing anything.",
                      "Never tell them to open a terminal or run a command."):
            self.assertIn(words, section)
        line = next(line for line in section.splitlines() if line.startswith("- Training runs from the dashboard"))
        self.assertIn("Never tell them to open a terminal or run a command.", line)
        self.assertLess(section.index(line), section.index("- To train (\"train it\")"))  # near the top
        self.assertLess(section.index(line), section.index("call mlagents_train with a new run id"))
        self.assertIn("Never install anything yourself.", section)  # still kept

    def test_the_user_presses_play_by_default_and_the_editor_only_says_when_the_trainer_is_ready(self):
        """"Set up the terminal" means: run the trainer, then tell them to start the scene. They press Play."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        section = prompt.split("\nML-Agents\n", 1)[1].split("\nLimits\n", 1)[0]
        line = next(line for line in section.splitlines() if line.startswith("- After mlagents_train:"))
        for words in ('at any point, if the state is "failed", reply with its message and don\'t press Play.',
                      "Otherwise don't press Play yourself, unless the command asks you to press Play (see the next line)",
                      'when `listening` is true, reply "The trainer is ready: press Play in Unity within 2 minutes to '
                      'start training."',
                      'when it isn\'t yet, reply "ML-Agents is starting: press Play once the dashboard says it is '
                      'waiting for Unity."'):
            self.assertIn(words, line)
        self.assertNotIn("EditorApplication.isPlaying = true;", line)  # a plain "train it" never presses Play
        self.assertNotIn("The trainer is listening: press Play in Unity within 2 minutes.", prompt)  # the old default
        self.assertNotIn("If they say they'll press Play themselves", prompt)  # not a special case any more
        asked = next(line for line in section.splitlines() if line.startswith("- If the command asks you to press Play"))
        self.assertGreater(section.index(asked), section.index(line))
        self.assertIn('as in "train it and press Play" or "start training and press Play"', asked)
        self.assertNotIn("start it for me", prompt)  # the prompt writer now writes "start training and press Play"
        # the first time Play is pressed in the section is in the line for "press Play too"
        self.assertGreater(section.index("`EditorApplication.isPlaying = true;`"), section.index(asked))

    def test_an_existing_trainer_config_in_the_project_is_read_and_passed_on(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1].split("\nLimits\n", 1)[0]
        line = next(line for line in section.splitlines() if line.startswith("- Then call mlagents_train"))
        existing = ("If the project already has a trainer config for the behavior (a .yaml file under `Assets/` or in a "
                    "top-level `config/` folder, such as one from ML-Agents' examples or one you saved earlier; never "
                    "look in `Library/` and never scan the whole project, because execute_csharp runs on Unity's main "
                    "thread), read it with execute_csharp (File.ReadAllText) and pass its text as config_yaml, after "
                    "checking its behavior names match the Behavior Names in the scene. Otherwise write the "
                    "config_yaml as below.")
        self.assertIn(existing, line)
        written = "Unless they asked otherwise, pass config_yaml with one entry per Behavior Name, in double quotes"
        self.assertIn(written, line)
        self.assertLess(line.index(existing), line.index(written))
        self.assertLess(line.index("a new run id every time"), line.index(existing))

    def test_even_ml_agents_replies_are_one_plain_short_sentence_but_a_failed_runs_message_is_given_whole(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1].split("\nLimits\n", 1)[0]
        line = next(line for line in section.splitlines() if line.startswith("- Even for ML-Agents"))
        self.assertIn("Even for ML-Agents, reply with one plain sentence of at most 20 words, with no markdown.", line)
        self.assertIn("The one exception to the 20-word limit is a failed run's message, which you give as it is.", line)
        self.assertIn('if the state is "failed", reply with its message', section)  # the rule the exception is about
        last = UNITY_EDITOR_PROMPT.read_text().splitlines()[-1]
        self.assertTrue(last.startswith("Reply with one plain sentence of at most 20 words saying what changed"))
        self.assertIn("(a failed training run's message is the one exception)", last)

    def test_a_failed_run_is_reported_and_play_is_not_pressed(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        rule = 'at any point, if the state is "failed", reply with its message and don\'t press Play.'
        self.assertIn(rule, section)
        self.assertLess(section.index(rule), section.index("Press Play only when"))

    def test_the_reload_rule_is_kept(self):
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        self.assertIn("If it says Unity reloaded and the call may not have finished, call mlagents_status", section)

    def test_the_trained_model_comes_from_the_newest_run_that_has_one_when_no_run_is_named(self):
        """After a restart the status is idle with no run id, so the list of runs is where the id comes from."""
        section = UNITY_EDITOR_PROMPT.read_text().split("\nML-Agents\n", 1)[1]
        line = next(line for line in section.splitlines() if line.startswith('- "Use the trained model"'))
        self.assertIn("the newest run in `runs`", line)
        self.assertIn("unless they name another", line)
        self.assertNotIn("(from mlagents_status, unless they name another)", line)

    def test_a_check_that_hasnt_finished_is_not_a_reason_to_press_install(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("still checking whether ML-Agents is installed. Try again in a moment.", prompt)
        self.assertIn("don't mention Install", prompt)

    def test_a_play_press_cut_off_by_a_reload_is_checked_and_not_assumed(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("Unity reloaded", prompt)
        self.assertIn('"training" means Play started', prompt)
        self.assertIn('"waiting" means set isPlaying again once', prompt)

    def test_a_stop_that_is_still_saving_is_not_used_yet(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn('replies "stopping"', prompt)
        self.assertIn("still being saved", prompt)

    def test_the_model_is_assigned_only_out_of_play_mode(self):
        self.assertIn("out of Play mode", UNITY_EDITOR_PROMPT.read_text())

    def test_every_new_training_run_gets_a_new_run_id(self):
        """A run that timed out or was interrupted still leaves its folder, and the same name would be refused."""
        prompt = UNITY_EDITOR_PROMPT.read_text()
        self.assertIn("new run id", prompt)
        self.assertIn("date and time", prompt)

    def test_play_mode_is_for_a_command_that_asks_for_it_and_training_does_not_count_as_asking(self):
        limits = UNITY_EDITOR_PROMPT.read_text().split("\nLimits\n", 1)[1]
        line = next(line for line in limits.splitlines() if "Play mode" in line)
        self.assertIn("Never enter Play mode", line)
        self.assertIn("unless the command asks for exactly that.", line)
        self.assertIn("Enter Play mode only when the command asks for it", line)
        self.assertNotIn("or to train ML-Agents", line)  # it read as permission to press Play on "train it"
        self.assertIn("A request to train ML-Agents still allows the `APP_UI_EDITOR_ONLY` scripting define, which is a "
                      "Project Settings change.", line)

    def test_the_prompt_writer_knows_unity_words_and_keeps_its_reply_format(self):
        prompt = UNITY_REFINER_PROMPT.read_text()
        for words in ("GameObject", "Rigidbody", "MonoBehaviour", "ProBuilder", '"kind"', '"title"', "80"):
            self.assertIn(words, prompt)

    def test_the_prompt_writer_passes_training_commands_on_instead_of_calling_them_a_stop(self):
        prompt = UNITY_REFINER_PROMPT.read_text()
        for words in ("train the agent", "how is training going", "stop training", "use the trained model",
                      'Stop training" is kind "command", not "stop"', "Stopping, cancelling or aborting training",
                      '"cancel the training"', '"abort the training"'):
            self.assertIn(words, prompt)

    def test_the_prompt_writer_knows_a_run_is_going_from_the_notice_and_reads_the_questions_as_training(self):
        prompt = UNITY_REFINER_PROMPT.read_text()
        self.assertIn('"notice"', prompt)  # the key the message carries it in
        rule = next(line for line in prompt.splitlines() if line.startswith("- Training agents with ML-Agents"))
        self.assertIn("When there is a Training line", rule)
        for words in ('"how\'s it going"', '"how is it doing"', '"stop it"', '"is it done"', "about the training",
                      'kind "command"'):
            self.assertIn(words, rule)
        self.assertLess(prompt.index(rule), prompt.index('- "undo", "redo", "stop", "cancel"'))

    def test_the_cancel_line_further_down_cannot_override_the_training_rule(self):
        prompt = UNITY_REFINER_PROMPT.read_text()
        line = next(line for line in prompt.splitlines() if line.startswith('- "undo", "redo", "stop", "cancel"'))
        self.assertIn("unless they are about training", line)
        self.assertLess(prompt.index("Stopping, cancelling or aborting training"), prompt.index(line))


    def test_the_prompt_writer_turns_terminal_and_trainer_requests_into_start_training(self):
        """The user says "set up the terminal part"; the dashboard runs the trainer, so it is the same as "train it"."""
        prompt = UNITY_REFINER_PROMPT.read_text()
        rule = next(line for line in prompt.splitlines() if line.startswith("- Training agents with ML-Agents"))
        for words in ("Requests to train, or to set up, start or run the training terminal, the Python side, the "
                      "trainer or mlagents-learn, are written as \"start training\" (kind \"command\").",
                      "Add \"and press Play\" only if they asked for Play to be pressed: \"press Play\", \"start the "
                      "scene\", \"play it\" and \"run it for me\", said about training, become \"start training and "
                      "press Play\"."):
            self.assertIn(words, rule)
        self.assertNotIn('only if they asked for that, like "start training and press Play"', prompt)  # the old wording
        self.assertNotIn("I'll press Play myself", prompt)  # pressing Play is theirs by default now
        self.assertLess(prompt.index(rule), prompt.index('- "undo", "redo", "stop", "cancel"'))


class MLAgentsDocsTest(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def readme(self):
        return (self.ROOT / "README.md").read_text()

    def section(self):
        text = self.readme()
        return text[text.index("## Train agents with ML-Agents"):text.index("## Use gaze")]

    def test_the_download_and_disk_sizes_are_the_measured_ones(self):
        section = self.section()
        self.assertIn("about 1.2 GB", section)
        self.assertIn("about 600 MB", section)
        self.assertNotIn("about 1 GB", self.readme())
        self.assertIn("1.2 GB", (self.ROOT / "PRODUCT.md").read_text())
        self.assertNotIn("about 1 GB", (self.ROOT / "PRODUCT.md").read_text())

    def test_it_doesnt_claim_that_nothing_is_ever_sent(self):
        section = self.section()
        self.assertNotIn("sends nothing anywhere", section)
        self.assertIn("GazeVoice and mlagents-learn send nothing", section)
        self.assertIn("Editor analytics", section)
        self.assertIn("anonymised training settings", section)

    def test_it_says_what_happens_on_a_normal_stop_and_on_a_crash(self):
        section = self.section()
        self.assertIn("Ctrl+C or closing the Terminal", section)
        self.assertIn("saves its model", section)
        self.assertIn("force-quit", section)
        self.assertIn("Activity Monitor", section)

    def test_it_says_a_run_stopped_before_play_saves_nothing(self):
        self.assertIn("Stopping a run before Play was pressed saves nothing", self.section())

    def test_train_the_agent_starts_the_trainer_and_the_user_presses_play(self):
        section = self.section()
        happens = section[section.index("**What happens.**"):section.index("**Watch it run.**")]
        self.assertIn('"Train the agent" starts `mlagents-learn` and waits until it is listening for Unity', happens)
        self.assertIn("The dashboard says when it is ready, and you press Play in Unity.", happens)
        self.assertIn('Or say "train it and press Play" and the editor presses Play for you once it is ready.', happens)
        self.assertNotIn("puts Unity into Play mode", section)  # it used to
        self.assertNotIn("which is what training waits for", section)
        self.assertIn("Once you press Play, Unity is in Play mode for training", section)

    def test_the_status_line_at_the_top_mentions_it(self):
        text = self.readme()
        status = text[text.index("**Status:**"):text.index("See [the design]")]
        self.assertIn("ML-Agents", status)


class DashboardCopyTest(unittest.TestCase):
    def test_the_page_doesnt_name_one_app_where_the_host_can_change(self):
        page = (Path(__file__).resolve().parents[1] / "gazevoice" / "server" / "static" / "index.html").read_text()
        self.assertNotIn("talk to Blender", page)


if __name__ == "__main__":
    unittest.main()
