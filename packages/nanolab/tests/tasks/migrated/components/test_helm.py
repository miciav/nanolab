from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from nanolab.tasks.components import helm as helm_mod
from nanolab.tasks.components.context import (
    ResolvedFunctionView,
    ScenarioExecutionContext,
)
from nanolab.tasks.loadtest.two_vm import LOADTEST_SCENARIOS
from nanolab.tasks.vm.models import VmRequest


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


def test_container_metrics_are_opt_in() -> None:
    """Asking for them changes what the chart deploys, so a run must ask.

    A natively compiled control plane publishes no JVM memory gauges, so cAdvisor
    is the only source that can price every build alike — but it also adds a
    kubelet scrape and the RBAC to reach it, which runs that never read the
    result should not be made to carry.
    """
    without = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest", expose_node_port=True
    )
    with_metrics = helm_mod.control_plane_helm_values(
        namespace="ns",
        control_plane_image="control:latest",
        expose_node_port=True,
        container_metrics=True,
    )

    assert "prometheus.containerMetrics.enabled" not in without
    assert with_metrics["prometheus.containerMetrics.enabled"] == "true"
    assert with_metrics["prometheus.containerMetrics.mode"] == "kubelet"


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


def test_sync_queue_settings_use_the_prefix_the_platform_binds() -> None:
    """SyncQueueProperties is bound to the bare `sync-queue` prefix.

    A `NANOFAAS_`-prefixed variant would resolve to `nanofaas.sync-queue.*`, which
    nothing in the platform declares, so it would set nothing while looking
    authoritative. Reading a load test, one such variable cost an hour: the run
    was blamed on a sync queue throttled to one in flight, a setting that had
    never been applied because the property does not exist.
    """
    values = helm_mod.control_plane_helm_values(
        namespace="ns", control_plane_image="control:latest", expose_node_port=True
    )
    sync_vars = {
        value
        for key, value in values.items()
        if key.endswith(".name") and "SYNC_QUEUE" in str(value)
    }

    assert sync_vars, "the load-test deploy must configure the sync queue"
    assert not any(name.startswith("NANOFAAS_SYNC_QUEUE") for name in sync_vars)


def test_control_plane_limits_reach_the_chart_as_kubernetes_quantities() -> None:
    """A bare 2 would land in the manifest as two millicore, and 1024 as bytes."""
    values = helm_mod.control_plane_helm_values(
        namespace="nanofaas",
        control_plane_image="reg/nanofaas/control-plane:jvm",
        control_plane_resources={"limits": {"cpu": 2, "memoryMiB": 1024}},
    )

    assert values["controlPlane.resources.limits.cpu"] == "2"
    assert values["controlPlane.resources.limits.memory"] == "1024Mi"


def test_control_plane_limits_are_absent_when_the_scenario_declares_none() -> None:
    """Silence must leave the chart default alone rather than set an empty one."""
    values = helm_mod.control_plane_helm_values(
        namespace="nanofaas", control_plane_image="reg/nanofaas/control-plane:jvm"
    )

    assert not [key for key in values if key.startswith("controlPlane.resources")]
