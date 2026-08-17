from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sonata_tasks.components import helm as helm_mod
from sonata_tasks.components.context import (
    ResolvedFunctionView,
    ScenarioExecutionContext,
)
from sonata_tasks.loadtest.two_vm import LOADTEST_SCENARIOS
from sonata_tasks.vm.models import VmRequest


@dataclass
class _RS:
    namespace: str | None
    functions: Sequence[ResolvedFunctionView]


def _ctx(scenario_name: str) -> ScenarioExecutionContext:
    return ScenarioExecutionContext(
        repo_root=Path("/repo"),
        scenario_name=scenario_name,
        runtime="java",
        namespace="ns",
        local_registry="localhost:5000",
        resolved_scenario=_RS(namespace="ns", functions=[]),
        vm_request=VmRequest(lifecycle="multipass", name="nanofaas-e2e", user="ubuntu"),
        cleanup_vm=True,
    )


def test_control_plane_planner_runs_for_loadtest_scenario() -> None:
    scenario = next(iter(LOADTEST_SCENARIOS))
    ops = helm_mod.plan_deploy_control_plane(_ctx(scenario))
    assert len(ops) >= 1
    assert any("NANOFAAS_METRICS_PROFILE" in argument for argument in ops[0].argv)
    assert any("advanced" in argument for argument in ops[0].argv)


def test_control_plane_planner_runs_for_plain_scenario() -> None:
    ops = helm_mod.plan_deploy_control_plane(_ctx("k3s-junit-curl"))
    assert len(ops) >= 1


def test_loadtest_scenario_exposes_node_port() -> None:
    scenario = next(iter(LOADTEST_SCENARIOS))
    ops = helm_mod.plan_deploy_control_plane(_ctx(scenario))
    # The RemoteCommandOperation argv should contain NodePort-related --set args
    argv = ops[0].argv
    assert any("NodePort" in arg for arg in argv)


def test_plain_scenario_no_node_port() -> None:
    ops = helm_mod.plan_deploy_control_plane(_ctx("k3s-junit-curl"))
    argv = ops[0].argv
    assert not any("NodePort" in arg for arg in argv)


def test_control_plane_helm_values_contains_namespace() -> None:
    values = helm_mod.control_plane_helm_values(
        namespace="myns",
        control_plane_image="localhost:5000/control-plane:latest",
    )
    assert values["namespace.name"] == "myns"


def _extra_env(values: dict[str, str], name: str) -> str:
    name_key = next(key for key, value in values.items() if value == name)
    return values[name_key.replace(".name", ".value")]


def test_loadtest_helm_values_allow_cold_start_queueing() -> None:
    regular = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest"
    )
    loadtest = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest", expose_node_port=True
    )

    assert _extra_env(regular, "SYNC_QUEUE_MAX_DEPTH") == "1"
    assert _extra_env(regular, "SYNC_QUEUE_MAX_QUEUE_WAIT") == "5s"
    assert _extra_env(loadtest, "SYNC_QUEUE_MAX_DEPTH") == "100"
    assert _extra_env(loadtest, "SYNC_QUEUE_MAX_QUEUE_WAIT") == "30s"
    assert _extra_env(loadtest, "SYNC_QUEUE_ADMISSION_ENABLED") == "false"


def test_loadtest_helm_values_enable_container_metrics() -> None:
    """Without this the run records no per-container CPU or memory at all.

    A natively compiled control plane publishes no JVM memory gauges, so the
    actuator scrape cannot answer "what did this build cost" for the very
    comparison the load test exists to make. cAdvisor is runtime-agnostic.
    """
    regular = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest"
    )
    loadtest = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest", expose_node_port=True
    )

    assert "prometheus.containerMetrics.enabled" not in regular
    assert loadtest["prometheus.containerMetrics.enabled"] == "true"
    assert loadtest["prometheus.containerMetrics.mode"] == "kubelet"


def test_helm_values_can_enable_sync_queue_admission_for_validation() -> None:
    values = helm_mod.control_plane_helm_values(
        namespace="ns",
        control_plane_image="control:latest",
        sync_queue_admission_enabled=True,
    )

    assert _extra_env(values, "SYNC_QUEUE_ADMISSION_ENABLED") == "true"


def test_helm_set_args_pairs_every_value_with_its_flag() -> None:
    assert helm_mod.helm_set_args({"a": "1", "b": "2"}) == (
        "--set", "a=1", "--set", "b=2",
    )


def test_helm_set_args_keeps_insertion_order() -> None:
    """Helm applies later --set values over earlier ones, so order is meaning."""
    assert helm_mod.helm_set_args({"z": "1", "a": "2"})[1::2] == ("z=1", "a=2")
