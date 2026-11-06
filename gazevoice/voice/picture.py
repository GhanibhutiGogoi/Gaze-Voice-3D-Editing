"""Describing a part of the model that has no name, from a picture.

Gaze can tell which object you looked at and, when it has vertex groups or materials, which part. A big mesh with
neither gives the editor nothing to go on, so a picture of the spot (a crop Blender made in memory, ring drawn on it)
goes to one short-lived `claude -p` process, which says in a few words what is inside the ring and exits. Only that
sentence goes any further: the editor, the refiner and every long session never see an image, so Codex and the API
providers work as before, and nothing is ever resent on a later turn.

The picture is never written to disk by GazeVoice. It goes to the process over its stdin, as a base64 image block, and
it lives in this process's memory for as long as the call takes. Anything a model says about a picture is untrusted,
since the viewport can show any text, so `clean_description` reduces it to a short plain line before it is used.

Pictures run only while a check has passed for the installed Claude Code (see leftovers.py): it sends a random picture
through this same route and searches this Mac for any copy Claude Code kept. The flag that keeps nothing has regressed
before, so a new Claude Code version switches pictures off until the check passes again.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..paths import AppPaths
from ..runner import CommandResult, CommandRunner
from .brains import BrainError, ClaudeSession, claude_session_argv
from .gaze import Target, clean_description, one_line

PICTURE_MIN_FACES = 500  # a connected piece smaller than this is already a part you can name by its face
PICTURE_SIZE = 512  # the long side of the crop, in pixels
PICTURE_SECONDS = 20.0  # the whole picture step: the crop and the describer share this
PICTURE_MODEL = "claude-haiku-5-5"
PICTURE_RULES = Path(__file__).parent / "prompts" / "picture_rules.md"  # the describer's fixed system prompt
PICTURE_PROMPT = Path(__file__).parent / "prompts" / "picture.md"  # the question about one picture, with blanks to fill
OBJECT_NAME_CHARS = 40  # as much of an object's name as the question carries (the bridge cuts labels at 40 too)
WORD_CHARS = 20

WATCH_SECONDS = 0.05  # how often the watchdog looks at the clock and at whether the command was stopped
EXIT_SECONDS = 10.0  # a describer that has answered gets this long to leave by itself, as Claude Code tidies up as it exits
TERM_SECONDS = 2.0  # then it is asked to stop, and killed this much later
PROBE_SECONDS = 5.0  # `claude --version` and `claude auth status` answer within this, or they count as not answering
PROBE_TTL = 60.0  # readiness is looked up again when the last answer is older than this...
STALE_AFTER = 3  # ...and an answer older than this many times as long is not believed, so a lookup that hangs means "no"
MESSAGE_CHARS = 200  # the most of anything a process said that goes into a message

IMAGE_PATTERN = "gazevoice-gaze-*.png"  # what a temp picture would be called. None is made; this is only swept


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
    wait_for_exit: bool = False,
) -> str:
    """Start one `claude -p`, send it the picture and `question`, and return its reply as it said it.

    The process is the refiner's kind of session (same working folder, same isolation, no saved session, one turn,
    the small model, here with the describer's own fixed rules). The picture goes over stdin as an image block beside a
    text block and nowhere else. A watchdog kills the process the moment `timeout` seconds have passed or `cancelled()`
    says so, wherever the call is: starting, or blocked writing a picture to a process that never reads. Any other
    failure kills it too, and in all of those it is gone by the time this returns.

    After a reply the process is not killed: its input is closed and it is left to exit by itself, which Claude Code
    does in its own time, tidying up as it goes. That wait happens on another thread so the edit isn't held up
    (`release`): it ends in SIGTERM after EXIT_SECONDS and SIGKILL TERM_SECONDS after that. With `wait_for_exit` this
    waits for it, which is what the leftover check needs before it looks for files.
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
    argv = [*command, *claude_session_argv(PICTURE_RULES, PICTURE_MODEL, None, max_turns=1)[1:]]
    over, done = threading.Event(), threading.Event()  # the watchdog's verdict, and the call being finished
    session = None
    watchdog = None
    leaving = None
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
        leaving = session.release(EXIT_SECONDS, TERM_SECONDS)
    except (BrainError, OSError, ValueError) as exc:
        if over.is_set():
            stopped = cancelled is not None and cancelled()
            raise PictureError("It was stopped." if stopped else f"Claude Code took longer than {timeout:g} seconds.") from None
        raise PictureError(_scrub(str(exc)) or "Claude Code stopped without saying why.") from None
    finally:
        done.set()
        if leaving is None and session is not None:  # it didn't answer: end it, and wait until it has gone
            session.kill()
            session.close()
        if watchdog is not None:
            watchdog.join(1.0)
    if wait_for_exit:
        leaving.join()
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

    `ready` never waits. Whether Claude Code is logged in, and which version it is, come from `claude auth status` and
    `claude --version`, which can take seconds on a busy Mac or hang; a thread asks them (`refresh`, which voice calls
    when it starts and at every key press, so that the answer is up to date by the time a command needs it however long
    it has been since the last one, and which `ready` calls again whenever the last answer is older than PROBE_TTL)
    and `ready` reads only the last answer. One lookup runs at a time, so a hung `claude` costs one thread, not one per command. No answer
    yet, or one older than STALE_AFTER times PROBE_TTL, means no pictures for that command, never a delay.
    Whether the check passed is read from its file each time, so a pass counts at once.
    """

    def __init__(
        self,
        paths: AppPaths,
        runner: CommandRunner,
        command: Sequence[str] = ("claude",),
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.paths, self.runner, self.clock = paths, runner, clock
        self.command = tuple(command)
        self.env = env
        self._lock = threading.Lock()
        self._status: tuple[str | None, bool] | None = None  # (version, logged in) from the last lookup that finished
        self._answered = 0.0  # when it finished
        self._asked: float | None = None  # when the last lookup started
        self._looking = False

    def refresh(self) -> None:
        """Start asking Claude Code about itself, unless a lookup is running or one started within PROBE_TTL."""
        with self._lock:
            now = self.clock()
            if self._looking or (self._asked is not None and now - self._asked < PROBE_TTL):
                return
            self._looking, self._asked = True, now
        threading.Thread(target=self._look, daemon=True, name="picture-ready").start()

    def _look(self) -> None:
        status = None
        try:
            status = claude_status(lambda argv: self.runner.run(argv, timeout=PROBE_SECONDS))
        except Exception:  # noqa: BLE001 - a lookup that fails is the same as one that finds nothing
            pass
        finally:
            with self._lock:
                if status is not None:
                    self._status, self._answered = status, self.clock()
                self._looking = False

    def ready(self) -> bool:
        self.refresh()  # does nothing unless the last answer is old
        with self._lock:
            status, answered = self._status, self._answered
        if status is None or self.clock() - answered > STALE_AFTER * PROBE_TTL:
            return False
        version, logged_in = status
        return logged_in and pictures_allowed(self.paths, version)

    def describe(
        self, png_base64: str, word: str, object_name: str, *, timeout: float, cancelled: Callable[[], bool] | None = None
    ) -> str | None:
        return describe_crop(png_base64, word, object_name, self.paths, timeout=timeout, cancelled=cancelled,
                             command=self.command, env=self.env)


# --- leftover temp pictures ---------------------------------------------------------------------------------------
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
