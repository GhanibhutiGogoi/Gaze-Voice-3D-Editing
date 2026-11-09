"""The check that a picture sent to Claude Code is cleaned up: send a random one, then search this Mac for any copy.

The picture goes through the same route as a real one (`ask_about_picture`), the process is allowed to finish and exit,
and then the places a copy could be kept are searched for three fingerprints of it: a stretch of its noise as the
bytes of a PNG, a stretch of its base64 from the middle, and where its base64 starts. A pass is saved with the version
of Claude Code it was for, and pictures work only while that matches the installed one.

What the search can and can't see, so a pass is not read as more than it is:
- It finds a copy stored as those bytes, or as that base64 text on one unbroken line. It can't find base64 that was
  wrapped over several lines or had characters escaped (`\\/` for `/`), a compressed copy, or a lossy re-encode such
  as a JPEG or a resized picture.
- It looks only where `leftover_roots` says, and only at files whose contents or place changed since the picture was
  sent.
- It says "couldn't look" instead of "found nothing" when it can't read what it has to: a place that must be searched
  (Claude Code's folder, the temp folder, the app's folder) that is missing or can't be listed, whoever owns it; a file
  in a strict place that changed since and can't be read, if it belongs to this user or its owner can't be told; and a
  folder there that belongs to this user, can't be listed, and changed since (it could hold a new file).
- It only counts, and says how many in the log, what it can't read and can't need to: other users' files, folders
  macOS itself refuses to list ("Operation not permitted", of which the temp folder holds a few for system services
  as well as `~/Library`), unreadable folders that haven't changed since, and anything unreadable in the wider places
  (`~/Library`, `~/.local`, the rest of the per-user temp folder). A copy kept in one of those is not found.
"""

from __future__ import annotations

import base64
import errno
import os
import stat
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..jobs import Job, JobError
from ..paths import AppPaths
from ..runner import CommandRunner
from .gaze import clean_description
from .picture import PROBE_SECONDS, PictureError, ask_about_picture, claude_status, pictures_allowed, write_picture_check

CHECK_QUESTION = "What colors do you see? One short sentence."
CHECK_SIDE = 256  # the test picture is this many pixels each way, as the one that was proven by hand
CHECK_SECONDS = 90.0  # the check's one call may take this long
LITERAL_BYTES = 256  # a stretch of the picture's own noise
MIDDLE_CHARS = 64  # a stretch of its base64 from the middle
PREFIX_CHARS = 96  # and its base64 start, which reaches past the header that every picture of this size shares
SCAN_CHUNK = 1 << 20  # a file is read this much at a time
SCAN_SLACK = 2.0  # seconds before the call that a file's change time may be and still count as made by it
MAX_LISTED = 5  # copies and trouble spots named in a message; the rest are counted
_VANISHED = (errno.ENOENT, errno.ELOOP)  # a file that went away, or became a link, while it was being looked at

CHECK_CONFIRM = (
    "Sends one small random picture to Claude Code, the way a real one is sent, then looks through this Mac for any copy "
    "it kept. It never touches your scene or your camera. Uses a little of your plan."
)


@dataclass(frozen=True)
class Place:
    """Somewhere to search. A `strict` place has to be read in full: a file or folder in it that belongs to this user
    and can't be read means the search can't be trusted. A `required` place also has to exist. Anything else is looked
    through as far as it lets us, and what it didn't let us read is counted."""

    path: Path
    strict: bool = False
    required: bool = False


@dataclass
class Scan:
    found: list[Path] = field(default_factory=list)  # files that hold a fingerprint
    failed: dict[str, str] = field(default_factory=dict)  # where a strict place couldn't be read, and why
    skipped: set[str] = field(default_factory=set)  # what else couldn't be read: other users' files, protected folders


