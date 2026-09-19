from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig


def test_containerd_soak_requires_process_cp_and_actual_cgroup_source() -> None:
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    data = yaml.safe_load(scenario.read_text())
    assert ScenarioConfig.model_validate(data).backend == "containerd"
    data["soak"]["roles"]["control-plane"]["collection_sources"] = [
        "procfs",
        "docker-engine",
    ]
    with pytest.raises(ValidationError, match="cannot claim Docker engine"):
        ScenarioConfig.model_validate(data)
    data["soak"]["roles"]["control-plane"]["collection_sources"] = [
        "procfs",
        "cgroup-v2",
    ]
    data["soak"]["images"]["control-plane"]["artifact_kind"] = "oci-image"
    with pytest.raises(ValidationError, match="requires process artifact"):
        ScenarioConfig.model_validate(data)


@pytest.mark.parametrize("workflow", ["validate", "cli", "loadtest"])
def test_accepts_supported_workflows(workflow: str) -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": workflow,
            "backend": "container" if workflow == "validate" else "k8s",
            "functions": ["word-stats-java"],
        }
    )

    assert config.workflow == workflow


def test_validate_requires_backend() -> None:
    with pytest.raises(ValidationError, match="backend is required"):
        ScenarioConfig(workflow="validate", functions=["word-stats-java"])


@pytest.mark.parametrize("workflow", ["validate", "cli", "loadtest"])
def test_containerd_backend_is_accepted_for_shared_workflows(workflow: str) -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": workflow,
            "backend": "containerd",
            "functions": ["word-stats-java"],
        }
    )

    assert config.backend == "containerd"


def test_containerd_async_validate_is_accepted() -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "containerd",
            "functions": ["word-stats-java"],
            "asyncLoad": True,
        }
    )

    assert config.async_load


@pytest.mark.parametrize("backend", ["container", "k8s"])
def test_persistent_recovery_is_opt_in_for_validate(backend: str) -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": backend,
            "functions": ["word-stats-java"],
            "persistentRecovery": True,
        }
    )

    assert config.persistent_recovery is True


def test_persistent_recovery_defaults_to_false() -> None:
    config = ScenarioConfig(
        workflow="validate", backend="container", functions=["word-stats-java"]
    )

    assert config.persistent_recovery is False


@pytest.mark.parametrize(
    ("workflow", "extra"),
    [
        ("cli", {"backend": "k8s"}),
        ("loadtest", {"backend": "k8s"}),
        ("offload", {}),
        ("offload-loadtest", {}),
        ("release", {"release": {"version": "v0.0.0"}}),
    ],
)
def test_persistent_recovery_requires_validate_workflow(
    workflow: str, extra: dict[str, object]
) -> None:
    with pytest.raises(
        ValidationError,
        match="persistentRecovery is only supported by the validate workflow",
    ):
        ScenarioConfig.model_validate(
            {
                "workflow": workflow,
                "functions": ["word-stats-java"],
                "persistentRecovery": True,
                **extra,
            }
        )


def test_cli_requires_backend() -> None:
    with pytest.raises(ValidationError, match="backend is required"):
        ScenarioConfig(workflow="cli", functions=["word-stats-java"])


def test_resource_request_must_not_exceed_limit() -> None:
    with pytest.raises(ValidationError, match="resource request must not exceed limit"):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "functions": ["word-stats-java"],
                "resources": {
                    "word-stats-java": {
                        "requests": {"cpu": 1, "memoryMiB": 513},
                        "limits": {"cpu": 0.5, "memoryMiB": 512},
                    }
                },
            }
        )


def test_resources_must_refer_to_selected_functions() -> None:
    with pytest.raises(
        ValidationError, match="resources must refer to selected functions"
    ):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "functions": ["word-stats-java"],
                "resources": {"other": {"limits": {"memoryMiB": 512}}},
            }
        )


def test_autoscaling_is_opt_in_for_loadtest() -> None:
    config = ScenarioConfig(
        workflow="loadtest", functions=["word-stats-java"], autoscaling=True
    )

    assert config.autoscaling is True


@pytest.mark.parametrize("non_k8s_backend", ["container", "containerd"])
def test_hpa_autoscaling_is_available_only_for_kubernetes_loadtests(
    non_k8s_backend: str,
) -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "autoscaling": True,
            "autoscalingStrategy": "HPA",
        }
    )

    assert config.autoscaling_strategy == "HPA"

    with pytest.raises(
        ValidationError, match="HPA autoscaling requires the k8s backend"
    ):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "backend": non_k8s_backend,
                "functions": ["word-stats-java"],
                "autoscaling": True,
                "autoscalingStrategy": "HPA",
            }
        )


def test_hpa_scale_to_zero_requires_hpa() -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "autoscaling": True,
            "autoscalingStrategy": "HPA",
            "hpaScaleToZero": True,
        }
    )

    assert config.hpa_scale_to_zero is True

    with pytest.raises(ValidationError, match="HPA scale-to-zero requires"):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "backend": "k8s",
                "functions": ["word-stats-java"],
                "hpaScaleToZero": True,
            }
        )


def test_autoscaling_is_rejected_outside_loadtest() -> None:
    with pytest.raises(ValidationError, match="autoscaling is only supported"):
        ScenarioConfig(
            workflow="validate",
            backend="k8s",
            functions=["word-stats-java"],
            autoscaling=True,
        )


def test_async_load_is_opt_in_for_container_validation() -> None:
    config = ScenarioConfig.model_validate(
        {
            "workflow": "validate",
            "backend": "container",
            "functions": ["word-stats-java"],
            "asyncLoad": True,
        }
    )

    assert config.async_load is True


def test_async_load_requires_container_validation() -> None:
    with pytest.raises(
        ValidationError, match="async load requires the validate workflow"
    ):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "k8s",
                "functions": ["word-stats-java"],
                "asyncLoad": True,
            }
        )


def test_rejects_legacy_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "functions": ["word-stats-java"],
                "base_scenario": "validate-container-local",
            }
        )


def test_resources_may_name_the_control_plane_as_well_as_functions() -> None:
    """Its limits are otherwise unreachable, so every load test gets one core."""
    config = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "k8s",
            "functions": ["word-stats-java"],
            "resources": {"control-plane": {"limits": {"cpu": 2, "memoryMiB": 1024}}},
        }
    )

    assert config.resources["control-plane"].limits is not None
    assert config.resources["control-plane"].limits.memory_mib == 1024


def test_resources_still_reject_a_name_that_is_neither() -> None:
    with pytest.raises(ValidationError):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "backend": "k8s",
                "functions": ["word-stats-java"],
                "resources": {"word-stats-python": {"limits": {"cpu": 1}}},
            }
        )
