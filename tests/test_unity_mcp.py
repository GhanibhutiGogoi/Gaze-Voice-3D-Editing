import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gazevoice.mcp_client import StdioMcpClient
from gazevoice.unity_mcp import NAMES, TOOLS, handle

READY = {"ok": True, "compiling": False}
BUSY = {"ok": True, "compiling": True}
RELOADING = ConnectionRefusedError("Unity is reloading")


def call(name, arguments=None):
    return handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}})


def text_of(reply):
    return reply["result"]["content"][0]["text"]


class UnityMcpTest(unittest.TestCase):
    def test_stdio_server_lists_fixed_scene_tools(self):
        with StdioMcpClient([sys.executable, "-m", "gazevoice.unity_mcp"], timeout=5) as client:
            info = client.initialize()
            self.assertEqual(info["serverInfo"]["name"], "gazevoice-unity")
            names = {tool["name"] for tool in client.list_tools()}
            self.assertEqual(names, {tool["name"] for tool in TOOLS})
            self.assertIn("transform_object", names)

    def test_unknown_tool_does_not_reach_unity(self):
        reply = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "execute_code", "arguments": {"code": "x"}}})
        self.assertEqual(reply["error"]["code"], -32000)

    def test_tool_call_forwards_arguments_and_reports_unity_error(self):
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "set_color", "arguments": {"target": 12, "color": "#2E5BFF"}}}
        with patch("gazevoice.unity_mcp.bridge_request", return_value={"ok": True, "id": 12}) as bridge:
            reply = handle(request)
        bridge.assert_called_once_with({"cmd": "tool", "name": "set_color",
                                        "arguments": {"target": 12, "color": "#2E5BFF"}},
                                       port=9878, timeout=10.0)
        self.assertFalse(reply["result"]["isError"])
        with patch("gazevoice.unity_mcp.bridge_request", return_value={"ok": False, "error": "missing"}):
            reply = handle(request)
        self.assertTrue(reply["result"]["isError"])


class FullAccessTest(unittest.TestCase):
    def setUp(self):
        sleeping = patch("gazevoice.unity_mcp.time.sleep")
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def bridge(self, *replies):
        patcher = patch("gazevoice.unity_mcp.bridge_request", side_effect=list(replies))
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def test_the_tools_cover_what_a_real_build_needs(self):
        self.assertLessEqual({"execute_csharp", "write_script", "get_compile_status", "read_console",
                              "capture_scene_view", "add_package"}, NAMES)

    def test_csharp_has_minutes_to_run(self):
        bridge = self.bridge({"ok": True, "result": "42", "logs": []})
        reply = call("execute_csharp", {"code": "return 6 * 7;"})
        self.assertEqual(bridge.call_args.kwargs["timeout"], 300.0)
        self.assertFalse(reply["result"]["isError"])
        self.assertIn("42", text_of(reply))

    def test_csharp_that_wont_compile_comes_back_as_an_error_the_model_can_read(self):
        self.bridge({"ok": False, "error": "The C# didn't compile.", "details": "snippet(1,1): error CS0103"})
        reply = call("execute_csharp", {"code": "nope"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("CS0103", text_of(reply))

    def test_a_call_made_during_a_reload_waits_and_then_goes_through(self):
        bridge = self.bridge(RELOADING, RELOADING, READY, READY, {"ok": True, "objects": []})
        reply = call("get_scene_objects")
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(bridge.call_count, 5)

    def test_a_call_cut_off_by_a_reload_is_never_sent_twice(self):
        for cut_off in (ConnectionResetError("reset"), ConnectionError("The bridge closed the connection")):
            with self.subTest(cut_off=cut_off):
                bridge = self.bridge(cut_off, READY, READY)
                reply = call("execute_csharp", {"code": "Create();"})
                self.assertTrue(reply["result"]["isError"])
                self.assertIn("may not have finished", text_of(reply))
                sent = [c.args[0] for c in bridge.call_args_list]
                self.assertEqual(sum(request["cmd"] == "tool" for request in sent), 1)

    def test_a_reload_that_never_ends_is_reported(self):
        with patch("gazevoice.unity_mcp.RELOAD_SECONDS", 0.0):
            self.bridge(RELOADING)
            reply = call("get_scene_objects")
        self.assertIn("error", reply)

    def test_write_script_waits_through_the_compile_and_reload(self):
        bridge = self.bridge({"ok": True, "path": "Assets/GazeVoice/Scripts/Spin.cs", "changed": True},
                             BUSY, RELOADING, RELOADING, READY, READY,
                             {"ok": True, "compiling": False, "errors": []})
        reply = call("write_script", {"path": "Assets/GazeVoice/Scripts/Spin.cs", "content": "class Spin {}"})
        self.assertFalse(reply["result"]["isError"])
        self.assertTrue(json.loads(text_of(reply))["compiled"])
        self.assertEqual(bridge.call_count, 7)

    def test_write_script_hands_back_compile_errors_for_fixing(self):
        self.bridge({"ok": True, "path": "Assets/Spin.cs", "changed": True}, READY, READY,
                    {"ok": True, "compiling": False, "errors": ["Assets/Spin.cs(3,1): error CS1002: ; expected"]})
        reply = call("write_script", {"path": "Assets/Spin.cs", "content": "class Spin"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("CS1002", text_of(reply))

    def test_a_script_that_didnt_change_doesnt_wait_for_a_compile(self):
        bridge = self.bridge({"ok": True, "path": "Assets/Spin.cs", "changed": False})
        call("write_script", {"path": "Assets/Spin.cs", "content": "class Spin {}"})
        self.assertEqual(bridge.call_count, 1)

    def test_a_compile_that_never_finishes_says_so(self):
        with patch("gazevoice.unity_mcp.RELOAD_SECONDS", 0.0):
            self.bridge({"ok": True, "path": "Assets/Spin.cs", "changed": True})
            reply = call("write_script", {"path": "Assets/Spin.cs", "content": "class Spin {}"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("still compiling", text_of(reply))

    def test_add_package_waits_and_confirms(self):
        self.bridge({"ok": True, "started": "com.unity.probuilder"}, READY, READY,
                    {"ok": True, "compiling": False, "errors": [], "package_error": None})
        reply = call("add_package", {"package": "com.unity.probuilder"})
        self.assertFalse(reply["result"]["isError"])

    def test_add_package_reports_why_it_failed(self):
        self.bridge({"ok": True, "started": "com.nope"}, READY, READY,
                    {"ok": True, "compiling": False, "errors": [], "package_error": "Package not found"})
        reply = call("add_package", {"package": "com.nope"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("Package not found", text_of(reply))

    def test_a_scene_view_capture_comes_back_as_an_image_and_the_file_goes(self):
        with tempfile.TemporaryDirectory() as folder:
            shot = Path(folder) / "view.png"
            shot.write_bytes(b"\x89PNG fake")
            self.bridge({"ok": True, "path": str(shot)})
            reply = call("capture_scene_view")
            image = reply["result"]["content"][0]
            self.assertEqual(image["type"], "image")
            self.assertEqual(image["mimeType"], "image/png")
            self.assertEqual(base64.b64decode(image["data"]), b"\x89PNG fake")
            self.assertFalse(shot.exists())

    def test_a_failed_capture_stays_a_readable_error(self):
        self.bridge({"ok": False, "error": "Open a Scene view first."})
        reply = call("capture_scene_view")
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("Scene view", text_of(reply))


if __name__ == "__main__":
    unittest.main()
