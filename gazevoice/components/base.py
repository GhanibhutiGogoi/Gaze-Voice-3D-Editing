"""What every component card is made of: a health check and some actions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar, Iterable, Sequence

from ..jobs import Job, JobError
from ..paths import AppPaths
from ..probes import ProbeCache
from ..runner import CommandResult, CommandRunner
from ..settings import Settings


class Health(str, Enum):
    MISSING = "missing"
    INSTALLED = "installed"
    READY = "ready"
    ERROR = "error"
    CHECKING = "checking"  # the check hasn't answered yet (a busy Mac); never a fault


@dataclass(frozen=True)
class ActionSpec:
    id: str
    label: str
    confirm: str | None = None
    uses_plan: bool = False  # spends some of the user's Claude/Codex usage

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "confirm": self.confirm, "uses_plan": self.uses_plan}


@dataclass(frozen=True)
class ComponentState:
    id: str
    name: str
    role: str
    health: Health
    summary: str
    details: tuple[tuple[str, str], ...] = ()
    actions: tuple[ActionSpec, ...] = ()

    def offers(self, action_id: str) -> bool:
        return any(a.id == action_id for a in self.actions)

    def detail(self, key: str) -> str | None:
        return next((v for k, v in self.details if k == key), None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "health": self.health.value,
            "summary": self.summary,
            "details": [[k, v] for k, v in self.details],
            "actions": [a.to_dict() for a in self.actions],
        }


@dataclass
class Context:
    runner: CommandRunner
    paths: AppPaths
    settings: Settings
    probes: ProbeCache = field(default_factory=ProbeCache)


class Component(ABC):
    id: ClassVar[str]
    name: ClassVar[str]
    role: ClassVar[str]

    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def runner(self) -> CommandRunner:
        return self.ctx.runner

    @property
    def paths(self) -> AppPaths:
        return self.ctx.paths

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    @abstractmethod
    def check(self) -> ComponentState:
        """Inspect this Mac and report the component's state. Must not change anything."""

    def probe(self, argv: Sequence[str], timeout: float = 15.0, ttl: float = 30.0) -> CommandResult:
        """Run a read-only status command for check(), shared and cached briefly. Never use in actions.

        Raises ProbeTimedOut if the command doesn't answer in time; the control center
        shows that unit as "Still checking…" instead of guessing.
        """
        return self.ctx.probes.run(self.runner, argv, timeout=timeout, ttl=ttl, raise_on_timeout=True)

    def state(
        self,
        health: Health,
        summary: str,
        details: Iterable[tuple[str, object]] = (),
        actions: Iterable[ActionSpec] = (),
    ) -> ComponentState:
        return ComponentState(
            id=self.id,
            name=self.name,
            role=self.role,
            health=health,
            summary=summary,
            details=tuple((str(k), str(v)) for k, v in details),
            actions=tuple(actions),
        )

    def run_action(self, action: str, job: Job) -> dict | None:
        method = getattr(self, f"action_{action}", None)
        if not callable(method):
            raise JobError(f"Unknown action {action}")
        return method(job)
