import sys
import unittest
from unittest.mock import patch

from gazevoice.mcp_client import StdioMcpClient
from gazevoice.unity_mcp import TOOLS, handle


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


if __name__ == "__main__":
    unittest.main()
