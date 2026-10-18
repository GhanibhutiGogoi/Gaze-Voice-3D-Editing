import socket
import sys
import unittest
import zipfile
from pathlib import Path

from gazevoice.components.base import Health
from gazevoice.components.blender import extensions_dir
from gazevoice.components.blender_mcp import (
    ADDON_ZIP_URL,
    SERVER_MCPB_URL,
    McpAddon,
    McpServer,
    server_command,
)
from gazevoice.jobs import JobError

from .helpers import TempCtx, make_job, with_blender

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
UV = {"uv": "/Users/me/.local/bin/uv"}


class Listener:
    """Something listening on a local port, like Blender's MCP add-on."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]

    def close(self):
        self.sock.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_downloader(payload_builder):
    calls = []

    def fake(url, dest, job):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload_builder(dest)
        return dest

    fake.calls = calls
    return fake


def bundle(files):
    def build(dest):
        with zipfile.ZipFile(dest, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
    return build


class McpAddonTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.addon = McpAddon(self.t.ctx)
        self.addon.candidates = [self.t.root / "Blender.app"]
        self.addon.home = self.t.root / "home"
        self.addon.port = free_port()

    def tearDown(self):
        self.t.cleanup()

    def ext_dir(self):
        return extensions_dir((5, 2, 2), home=self.addon.home) / "mcp"

    def test_needs_blender(self):
        state = self.addon.check()
        self.assertEqual((state.health, state.actions), (Health.MISSING, ()))
        self.assertIn("Install Blender first", state.summary)

    def test_not_installed(self):
        with_blender(self.t)
        state = self.addon.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_installed_not_listening(self):
        with_blender(self.t)
        self.ext_dir().mkdir(parents=True)
        state = self.addon.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertIn("online access", state.summary)

    def test_ready_when_listening(self):
        with_blender(self.t, running=True)
        self.ext_dir().mkdir(parents=True)
        listener = Listener()
        self.addon.port = listener.port
        try:
            state = self.addon.check()
        finally:
            listener.close()
        self.assertEqual(state.health, Health.READY)
        self.assertIn("Listening", state.summary)

    def test_install_downloads_official_zip_and_installs(self):
        binary = with_blender(self.t)
        self.addon.download = fake_downloader(bundle({"blender_manifest.toml": 'id = "mcp"'}))
        zip_path = self.t.paths.downloads / "mcp-1.0.3.zip"
        argv = [str(binary), "--command", "extension", "install-file", "-r", "user_default", "-e", str(zip_path)]
        self.t.runner.add(argv)
        self.addon.run_action("install", make_job("mcp_addon", "install"))
        self.assertEqual(self.addon.download.calls, [ADDON_ZIP_URL])
        self.assertIn(tuple(argv), self.t.runner.calls)


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx(which=UV)
        self.server = McpServer(self.t.ctx)
        self.server.candidates = [self.t.root / "Blender.app"]
        self.server.addon_port = free_port()

    def tearDown(self):
        self.t.cleanup()

    def test_needs_uv(self):
        self.t.runner.set_which("uv", None)
        state = self.server.check()
        self.assertEqual((state.health, state.actions), (Health.ERROR, ()))
        self.assertIn("uv", state.summary)

    def test_not_installed_offers_install_with_consent(self):
        state = self.server.check()
        self.assertEqual(state.health, Health.MISSING)
        install = next(a for a in state.actions if a.id == "install")
        self.assertIsNotNone(install.confirm)

    def mark_installed(self, synced=True):
        folder = self.t.paths.mcp_server_dir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "pyproject.toml").write_text("[project]\nname='blender-mcp'\n")
        if synced:
            (folder / ".venv" / "bin").mkdir(parents=True)
            (folder / ".venv" / "bin" / "python").write_text("")

    def test_installed_waiting_for_addon(self):
        self.mark_installed()
        state = self.server.check()
        self.assertEqual(state.health, Health.INSTALLED)
        self.assertTrue(state.offers("test"))

    def test_unpacked_but_never_synced_offers_install_again(self):
        self.mark_installed(synced=False)
        state = self.server.check()
        self.assertEqual(state.health, Health.MISSING)
        self.assertTrue(state.offers("install"))

    def test_failed_sync_leaves_install_offered(self):
        self.server.download = fake_downloader(bundle({"pyproject.toml": "[project]", "blmcp/__init__.py": ""}))
        target = str(self.t.paths.mcp_server_dir)
        self.t.runner.add(["uv", "sync", "--python", "3.12", "--directory", target], returncode=1, stdout="network\n")
        with self.assertRaises(JobError):
            self.server.run_action("install", make_job("mcp_server", "install"))
        self.assertTrue(self.server.check().offers("install"))

    def test_ready_when_addon_listening(self):
        self.mark_installed()
        listener = Listener()
        self.server.addon_port = listener.port
        try:
            self.assertEqual(self.server.check().health, Health.READY)
        finally:
            listener.close()

    def test_install_extracts_bundle_and_syncs(self):
        self.server.download = fake_downloader(
            bundle({"pyproject.toml": "[project]", "manifest.json": "{}", "blmcp/__init__.py": ""})
        )
        target = str(self.t.paths.mcp_server_dir)
        self.t.runner.add(["uv", "sync", "--python", "3.12", "--directory", target], stdout="Resolved 30 packages\n")
        self.server.run_action("install", make_job("mcp_server", "install"))
        self.assertEqual(self.server.download.calls, [SERVER_MCPB_URL])
        self.assertTrue((self.t.paths.mcp_server_dir / "blmcp" / "__init__.py").is_file())

    def test_install_handles_single_top_folder(self):
        self.server.download = fake_downloader(bundle({"blender-1.0.3/pyproject.toml": "", "blender-1.0.3/blmcp/x.py": ""}))
        target = str(self.t.paths.mcp_server_dir)
        self.t.runner.add(["uv", "sync", "--python", "3.12", "--directory", target])
        self.server.run_action("install", make_job("mcp_server", "install"))
        self.assertTrue((self.t.paths.mcp_server_dir / "pyproject.toml").is_file())

    def test_install_rejects_unexpected_bundle(self):
        self.server.download = fake_downloader(bundle({"README.md": "hi"}))
        with self.assertRaises(JobError) as caught:
            self.server.run_action("install", make_job("mcp_server", "install"))
        self.assertIn("Unexpected bundle layout", str(caught.exception))
        self.assertFalse((self.t.paths.mcp_server_dir / "README.md").exists())

    def test_install_refuses_zip_slip(self):
        self.server.download = fake_downloader(bundle({"../evil.txt": "x", "pyproject.toml": ""}))
        with self.assertRaises(JobError):
            self.server.run_action("install", make_job("mcp_server", "install"))
        self.assertFalse((self.t.paths.root / "evil.txt").exists())

    def test_test_action_lists_tools(self):
        self.mark_installed()
        self.server.command_override = {"command": sys.executable, "args": [FAKE_SERVER], "env": {}}
        result = self.server.run_action("test", make_job("mcp_server", "test"))
        self.assertEqual(result, {"tools": 3, "blender": "not running"})

    def test_test_action_reaches_blender_when_listening(self):
        self.mark_installed()
        self.server.command_override = {"command": sys.executable, "args": [FAKE_SERVER], "env": {}}
        listener = Listener()
        self.server.addon_port = listener.port
        try:
            result = self.server.run_action("test", make_job("mcp_server", "test"))
        finally:
            listener.close()
        self.assertEqual(result["blender"], "connected")

    def test_server_command_shape(self):
        cmd = server_command(self.t.paths, Path("/Applications/Blender.app/Contents/MacOS/Blender"))
        self.assertEqual(cmd["command"], "uv")
        self.assertEqual(cmd["args"], ["run", "--directory", str(self.t.paths.mcp_server_dir), "blender-mcp"])
        self.assertEqual(cmd["env"], {"BLENDER_PATH": "/Applications/Blender.app/Contents/MacOS/Blender"})
        self.assertEqual(server_command(self.t.paths, None)["env"], {})


if __name__ == "__main__":
    unittest.main()
