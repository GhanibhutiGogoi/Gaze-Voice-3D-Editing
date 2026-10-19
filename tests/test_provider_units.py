import dataclasses
import unittest

from gazevoice.components import build_components
from gazevoice.components.base import Health
from gazevoice.components.providers import provider_units
from gazevoice.providers import KeyStore, ProviderConfig, preset
from gazevoice.settings import update_settings

from .fake_openai import FakeOpenAI
from .helpers import TempCtx, make_job


def find(pid, secret=False):
    argv = ["security", "find-generic-password", "-s", "GazeVoice", "-a", pid]
    return argv + ["-w"] if secret else argv


class ProviderUnitsTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.keys = KeyStore(self.t.runner)

    def tearDown(self):
        self.t.cleanup()

    def units(self):
        return {u.id: u for u in provider_units(self.t.ctx, self.keys)}

    def settle(self, **changes):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, changes)

    def test_units_without_keys_ask_for_one(self):
        state = self.units()["deepseek"].check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Add your API key", state.summary)
        self.assertFalse(state.offers("test"))
        provider = state.to_dict()["provider"]
        self.assertEqual((provider["kind"], provider["has_key"]), ("deepseek", False))
        self.assertEqual(self.units()["qwen"].check().to_dict()["provider"]["regions"][0], ["ap-southeast-1", "Singapore"])

    def test_saved_key_shows_only_its_hint(self):
        self.settle(providers=[dataclasses.replace(preset("deepseek"), key_hint="1234").to_dict()])
        self.t.runner.add(find("deepseek"))
        state = self.units()["deepseek"].check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("••••1234", state.summary)
        self.assertTrue(state.offers("test"))
        self.assertTrue(state.to_dict()["provider"]["has_key"])

    def test_custom_units_follow_settings(self):
        a = ProviderConfig("custom_a", "custom", "A", base_url="https://a.test/v1").to_dict()
        b = ProviderConfig("custom_b", "custom", "B", base_url="https://b.test/v1").to_dict()
        self.settle(providers=[a, b])
        self.assertEqual(list(self.units()), ["deepseek", "qwen", "custom_a", "custom_b", "custom_new"])
        new = self.units()["custom_new"].check().to_dict()["provider"]
        self.assertEqual((new["kind"], new["new"]), ("custom", True))

    def test_provider_lists_the_steps_that_use_it(self):
        self.settle(providers=[dataclasses.replace(preset("deepseek"), key_hint="1234").to_dict()],
                    refiner_source="deepseek", editor_source="deepseek")
        self.t.runner.add(find("deepseek"))
        state = self.units()["deepseek"].check()
        self.assertEqual(state.detail("Used for"), "Cleanup, Editing")
        self.assertEqual(state.to_dict()["provider"]["used_for"], ["refiner", "editor"])

    def test_test_action_checks_each_step_model(self):
        fake = FakeOpenAI()
        self.addCleanup(fake.close)
        cfg = dataclasses.replace(preset("deepseek"), base_url=fake.url, key_hint="1234")
        self.settle(providers=[cfg.to_dict()], editor_source="deepseek", editor_api_model="deepseek-v4-pro")
        self.t.runner.add(find("deepseek"))
        self.t.runner.add(find("deepseek", secret=True), stdout="sk-1234\n")
        fake.reply({"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]})
        fake.message("ok")
        result = self.units()["deepseek"].run_action("test", make_job("deepseek", "test"))
        self.assertEqual((result["editing"], result["cleanup"]), ("ok", None))
        self.assertEqual(result["models"], ["deepseek-flash", "deepseek-v4-pro"])
        chat = fake.requests[1]["body"]
        self.assertEqual(chat["model"], "deepseek-v4-pro")
        self.assertTrue(chat["tools"])
        self.assertIn("Passed", self.units()["deepseek"].check().detail("Last test") or "")

    def test_test_without_a_model_says_where_to_pick_one(self):
        fake = FakeOpenAI()
        self.addCleanup(fake.close)
        cfg = dataclasses.replace(preset("deepseek"), base_url=fake.url, key_hint="1234")
        self.settle(providers=[cfg.to_dict()], refiner_source="deepseek")
        self.t.runner.add(find("deepseek", secret=True), stdout="sk-1234\n")
        fake.reply({"data": [{"id": "deepseek-flash"}]})
        with self.assertRaisesRegex(Exception, "Routing"):
            self.units()["deepseek"].run_action("test", make_job("deepseek", "test"))

    def test_rack_includes_provider_units(self):
        ids = [c.id for c in build_components(self.t.ctx)]
        self.assertLess(ids.index("codex"), ids.index("deepseek"))
        self.assertIn("custom_new", ids)
        self.assertEqual(ids[-1], "workflow")


if __name__ == "__main__":
    unittest.main()
