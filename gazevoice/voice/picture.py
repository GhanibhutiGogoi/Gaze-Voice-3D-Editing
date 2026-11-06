"""Describing a part of the model that has no name, from a picture, and proving the picture is cleaned up.

Gaze can tell which object you looked at and, when it has vertex groups or materials, which part. A big mesh with
neither gives the editor nothing to go on, so a picture of the spot (a crop Blender made in memory, ring drawn on it)
goes to one short-lived `claude -p` process, which says in a few words what is inside the ring and exits. Only that
sentence goes any further: the editor, the refiner and every long session never see an image, so Codex and the API
providers work as before, and nothing is ever resent on a later turn.

The picture is never written to disk by GazeVoice. It goes to the process over its stdin, as a base64 image block, and
it lives in this process's memory for as long as the call takes. Anything a model says about a picture is untrusted,
since the viewport can show any text, so `clean_description` reduces it to a short plain line before it is used.

Pictures run only while a check has passed for the installed Claude Code (`run_leftover_check`): it sends a random
picture through this same route and searches this Mac for any copy Claude Code kept. The flag that keeps nothing has
regressed before, so a new Claude Code version switches pictures off until the check passes again.
"""

from __future__ import annotations

import base64
import json
import math
import os
import stat
import struct
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..jobs import Job, JobError
from ..paths import AppPaths
from ..probes import ProbeCache
from ..runner import CommandResult, CommandRunner
from .brains import BrainError, ClaudeSession, claude_session_argv
from .gaze import Target, clean_description, one_line

PICTURE_MIN_FACES = 500  # a connected piece smaller than this is already a part you can name by its face
PICTURE_SIZE = 512  # the long side of the crop, in pixels
PICTURE_SECONDS = 20.0  # the whole picture step: the crop and the describer share this
PICTURE_MODEL = "claude-haiku-5-5"
PICTURE_PROMPT = Path(__file__).parent / "prompts" / "picture.md"
OBJECT_NAME_CHARS = 40  # as much of an object's name as the question carries (the bridge cuts labels at 40 too)
WORD_CHARS = 20

WATCH_SECONDS = 0.05  # how often the watchdog looks at the clock and at whether the command was stopped
EXIT_GRACE = 1.0  # seconds a describer that has answered gets to leave by itself before it is killed
PROBE_SECONDS = 5.0  # `claude --version` and `claude auth status` answer within this, or they count as not answering
PROBE_TTL = 60.0  # and an answer is kept this long, so an update is noticed within a minute and nothing is asked per picture
MESSAGE_CHARS = 200  # the most of anything a process said that goes into a message

IMAGE_PATTERN = "gazevoice-gaze-*.png"  # what a temp picture would be called. None is made; this is only swept

# The leftover check
CHECK_QUESTION = "What colors do you see? One short sentence."
CHECK_SIDE = 256  # the test picture is this many pixels each way, as the one that was proven by hand
CHECK_SECONDS = 90.0  # the check's one call may take this long
LITERAL_BYTES = 256  # a stretch of the picture's own noise
MIDDLE_CHARS = 64  # a stretch of its base64 from the middle
PREFIX_CHARS = 96  # and its base64 start, which reaches past the header that every picture of this size shares
SCAN_CHUNK = 1 << 20  # a file is read this much at a time
SCAN_SLACK = 2.0  # seconds before the call that a file's change time may be and still count as made by it
MAX_LISTED = 5  # copies named in the message; the rest are counted

CHECK_CONFIRM = (
    "Sends one small random picture to Claude Code, the way a real one is sent, then looks through this Mac for any copy "
    "it kept. It never touches your scene or your camera. Uses a little of your plan."
)


class PictureError(Exception):
    """A describer call didn't work. The message says why in plain words and never holds any of the picture."""


def _first_token(text: str) -> str | None:
    parts = text.split()
    return parts[0] if parts else None


def _scrub(text: str) -> str:
    """What a process said, fit for a message: one line, with anything that could be a picture cut out."""
    words = (word if len(word) < 40 else "…" for word in one_line(text, 10_000).split())
    return " ".join(words)[:MESSAGE_CHARS]


