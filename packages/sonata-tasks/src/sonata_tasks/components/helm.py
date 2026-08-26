from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from sonata_tasks.components.context import ScenarioExecutionContext
from sonata_tasks.components.operations import RemoteCommandOperation
from sonata_tasks.components.images import control_image
from sonata_tasks.deployment import DEFAULT_NAMESPACE
from sonata_tasks.loadtest.two_vm import (
    LOADTEST_SCENARIOS,
    TWO_VM_CONTROL_PLANE_ACTUATOR_NODE_PORT,
    TWO_VM_CONTROL_PLANE_HTTP_NODE_PORT,
    TWO_VM_PROMETHEUS_NODE_PORT,
)


def _image_parts(image: str) -> tuple[str, str]:
    repository, separator, tag = image.rpartition(":")
    if not separator:
        return image, "latest"
    return repository, tag


def control_plane_helm_values(
    *,
    namespace: str,
    control_plane_image: str,
    expose_node_port: bool = False,
    metrics_profile: str | None = None,
    sync_queue_admission_enabled: bool = False,
    sync_queue_max_depth: int | None = None,
    container_metrics: bool = False,
    control_plane_resources: Mapping[str, Mapping[str, float | int | str]] | None = None,
) -> dict[str, str]:
    repository, tag = _image_parts(control_plane_image)
    callback_url = f"http://control-plane.{namespace}.svc.cluster.local:8080/v1/internal/executions"  # NOSONAR (S5332): in-cluster service DNS
    values = {
        "namespace.create": "false",
        "namespace.name": namespace,
        "controlPlane.image.repository": repository,
        "controlPlane.image.tag": tag,
        "controlPlane.image.pullPolicy": "Always",
        "demos.enabled": "false",
        "prometheus.create": "false",
    }
    if sync_queue_max_depth is not None:
        sync_queue_depth_value = sync_queue_max_depth
    elif expose_node_port:
        sync_queue_depth_value = 100
    else:
        sync_queue_depth_value = 1
    sync_queue_depth = str(sync_queue_depth_value)
    sync_queue_wait = "30s" if expose_node_port else "5s"
    extra_env = [
        ("NANOFAAS_DEPLOYMENT_DEFAULT_BACKEND", "k8s"),
        ("NANOFAAS_K8S_CALLBACK_URL", callback_url),
        # SyncQueueProperties is bound to the bare `sync-queue` prefix, so these
        # carry no `NANOFAAS_`. Two that did were removed: nothing in the platform
        # declares a `nanofaas.sync-queue` prefix, so they set nothing —
        # NANOFAAS_SYNC_QUEUE_ENABLED silently duplicated the line above it, and
        # NANOFAAS_SYNC_QUEUE_MAX_CONCURRENCY named a property that does not exist
        # on the record at all. Reading a load test's results, the second one cost
        # an hour: a run was blamed on a sync queue throttled to one in flight, a
        # setting nothing had ever applied.
        ("SYNC_QUEUE_ENABLED", "true"),
        ("SYNC_QUEUE_ADMISSION_ENABLED", str(sync_queue_admission_enabled).lower()),
        ("SYNC_QUEUE_MAX_DEPTH", sync_queue_depth),
        ("SYNC_QUEUE_MAX_ESTIMATED_WAIT", "2s"),
        ("SYNC_QUEUE_MAX_QUEUE_WAIT", sync_queue_wait),
        ("SYNC_QUEUE_RETRY_AFTER_SECONDS", "2"),
        ("SYNC_QUEUE_THROUGHPUT_WINDOW", "10s"),
        ("SYNC_QUEUE_PER_FUNCTION_MIN_SAMPLES", "1"),
    ]
    if metrics_profile is not None:
        extra_env.append(("NANOFAAS_METRICS_PROFILE", metrics_profile))
    for index, (name, value) in enumerate(extra_env):
        values[f"controlPlane.extraEnv[{index}].name"] = name
        values[f"controlPlane.extraEnv[{index}].value"] = value
    if expose_node_port:
        values["controlPlane.service.type"] = "NodePort"
        values["controlPlane.service.nodePorts.http"] = str(TWO_VM_CONTROL_PLANE_HTTP_NODE_PORT)
        values["controlPlane.service.nodePorts.actuator"] = str(TWO_VM_CONTROL_PLANE_ACTUATOR_NODE_PORT)
        values["prometheus.create"] = "true"
        values["prometheus.service.type"] = "NodePort"
        values["prometheus.service.nodePort"] = str(TWO_VM_PROMETHEUS_NODE_PORT)
    # Il chart mette 1 CPU di default, e un load test lo eredita in silenzio: un
    # run che non dichiara la risorsa piu' scarsa della cosa che misura lascia
    # decidere il risultato a un default di packaging. Qui i limiti arrivano
    # dallo scenario, CPU e memoria insieme - un solo modo di dirlo, perche' due
    # prima o poi si contraddicono.
    if control_plane_resources:
        # requests e limits arrivano nella forma dello schema di scenario
        # ({"limits": {"cpu": 2, "memoryMiB": 1024}}); il chart li vuole come
        # quantita Kubernetes, e senza la conversione un `2` finirebbe nel
        # manifest come due millicore.
        for section, quantities in control_plane_resources.items():
            for name, value in (quantities or {}).items():
                if name == "cpu":
                    rendered = f"{value:g}" if isinstance(value, (int, float)) else str(value)
                elif name in ("memoryMiB", "memory_mib"):
                    name, rendered = "memory", f"{int(value)}Mi"
                else:
                    continue
                values[f"controlPlane.resources.{section}.{name}"] = rendered
    if container_metrics:
        # What every container cost in memory and CPU, which nothing else records:
        # Prometheus scrapes the control plane's own actuator, so the functions'
        # footprint is absent from a run record, and the control plane reports only
        # its JVM heap — a number a natively compiled build does not publish at all.
        # The chart already carries the scrape job and the RBAC for it.
        #
        # Off by default rather than on for every load test: it adds a kubelet
        # scrape and the RBAC to reach it, and turning that on underneath runs that
        # never asked for it would change what they deploy in order to serve a
        # different experiment. Ask for it where it is read.
        values["prometheus.containerMetrics.enabled"] = "true"
        values["prometheus.containerMetrics.mode"] = "kubelet"
    return values


