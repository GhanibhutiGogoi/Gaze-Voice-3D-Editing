import json
import unittest

from gazevoice.voice.commands import (
    EDITOR_PROMPT,
    REFINER_PROMPT,
    describe_focus,
    editor_message,
    meta_command,
    parse_refined,
    refiner_message,
    vocabulary_prompt,
)


class MetaCommandTest(unittest.TestCase):
    def test_recognized(self):
        cases = {
            "Undo.": "undo", "uh undo that": "undo", "Take that back!": "undo", "undo it please": "undo",
            "redo": "redo", "Redo that.": "redo",
            "Stop": "stop", "never mind": "stop", "Cancel that.": "stop", "nevermind": "stop",
        }
        for spoken, kind in cases.items():
            self.assertEqual(meta_command(spoken), kind, spoken)

    def test_how_people_actually_say_it(self):
        cases = {
            "No, stop!": "stop", "Stop, stop!": "stop", "Wait, stop.": "stop", "Stop now.": "stop",
            "Stop right there.": "stop", "Hold on, stop it.": "stop", "Wait!": "stop", "Hold on.": "stop",
            "Stop stop stop": "stop", "Okay cancel that please": "stop", "Hey Blender, stop.": "stop",
            "No, undo that.": "undo", "Undo, undo.": "undo", "Oh no, undo it.": "undo", "Undo that now.": "undo",
            "Wait, redo that.": "redo",
        }
        for spoken, kind in cases.items():
            self.assertEqual(meta_command(spoken), kind, spoken)

    def test_real_edits_are_not_meta(self):
        for spoken in ("undo the bevel on the left leg", "stop the animation at frame ten", "make it bigger", "",
                       "no wait make it blue", "don't stop", "wait for it", "no", "stop the playback now"):
            self.assertIsNone(meta_command(spoken), spoken)


class RefinerTest(unittest.TestCase):
    def test_message_is_json_with_context(self):
        payload = json.loads(refiner_message("uh make this taller", {"active": "Chair"}, ["Chair"], ["Add a cube."]))
        self.assertEqual(payload["heard"], "uh make this taller")
        self.assertEqual(payload["scene_objects"], ["Chair"])
        self.assertEqual(payload["previous_commands"], ["Add a cube."])

    def test_parse_plain_json(self):
        refined = parse_refined('{"kind": "command", "text": "Make this twice as tall."}', "heard")
        self.assertEqual((refined.kind, refined.text, refined.fallback), ("command", "Make this twice as tall.", False))

    def test_a_build_request_comes_back_as_a_title_and_a_detailed_prompt(self):
        brief = ("Model a bird with realistic proportions: a teardrop body, a round head, a short pointed beak, "
                 "two eyes, wings with layered feathers, a fanned tail and thin legs. Use smooth shading.")
        refined = parse_refined(json.dumps({"kind": "command", "title": "Make a bird model.", "text": brief}), "make a bird")
        self.assertEqual((refined.kind, refined.title, refined.text, refined.fallback),
                         ("command", "Make a bird model.", brief, False))

    def test_without_a_title_the_instruction_is_its_own_title(self):
        refined = parse_refined('{"kind": "command", "text": "Play the animation."}', "play the animation please")
        self.assertEqual((refined.title, refined.text), ("Play the animation.", "Play the animation."))
        long = parse_refined(json.dumps({"kind": "command", "text": "Add a chair. " + "Give it detail. " * 20}), "x")
        self.assertEqual(long.title, "Add a chair.")

    def test_parse_json_in_a_fence_or_after_text(self):
        for reply in ('```json\n{"kind":"undo","text":""}\n```', 'Sure! {"kind": "undo", "text": ""}'):
            self.assertEqual(parse_refined(reply, "undo that").kind, "undo")

    def test_unparseable_falls_back_to_what_was_heard(self):
        refined = parse_refined("I think you want it taller", "make it taller")
        self.assertEqual((refined.kind, refined.text, refined.fallback), ("command", "make it taller", True))
        self.assertEqual(refined.title, "make it taller")

    def test_empty_command_text_falls_back(self):
        refined = parse_refined('{"kind": "command", "text": ""}', "bevel the edges")
        self.assertEqual((refined.text, refined.fallback), ("bevel the edges", True))

    def test_unknown_kind_falls_back(self):
        self.assertTrue(parse_refined('{"kind": "dance", "text": "x"}', "heard").fallback)

    def test_ignore(self):
        self.assertEqual(parse_refined('{"kind": "ignore", "text": ""}', "what time is it").kind, "ignore")


class EditorMessageTest(unittest.TestCase):
    def test_includes_command_heard_and_focus(self):
        focus = {"active": "Chair", "selected": ["Chair", "Leg.001"], "mode": "OBJECT"}
        message = editor_message("Make this twice as tall.", "uh make this like twice as tall", focus)
        self.assertIn("Command: Make this twice as tall.", message)
        self.assertIn('Heard: "uh make this like twice as tall"', message)
        self.assertIn("Chair, Leg.001", message)

    def test_focus_descriptions(self):
        self.assertIn("nothing selected", describe_focus({"active": None, "selected": [], "mode": "OBJECT"}))
        self.assertIn("unknown", describe_focus(None))


class PromptsTest(unittest.TestCase):
    def test_prompt_files_exist_and_carry_the_rules(self):
        editor = EDITOR_PROMPT.read_text()
        refiner = REFINER_PROMPT.read_text()
        self.assertIn("execute_blender_code", editor)
        self.assertIn("Never ask", editor)
        self.assertIn('"kind"', refiner)
        self.assertIn('"title"', refiner)
        self.assertIn("exactly as asked", refiner)  # short, exact edits stay as they were said
        self.assertIn("detailed", refiner)  # build requests become detailed briefs
        self.assertNotIn("short, direct", editor)
        self.assertIn("low-poly", editor)

    def test_vocabulary_prompt_is_bounded(self):
        names = [f"Object_{i}" for i in range(500)]
        prompt = vocabulary_prompt(names)
        self.assertIn("bevel", prompt)
        self.assertIn("Object_0", prompt)
        self.assertLess(len(prompt.split()), 220)


if __name__ == "__main__":
    unittest.main()
