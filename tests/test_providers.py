import socket
import ssl
import threading
import unittest
import urllib.error
from unittest import mock

from gazevoice.providers import (
    THINKING,
    ChatClient,
    KeyStore,
    ProviderConfig,
    ProviderError,
    clean_key,
    custom_id,
    normalize_base_url,
    preset,
    thinking_fields,
)
from gazevoice.runner import FakeRunner

from .fake_openai import FakeOpenAI

FIND = ["security", "find-generic-password", "-s", "GazeVoice", "-a", "deepseek"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CatalogTest(unittest.TestCase):
    def test_deepseek_preset(self):
        self.assertEqual(preset("deepseek").endpoint(), "https://api.deepseek.com")
        self.assertEqual((preset("deepseek").name, preset("qwen").name), ("DeepSeek", "Qwen"))

    def test_qwen_endpoints(self):
        ws = ProviderConfig("qwen", "qwen", "Qwen", region="eu-central-1", workspace="ws123")
        self.assertEqual(ws.endpoint(), "https://ws123.eu-central-1.maas.aliyuncs.com/compatible-mode/v1")
        shared = ProviderConfig("qwen", "qwen", "Qwen", region="ap-southeast-1")
        self.assertEqual(shared.endpoint(), "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
        with self.assertRaises(ValueError) as caught:
            ProviderConfig("qwen", "qwen", "Qwen", region="us-east-1").endpoint()
        self.assertIn("Workspace ID", str(caught.exception))

    def test_normalize_base_url(self):
        self.assertEqual(normalize_base_url("https://x.test/v1/"), "https://x.test/v1")
        self.assertEqual(normalize_base_url(" https://x.test/v1/chat/completions "), "https://x.test/v1")
        for bad in ("ftp://x", "", "not a url"):
            with self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_clean_key(self):
        self.assertEqual(clean_key("  sk-abc123\n"), "sk-abc123")
        for bad in ("sk abc", 'sk"abc', "sk\tabc", "", "   "):
            with self.assertRaises(ValueError) as caught:
                clean_key(bad)
            if bad.strip():
                self.assertNotIn(bad.strip(), str(caught.exception))

    def test_a_quote_never_reaches_the_keychain_command(self):
        with self.assertRaises(ValueError):
            clean_key("sk'abc")

    def test_custom_id(self):
        self.assertEqual(custom_id("My Box!", set()), "custom_my_box")
        self.assertEqual(custom_id("My Box!", {"custom_my_box"}), "custom_my_box_2")
        self.assertLessEqual(len(custom_id("x" * 80, set())), 32)
        self.assertEqual(custom_id("!!!", set()), "custom_provider")

    def test_thinking_fields(self):
        self.assertEqual(thinking_fields("deepseek", "off"), {"thinking": {"type": "disabled"}})
        self.assertEqual(thinking_fields("deepseek", "max"), {"reasoning_effort": "max"})
        self.assertEqual(thinking_fields("qwen", "on"), {"enable_thinking": True})
        self.assertEqual(thinking_fields("qwen", "off"), {"enable_thinking": False})
        self.assertEqual(thinking_fields("custom", "default"), {})
        self.assertEqual(thinking_fields("deepseek", "default"), {})
        self.assertEqual(THINKING["custom"], ("default",))

    def test_config_round_trip_and_validation(self):
        cfg = ProviderConfig("custom_box", "custom", "Box", base_url="https://box.test/v1", key_hint="1234")
        self.assertEqual(ProviderConfig.from_dict(cfg.to_dict()), cfg)
        for bad in ({"id": "x", "kind": "gpt", "name": "X"},
                    {"id": "Bad Id", "kind": "custom", "name": "X", "base_url": "https://a.test"},
                    {"id": "custom_x", "kind": "custom", "name": "X"},
                    {"id": "qwen", "kind": "qwen", "name": "Qwen", "region": "mars-1"}):
            with self.assertRaises(ValueError):
                ProviderConfig.from_dict(bad)


class KeyStoreTest(unittest.TestCase):
    def test_keystore_keeps_the_key_out_of_argv(self):
        runner = FakeRunner()
        runner.add(["security", "-i"])
        runner.add(FIND)
        keys = KeyStore(runner)
        keys.set("deepseek", "  sk-secret\n")
        self.assertIn(("security", "-i"), runner.calls)
        sent = runner.call_kwargs[runner.calls.index(("security", "-i"))]["input_text"]
        self.assertIn('add-generic-password -U -s GazeVoice -a deepseek -w "sk-secret"', sent)
        self.assertFalse(any("sk-secret" in part for call in runner.calls for part in call))

        runner.add([*FIND, "-w"], stdout="sk-secret\n")
        self.assertEqual(keys.get("deepseek"), "sk-secret")
        self.assertTrue(keys.has("deepseek"))
        runner.add([*FIND, "-w"], returncode=44)
        self.assertIsNone(keys.get("deepseek"))
        runner.add(["security", "delete-generic-password", "-s", "GazeVoice", "-a", "deepseek"])
        keys.delete("deepseek")
        self.assertIn(("security", "delete-generic-password", "-s", "GazeVoice", "-a", "deepseek"), runner.calls)

    def test_a_failed_save_says_so(self):
        runner = FakeRunner()
        runner.add(["security", "-i"])
        runner.add(FIND, returncode=44)  # the item isn't there afterwards
        with self.assertRaises(ProviderError) as caught:
            KeyStore(runner).set("deepseek", "sk-secret")
        self.assertIn("Keychain", str(caught.exception))
        self.assertNotIn("sk-secret", str(caught.exception))

    def test_bad_provider_ids_never_reach_security(self):
        runner = FakeRunner()
        with self.assertRaises(ValueError):
            KeyStore(runner).set("x; rm -rf /", "sk-secret")
        self.assertEqual(runner.calls, [])


class ChatClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenAI()
        self.cfg = ProviderConfig("custom_fake", "custom", "Fake AI", base_url=self.fake.url)

    def tearDown(self):
        self.fake.close()

    def test_chat_sends_bearer_key_and_body(self):
        self.fake.message("hi")
        reply = ChatClient(self.cfg, "sk-x").chat({"model": "m", "messages": [{"role": "user", "content": "yo"}]})
        self.assertEqual(reply["choices"][0]["message"]["content"], "hi")
        sent = self.fake.requests[0]
        self.assertEqual((sent["method"], sent["path"]), ("POST", "/v1/chat/completions"))
        self.assertEqual(sent["headers"]["authorization"], "Bearer sk-x")
        self.assertEqual(sent["body"]["model"], "m")

    def test_qwen_answers_are_streamed_and_assembled(self):
        # Alibaba only lets Qwen think in streamed calls, so every Qwen chat is streamed.
        qwen = ProviderConfig("qwen", "qwen", "Qwen", base_url=self.fake.url, region="ap-southeast-1")
        self.fake.stream([
            {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "thi"}}]},
            {"choices": [{"index": 0, "delta": {"reasoning_content": "nk", "content": ""}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                 "function": {"name": "get_objects_summary", "arguments": ""}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"a": '}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]},
                          "finish_reason": "tool_calls"}]},
        ])
        reply = ChatClient(qwen, "sk-x").chat({"model": "qwen-max", "messages": [], "enable_thinking": True})
        self.assertIs(self.fake.requests[0]["body"]["stream"], True)
        choice = reply["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["reasoning_content"], "think")
        self.assertEqual(choice["message"]["tool_calls"], [{"id": "call_1", "type": "function", "function": {
            "name": "get_objects_summary", "arguments": '{"a": 1}'}}])

    def test_a_streamed_text_answer(self):
        qwen = ProviderConfig("qwen", "qwen", "Qwen", base_url=self.fake.url, region="ap-southeast-1")
        self.fake.stream([{"choices": [{"index": 0, "delta": {"content": "Made it "}}]},
                          {"choices": [{"index": 0, "delta": {"content": "red."}, "finish_reason": "stop"}]}])
        reply = ChatClient(qwen, "sk-x").chat({"model": "qwen-max", "messages": []})
        self.assertEqual(reply["choices"][0]["message"]["content"], "Made it red.")

    def test_deepseek_is_not_streamed(self):
        deepseek = ProviderConfig("deepseek", "deepseek", "DeepSeek", base_url=self.fake.url)
        self.fake.message("ok")
        ChatClient(deepseek, "sk-x").chat({"model": "deepseek-flash", "messages": []})
        self.assertNotIn("stream", self.fake.requests[0]["body"])

    def test_models_lists_ids(self):
        self.fake.reply({"object": "list", "data": [{"id": "a"}, {"id": "b"}, {"oops": 1}]})
        self.assertEqual(ChatClient(self.cfg, "sk-x").models(), ["a", "b"])
        self.assertEqual(self.fake.requests[0]["path"], "/v1/models")

    def error_for(self, status, body=None, cfg=None, headers=None):
        self.fake.reply(body or {"error": {"message": "nope"}}, status=status, headers=headers)
        with self.assertRaises(ProviderError) as caught:
            ChatClient(cfg or self.cfg, "sk-x").chat({"model": "m1", "messages": []})
        self.assertNotIn("sk-x", str(caught.exception))
        return str(caught.exception)

    def test_errors_are_plain_and_never_contain_the_key(self):
        self.assertIn("key was rejected", self.error_for(401))
        qwen = ProviderConfig("qwen", "qwen", "Qwen", base_url=self.fake.url, region="cn-beijing")
        self.assertIn("China (Beijing)", self.error_for(401, cfg=qwen))
        self.assertIn("out of credit", self.error_for(402))
        self.assertIn("doesn't have the model m1", self.error_for(404))
        self.assertIn("can't call tools",
                      self.error_for(400, {"error": {"message": "This model does not support tools"}}))
        self.assertIn("Fake AI", self.error_for(500))
        self.assertNotIn("sk-x", self.error_for(400, {"error": {"message": "bad key sk-x given"}}))

    def test_only_real_tool_support_errors_say_cant_call_tools(self):
        history = {"error": {"message": "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"}}
        message = self.error_for(400, history)
        self.assertNotIn("can't call tools", message)
        self.assertIn("refused the request", message)
        for text in ("Function calling is not supported for this model", "tools are not supported by deepseek-x",
                     "This model doesn't support tool use"):
            self.assertIn("can't call tools", self.error_for(400, {"error": {"message": text}}))

    def test_a_cut_off_answer_is_a_plain_error(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def answer_half():
            conn, _ = server.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"cho")
            conn.close()

        threading.Thread(target=answer_half, daemon=True).start()
        cfg = ProviderConfig("custom_half", "custom", "Half AI", base_url=f"http://127.0.0.1:{server.getsockname()[1]}/v1")
        with self.assertRaises(ProviderError) as caught:
            ChatClient(cfg, "sk-x").chat({"model": "m", "messages": []})
        self.assertIn("Lost the connection to Half AI", str(caught.exception))

    def test_a_certificate_problem_says_what_to_do(self):
        problem = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        with mock.patch("gazevoice.providers.urllib.request.urlopen", side_effect=problem):
            with self.assertRaises(ProviderError) as caught:
                ChatClient(self.cfg, "sk-x").models()
        self.assertIn("Install Certificates", str(caught.exception))

    def test_rate_limits_are_retried_once(self):
        self.fake.reply({"error": {"message": "slow down"}}, status=429, headers={"Retry-After": "0"})
        self.fake.message("ok")
        reply = ChatClient(self.cfg, "sk-x").chat({"model": "m", "messages": []})
        self.assertEqual(reply["choices"][0]["message"]["content"], "ok")

    def test_a_rate_limit_that_persists_is_reported(self):
        self.fake.reply({"error": {"message": "slow down"}}, status=429, headers={"Retry-After": "0"})
        self.assertIn("rate-limiting", self.error_for(429, headers={"Retry-After": "0"}))
        self.assertEqual(len(self.fake.requests), 2)

    def test_unreachable(self):
        cfg = ProviderConfig("custom_gone", "custom", "Gone AI", base_url=f"http://127.0.0.1:{free_port()}/v1")
        with self.assertRaises(ProviderError) as caught:
            ChatClient(cfg, "sk-x").models()
        self.assertIn("Couldn't reach Gone AI", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
