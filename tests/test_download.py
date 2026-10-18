import io
import unittest
import urllib.error

from gazevoice.download import download
from gazevoice.jobs import JobError

from .helpers import TempCtx, make_job


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.t = TempCtx()
        self.dest = self.t.paths.downloads / "mcp-1.0.3.zip"

    def tearDown(self):
        self.t.cleanup()

    def test_writes_file_and_logs_progress(self):
        seen = {}

        def opener(request, timeout):
            seen["ua"] = request.get_header("User-agent")
            return FakeResponse(b"x" * 200_000)

        job = make_job()
        path = download("https://example.invalid/mcp.zip", self.dest, job, opener=opener)
        self.assertEqual(path.read_bytes(), b"x" * 200_000)
        self.assertFalse(self.dest.with_name(self.dest.name + ".part").exists())
        self.assertTrue(seen["ua"].startswith("GazeVoice/"))
        self.assertTrue(any("100%" in line for line in job.lines))

    def test_http_error_leaves_no_file(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with self.assertRaises(JobError) as caught:
            download("https://example.invalid/mcp.zip", self.dest, make_job(), opener=opener)
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name(self.dest.name + ".part").exists())

    def test_offline_is_a_readable_error(self):
        def opener(request, timeout):
            raise urllib.error.URLError("nodename nor servname provided")

        with self.assertRaises(JobError) as caught:
            download("https://example.invalid/mcp.zip", self.dest, make_job(), opener=opener)
        self.assertIn("Couldn't download", str(caught.exception))

    def test_interrupted_transfer_cleans_part_file(self):
        class Broken(FakeResponse):
            def read(self, n=-1):
                raise TimeoutError("read timed out")

        with self.assertRaises(JobError):
            download("https://example.invalid/mcp.zip", self.dest, make_job(), opener=lambda r, timeout: Broken(b"abc"))
        self.assertFalse(self.dest.with_name(self.dest.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