def needs_picture(target: Target) -> bool:
    """Whether this target is a big stretch of mesh with nothing to call it by.

    The piece has no `part` (no vertex group or material to name it) and the faces joined to the one looked at number
    at least `PICTURE_MIN_FACES`. Blender skips counting above 200,000 faces and says null: then the whole object is the
    best figure there is. Nothing known about the piece means no picture.
    """
    piece = target.piece or {}
    if piece.get("part"):
        return False
    faces = piece.get("piece_faces")
    if faces is None and piece.get("face") is not None:
        faces = piece.get("object_faces")
    return isinstance(faces, int) and not isinstance(faces, bool) and faces >= PICTURE_MIN_FACES


# --- asking Claude Code -----------------------------------------------------------------------------------------
def ask_about_picture(
    png_base64: str,
    question: str,
    paths: AppPaths,
    *,
    timeout: float = PICTURE_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    command: Sequence[str] = ("claude",),
    env: Mapping[str, str] | None = None,
) -> str:
    """Start one `claude -p`, send it the picture and `question`, and return its reply as it said it.

    The process is the refiner's kind of session (same working folder, same isolation, no saved session, one turn,
    the small model) and has exited, or been killed, by the time this returns. The picture goes over stdin as an image
    block beside a text block and nowhere else. A watchdog ends the process the moment `timeout` seconds have passed or
    `cancelled()` says so, wherever the call is: starting, or blocked writing a picture to a process that never reads.
    Raises PictureError, with a message that is safe to show, for every way it can fail.
    """
    if not isinstance(png_base64, str) or not png_base64:
        raise PictureError("There was no picture to send.")
    if cancelled is not None and cancelled():
        raise PictureError("It was stopped before it started.")
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_base64}},
        {"type": "text", "text": question},
    ]
    argv = [*command, *claude_session_argv(PICTURE_PROMPT, PICTURE_MODEL, None, max_turns=1)[1:]]
    over, done = threading.Event(), threading.Event()  # the watchdog's verdict, and the call being finished
    session = None
    watchdog = None
    try:
        paths.work.mkdir(parents=True, exist_ok=True)
        session = ClaudeSession(argv, cwd=paths.work, env=env)

        def watch() -> None:
            end = time.monotonic() + timeout
            while not done.wait(WATCH_SECONDS):
                if time.monotonic() >= end or (cancelled is not None and cancelled()):
                    over.set()
                    session.kill()
                    return

        watchdog = threading.Thread(target=watch, daemon=True, name="picture-watch")
        watchdog.start()
        session.start()
        if over.is_set():
            raise BrainError("it ran out of time")
        turn = session.send(blocks, timeout=timeout + 1.0)  # the watchdog is the limit; this is a second pair of eyes
    except (BrainError, OSError, ValueError) as exc:
        if over.is_set():
            stopped = cancelled is not None and cancelled()
            raise PictureError("It was stopped." if stopped else f"Claude Code took longer than {timeout:g} seconds.") from None
        raise PictureError(_scrub(str(exc)) or "Claude Code stopped without saying why.") from None
    finally:
        done.set()
        if session is not None:
            session.close(grace=EXIT_GRACE)  # a process the watchdog killed is already gone; one that answered leaves
        if watchdog is not None:
            watchdog.join(1.0)
    if turn.is_error:
        raise PictureError(f"Claude Code reported an error: {_scrub(turn.text) or 'no reason given'}")
    return turn.text


def describe_crop(
    png_base64: str,
    word: str,
    object_name: str,
    paths: AppPaths,
    *,
    timeout: float = PICTURE_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    command: Sequence[str] = ("claude",),
    env: Mapping[str, str] | None = None,
) -> str | None:
    """One short sentence saying what is inside the ring of `png_base64`, a PNG as base64, or None.

    `word` is what was said and `object_name` what it was looking at. The sentence has been through
    `clean_description`; None also means the describer said "unclear", said nothing, failed, ran out of `timeout`
    seconds or was stopped by `cancelled()`. Nothing is written to disk and nothing about why it failed is kept.
    """
    try:
        question = PICTURE_PROMPT.read_text().format(
            word=one_line(word, WORD_CHARS) or "this",
            object=one_line(object_name, OBJECT_NAME_CHARS) or "a 3D model",
        )
        reply = ask_about_picture(png_base64, question, paths, timeout=timeout, cancelled=cancelled,
                                  command=command, env=env)
    except (PictureError, OSError, KeyError, IndexError, ValueError):
        return None
    return clean_description(reply)


