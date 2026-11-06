"""Checks on the C# package that run without Unity: a package Unity can't compile breaks the whole project."""

import json
import re
import unittest
from pathlib import Path

from gazevoice.components.unity_bridge import BRIDGE_PORT
from gazevoice.unity_install import PACKAGE_DIR, PACKAGE_NAME
from gazevoice.unity_mcp import TOOLS
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
        source = SOURCE.read_text()
        for tool in TOOLS:
            self.assertIn(f'"{tool["name"]}"', source)

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

    def test_it_never_listens_in_batch_mode(self):
        self.assertIn("Application.isBatchMode", SOURCE.read_text())


class PromptTest(unittest.TestCase):
    def test_the_editor_prompt_covers_the_tools_and_the_rules(self):
        prompt = UNITY_EDITOR_PROMPT.read_text()
        for words in ("execute_csharp", "write_script", "capture_scene_view", "read_console", "add_package",
                      "Undo.RegisterCreatedObjectUndo", "Undo.DestroyObjectImmediate", "compile error",
                      "Assets/GazeVoice/", "Play mode", "Blender"):
            self.assertIn(words, prompt)

    def test_the_prompt_writer_knows_unity_words_and_keeps_its_reply_format(self):
        prompt = UNITY_REFINER_PROMPT.read_text()
        for words in ("GameObject", "Rigidbody", "MonoBehaviour", "ProBuilder", '"kind"', '"title"', "80"):
            self.assertIn(words, prompt)



class DashboardCopyTest(unittest.TestCase):
    def test_the_page_doesnt_name_one_app_where_the_host_can_change(self):
        page = (Path(__file__).resolve().parents[1] / "gazevoice" / "server" / "static" / "index.html").read_text()
        self.assertNotIn("talk to Blender", page)


if __name__ == "__main__":
    unittest.main()
