import json
import sys
import unittest
from pathlib import Path

from gazevoice.mcp_client import McpError, StdioMcpClient

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


class StdioMcpClientTest(unittest.TestCase):
    def test_initialize_list_and_call(self):
        with StdioMcpClient([sys.executable, FAKE], timeout=10) as client:
            info = client.initialize()
            self.assertEqual(info["serverInfo"]["name"], "fake-blender")
            tools = client.list_tools()
            self.assertEqual(
                [t["name"] for t in tools],
                ["get_objects_summary", "execute_blender_code", "get_blendfile_summary_datablocks"],
            )
            result = client.call_tool("get_objects_summary", {"limit": 3})
            self.assertEqual(json.loads(result["content"][0]["text"]), {"limit": 3})

    def test_unknown_method_raises(self):
        with StdioMcpClient([sys.executable, FAKE], timeout=10) as client:
            client.initialize()
            with self.assertRaises(McpError):
                client._request("resources/list")

    def test_crashing_server_reports_stderr(self):
        with self.assertRaises(McpError) as caught:
            with StdioMcpClient([sys.executable, FAKE, "--crash"], timeout=10) as client:
                client.initialize()
        self.assertIn("boom", str(caught.exception))

    def test_silent_server_times_out(self):
        with self.assertRaises(McpError) as caught:
            with StdioMcpClient([sys.executable, FAKE, "--silent"], timeout=0.5) as client:
                client.initialize()
        self.assertIn("timed out", str(caught.exception))

    def test_missing_command(self):
        with self.assertRaises(McpError):
            with StdioMcpClient(["definitely-not-a-command-xyz"], timeout=2) as client:
                client.initialize()


if __name__ == "__main__":
    unittest.main()