def leftover_roots(paths: AppPaths) -> list[Place]:
    """Where Claude Code, or a library under it, could keep a copy.

    Strict: Claude Code's folder (`$CLAUDE_CONFIG_DIR`, else `~/.claude`) and its config file, the temp folder
    (and `$CLAUDE_CODE_TMPDIR`), `/tmp` and the app's own folder. Searched as far as they allow: `~/.local`, the
    Application Support, Caches, Logs and Preferences folders in `~/Library`, and the whole per-user folder that
    holds the temp folder (`/var/folders/<x>/<y>`).
    """
    home, library, temp = Path.home(), Path.home() / "Library", Path(tempfile.gettempdir())
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    places = [Place(Path(configured).expanduser(), strict=True, required=True)] if configured else []
    places.append(Place(home / ".claude", strict=True, required=not configured))
    places += [Place(home / ".claude.json", strict=True), Place(temp, strict=True, required=True),
               Place(Path("/tmp"), strict=True), Place(paths.root, strict=True, required=True)]
    if os.environ.get("CLAUDE_CODE_TMPDIR"):
        places.append(Place(Path(os.environ["CLAUDE_CODE_TMPDIR"]), strict=True, required=True))
    wide = [home / ".local", library / "Application Support", library / "Caches", library / "Logs",
            library / "Preferences"]
    if temp.name == "T":  # /var/folders/<x>/<y>/T: its parent holds the rest of this user's temp and cache folders
        wide.append(temp.parent)
    places += [Place(path) for path in wide]
    merged: dict[Path, Place] = {}  # each place once, as strict as any of its mentions
    for place in places:
        old = merged.get(place.path)
        merged[place.path] = place if old is None else Place(place.path, old.strict or place.strict,
                                                              old.required or place.required)
    return list(merged.values())


class _Judge:
    """What the search knows about every place it was asked to look through, however they are nested.

    How strictly a path is judged depends on the places it is in, not on which place's walk happened to find it, so a
    place that must be searched still counts as one when it sits inside another place that was searched first.
    """

    def __init__(self, places: Sequence[Place]):
        self.places: dict[str, tuple[bool, bool]] = {}  # real path -> (strict, required)
        for place in places:
            real = os.path.realpath(place.path)
            strict, required = self.places.get(real, (False, False))
            self.places[real] = (strict or place.strict or place.required, required or place.required)

    def strict(self, path: str) -> bool:
        return any(strict and (path == real or path.startswith(real + os.sep)) for real, (strict, _) in self.places.items())

    def required(self, path: str) -> bool:
        return self.places.get(path, (False, False))[1]

    def is_place(self, path: str) -> bool:
        return path in self.places


def _could_be_mine(path: str) -> bool:
    """Whether this path belongs to this user. When that can't be told, it might: an unknown is not an "ignore"."""
    try:
        return os.lstat(path).st_uid == os.getuid()
    except OSError:
        return True


def _changed_since(path: str, since: float) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True  # can't tell, so it might have
    return max(info.st_mtime, info.st_ctime) >= since


def _note(scan: Scan, judge: _Judge, path: str, exc: OSError, since: float, folder: bool = False) -> None:
    """Record something that couldn't be read.

    It is a failure when it is a place that must be searched (whoever owns it); or, in a strict place, when it belongs
    to this user or can't be told: a file (only files that changed since the picture was sent are ever opened), a place
    itself, or a folder inside one that changed since, which could hold a new file. A folder inside that macOS itself
    refuses to list ("Operation not permitted": the temp folder holds a few for system services, closed to their owner
    too) only counts. So does anything else, anywhere else.
    """
    if exc.errno in _VANISHED:
        return  # gone by the time we got there: there is nothing in it to read
    failed = judge.required(path)
    if not failed and judge.strict(path) and _could_be_mine(path):
        inside = folder and not judge.is_place(path)  # a folder that is not itself one of the places
        failed = not inside or (exc.errno != errno.EPERM and _changed_since(path, since))
    if failed:
        scan.failed.setdefault(path, exc.strerror or "couldn't be read")
        scan.skipped.discard(path)
    elif path not in scan.failed:
        scan.skipped.add(path)


def _files_since(root: Path, since: float, shortest: int, scan: Scan, judge: _Judge):
    """Regular files under `root` changed at or after `since` and at least `shortest` bytes long, as (path, id).

    Symlinks are never followed, so nothing outside the root is read through a link inside it. The root itself may be
    a link (a home folder set up that way) or a single file; it is resolved once, and what is found is named under its
    real path. What can't be listed or looked at is noted in `scan`: an entry that can't even be looked at (its folder
    can be listed but not entered) is put down to its folder.
    """
    real = os.path.realpath(root)
    single = os.path.isfile(real)
    if single:
        walk = [(os.path.dirname(real), [], [os.path.basename(real)])]
    else:
        walk = os.walk(real, onerror=lambda exc: _note(scan, judge, exc.filename or real, exc, since, folder=True))
    for folder, _dirs, names in walk:
        for name in names:
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError as exc:
                _note(scan, judge, path if single else folder, exc, since, folder=not single)
                continue
            # A file counts as changed when its contents or its place changed (a rename keeps the old modified time).
            if stat.S_ISREG(info.st_mode) and max(info.st_mtime, info.st_ctime) >= since and info.st_size >= shortest:
                yield path, (info.st_dev, info.st_ino)


