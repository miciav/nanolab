from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
import subprocess
import tempfile
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


@pytest.fixture
def canonical_release_configs(tmp_path: Path, nanofaas_root: Path) -> tuple[Path, Path]:
    """The (scenario, environment) pair the release preflight accepts, in tmp_path.

    Every value here is pinned by `validate_release_environment` and by the
    canonical policy check in `build_release_request`, so it lives in one place.
    """
    scenario = tmp_path / "release.yaml"
    scenario.write_text(
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
    (tmp_path / "loadtest.yaml").write_text(
        "workflow: loadtest\nfunctions: [word-stats-java]\n", encoding="utf-8"
    )
    environment = tmp_path / "environment.yaml"
    environment.write_text(
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
    return scenario, environment


class RejectingProvider:
    """A VM provider whose every method fails: proves a path stayed offline."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def reject(*_args, **_kwargs):
            self.calls.append(name)
            raise AssertionError(f"cloud provider called: {name}")

        return reject


def _require_checkout(value: str) -> Path:
    if not value.strip():
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


# The checkout the operator pointed at, and the extraction the suite reads.
_CHECKOUT: Path | None = None
_SOURCE: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Repoint NANOFAAS_ROOT at `git archive` output before any test module loads.

    Release planning derives from the guarded commit, so the suite must exercise
    the same bytes. Reading the checkout instead made results depend on the
    developer's working tree: ignored build output left behind by a branch switch
    invented phantom image targets and failed 134 tests, which is exactly the
    defect the release preflight now prevents. Test modules capture NANOFAAS_ROOT
    into constants at import, and pytest_configure runs before collection, so
    swapping it here fixes every one of them at once.

    `nanofaas_checkout` still hands out the real repository for the few tests that
    need git itself.
    """
    del config
    from nanolab.release.build import extract_commit_tree

    global _CHECKOUT, _SOURCE
    _CHECKOUT = _require_checkout(os.environ.get("NANOFAAS_ROOT", ""))
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=_CHECKOUT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # .resolve(): mkdtemp hands back /var/... which is a symlink to /private/var,
    # and the catalog resolves the paths it discovers -- an unresolved root makes
    # every example_dir comparison fail on macOS.
    _SOURCE = extract_commit_tree(
        _CHECKOUT, head, Path(tempfile.mkdtemp(prefix="nanofaas-source-")).resolve() / "tree"
    )
    os.environ["NANOFAAS_ROOT"] = str(_SOURCE)


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    if _SOURCE is not None:
        shutil.rmtree(_SOURCE.parent, ignore_errors=True)
    if _CHECKOUT is not None:
        os.environ["NANOFAAS_ROOT"] = str(_CHECKOUT)


@pytest.fixture(scope="session")
def nanofaas_root() -> Path:
    """What the suite plans from: the commit, materialized by `git archive`."""
    assert _SOURCE is not None
    return _SOURCE


@pytest.fixture(scope="session")
def nanofaas_checkout() -> Path:
    """The real git repository, for the few tests that need git itself."""
    assert _CHECKOUT is not None
    return _CHECKOUT
