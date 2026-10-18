"""Download a file with progress lines; never leave a half-written file behind."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .jobs import Job, JobError

CHUNK = 1 << 16


def download(url: str, dest: Path, job: Job, opener: Callable = urllib.request.urlopen) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": f"GazeVoice/{__version__}"})
    job.log(f"Downloading {url}")
    done = 0
    try:
        with opener(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            next_mark = 10
            with open(part, "wb") as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    done += len(block)
                    if total:
                        while next_mark <= 100 and done * 100 >= next_mark * total:
                            job.log(f"  {next_mark}% of {total / 1e6:.1f} MB")
                            next_mark += 10
        os.replace(part, dest)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        exc.close()  # an HTTPError is also the error response; release its connection
        raise JobError(f"Download failed: HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise JobError(f"Couldn't download {url}: {getattr(exc, 'reason', exc)}") from exc
    job.log(f"Saved {dest.name} ({done / 1e6:.2f} MB)")
    return dest