def _holds(path: str, markers: Sequence[bytes], keep: int) -> bool:
    """Whether the file has any of `markers`, read a chunk at a time, stopping at the first one found.

    Raises OSError if the file can't be opened or read.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    tail = b""
    with os.fdopen(fd, "rb") as handle:
        while True:
            chunk = handle.read(SCAN_CHUNK)
            if not chunk:
                return False
            data = tail + chunk
            if any(marker in data for marker in markers):
                return True
            tail = data[-keep:] if keep else b""


def search(markers: Sequence[bytes], since: float, places: Sequence[Place | Path]) -> Scan:
    """Search `places` for any of `markers`, and note what couldn't be read.

    A file that hasn't changed since `since` can't hold a picture sent after it, so it isn't opened. Nor is one shorter
    than the shortest marker, or anything that isn't a plain file, or a link. The rest are read in chunks, a file
    stopping at its first hit. Each file is looked at once, however many places reach it. Places that must be read go
    first, so one inside another is searched, and judged, as strictly as it should be.
    """
    places = [place if isinstance(place, Place) else Place(Path(place)) for place in places]
    places.sort(key=lambda place: not (place.strict or place.required))
    judge = _Judge(places)
    shortest, keep = min(len(m) for m in markers), max(len(m) for m in markers) - 1
    scan, seen, scanned = Scan(), set(), []
    for place in places:
        real = os.path.realpath(place.path)
        if place.required and not os.path.exists(real):
            scan.failed[str(place.path)] = "it isn't there"
    for place in places:
        real = os.path.realpath(place.path)
        if any(real == done or real.startswith(done + os.sep) for done in scanned):  # inside a place already done
            continue
        scanned.append(real)
        for path, identity in _files_since(place.path, since, shortest, scan, judge):
            if identity in seen:
                continue
            seen.add(identity)
            try:
                if _holds(path, markers, keep):
                    scan.found.append(Path(path))
            except OSError as exc:
                _note(scan, judge, path, exc, since)
    scan.found.sort()
    return scan


def find_leftovers(marker: bytes, since: float, roots: Sequence[Place | Path]) -> list[Path]:
    """The files under `roots` (folders, or single files) that hold `marker` and were changed at or after `since`.

    `since` and the files' change times are `time.time()` seconds. Links are not followed, and a file that couldn't be
    changed since then is not even opened. What couldn't be read is not reported here (see `run_leftover_check`).
    """
    return search([marker], since, roots).found


# --- the check ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NoisePicture:
    """A random PNG made in memory. `markers` are bytes of it that would turn up in any copy a program kept."""

    png: bytes
    base64: str
    markers: list[bytes]  # a stretch of its noise, a stretch of its base64 from the middle, and where its base64 starts


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def make_noise_picture(side: int = CHECK_SIDE) -> NoisePicture:
    """A `side` by `side` picture of random noise. It can't be anyone else's, and is stored uncompressed so that a copy
    holds its noise as it is."""
    rows = b"".join(b"\x00" + os.urandom(3 * side) for _ in range(side))  # every row starts with the "no filter" byte
    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows, 0))
           + _png_chunk(b"IEND", b""))
    text = base64.b64encode(png)
    middle = (len(text) // 2) // 4 * 4  # a start that is a whole number of base64 groups, so any copy of the text holds it
    markers = [rows[1000:1000 + LITERAL_BYTES], text[middle:middle + MIDDLE_CHARS], text[:PREFIX_CHARS]]
    return NoisePicture(png, text.decode("ascii"), markers)


def _listed(items: Sequence[str]) -> str:
    more = f" and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more


def _no_pass(paths: AppPaths, version: str, removal_failed: bool = False) -> str:
    """What to tell the person about pictures after a check that recorded no pass, true of the file as it is now.

    An earlier pass for this version is only taken away when a copy is found, and may be impossible to take away,
    so "pictures stay off" would not always be true.
    """
    if not pictures_allowed(paths, version):
        return "No pass was recorded, so pictures stay off."
    if removal_failed:
        return ("No pass was recorded, but the earlier pass for this Claude Code version couldn't be removed, so pictures "
                "are still on. Delete state/gaze-picture-check.json yourself to turn them off.")
    return "No pass was recorded. The earlier pass for this Claude Code version still stands, so pictures are still on."


def run_leftover_check(
    job: Job,
    paths: AppPaths,
    *,
    runner: CommandRunner | None = None,
    command: Sequence[str] = ("claude",),
    env: Mapping[str, str] | None = None,
    roots: Sequence[Place | Path] | None = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Send a random picture through the real describer route, then search this Mac for any copy of it.

    Passes only if the call worked, the process has exited, nothing was found, and every place that must be searched
    could be; then it saves `state/gaze-picture-check.json` for the installed Claude Code version. A failed call, or a
    search that couldn't look, saves nothing and leaves an earlier pass alone. A copy that is found is reported with its
    path and left where it is, because it may be Claude Code's own file; it also cancels an earlier pass, since
    pictures are no longer known to be cleaned up. Raises JobError, with a plain message, for every way the check can
    fail.
    """
    runner = runner or CommandRunner()
    version, logged_in = claude_status(lambda argv: runner.run(argv, timeout=PROBE_SECONDS * 3))
    if version is None:
        raise JobError("Claude Code isn't installed, or it didn't answer. Install it (claude.com/code), log in once, "
                       "then check again.")
    if not logged_in:
        raise JobError("Claude Code isn't logged in. Run `claude auth login` in Terminal, then check again.")
    picture = make_noise_picture()
    job.log(f"Sending one random test picture to Claude Code {version}, the way a real one is sent "
            "(uses a little of your plan)…")
    since, began = clock() - SCAN_SLACK, time.monotonic()
    try:
        # Claude Code tidies up as it exits, so the search waits until it has.
        reply = ask_about_picture(picture.base64, CHECK_QUESTION, paths, timeout=CHECK_SECONDS, command=command,
                                  env=env, wait_for_exit=True)
    except PictureError as exc:
        raise JobError(f"The test picture didn't go through. {exc} {_no_pass(paths, version)}") from None
    seconds = round(time.monotonic() - began, 1)
    job.log(f"Claude Code answered in {seconds} s: {clean_description(reply) or '(nothing to repeat)'}")
    places = list(roots) if roots is not None else leftover_roots(paths)
    job.log(f"Looking for a copy of the picture in {len(places)} places on this Mac…")
    scan = search(picture.markers, since, places)
    if scan.skipped:
        job.log(f"{len(scan.skipped)} files or folders in other users' space or in places macOS keeps private couldn't "
                "be read. They don't count against the check.")
    if scan.found:
        listed = _listed([str(path) for path in scan.found])
        job.log(f"Found a copy: {listed}")  # said before anything else is done, so no later error can lose it
        removal_failed = False
        try:
            paths.gaze_picture_check.unlink(missing_ok=True)
        except OSError:
            removal_failed = True
            job.log("The saved check couldn't be removed. Delete state/gaze-picture-check.json yourself.")
        raise JobError(
            f"Claude Code kept a copy of the test picture: {listed}. GazeVoice left it where it is, because it may be "
            f"Claude Code's own file. {_no_pass(paths, version, removal_failed)} Deleting that file won't change the "
            "result by itself: every check sends a new picture and passes only if Claude Code keeps no copy of that "
            "one. If a new check still finds a copy, leave pictures off.")
    if scan.failed:
        listed = _listed([f"{path} ({why})" for path, why in scan.failed.items()])
        job.log(f"Couldn't look through: {listed}")
        raise JobError(
            f"The check couldn't look everywhere it has to, so it can't say the picture was cleaned up: {listed}. "
            f"{_no_pass(paths, version)} Fix what is in the way (a folder that is missing or locked, or a file you can't "
            "read) and check again.")
    write_picture_check(paths.gaze_picture_check, version, clock())
    job.log(f"No copy found. Pictures can be used while Claude Code is at {version}.")
    return {"passed": True, "claude": version, "seconds": seconds}
