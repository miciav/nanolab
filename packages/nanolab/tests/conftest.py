from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import os
from pathlib import Path

import pytest
import yaml

from nanolab.release.versioning import read_project_version
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


def write_canonical_release_environment(path: Path) -> Path:
    """The one Azure environment `validate_release_environment` accepts."""
    path.write_text(
        yaml.safe_dump(
            {
                "provider": "azure",
                "roles": {
                    "stack": {"name": "nanofaas-azure-release", "disk": "128G"},
                    "loadgen": {"name": "nanofaas-azure-release-loadgen", "disk": "30G"},
                    "arm-builder": {"name": "nanofaas-azure-release-arm", "disk": "64G"},
                },
                "azure": {
                    "resource_group": "nanofaas-rg",
                    "location": "westeurope",
                    "vm_size": "Standard_D8s_v5",
                    "loadgen_vm_size": "Standard_D2s_v5",
                    "arm_vm_size": "Standard_D8ps_v5",
                    "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
                    "arm_image_urn": "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140",
                    "operator_source_cidr": "8.8.8.8/32",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_canonical_release_scenario(path: Path, *, nanofaas_root: Path) -> Path:
    """A release scenario matching the canonical performance policy, plus its benchmark."""
    path.write_text(
        yaml.safe_dump(
            {
                "workflow": "release",
                "functions": ["word-stats-java"],
                "release": {
                    "version": f"v{read_project_version(nanofaas_root)}",
                    "profile": "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1",
                    "max_parallelism": 4,
                    "benchmark_runs": 3,
                    "benchmark_scenario": "loadtest.yaml",
                    "throughput_max_loss_percent": 10,
                    "p95_max_increase_percent": 15,
                    "error_rate_max": 0.30,
                },
            }
        ),
        encoding="utf-8",
    )
    (path.parent / "loadtest.yaml").write_text(
        "workflow: loadtest\nfunctions: [word-stats-java]\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def canonical_release_configs(
    tmp_path: Path, nanofaas_root: Path
) -> Callable[[Path], tuple[Path, Path]]:
    """Write (scenario, environment) canonical release configuration into a directory."""

    def write(directory: Path | None = None) -> tuple[Path, Path]:
        target = directory or tmp_path
        target.mkdir(parents=True, exist_ok=True)
        return (
            write_canonical_release_scenario(target / "release.yaml", nanofaas_root=nanofaas_root),
            write_canonical_release_environment(target / "environment.yaml"),
        )

    return write


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
