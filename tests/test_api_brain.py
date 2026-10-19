import copy
import json
import threading
import time
import unittest

from gazevoice.providers import ProviderError
from gazevoice.voice.api_brain import ApiSession
from gazevoice.voice.brains import BrainError

TOOLS = [
    {"name": "execute_blender_code", "description": "Run Python in Blender",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}}},
    {"name": "get_objects_summary", "description": "List the objects", "inputSchema": {"type": "object", "properties": {}}},
]


def msg(content="", tool_calls=None, **extra):
    message = {"role": "assistant", "content": content, **extra}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}


def call(name, args, cid="call_1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeClient:
    """Answers chat requests from a script; a reply can be an exception. `slow` makes every call block."""

    def __init__(self, replies=(), slow=0.0):
        self.replies = list(replies)
        self.bodies = []
        self.slow = slow

    def chat(self, body, timeout=None):
        self.bodies.append(copy.deepcopy(body))
        if self.slow and len(self.bodies) > 1:  # the warm-up answers at once
            time.sleep(self.slow)
        reply = self.replies.pop(0) if self.replies else msg("done")
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeMcp:
    def __init__(self):
        self.calls = []
        self.closed = False

    def initialize(self):
        return {}

    def list_tools(self):
        return [dict(t) for t in TOOLS]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"{name} ok"}], "isError": False}

    def close(self):
        self.closed = True


class ApiSessionTest(unittest.TestCase):
    def editor(self, replies, **kwargs):
        self.mcp = FakeMcp()
        self.client = FakeClient([msg("ok"), *replies], **kwargs.pop("client", {}))
        session = ApiSession(self.client, "deepseek-v4-pro", "You edit Blender.", kind="deepseek",
                             mcp_factory=lambda: self.mcp, **kwargs)
        session.start()
        self.addCleanup(session.close)
        return session

    def test_refiner_is_a_plain_chat(self):
        client = FakeClient([msg("ok"), msg('{"kind": "command", "text": "Make it red."}')])
        session = ApiSession(client, "deepseek-flash", "You clean up.", kind="deepseek", thinking="off")
        session.start()
        turn = session.send('{"heard": "make it red"}')
        self.assertEqual((turn.text, turn.is_error, turn.tools), ('{"kind": "command", "text": "Make it red."}', False, []))
        body = client.bodies[1]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "You clean up."})
        self.assertEqual(body["messages"][-1], {"role": "user", "content": '{"heard": "make it red"}'})
        self.assertNotIn("tools", body)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual((session.init, session.effort), ({"model": "deepseek-flash"}, "off"))

    def test_editor_runs_the_tool_loop(self):
        session = self.editor([msg(tool_calls=[call("execute_blender_code", {"code": "x"})]), msg("Made it red.")])
        seen = []
        turn = session.send("Command: make it red", on_tool=seen.append)
        self.assertEqual((turn.text, turn.is_error, turn.tools), ("Made it red.", False, ["execute_blender_code"]))
        self.assertEqual(seen, ["execute_blender_code"])
        self.assertEqual(self.mcp.calls, [("execute_blender_code", {"code": "x"})])
        tool_message = self.client.bodies[2]["messages"][-1]
        self.assertEqual(tool_message, {"role": "tool", "tool_call_id": "call_1", "content": "execute_blender_code ok"})

    def test_tools_come_from_mcp(self):
        session = self.editor([msg("Nothing to do.")])
        session.send("Command: look")
        expected = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["inputSchema"]}} for t in TOOLS]
        self.assertEqual(self.client.bodies[1]["tools"], expected)
        self.assertNotIn("tool_choice", self.client.bodies[1])

    def test_reasoning_content_goes_back_in_tool_turns(self):
        session = self.editor([
            msg(tool_calls=[call("get_objects_summary", {})], reasoning_content="think"),
            msg("There are 3 objects."),
        ])
        session.send("Command: count")
        assistant = [m for m in self.client.bodies[2]["messages"] if m["role"] == "assistant"]
        self.assertEqual(assistant[-1]["reasoning_content"], "think")
        self.assertEqual(assistant[-1]["tool_calls"][0]["id"], "call_1")

    def test_memory_spans_commands_and_is_bounded(self):
        session = self.editor([msg(f"done {i}") for i in range(10)], keep_commands=8)
        for i in range(10):
            session.send(f"Command: step {i}")
        users = [m["content"] for m in self.client.bodies[-1]["messages"] if m["role"] == "user"]
        self.assertIn("Command: step 8", users)
        self.assertNotIn("Command: step 0", users)
        self.assertEqual(len(users), 9)  # the 8 kept commands, plus this one
        self.assertIn("Command: step 0", [m["content"] for m in self.client.bodies[2]["messages"] if m["role"] == "user"])
        session.close()
        session.start()
        session.send("Command: fresh")
        users = [m["content"] for m in self.client.bodies[-1]["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["Command: fresh"])

    def test_step_cap(self):
        endless = [msg(tool_calls=[call("get_objects_summary", {}, cid=f"c{i}")]) for i in range(20)]
        session = self.editor(endless)
        turn = session.send("Command: loop forever")
        self.assertTrue(turn.is_error)
        self.assertEqual(len(self.client.bodies), 1 + 12)

    def test_interrupt_ends_the_turn_quickly(self):
        session = self.editor([msg(tool_calls=[call("execute_blender_code", {"code": "x"})])], client={"slow": 5})
        threading.Timer(0.2, session.interrupt).start()
        began = time.monotonic()
        turn = session.send("Command: slow")
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertTrue(turn.interrupted)
        self.assertTrue(turn.is_error)
        self.assertEqual(self.mcp.calls, [])
        self.assertTrue(session.alive)

    def test_provider_error_becomes_brain_error(self):
        session = self.editor([ProviderError("Your DeepSeek account is out of credit. Top it up, then try again.")])
        with self.assertRaises(BrainError) as caught:
            session.send("Command: anything")
        self.assertIn("out of credit", str(caught.exception))

    def test_start_checks_the_model_and_tools(self):
        session = self.editor([])
        warmup = self.client.bodies[0]
        self.assertIn("tools", warmup)
        self.assertLessEqual(warmup["max_tokens"], 16)
        mcp = FakeMcp()
        bad = ApiSession(FakeClient([ProviderError("deepseek-x can't call tools, so it can't edit Blender.")]),
                         "deepseek-x", "You edit Blender.", kind="deepseek", mcp_factory=lambda: mcp)
        with self.assertRaises(BrainError) as caught:
            bad.start()
        self.assertIn("can't call tools", str(caught.exception))
        self.assertTrue(mcp.closed)
        self.assertFalse(bad.alive)
        session.close()
        self.assertTrue(self.mcp.closed)

    def test_bad_tool_arguments_are_reported_back_to_the_model(self):
        broken = {"id": "c9", "type": "function", "function": {"name": "execute_blender_code", "arguments": "{oops"}}
        session = self.editor([msg(tool_calls=[broken]), msg("Fixed it.")])
        turn = session.send("Command: go")
        self.assertEqual(turn.text, "Fixed it.")
        self.assertEqual(self.mcp.calls, [])
        self.assertIn("not valid JSON", self.client.bodies[2]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