# --- whether pictures may be used --------------------------------------------------------------------------------
def claude_status(run: Callable[[list[str]], CommandResult]) -> tuple[str | None, bool]:
    """(the installed Claude Code's version, whether it is logged in), asked with `run`.

    (None, False) when `claude --version` doesn't answer. A Claude Code that doesn't say it is logged in isn't.
    """
    asked = run(["claude", "--version"])
    version = _first_token(asked.stdout) if asked.ok else None
    if version is None:
        return None, False
    status = run(["claude", "auth", "status"])
    try:
        method = json.loads(status.stdout).get("authMethod")
    except (ValueError, AttributeError):
        method = None
    return version, bool(status.ok and method and method != "none")


def read_picture_check(path: Path) -> dict | None:
    """The saved check, `{"claude": "2.1.303", "passed": true, "when": …}`, or None.

    None means there is no file, it can't be read, or it isn't a pass in this shape (an older file, a half-written one,
    or a failure). Either way pictures are off.
    """
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("passed") is not True:
        return None
    claude, when = data.get("claude"), data.get("when")
    if not (isinstance(claude, str) and claude):
        return None
    if isinstance(when, bool) or not isinstance(when, (int, float)) or not math.isfinite(when):
        return None
    return data


def write_picture_check(path: Path, version: str, when: float) -> None:
    """Record a pass for Claude Code `version`: the whole file or none of it, moved over the old one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gaze-picture-check-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"claude": version, "passed": True, "when": when}, handle, allow_nan=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pictures_allowed(paths: AppPaths, version: str | None) -> bool:
    """Whether the check has passed for exactly this Claude Code version. No version known means no."""
    check = read_picture_check(paths.gaze_picture_check)
    return bool(version) and check is not None and check["claude"] == version


class Describer:
    """What the voice service holds for pictures: whether they may be used right now, and how to describe one.

    It starts nothing until `describe` is called. `ready` asks `claude --version` and `claude auth status` through the
    shared probe cache, so an update to Claude Code, or a logout, is noticed within a minute.
    """

    def __init__(
        self,
        paths: AppPaths,
        runner: CommandRunner,
        probes: ProbeCache,
        command: Sequence[str] = ("claude",),
        env: Mapping[str, str] | None = None,
    ):
        self.paths, self.runner, self.probes = paths, runner, probes
        self.command = tuple(command)
        self.env = env

    def _probe(self, argv: list[str]) -> CommandResult:
        return self.probes.run(self.runner, argv, timeout=PROBE_SECONDS, ttl=PROBE_TTL)

    def ready(self) -> bool:
        version, logged_in = claude_status(self._probe)
        return logged_in and pictures_allowed(self.paths, version)

    def describe(
        self, png_base64: str, word: str, object_name: str, *, timeout: float, cancelled: Callable[[], bool] | None = None
    ) -> str | None:
        return describe_crop(png_base64, word, object_name, self.paths, timeout=timeout, cancelled=cancelled,
                             command=self.command, env=self.env)


# --- leftovers ---------------------------------------------------------------------------------------------------
def clear_leftover_images(paths: AppPaths) -> int:
    """Delete any `gazevoice-gaze-*.png` in `paths.gaze_images`, and say how many went.

    GazeVoice writes no such file, so this is a safety net for a build that did, or a crash. A link with such a name is
    removed, never what it points at. Called whenever voice starts.
    """
    folder = paths.gaze_images
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not fnmatch(name, IMAGE_PATTERN):
            continue
        try:
            (folder / name).unlink()
        except OSError:  # a folder with such a name, or a file that won't go: not ours to force
            continue
        removed += 1
    return removed


def leftover_roots(paths: AppPaths) -> list[Path]:
    """Where Claude Code or a library under it could keep a copy: its folder, caches, logs, temp folders and ours."""
    home = Path.home()
    places = [
        home / ".claude", home / ".claude.json", home / ".local", home / "Library" / "Application Support",
        home / "Library" / "Caches", home / "Library" / "Logs", Path(tempfile.gettempdir()), Path("/tmp"), paths.root,
    ]
    if os.environ.get("CLAUDE_CODE_TMPDIR"):
        places.append(Path(os.environ["CLAUDE_CODE_TMPDIR"]))
    return list(dict.fromkeys(places))


def _files_since(root: Path, since: float, shortest: int):
    """Regular files under `root` changed at or after `since` and at least `shortest` bytes long, as (path, id).

    Symlinks are never followed, so nothing outside the root is read through a link inside it. The root itself may be
    a link (a home folder set up that way) or a single file; it is resolved once, and what is found is named under its
    real path.
    """
    real = os.path.realpath(root)
    walk = [(os.path.dirname(real), [], [os.path.basename(real)])] if os.path.isfile(real) else os.walk(real)
    for folder, _dirs, names in walk:
        for name in names:
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= since and info.st_size >= shortest:
                yield path, (info.st_dev, info.st_ino)


def _holds(path: str, markers: Sequence[bytes], keep: int) -> bool:
    """Whether the file has any of `markers`, read a chunk at a time, stopping at the first one found."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    tail = b""
    with os.fdopen(fd, "rb") as handle:
        try:
            while True:
                chunk = handle.read(SCAN_CHUNK)
                if not chunk:
                    return False
                data = tail + chunk
                if any(marker in data for marker in markers):
                    return True
                tail = data[-keep:] if keep else b""
        except OSError:
            return False


