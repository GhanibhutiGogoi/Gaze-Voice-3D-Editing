"""Run external commands without a shell.

Every command is an argv list, never a shell string: app paths live under
"~/Library/Application Support", and the space in it breaks shell quoting.

Each command runs in its own process group with an empty stdin. A timeout
kills the whole group, so grandchildren (installers spawn plenty) can't keep
an output pipe open, and nothing ever sits waiting for keyboard input.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

NOT_FOUND = 127
TIMED_OUT = 124


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _merged_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    merged = dict(os.environ)
    merged.update(env)
    return merged


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


class CommandRunner:
    """Runs real commands. Tests use FakeRunner instead."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = 30.0,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        args = tuple(str(a) for a in argv)
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                env=_merged_env(env),
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            return CommandResult(args, NOT_FOUND, "", f"{args[0]}: not found")
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                stdout, _ = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout = ""
            return CommandResult(args, TIMED_OUT, _as_text(stdout), f"{args[0]}: timed out after {timeout:g}s")
        return CommandResult(args, proc.returncode, stdout, stderr)

    def stream(
        self,
        argv: Sequence[str],
        on_line: Callable[[str], None],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike | None = None,
    ) -> int:
        """Run a command, passing each output line (stdout and stderr merged) to on_line."""
        args = tuple(str(a) for a in argv)
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_merged_env(env),
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            on_line(f"{args[0]}: not found")
            return NOT_FOUND

        timed_out = threading.Event()
        timer = None
        if timeout is not None:
            def _kill() -> None:
                timed_out.set()
                _kill_group(proc)

            timer = threading.Timer(timeout, _kill)
            timer.daemon = True
            timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
        if timed_out.is_set():
            on_line(f"{args[0]}: timed out after {timeout:g}s")
            return TIMED_OUT
        return proc.returncode


class FakeRunner(CommandRunner):
    """Replays canned results for exact argv lists and records every call."""

    def __init__(self, which: Mapping[str, str] | None = None):
        self._which = dict(which or {})
        self._responses: dict[tuple[str, ...], tuple[CommandResult, float]] = {}
        self._lock = threading.Lock()
        self.calls: list[tuple[str, ...]] = []
        self.call_kwargs: list[dict] = []

    def add(
        self,
        argv: Sequence[str],
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        delay: float = 0.0,
    ) -> None:
        args = tuple(str(a) for a in argv)
        self._responses[args] = (CommandResult(args, returncode, stdout, stderr), delay)

    def set_which(self, name: str, path: str | None) -> None:
        if path is None:
            self._which.pop(name, None)
        else:
            self._which[name] = path

    def which(self, name: str) -> str | None:
        return self._which.get(name)

    def run(self, argv, *, timeout=30.0, env=None, cwd=None, input_text=None) -> CommandResult:
        args = tuple(str(a) for a in argv)
        with self._lock:
            self.calls.append(args)
            self.call_kwargs.append(
                {"env": dict(env) if env else None, "cwd": cwd, "timeout": timeout, "input_text": input_text}
            )
        found = self._responses.get(args)
        if found is None:
            return CommandResult(args, NOT_FOUND, "", f"{args[0]}: not faked")
        result, delay = found
        if delay:
            time.sleep(delay)
        return result

    def stream(self, argv, on_line, *, timeout=None, env=None, cwd=None) -> int:
        result = self.run(argv, timeout=timeout if timeout is not None else 30.0, env=env, cwd=cwd)
        for line in (result.stdout + result.stderr).splitlines():
            on_line(line)
        return result.returncode
