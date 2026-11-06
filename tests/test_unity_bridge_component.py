import socket
import socketserver
import json
import threading
import unittest

from gazevoice.components.base import Health
from gazevoice.components.unity_bridge import UnityBridge, UnityMcp
from gazevoice.jobs import JobError
from gazevoice.settings import update_settings
from gazevoice.unity_install import PACKAGE_NAME, add_bridge, bridge_current, bridge_installed
from gazevoice.unity_mcp import TOOLS

from .helpers import TempCtx, fake_unity_project, make_job
from .test_unity import UnityEnv


class FakeUnity:
    """A real TCP server answering like the Unity package does."""

    def __init__(self, reply=None):
        self.requests = []
        requests = self.requests
        answer = reply or (lambda request: {
            "ping": {"ok": True, "bridge": "0.1.0", "unity": "6000.3.7f1", "key": "⌥ Space"},
            "selection": {"ok": True, "active": "Cube", "selected": ["Cube"], "mode": "editor"},
            "scene_names": {"ok": True, "names": ["Cube", "Main Camera"]},
            "status": {"ok": True},
            "tool": {"ok": True, "result": "6000.3.7f1", "logs": []},
        }.get(request.get("cmd"), {"ok": False}))

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    request = json.loads(line)
                    requests.append(request)
                    self.wfile.write((json.dumps(answer(request)) + "\n").encode())

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BridgeUnitTest(unittest.TestCase):
    def setUp(self):
        self.env = UnityEnv(self)
        self.unit = UnityBridge(self.env.ctx)
        self.unit.port = closed_port()
        self.project = fake_unity_project(self.env.projects / "Game")

    def serve(self, **kwargs):
        fake = FakeUnity(**kwargs)
        self.addCleanup(fake.close)
        self.unit.port = fake.port
        return fake

    def test_it_needs_a_project_first(self):
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertIn("Set up Unity", state.summary)
        self.assertFalse(state.offers("install"))

    def test_not_added_offers_add_and_the_confirm_names_the_project_and_folder(self):
        self.env.choose(self.project)
        state = self.unit.check()
        self.assertEqual(state.health, Health.MISSING)
        confirm = next(a.confirm for a in state.actions if a.id == "install")
        self.assertIn("Game", confirm)
        self.assertIn(f"Packages/{PACKAGE_NAME}", confirm)

    def test_add_copies_the_package_in(self):
        self.env.choose(self.project)
        self.unit.run_action("install", make_job("unity_bridge", "install"))
        self.assertTrue(bridge_current(self.project))

    def test_added_with_unity_closed_says_to_open_it(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        state = self.unit.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("Open the project", state.summary)

    def test_added_with_unity_open_says_to_switch_to_it(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.env.running()
        self.assertIn("Switch to Unity", self.unit.check().summary)

    def test_an_older_copy_offers_update(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        (self.project / "Packages" / PACKAGE_NAME / "package.json").write_text("{}")
        state = self.unit.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertEqual(next(a.label for a in state.actions if a.id == "install"), "Update")

    def test_connected_is_ready_and_explains_how_to_talk(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.serve()
        state = self.unit.check()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("⌥ Space", state.summary)
        self.assertIn("Window › GazeVoice", state.summary)
        self.assertTrue(state.offers("test"))

    def test_something_else_on_the_port_is_an_error(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.serve(reply=lambda request: {"nope": True})
        self.assertEqual(self.unit.check().health, Health.ERROR)

    def test_test_shows_a_status_and_reads_the_scene(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        fake = self.serve()
        result = self.unit.run_action("test", make_job("unity_bridge", "test"))
        self.assertEqual(result["objects"], 2)
        self.assertEqual(result["unity"], "6000.3.7f1")
        self.assertIn("status", [r["cmd"] for r in fake.requests])

    def test_test_proves_csharp_really_runs_in_unity(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        fake = self.serve()
        self.unit.run_action("test", make_job("unity_bridge", "test"))
        self.assertIn("execute_csharp", [r.get("name") for r in fake.requests])

    def test_test_fails_when_csharp_wont_run(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.serve(reply=lambda request: (
            {"ok": False, "error": "Couldn't find the C# compiler."} if request["cmd"] == "tool"
            else {"ok": True, "names": []}))
        with self.assertRaises(JobError) as caught:
            self.unit.run_action("test", make_job("unity_bridge", "test"))
        self.assertIn("C# compiler", str(caught.exception))

    def test_a_package_that_wont_compile_shows_unitys_error(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.env.running()
        self.unit.editor_log = self.env.t.root / "Editor.log"
        self.unit.editor_log.write_text(
            "Starting...\nPackages/com.gazevoice.bridge/Editor/GazeVoiceBridge.cs(12,5): error CS0246: nope\n")
        state = self.unit.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("CS0246", state.summary)

    def test_a_compile_error_in_the_projects_own_log_is_found(self):
        """Newer Unity versions log to the project, not to ~/Library/Logs."""
        self.env.choose(self.project)
        add_bridge(self.project)
        self.env.running()
        self.unit.editor_log = self.env.t.root / "no-such.log"
        (self.project / "Logs").mkdir()
        (self.project / "Logs" / "Editor.log").write_text(
            "Packages/com.gazevoice.bridge/Editor/GazeVoiceBridge.cs(528,56): error CS0619: 'x' is obsolete\n")
        state = self.unit.check()
        self.assertEqual(state.health, Health.ERROR)
        self.assertIn("CS0619", state.summary)

    def test_someone_elses_compile_error_isnt_blamed_on_the_package(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.env.running()
        self.unit.editor_log = self.env.t.root / "Editor.log"
        self.unit.editor_log.write_text("Assets/Mine.cs(1,1): error CS0246: nope\n")
        self.assertEqual(self.unit.check().health, Health.INSTALLED)

    def test_test_without_unity_fails_plainly(self):
        with self.assertRaises(JobError):
            self.unit.run_action("test", make_job("unity_bridge", "test"))

    def test_remove_takes_the_line_out(self):
        self.env.choose(self.project)
        add_bridge(self.project)
        self.assertTrue(self.unit.check().offers("uninstall"))
        self.unit.run_action("uninstall", make_job("unity_bridge", "uninstall"))
        self.assertFalse(bridge_installed(self.project))


class McpUnitTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.addCleanup(self.t.cleanup)
        self.unit = UnityMcp(self.t.ctx)
        self.unit.port = closed_port()

    def serve(self, reply=None):
        fake = FakeUnity(reply=reply)
        self.addCleanup(fake.close)
        self.unit.port = fake.port
        return fake

    def test_the_built_in_server_has_nothing_to_install(self):
        state = self.unit.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("nothing to install", state.summary)
        self.assertEqual(state.detail("Tools"), str(len(TOOLS)))

    def test_it_is_ready_once_unity_is_connected(self):
        self.serve()
        self.assertEqual(self.unit.check().health, Health.READY)

    def test_your_own_server_is_named(self):
        self.t.ctx.settings = update_settings(self.t.ctx.settings, {"unity_mcp_command": "/opt/unity-mcp"})
        self.assertIn("/opt/unity-mcp", self.unit.check().summary)

    def test_test_lists_the_tools_without_unity(self):
        result = self.unit.run_action("test", make_job("unity_mcp", "test"))
        self.assertEqual(result, {"tools": len(TOOLS), "unity": "not running"})


if __name__ == "__main__":
    unittest.main()