def _frozen_env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return MappingProxyType(dict(env or {}))


def _effective_namespace(context: ScenarioExecutionContext) -> str:
    if context.namespace:
        return context.namespace
    if context.resolved_scenario is not None and context.resolved_scenario.namespace:
        return context.resolved_scenario.namespace
    return DEFAULT_NAMESPACE


def _kubeconfig_path(context: ScenarioExecutionContext) -> str:
    vm_request = context.vm_request
    home = vm_request.home
    if home:
        return f"{home}/.kube/config"
    if vm_request.user == "root":
        return "/root/.kube/config"
    return f"/home/{vm_request.user}/.kube/config"


def helm_set_args(values: Mapping[str, str]) -> tuple[str, ...]:
    """Turn a value map into Helm's `--set key=value` arguments.

    Public and shared: four plan builders need it, and the private copies they
    each kept had to be corrected in three places at once.
    """
    args: list[str] = []
    for key, value in values.items():
        args.extend(["--set", f"{key}={value}"])
    return tuple(args)


def plan_deploy_control_plane(context: ScenarioExecutionContext) -> tuple[RemoteCommandOperation, ...]:
    namespace = _effective_namespace(context)
    loadtest = context.scenario_name in LOADTEST_SCENARIOS
    values = control_plane_helm_values(
        namespace=namespace,
        control_plane_image=control_image(context.local_registry),
        expose_node_port=loadtest,
        metrics_profile="advanced" if loadtest else None,
    )
    return (
        RemoteCommandOperation(
            operation_id="helm.deploy_control_plane",
            summary="Deploy control plane with Helm",
            argv=(
                "helm",
                "upgrade",
                "--install",
                "control-plane",
                "deploy/helm/nanofaas",
                "-n",
                namespace,
                "--wait",
                "--timeout",
                "5m",
                *helm_set_args(values),
            ),
            env=_frozen_env({"KUBECONFIG": _kubeconfig_path(context)}),
            execution_target="vm",
        ),
    )