def _scan(markers: Sequence[bytes], since: float, roots: Sequence[Path]) -> list[Path]:
    """Files under `roots` that hold any of `markers`.

    A file that hasn't changed since `since` can't hold a picture sent after it, so it isn't opened. Nor is one shorter
    than the shortest marker, or anything that isn't a plain file, or a link. The rest are read in chunks, a file
    stopping at its first hit. Each file is looked at once, however many roots reach it.
    """
    shortest, keep = min(len(m) for m in markers), max(len(m) for m in markers) - 1
    found, seen, scanned = [], set(), []
    for root in roots:
        real = os.path.realpath(root)
        if any(real == done or real.startswith(done + os.sep) for done in scanned):  # inside a root already done
            continue
        scanned.append(real)
        for path, identity in _files_since(Path(root), since, shortest):
            if identity in seen:
                continue
            seen.add(identity)
            if _holds(path, markers, keep):
                found.append(Path(path))
    return sorted(found)


def find_leftovers(marker: bytes, since: float, roots: Sequence[Path]) -> list[Path]:
    """The files under `roots` (folders, or single files) that hold `marker` and were changed at or after `since`.

    `since` and the files' change times are `time.time()` seconds. Links are not followed, and a file that couldn't be
    changed since then is not even opened.
    """
    return _scan([marker], since, roots)


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


def run_leftover_check(
    job: Job,
    paths: AppPaths,
    *,
    runner: CommandRunner | None = None,
    command: Sequence[str] = ("claude",),
    env: Mapping[str, str] | None = None,
    roots: Sequence[Path] | None = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Send a random picture through the real describer route, then search this Mac for any copy of it.

    Passes only if the call worked and nothing was found; then it saves `state/gaze-picture-check.json` for the
    installed Claude Code version. A failed call saves nothing and leaves an earlier pass alone. A copy that is found
    is reported with its path and left where it is, because it may be Claude Code's own file; it also cancels an earlier
    pass, since pictures are no longer known to be cleaned up. Raises JobError, with a plain message, for every way
    the check can fail.
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
        reply = ask_about_picture(picture.base64, CHECK_QUESTION, paths, timeout=CHECK_SECONDS, command=command, env=env)
    except PictureError as exc:
        raise JobError(f"The test picture didn't go through. {exc} No pass was recorded, so pictures stay off.") from None
    seconds = round(time.monotonic() - began, 1)
    job.log(f"Claude Code answered in {seconds} s: {clean_description(reply) or '(nothing to repeat)'}")
    places = list(roots) if roots is not None else leftover_roots(paths)
    job.log(f"Looking for a copy of the picture in {len(places)} places on this Mac…")
    found = _scan(picture.markers, since, places)
    if found:
        paths.gaze_picture_check.unlink(missing_ok=True)
        listed = ", ".join(str(path) for path in found[:MAX_LISTED])
        more = f" and {len(found) - MAX_LISTED} more" if len(found) > MAX_LISTED else ""
        job.log(f"Found a copy: {listed}{more}")
        raise JobError(
            f"Claude Code kept a copy of the test picture: {listed}{more}. GazeVoice left it where it is, because it may "
            "be Claude Code's own file. No pass was recorded, so pictures stay off until it's gone and this check passes.")
    write_picture_check(paths.gaze_picture_check, version, clock())
    job.log(f"No copy found. Pictures can be used while Claude Code is at {version}.")
    return {"passed": True, "claude": version, "seconds": seconds}
