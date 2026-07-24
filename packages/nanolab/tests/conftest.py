from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

import pytest

from workflow_tasks.workflow.events import WorkflowEvent


class FakeSink:
    """Shared test double for WorkflowSink — records emitted events and status calls."""

    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []
        self.status_events: list[tuple[str, str]] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, label: str):
        self.status_events.append(("start", label))
        try:
            yield
        finally:
            self.status_events.append(("end", label))


@pytest.fixture
def fake_sink() -> FakeSink:
    return FakeSink()


@pytest.fixture(scope="session")
def nanofaas_root() -> Path:
    value = os.environ.get("NANOFAAS_ROOT", "").strip()
    if not value:
        raise RuntimeError("NANOFAAS_ROOT must point to a nanoFaaS checkout")
    root = Path(value).expanduser().resolve()
    missing = [
        marker
        for marker in ("build.gradle", "settings.gradle")
        if not (root / marker).is_file()
    ]
    if missing:
        raise RuntimeError(
            f"NANOFAAS_ROOT is not a nanoFaaS checkout; missing: {', '.join(missing)}"
        )
    return root
