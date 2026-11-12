import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gazevoice.mcp_client import StdioMcpClient
from gazevoice.unity_mcp import LOCAL_TOOLS, NAMES, TOOLS, handle, server_command

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


ML_TOOLS = ("mlagents_status", "mlagents_train", "mlagents_stop", "mlagents_import_model")
UNREACHABLE = ("The GazeVoice dashboard isn't reachable, so ML-Agents tools can't run. "
               "If you restarted the dashboard, turn voice off and on.")


class Dashboard:
    """A tiny stand-in for the control center: records each request and answers with whatever is set."""

    def __init__(self):
        self.seen = []
        self.status = 200
        self.reply = {"state": "idle"}
        seen, owner = self.seen, self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append({"method": self.command, "path": self.path, "header": self.headers.get("X-GazeVoice"),
                             "body": json.loads(body) if body else None})
                payload = json.dumps(owner.reply).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LocalToolsTest(unittest.TestCase):
    """The ML-Agents tools are answered here, over HTTP to the dashboard, never by the Unity bridge."""

    def setUp(self):
        self.dashboard = Dashboard()
        self.addCleanup(self.dashboard.close)
        env = patch.dict(os.environ, {"GAZEVOICE_URL": self.dashboard.url})
        env.start()
        self.addCleanup(env.stop)
        bridge = patch("gazevoice.unity_mcp.bridge_request", side_effect=AssertionError("Unity was asked"))
        bridge.start()
        self.addCleanup(bridge.stop)

    def test_the_four_tools_are_listed_beside_the_unity_ones(self):
        self.assertEqual([tool["name"] for tool in LOCAL_TOOLS], list(ML_TOOLS))
        listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertLessEqual(set(ML_TOOLS), {tool["name"] for tool in listed})
        self.assertIn("get_scene_objects", {tool["name"] for tool in listed})
        self.assertEqual(NAMES, {tool["name"] for tool in TOOLS})
        self.assertLessEqual(set(ML_TOOLS), NAMES)

    def test_the_arguments_each_tool_takes(self):
        by_name = {tool["name"]: tool["inputSchema"] for tool in LOCAL_TOOLS}
        self.assertEqual(set(by_name["mlagents_status"]["properties"]), set())
        self.assertEqual(set(by_name["mlagents_stop"]["properties"]), set())
        train = by_name["mlagents_train"]
        self.assertEqual(set(train["properties"]), {"run_id", "config_yaml", "resume", "overwrite"})
        self.assertEqual(train["required"], ["run_id"])
        self.assertEqual(train["properties"]["resume"]["type"], "boolean")
        imported = by_name["mlagents_import_model"]
        self.assertEqual(set(imported["properties"]), {"run_id", "behavior"})
        self.assertEqual(imported["required"], ["run_id"])

    def test_the_descriptions_say_plainly_what_each_tool_does(self):
        said = {tool["name"]: tool["description"] for tool in LOCAL_TOOLS}
        self.assertIn("EditorApplication.isPlaying = true;", said["mlagents_train"])
        self.assertIn("execute_csharp", said["mlagents_train"])
        self.assertIn("Play", said["mlagents_train"])
        self.assertIn("installs nothing", said["mlagents_train"])
        self.assertIn("listening", said["mlagents_train"])  # Play is pressed once the trainer is listening
        self.assertNotIn("right after this returns", said["mlagents_train"])
        self.assertIn("listening", said["mlagents_status"])
        self.assertIn("runs", said["mlagents_status"])
        self.assertIn("saves", said["mlagents_stop"])
        self.assertIn("stopping", said["mlagents_stop"])
        self.assertIn("Assets", said["mlagents_import_model"])

    def test_each_tool_calls_the_right_method_and_path_with_the_header(self):
        cases = (
            ("mlagents_status", {}, "GET", "/api/mlagents", None),
            ("mlagents_train", {"run_id": "Mover-1", "config_yaml": "behaviors: {}", "overwrite": True},
             "POST", "/api/mlagents/train", {"run_id": "Mover-1", "config_yaml": "behaviors: {}", "overwrite": True}),
            ("mlagents_stop", {}, "POST", "/api/mlagents/stop", {}),
            ("mlagents_import_model", {"run_id": "Mover-1", "behavior": "Mover"},
             "POST", "/api/mlagents/import", {"run_id": "Mover-1", "behavior": "Mover"}),
        )
        for name, arguments, method, path, body in cases:
            with self.subTest(name):
                self.dashboard.seen.clear()
                reply = call(name, arguments)
                self.assertEqual(self.dashboard.seen,
                                 [{"method": method, "path": path, "header": "1", "body": body}])
                self.assertFalse(reply["result"]["isError"])

    def test_the_dashboards_answer_comes_back_as_text(self):
        self.dashboard.reply = {"ok": True, "installed": True, "state": "training", "message": "Training run1."}
        reply = call("mlagents_status")
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(json.loads(text_of(reply))["state"], "training")

    def test_a_success_without_an_ok_still_counts_as_one(self):
        self.dashboard.reply = {"state": "waiting", "run_id": "run1"}
        reply = call("mlagents_train", {"run_id": "run1"})
        self.assertFalse(reply["result"]["isError"])
        self.assertTrue(json.loads(text_of(reply))["ok"])

    def test_a_refusal_comes_back_as_an_error_with_the_dashboards_sentence(self):
        self.dashboard.status = 409
        self.dashboard.reply = {"error": "Pick your Unity project in the dashboard first."}
        reply = call("mlagents_train", {"run_id": "run1"})
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(json.loads(text_of(reply)),
                         {"ok": False, "error": "Pick your Unity project in the dashboard first."})

    def test_a_reply_that_is_not_ok_is_an_error(self):
        self.dashboard.reply = {"ok": False, "error": "nope"}
        self.assertTrue(call("mlagents_status")["result"]["isError"])

    def test_an_answer_that_is_not_json_is_an_error(self):
        self.dashboard.status = 500
        self.dashboard.reply = "not an object"
        self.assertTrue(call("mlagents_stop")["result"]["isError"])

    def test_no_dashboard_address_gives_the_plain_error(self):
        with patch.dict(os.environ):
            os.environ.pop("GAZEVOICE_URL")
            reply = call("mlagents_status")
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(json.loads(text_of(reply)), {"ok": False, "error": UNREACHABLE})

    def test_a_dashboard_that_is_not_there_gives_the_plain_error(self):
        self.dashboard.close()
        reply = call("mlagents_train", {"run_id": "run1"})
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(json.loads(text_of(reply)), {"ok": False, "error": UNREACHABLE})

    def test_only_this_macs_own_address_is_called(self):
        for url in ("http://example.invalid:8977", "http://192.168.1.5:8977", "file:///etc/passwd", "not a url",
                    "http://127.0.0.1@evil.example/", "http://localhost@evil.example:8977",
                    "http://localhost.evil.example:8977", "https://127.0.0.1:8977", "http://127.0.0.1:99999", ""):
            with self.subTest(url):
                with patch.dict(os.environ, {"GAZEVOICE_URL": url}):
                    with patch("urllib.request.OpenerDirector.open") as opened:
                        reply = call("mlagents_status")
                self.assertEqual(json.loads(text_of(reply))["error"], UNREACHABLE)
                opened.assert_not_called()

    def test_the_plain_error_says_what_to_do_after_a_restart(self):
        with patch.dict(os.environ):
            os.environ.pop("GAZEVOICE_URL")
            error = json.loads(text_of(call("mlagents_status")))["error"]
        self.assertTrue(error.endswith("If you restarted the dashboard, turn voice off and on."))

    def test_an_answer_cut_off_halfway_gives_the_plain_error_and_the_server_carries_on(self):
        for cut in (http.client.IncompleteRead(b"{", 20), http.client.BadStatusLine("x"),
                    http.client.RemoteDisconnected("closed")):
            with self.subTest(type(cut).__name__):
                answer = MagicMock()
                answer.__enter__.return_value.read.side_effect = cut
                opener = SimpleNamespace(open=MagicMock(return_value=answer))
                with patch("urllib.request.build_opener", return_value=opener):
                    reply = call("mlagents_status")
                self.assertTrue(reply["result"]["isError"])
                self.assertEqual(json.loads(text_of(reply)), {"ok": False, "error": UNREACHABLE})
        self.assertFalse(call("mlagents_status")["result"]["isError"])  # and it still answers the next call

    def test_a_dashboard_that_hangs_up_on_the_request_gives_the_plain_error(self):
        opener = SimpleNamespace(open=MagicMock(side_effect=http.client.RemoteDisconnected("closed")))
        with patch("urllib.request.build_opener", return_value=opener):
            reply = call("mlagents_stop")
        self.assertEqual(json.loads(text_of(reply)), {"ok": False, "error": UNREACHABLE})

    def test_train_waits_for_the_trainer_to_listen_and_still_answers_inside_codexs_60_second_limit(self):
        from gazevoice.mlagents import LISTEN_SECONDS
        from gazevoice.unity_mcp import DASHBOARD_SECONDS

        self.assertEqual(DASHBOARD_SECONDS["mlagents_train"], 55.0)
        self.assertGreater(DASHBOARD_SECONDS["mlagents_train"], LISTEN_SECONDS)
        self.assertLess(DASHBOARD_SECONDS["mlagents_train"], 60.0)

    def test_stop_waits_a_little_longer_than_the_dashboard_does_and_the_rest_30(self):
        answer = MagicMock()
        answer.__enter__.return_value.read.return_value = b"{}"
        opener = SimpleNamespace(open=MagicMock(return_value=answer))
        for name, arguments, seconds in (("mlagents_status", {}, 30.0), ("mlagents_train", {"run_id": "r"}, 55.0),
                                         ("mlagents_import_model", {"run_id": "r"}, 30.0), ("mlagents_stop", {}, 50.0)):
            with self.subTest(name):
                with patch("urllib.request.build_opener", return_value=opener):
                    call(name, arguments)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], seconds)

    def test_a_dashboard_that_goes_quiet_says_so_instead_of_pretending_it_is_gone(self):
        opener = SimpleNamespace(open=MagicMock(side_effect=TimeoutError("timed out")))
        with patch("urllib.request.build_opener", return_value=opener):
            reply = call("mlagents_stop")
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("didn't answer in time", json.loads(text_of(reply))["error"])

    def test_a_proxy_on_this_mac_is_not_used_to_reach_the_dashboard(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            self.dashboard.reply = {"ok": True}
            self.assertFalse(call("mlagents_status")["result"]["isError"])

    def test_an_unknown_tool_is_still_refused(self):
        for name in ("mlagents_install", "mlagents", "execute_code"):
            with self.subTest(name):
                reply = call(name, {})
                self.assertEqual(reply["error"]["code"], -32000)
        self.assertEqual(self.dashboard.seen, [])

    def test_the_stdio_server_answers_them_over_the_wire(self):
        env = {**os.environ, "GAZEVOICE_URL": self.dashboard.url, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        with StdioMcpClient([sys.executable, "-m", "gazevoice.unity_mcp"], env=env, timeout=5) as client:
            client.initialize()
            self.assertLessEqual(set(ML_TOOLS), {tool["name"] for tool in client.list_tools()})
            result = client.call_tool("mlagents_status", {})
        self.assertFalse(result["isError"])
        self.assertEqual(self.dashboard.seen[0]["path"], "/api/mlagents")


class ServerCommandTest(unittest.TestCase):
    def settings(self, **kwargs):
        return SimpleNamespace(unity_mcp_command="", unity_mcp_args=(), **kwargs)

    def test_the_built_in_server_is_told_where_the_dashboard_is(self):
        command = server_command(self.settings(), "http://127.0.0.1:8977")
        self.assertEqual(command["env"]["GAZEVOICE_URL"], "http://127.0.0.1:8977")
        self.assertIn("PYTHONPATH", command["env"])
        self.assertEqual(command["args"], ["-m", "gazevoice.unity_mcp"])

    def test_without_an_address_the_built_in_server_gets_no_dashboard_variable(self):
        self.assertNotIn("GAZEVOICE_URL", server_command(self.settings()).get("env", {}))
        self.assertNotIn("GAZEVOICE_URL", server_command(self.settings(), None).get("env", {}))

    def test_your_own_server_is_left_exactly_as_it_is(self):
        mine = SimpleNamespace(unity_mcp_command="/opt/unity-mcp", unity_mcp_args=("--stdio",))
        self.assertEqual(server_command(mine, "http://127.0.0.1:8977"), {"command": "/opt/unity-mcp", "args": ["--stdio"]})


if __name__ == "__main__":
    unittest.main()
