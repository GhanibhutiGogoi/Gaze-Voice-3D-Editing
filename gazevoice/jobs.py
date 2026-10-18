"""Background jobs (installs, tests) that stream log lines to the dashboard."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .events import EventBus

MAX_LINES_PER_JOB = 2000


class JobState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobError(Exception):
    """A failure whose message is shown to the user as-is."""


class JobAlreadyRunning(Exception):
    def __init__(self, component_id: str, job_id: str):
        super().__init__(f"{component_id} is already running job {job_id}")
        self.component_id = component_id
        self.job_id = job_id


@dataclass
class Job:
    id: str
    component_id: str
    action: str
    state: JobState = JobState.RUNNING
    lines: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    result: dict | None = None
    _bus: EventBus | None = field(default=None, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def log(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LINES_PER_JOB:
                del self.lines[: len(self.lines) - MAX_LINES_PER_JOB]
        if self._bus is not None:
            self._bus.publish(
                "job.log", {"job_id": self.id, "component_id": self.component_id, "line": line}
            )

    def to_dict(self, tail: int | None = None) -> dict:
        with self._lock:
            lines = list(self.lines if tail is None else self.lines[-tail:])
        return {
            "id": self.id,
            "component_id": self.component_id,
            "action": self.action,
            "state": self.state.value,
            "lines": lines,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
        }


class JobManager:
    def __init__(self, bus: EventBus, max_history: int = 100):
        self._bus = bus
        self._max_history = max_history
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def start(self, component_id: str, action: str, fn: Callable[[Job], dict | None]) -> Job:
        with self._lock:
            for existing in self._jobs.values():
                if existing.component_id == component_id and existing.state is JobState.RUNNING:
                    raise JobAlreadyRunning(component_id, existing.id)
            job = Job(id=uuid.uuid4().hex[:8], component_id=component_id, action=action, _bus=self._bus)
            self._jobs[job.id] = job
            self._trim()
        self._bus.publish("job.started", {"job": job.to_dict()})
        threading.Thread(
            target=self._run, args=(job, fn), name=f"job-{component_id}-{action}", daemon=True
        ).start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], dict | None]) -> None:
        try:
            result = fn(job)
            job.result = result if isinstance(result, dict) else None
            job.state = JobState.SUCCEEDED
        except JobError as exc:
            job.error = str(exc) or "Failed"
            job.state = JobState.FAILED
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            for line in traceback.format_exc().rstrip().splitlines():
                job.log(line)
            job.error = f"Unexpected error: {exc}"
            job.state = JobState.FAILED
        finally:
            job.finished_at = time.time()
            job._done.set()
            self._bus.publish("job.finished", {"job": job.to_dict()})

    def _trim(self) -> None:
        excess = len(self._jobs) - self._max_history
        if excess <= 0:
            return
        finished = [j for j in self._jobs.values() if j.state is not JobState.RUNNING]
        for job in finished[:excess]:
            del self._jobs[job.id]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            return list(reversed(list(self._jobs.values())))[:limit]

    def running(self) -> dict[str, str]:
        """component_id -> id of its running job."""
        with self._lock:
            return {
                j.component_id: j.id for j in self._jobs.values() if j.state is JobState.RUNNING
            }

    def wait(self, job_id: str, timeout: float) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job._done.wait(timeout)
        return job
