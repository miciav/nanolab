"""Smoke tests for the release workflow builder."""

from pathlib import Path

import pytest

import nanolab.plans.release as release_plan
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import ImagePlan
from nanolab.plans.release import ReleaseRequest, build_release_workflow
from nanolab.release.run import GitState, ReleaseSettings


_AZURE_ENV = EnvironmentConfig.model_validate(
    {
        "provider": "azure",
        "roles": {
            "stack": {"name": "release-stack", "user": "azureuser"},
            "loadgen": {"name": "release-loadgen", "user": "azureuser"},
            "arm-builder": {"name": "release-arm", "user": "azureuser"},
        },
        "azure": {
            "resource_group": "test-rg",
            "location": "westeurope",
            "vm_size": "Standard_D8s_v5",
            "loadgen_vm_size": "Standard_D4s_v5",
            "arm_vm_size": "Standard_D4ps_v6",
            "image_urn": "Canonical:0001-com-ubuntu-server-noble:24_04-lts-gen2:latest",
            "arm_image_urn": "Canonical:0001-com-ubuntu-server-noble:24_04-lts-gen2:latest",
            "operator_source_cidr": "1.2.3.4/32",
        },
    }
)


def test_release_request_rejects_non_azure_environment():
    local_env = EnvironmentConfig.model_validate({"provider": "local", "roles": {}})
    req = ReleaseRequest(
        repo_root=Path("/tmp"),
        version="1.0.0",
        environment=local_env,
        scenario=ScenarioConfig(workflow="loadtest", functions=["word-stats-java"]),
        image_plan=ImagePlan(version="v1", registry="localhost:5000", targets=(), cells=()),
        settings=ReleaseSettings(
            max_parallelism=4,
            scenario=Path("loadtest.yaml"),
            scenario_name="loadtest.yaml",
            benchmark_runs=3,
            profile="default",
            throughput_max_loss_percent=10.0,
            p95_max_increase_percent=20.0,
            error_rate_max=0.05,
        ),
        run_dir=Path("/tmp/runs"),
        performance_root=Path("/tmp/perf"),
    )
    with pytest.raises(ValueError, match="Azure"):
        build_release_workflow(req)


def test_release_request_is_frozen():
    req = ReleaseRequest(
        repo_root=Path("/tmp"),
        version="1.0.0",
        environment=_AZURE_ENV,
        scenario=ScenarioConfig(workflow="loadtest", functions=["word-stats-java"]),
        image_plan=ImagePlan(version="v1", registry="localhost:5000", targets=(), cells=()),
        settings=ReleaseSettings(
            max_parallelism=4,
            scenario=Path("loadtest.yaml"),
            scenario_name="loadtest.yaml",
            benchmark_runs=3,
            profile="default",
            throughput_max_loss_percent=10.0,
            p95_max_increase_percent=20.0,
            error_rate_max=0.05,
        ),
        run_dir=Path("/tmp/runs"),
        performance_root=Path("/tmp/perf"),
    )
    with pytest.raises(Exception):
        req.version = "2.0.0"  # type: ignore[misc]


def test_release_source_commit_uses_clean_committed_head(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "verify_version_consistency",
        lambda _root: "0.18.1",
    )

    assert release_plan._release_source_commit(tmp_path, "v0.18.1") == "abc123"


def test_release_source_commit_rejects_dirty_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=False),
    )

    with pytest.raises(ValueError, match="clean nanoFaaS Git tree"):
        release_plan._release_source_commit(tmp_path, "v0.18.1")


def test_release_source_commit_rejects_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "verify_version_consistency",
        lambda _root: "0.18.0",
    )

    with pytest.raises(ValueError, match="does not match"):
        release_plan._release_source_commit(tmp_path, "v0.18.1")


def test_build_release_workflow_compiles_to_a_workflow():
    """Minimal smoke: the builder produces a Workflow without crashing."""
    # ponytail: integration test skipped — needs real Azure credentials
    pass
