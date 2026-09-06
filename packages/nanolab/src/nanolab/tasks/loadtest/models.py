from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sonata_engine import Resource
from sonata_tasks.k6_models import K6Stage


@dataclass(frozen=True)
class K6Config:
    script_path: Path
    target_url: str | Resource[str]
    summary_output_path: Path
    stages: tuple[K6Stage, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    vus: int | None = None
    duration: str | None = None
    payload_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "env", dict(self.env))


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PrometheusQuery:
    name: str
    expr: str
    required: bool = False
