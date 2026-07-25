from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

import pytest

from workflow_tasks.workflow.events import WorkflowEvent

# Typer force-enables Rich terminal rendering (ANSI highlighting of option-like
# tokens such as "--provision") whenever GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS is
# set, even though CliRunner's output stream isn't a real TTY. That splits the
# plain-text error substrings CLI tests assert on. Disable it for the whole
# suite so results don't depend on which CI system runs them.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")


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
