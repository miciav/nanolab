from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path, PurePosixPath
from typing import Any, cast

from sonata_engine import Steps, Task, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.compose import DockerComposeProject, docker_compose_resource
from sonata_tasks.registry import docker_registry_resource
from sonata_tasks.loadtest import (
    VerifyConcurrencyTask,
    CapturePrometheusTask,
    EvaluateGateTask,
    FetchResultsTask,
    RunK6Task,
    VerifyAutoscalingTask,
    WriteReportTask,
    WriteSummaryTask,
    build_loadtest_workflow,
    loadtest_composite,
)
from sonata_tasks.platform import Backend, Build, PlatformRequest
from sonata_tasks.components.helm import control_plane_helm_values, helm_set_args
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.k6 import K6Task
from sonata_tasks.loadtest.concurrency import (
    ConcurrencyWatcher,
    ScrapeConcurrencyProbe,
)
from sonata_tasks.loadtest.autoscaling import (
    HttpReplicaProbe,
    ReplicaProbe,
    ReplicaWatcher,
    ReplicaStatusProbe,
    VerifyInitialAutoscalingReplicas,
    VerifyAutoscalingReplicas,
)
from sonata_tasks.loadtest.models import K6Config, K6Stage, PrometheusQuery
from sonata_tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher
from sonata_tasks.loadtest.tasks import (
    CapturePrometheusSnapshot,
    FetchVmResults,
    WriteK6Report,
    WriteLoadtestSummary,
)
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import resolve_function, sonata_function
from nanolab.workspace.paths import discover_tool_root
from nanolab.workspace.provenance import source_fingerprint

_REMOTE_DIR = "."
_HPA_METRIC_WAIT_SECONDS = 180


def _default_prometheus_queries(function_name: str) -> tuple[PrometheusQuery, ...]:
    function = f"{{function={json.dumps(function_name)}}}"
    control_plane = '{app="nanofaas-control-plane"}'
    # kube-state-metrics labels by object name, not by the `function` label the
    # control plane's own series carry; the HPA is named after the deployment.
    hpa = f"{{horizontalpodautoscaler={json.dumps(f'fn-{function_name}')}}}"
    return (
        PrometheusQuery("function_dispatch_total", f"function_dispatch_total{function}", True),
        PrometheusQuery("function_success_total", f"function_success_total{function}", True),
        PrometheusQuery("function_error_total", f"function_error_total{function}"),
        PrometheusQuery("function_retry_total", f"function_retry_total{function}"),
        PrometheusQuery("function_timeout_total", f"function_timeout_total{function}"),
        PrometheusQuery(
            "function_queue_rejected_total", f"function_queue_rejected_total{function}"
        ),
        PrometheusQuery("function_cold_start_total", f"function_cold_start_total{function}"),
        PrometheusQuery("function_warm_start_total", f"function_warm_start_total{function}"),
        PrometheusQuery(
            "function_latency_count", f"function_latency_ms_seconds_count{function}", True
        ),
        PrometheusQuery(
            "function_latency_sum", f"function_latency_ms_seconds_sum{function}", True
        ),
        PrometheusQuery(
            "function_init_duration_count",
            f"function_init_duration_ms_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_init_duration_sum", f"function_init_duration_ms_seconds_sum{function}"
        ),
        PrometheusQuery(
            "function_queue_wait_count", f"function_queue_wait_ms_seconds_count{function}"
        ),
        PrometheusQuery(
            "function_queue_wait_sum", f"function_queue_wait_ms_seconds_sum{function}"
        ),
        PrometheusQuery(
            "function_e2e_latency_count", f"function_e2e_latency_ms_seconds_count{function}"
        ),
        PrometheusQuery(
            "function_e2e_latency_sum", f"function_e2e_latency_ms_seconds_sum{function}"
        ),
        PrometheusQuery("process_cpu_usage", f"process_cpu_usage{control_plane}", True),
        PrometheusQuery(
            "jvm_heap_used_bytes",
            'jvm_memory_used_bytes{app="nanofaas-control-plane",area="heap"}',
            True,
        ),
        # The HPA controller's own view, published by kube-state-metrics. Not
        # required: only autoscaling runs enable it, and a run without an HPA
        # should record its absence rather than fail on it.
        PrometheusQuery(
            "hpa_desired_replicas",
            f"kube_horizontalpodautoscaler_status_desired_replicas{hpa}",
        ),
        PrometheusQuery(
            "hpa_current_replicas",
            f"kube_horizontalpodautoscaler_status_current_replicas{hpa}",
        ),
        # Was the controller able to read its metric at all — the condition that
        # would have named an unservable external metric outright, instead of
        # leaving the preflight to time out against it.
        PrometheusQuery(
            "hpa_scaling_active",
            "kube_horizontalpodautoscaler_status_condition"
            f'{{horizontalpodautoscaler="fn-{function_name}",'
            'condition="ScalingActive",status="true"}',
        ),
        # Was the replica count it wanted clamped by minReplicas/maxReplicas.
        # This is what tells a saturated metric apart from a capped decision.
        PrometheusQuery(
            "hpa_scaling_limited",
            "kube_horizontalpodautoscaler_status_condition"
            f'{{horizontalpodautoscaler="fn-{function_name}",'
            'condition="ScalingLimited",status="true"}',
        ),
        # The same four questions for the INTERNAL strategy, which owns no HPA
        # object and so appears in none of the series above. Published by the
        # control plane's own autoscaler, and one of them is better than what
        # Kubernetes offers: `recommended` is the count the ratio asked for
        # BEFORE the clamp, which the HPA never exposes.
        PrometheusQuery(
            "internal_scaling_recommended_replicas",
            f"function_scaling_recommended_replicas{function}",
        ),
        PrometheusQuery(
            "internal_scaling_desired_replicas",
            f"function_scaling_desired_replicas{function}",
        ),
        PrometheusQuery("internal_scaling_limited", f"function_scaling_limited{function}"),
        PrometheusQuery("internal_scaling_ratio_milli", f"function_scaling_ratio_milli{function}"),
    )


class _RoleRunner:
    """The VM-command shape the legacy replica probe expects, bound to a role."""

    def __init__(self, bindings: RoleBindings, role: ExecutionRole) -> None:
        self._executor = bindings.executor_for(role)
        self._role: ExecutionRole = role

    def run_vm_command(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        remote_dir: str | None,
        dry_run: bool,
    ) -> Any:
        return self._executor.run(
            CommandTaskSpec(
                task_id="",
                summary="Run k6",
                argv=argv,
                role=self._role,
                env=env,
                remote_dir=remote_dir,
            ),
            dry_run=dry_run,
        )


def _validate_loadtest(config: ScenarioConfig, environment: EnvironmentConfig) -> Backend:
    if config.workflow != "loadtest":
        raise ValueError("load-test plan requires a loadtest scenario")
    backend: Backend = config.backend or "k8s"
    if backend == "container" and environment.provider != "local":
        raise ValueError("container load-test requires a local environment")
    return backend


def _autoscaling_setup(
    config: ScenarioConfig,
) -> tuple[bool, int, dict[str, object] | None]:
    hpa = config.autoscaling_strategy == "HPA"
    hpa_scale_to_zero = hpa and config.hpa_scale_to_zero
    replica_floor = 0 if hpa_scale_to_zero or not hpa else 1
    scaling_config: dict[str, object] | None = None
    if config.autoscaling:
        scaling_config = {
            "strategy": config.autoscaling_strategy,
            "minReplicas": replica_floor,
            "maxReplicas": 5,
            # rps, not in_flight: in-flight concurrency is throughput x service
            # time, which for this function sits below 1 at any rate the load
            # test produces, and is capped at the queue's concurrency limit
            # anyway. This target type is `Value` (KubernetesMetricsTranslator),
            # not `AverageValue`, so the recommendation is
            # ceil(currentReplicas * value / target) — it multiplies by the
            # current replica count, not divides. The metric is also a
            # control-plane-wide sum rather than per-pod, so any target below
            # the offered rate drives the recommendation straight to
            # maxReplicas. 100 is therefore an upper-bound trigger, not a
            # proportional setpoint; switching the translator to
            # `AverageValue` is the open follow-up that would make it
            # proportional.
            "metrics": [{"type": "rps", "target": "100"}],
        }
    return hpa, replica_floor, scaling_config


# Deliberately short next to the platform defaults of 30s/60s. The governor moves
# the per-replica target by one step per cooldown, so with production cooldowns a
# run would need minutes per phase to show a trajectory rather than a single step.
# The cooldowns themselves are not what this scenario is checking.
_CONCURRENCY_COOLDOWN_MS = 5000
# The ceiling the governor works under: FunctionQueueState clamps the effective
# limit to the function's `concurrency`, so it has to leave room above
# maxTargetInFlightPerPod or the trajectory is flat by construction.
_CONCURRENCY_CEILING = 8
# The governor only reacts to a function that gets slower under concurrency, and
# on an unconstrained multi-core host word-stats-java does not: an early run
# climbed to the ceiling and stayed there, correctly, because eight parallel
# requests never contended for anything. The cap creates the contention at a
# concurrency the governor can reach. This is controlling the variable, not
# arranging the answer - the run still has to show that it notices and recovers.
#
# Four cores rather than one, because one flattens the very comparison the
# scenarios exist for: with a single core the optimum is 1-2 for every runtime,
# so a JVM and a GIL-bound interpreter look identical. With four, a runtime that
# can use them should settle near four while one that serialises CPU work should
# still settle near one. The ceiling stays above that, or the governor would be
# clamped rather than converging.
_CONCURRENCY_FUNCTION_RESOURCES: dict[str, object] = {
    "requests": {"cpu": 2.0, "memoryMiB": 256},
    "limits": {"cpu": 4.0, "memoryMiB": 512},
}


def _concurrency_control_setup(config: ScenarioConfig) -> dict[str, object] | None:
    """Fixed replicas, adaptive per-replica concurrency.

    `NONE` rather than `INTERNAL` so nothing moves the replica count even if an
    autoscaler were present: with one replica pinned, every change in
    `function_effective_concurrency` is the governor's doing.
    """
    if not config.concurrency_control:
        return None
    return {
        "strategy": "NONE",
        "minReplicas": 1,
        "maxReplicas": 1,
        "concurrencyControl": {
            "mode": "ADAPTIVE_PER_POD",
            "targetInFlightPerPod": 4,
            "minTargetInFlightPerPod": 1,
            "maxTargetInFlightPerPod": _CONCURRENCY_CEILING,
            "upscaleCooldownMs": _CONCURRENCY_COOLDOWN_MS,
            "downscaleCooldownMs": _CONCURRENCY_COOLDOWN_MS,
        },
    }


def _resolve_with_prebuilt_images(
    config: ScenarioConfig,
    repo_root: Path | None,
    tool_root: Path | None,
    prebuilt_control_plane_image: str | None,
    prebuilt_function_images: Mapping[str, str] | None,
) -> tuple[tuple[Any, ...], bool]:
    resolved = tuple(
        resolve_function(config, key, source_root=repo_root, tool_root=tool_root)
        for key in config.functions
    )
    prebuilt = prebuilt_control_plane_image is not None or (
        prebuilt_function_images is not None
    )
    if prebuilt:
        if prebuilt_function_images is None:
            raise ValueError("prebuilt function images are required in prebuilt mode")
        missing = [
            function.key
            for function in resolved
            if not prebuilt_function_images.get(function.key)
        ]
        if missing:
            raise ValueError("missing prebuilt function images: " + ", ".join(missing))
        resolved = tuple(
            replace(function, image=prebuilt_function_images[function.key])
            for function in resolved
        )
    return resolved, prebuilt


def _resolve_functions(
    config: ScenarioConfig,
    repo_root: Path | None,
    tool_root: Path | None,
    scaling_config: dict[str, object] | None,
    prebuilt_control_plane_image: str | None,
    prebuilt_function_images: Mapping[str, str] | None,
    concurrency: int = 4,
    resources: dict[str, object] | None = None,
) -> tuple[tuple[Any, ...], bool]:
    resolved, prebuilt = _resolve_with_prebuilt_images(
        config,
        repo_root,
        tool_root,
        prebuilt_control_plane_image,
        prebuilt_function_images,
    )
    functions = tuple(sonata_function(function) for function in resolved)
    if scaling_config is not None:
        functions = tuple(
            replace(
                function,
                scaling_config=scaling_config,
                timeout_ms=30000,
                concurrency=concurrency,
                queue_size=100,
                **({"resources": resources} if resources is not None else {}),
            )
            for function in functions
        )
    return functions, prebuilt


def _additional_modules(
    autoscaling: bool, hpa: bool, concurrency_control: bool = False
) -> tuple[str, ...]:
    modules: list[str] = []
    if autoscaling:
        if not hpa:
            modules.append("autoscaler")
        modules.extend(("async-queue", "sync-queue"))
    if concurrency_control:
        # No autoscaler: the governor has had its own tick loop since nanofaas
        # #180 and no longer needs ScalingStrategy.INTERNAL. No sync-queue
        # either - its admission control would reject under the very load the
        # governor is being watched through, which muddies the reading. The
        # async queue is what actually enforces the limit
        # (FunctionQueueState.tryAcquireSlot), so it is required.
        modules.extend(("concurrency-control", "async-queue"))
    return tuple(modules)


def _validate_remote_run_dir(remote_run_dir: Path | None, home: str) -> None:
    if remote_run_dir is None:
        return
    requested = PurePosixPath(str(remote_run_dir))
    selected_home = PurePosixPath(home)
    try:
        relative = requested.relative_to(selected_home)
    except ValueError:
        relative = PurePosixPath()
    parts = relative.parts
    run_number = requested.name.removeprefix("run-")
    if (
        not requested.is_absolute()
        or ".." in requested.parts
        or len(parts) != 4
        or parts[0] != "nanofaas-release"
        or not parts[1]
        or parts[2] != "benchmarks"
        or not run_number.isdigit()
        or int(run_number) < 1
    ):
        raise ValueError("remote run directory must be an absolute run-N child")


def load_script_name(config: ScenarioConfig) -> str:
    """Which k6 script drives the function.

    `autoscaling.js` is not about autoscaling: it is the one that hammers a
    single function with whatever stages the plan supplies, which is exactly
    what a concurrency run needs. Selecting it by `config.autoscaling` alone
    silently gave the governor scenario `two-vm-function-invoke.js` instead,
    whose 100ms think time holds offered concurrency near 2 against a limit of
    8 — so the function was never concurrent, the governor had nothing to react
    to, and a think-time override aimed at the other script changed nothing.
    """
    if config.autoscaling or config.concurrency_control:
        return "autoscaling.js"
    return "two-vm-function-invoke.js"


def _resolve_script_and_summary(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    remote: bool,
    dedicated: bool,
    remote_run_dir: Path | None,
    run_dir: Path,
    tool_root: Path | None,
) -> tuple[Path, Path]:
    script_name = load_script_name(config)
    if remote:
        role_target = environment.target("loadgen" if dedicated else "stack")
        home = role_target.remote_home
        script_path = Path(home) / f"nanolab-assets/k6/{script_name}"
        output_dir = remote_run_dir or Path(home) / "nanofaas-loadtest"
        _validate_remote_run_dir(remote_run_dir, home)
        summary_path = output_dir / "k6-summary.json"
    else:
        product_root = tool_root or discover_tool_root()
        script_path = product_root / "assets" / "k6" / script_name
        summary_path = run_dir / "k6-summary.json"
    return script_path, summary_path


def _build_platform_request(
    *,
    backend: Backend,
    build: Build,
    functions: tuple[Any, ...],
    additional_modules: tuple[str, ...],
    prebuilt: bool,
    prebuilt_control_plane_image: str | None,
    root: Path,
    remote_repo_root: Path | None,
    hpa: bool,
) -> PlatformRequest:
    request = PlatformRequest(
        backend=backend,
        build=build,
        functions=functions,
        additional_modules=additional_modules,
        build_images=not prebuilt,
        build_control_plane=backend == "k8s",
        push_function_images=backend == "container" and not prebuilt,
        control_plane_image=prebuilt_control_plane_image,
        source_fingerprint=source_fingerprint(root),
        helm_chart=(
            str(remote_repo_root / "deploy/helm/nanofaas")
            if remote_repo_root is not None
            else "deploy/helm/nanofaas"
        ),
    )
    if backend == "k8s":
        # NodePort because the load generator reaches the control plane from outside
        # the cluster, unlike validate which curls it from within the VM.
        helm_values = control_plane_helm_values(
            namespace=request.namespace,
            control_plane_image=request.control_plane_image_reference(),
            expose_node_port=True,
            metrics_profile="advanced",
        )
        if hpa:
            helm_values["hpa-metrics-adapter.enabled"] = "true"
            helm_values["hpa-metrics-adapter.metricsRelistInterval"] = "10s"
            # The adapter says what the HPA is told; this says what the HPA did
            # with it. Without it a run records the replica counts and the metric
            # value but not the controller's own verdict, so a plateau at
            # maxReplicas cannot be told apart from a metric that stopped rising.
            helm_values["kube-state-metrics.enabled"] = "true"
        request = replace(
            request,
            helm_values=helm_set_args(helm_values),
        )
    return request


def compose_control_plane_modules(additional_modules: tuple[str, ...]) -> str:
    """The module set compiled into the control-plane image on the container path.

    This is a build arg, not a runtime toggle, so it decides what the image
    contains rather than what it enables. The deployment provider is always
    needed to run functions at all; the rest is whatever the scenario asked for,
    falling back to the historical autoscaling set for scenarios that ask for
    nothing.
    """
    selected = additional_modules or ("autoscaler", "async-queue", "sync-queue")
    return ",".join(("container-deployment-provider",) + tuple(selected))


def _build_platform_requires(
    backend: str,
    executor: RoleBoundCommandTaskExecutor,
    root: Path,
    additional_modules: tuple[str, ...] = (),
) -> tuple[Any, ...]:
    platform_requires = ()
    if backend == "container":
        registry = docker_registry_resource(executor=executor, role="host")
        compose = docker_compose_resource(
            DockerComposeProject(
                name="nanofaas-loadtest",
                file=Path("deploy/compose/compose.yaml"),
                ready_url="http://127.0.0.1:8081/actuator/health/readiness",
                env={
                    "NANOFAAS_CONTROL_PLANE_MODULES": compose_control_plane_modules(
                        additional_modules
                    )
                },
            ),
            executor=executor,
            cwd=root,
            requires=(registry,),
        )
        platform_requires = (
            registry,
            compose,
        )
    return platform_requires


def k6_environment(
    config: ScenarioConfig, control_plane_url: str, function_name: str
) -> dict[str, str]:
    """What the load generator is told, including how much concurrency to offer.

    Measured, not assumed: with the script's default 50ms think time, a 180s
    phase at 25 VUs held a mean of 1.0 requests in flight against a limit of 8.
    A closed-loop VU keeps only S/(S+Z) of a request in flight, so for a 2.5ms
    function that think time caps the offered concurrency near 1 however many
    VUs are added — the function never became concurrent and the governor had
    nothing to react to. Zero think time makes in-flight equal the VU count.
    """
    env = {"NANOFAAS_URL": control_plane_url, "NANOFAAS_FUNCTION": function_name}
    if config.concurrency_control:
        env["K6_THINK_SECONDS"] = "0"
    return env


def _build_run_k6(
    *,
    executor: RoleBoundCommandTaskExecutor,
    load_role: ExecutionRole,
    control_plane_url: str,
    script_path: Path,
    summary_path: Path,
    target: Any,
    stages: tuple[tuple[str, int], ...] | None,
    config: ScenarioConfig,
) -> K6Task:
    return K6Task(
        executor=executor,
        role=load_role,
        title="Run k6",
        config=K6Config(
            script_path=script_path,
            target_url=control_plane_url,
            summary_output_path=summary_path,
            stages=tuple(
                K6Stage(duration=duration, target=count)
                for duration, count in (
                    stages
                    or _default_stages(config)
                )
            ),
            env=k6_environment(config, control_plane_url, target.name),
        ),
        remote_dir=_REMOTE_DIR,
    )


def _default_stages(config: ScenarioConfig) -> tuple[tuple[str, int], ...]:
    if config.autoscaling:
        return (("10s", 10), ("20s", 20), ("90s", 20), ("10s", 0))
    if config.concurrency_control:
        # Light, then heavy, then light again. The governor raises the limit
        # while the function answers at its best, backs it off once the service
        # time degrades, and raises it again on the way out; a run that stopped
        # at the heavy phase could not tell a governor that recovers from one
        # stuck at minTargetInFlightPerPod.
        #
        # The phases are long because a 150s version was not conclusive: the
        # limit held at its ceiling for the whole 60s of heavy load and only
        # began falling as the load receded, which reads the same whether the
        # controller reacts slowly or ignores the signal outright. Three minutes
        # of sustained load is some thirty downscale cooldowns, so a controller
        # that reacts at all has to show it; and two minutes of tail leaves room
        # for the climb back, which needs a step per cooldown.
        #
        # 50 VUs in the heavy phase, with the think time zeroed: in a closed loop
        # each VU holds one outstanding request, so in-flight is the VU count
        # until the limit intervenes, and 50 against a ceiling of 8 keeps the
        # limit saturated even after the governor has cut it. More would not add
        # load: delivered throughput is capped at limit/service-time whatever the
        # VU count, and the surplus only deepens a queue that rejects past 100.
        return (("60s", 2), ("30s", 50), ("180s", 50), ("120s", 2))
    return (("15s", 1), ("30s", 3))


def _build_replica_watcher(
    *,
    config: ScenarioConfig,
    backend: str,
    bindings: RoleBindings,
    control_plane_url: str,
    target: Any,
    request: PlatformRequest,
) -> tuple[ReplicaWatcher | None, ReplicaStatusProbe | None]:
    watcher: ReplicaWatcher | None = None
    replica_probe: ReplicaStatusProbe | None = None
    if config.autoscaling:
        if backend == "container":
            replica_probe = HttpReplicaProbe(
                endpoint=control_plane_url,
                function_name=target.name,
            )
        else:
            replica_probe = ReplicaProbe(
                runner=_RoleRunner(bindings, "stack"),
                namespace=request.namespace,
                deployment_name=f"fn-{target.name}",
                remote_dir=_REMOTE_DIR,
            )
        watcher = ReplicaWatcher(replica_probe)
    return watcher, replica_probe


def _build_concurrency_watcher(
    *,
    config: ScenarioConfig,
    control_plane_url: str,
    target: Any,
) -> ConcurrencyWatcher | None:
    """Watches the governor's own gauge, which lives on the management port.

    The API port serves no view of the effective limit — there is no endpoint
    for it, only the `function_effective_concurrency` gauge in the metrics
    scrape — so the URL is derived the same way sonata_tasks.metrics derives it.
    """
    if not config.concurrency_control:
        return None
    return ConcurrencyWatcher(
        ScrapeConcurrencyProbe(
            management_url=control_plane_url.replace(":8080", ":8081"),
            function_name=target.name,
        )
    )


def _build_steps_after(
    *,
    remote: bool,
    summary_path: Path,
    run_dir: Path,
    fetcher: RemoteFileFetcher | object | None,
    watcher: ReplicaWatcher | None,
    concurrency_watcher: ConcurrencyWatcher | None = None,
    replica_probe: ReplicaStatusProbe | None,
    bindings: RoleBindings,
    request: PlatformRequest,
    target: Any,
    replica_floor: int,
    prometheus_client: PrometheusClient,
) -> list[Task[Any]]:
    after: list[Task[Any]] = []
    if remote:
        after.append(
            FetchResultsTask(
                fetch=FetchVmResults(
                    task_id="",
                    title="Fetch k6 results",
                    fetcher=cast(RemoteFileFetcher, fetcher),
                    remote_source=str(summary_path),
                    local_dest=run_dir,
                )
            )
        )
    if concurrency_watcher is not None:
        after.append(
            VerifyConcurrencyTask(
                watcher=concurrency_watcher,
                function_name=target.name,
                series_path=run_dir / "concurrency-series.json",
            )
        )
    if watcher is not None:
        after.append(
            VerifyAutoscalingTask(
                verifier=VerifyAutoscalingReplicas(
                    task_id="",
                    title="Verify autoscaling replicas",
                    runner=_RoleRunner(bindings, "stack"),
                    namespace=request.namespace,
                    deployment_name=f"fn-{target.name}",
                    remote_dir=_REMOTE_DIR,
                    watcher=watcher,
                    probe=replica_probe,
                    expected_final_replicas=replica_floor,
                )
            )
        )
    after.extend(
        (
            CapturePrometheusTask(
                snapshot=lambda window: CapturePrometheusSnapshot(
                    task_id="",
                    title="Capture Prometheus snapshot",
                    client=prometheus_client,
                    queries=_default_prometheus_queries(target.name),
                    window=window,
                    output_dir=run_dir,
                )
            ),
            WriteReportTask(
                report=WriteK6Report(
                    task_id="", title="Write the report", data_dir=run_dir, output_dir=run_dir
                )
            ),
            WriteSummaryTask(
                summary=lambda autoscaling: WriteLoadtestSummary(
                    task_id="",
                    title="Write the summary",
                    data_dir=run_dir,
                    output_dir=run_dir,
                    # Cast, not a lie: WriteLoadtestSummary reads `.result` and
                    # nothing else. Widening its annotation would touch a class
                    # three workflows still share, for a shim the last of them
                    # will delete.
                    autoscaling=(
                        cast(VerifyAutoscalingReplicas, _AsResult(autoscaling))
                        if autoscaling is not None
                        else None
                    ),
                )
            ),
            EvaluateGateTask(),
        )
    )
    return after


def _prepare_run_directory_argv(
    summary_path: Path, remote_run_dir: Path | None
) -> tuple[str, ...]:
    prepare_argv = ("mkdir", "-p", str(summary_path.parent))
    if remote_run_dir is not None:
        prepare_argv = (
            "sh",
            "-c",
            'set -eu; rm -rf -- "$1"; mkdir -p -- "$1"',
            "clean-loadtest-run",
            str(summary_path.parent),
        )
    return prepare_argv


def _build_park_at_zero_command(
    *,
    request: PlatformRequest,
    target: Any,
    executor: RoleBoundCommandTaskExecutor,
    hpa: bool,
) -> Task[Any]:
    return CommandTask(
        title="Wait for the autoscaler to park the function at zero",
        argv=(
            "bash",
            "-lc",
            # The function is created with one replica. Under HPA only the
            # autoscaler taking it to zero earns the ScaledToZero condition that
            # lets it come back up; under INTERNAL the wake path is the control
            # plane's own. Either way the run asserts it starts at its floor.
            f"deadline=$((SECONDS + {_HPA_METRIC_WAIT_SECONDS})); "
            "while [ $SECONDS -lt $deadline ]; do "
            f"[ \"$(sudo kubectl -n {request.namespace} get "
            f"deploy/fn-{target.name} -o jsonpath='{{.spec.replicas}}' "
            "2>/dev/null)\" = 0 ] && exit 0; "
            "sleep 5; done; "
            "echo 'function never parked at zero:'; "
            f"sudo kubectl -n {request.namespace} get "
            f"deploy/fn-{target.name} || true; "
            + (
                f"sudo kubectl -n {request.namespace} describe hpa fn-{target.name} || true; "
                if hpa
                else f"sudo kubectl -n {request.namespace} logs "
                "deploy/nanofaas-control-plane --tail=100 2>&1 | grep -i scal || true; "
            )
            + "exit 1",
        ),
        executor=executor,
        role="stack",
    )


def waits_for_parking(
    config: ScenarioConfig, backend: Backend, replica_floor: int
) -> bool:
    """Whether the run should wait for the function to be parked at zero first.

    Three conditions, and the first two used to be missing. The wait shells out
    to `kubectl`, so it means nothing off Kubernetes; and it waits for an
    autoscaler to do the parking, so it can never be satisfied on a run that has
    none. Gating on the replica floor alone was not enough because that floor is
    0 whenever HPA is off — true for plain runs and for the concurrency governor
    scenario, which is what finally tripped it: 182 seconds waiting for a
    parking that could not happen, then a failure on sudo.
    """
    return config.autoscaling and backend == "k8s" and replica_floor == 0


def _build_preflight(
    *,
    executor: RoleBoundCommandTaskExecutor,
    load_role: ExecutionRole,
    hpa: bool,
    parks_at_zero: bool,
    request: PlatformRequest,
    target: Any,
) -> Task[Any]:
    preflight: Task[Any] = CommandTask(
        title="Check k6 is usable",
        argv=("k6", "version"),
        executor=executor,
        role=load_role,
    )
    # Parking at zero is not an HPA speciality: the INTERNAL strategy is given the
    # same replica floor of 0 and scales down to it too. Without this wait the load
    # starts against a function that was never at zero, so the wake path — the
    # interesting half of scale-to-zero — goes unexercised and the run proves less
    # than it appears to.
    park_at_zero = _build_park_at_zero_command(
        request=request, target=target, executor=executor, hpa=hpa
    )
    if hpa:
        control_plane_metrics_path = (
            f"/api/v1/namespaces/{request.namespace}/services/"
            "http:control-plane:8081/proxy/actuator/prometheus"
        )
        hpa_metric_path = (
            f"/apis/external.metrics.k8s.io/v1beta1/namespaces/{request.namespace}/"
            f"nanofaas_rps?labelSelector=function%3D{target.name}"
        )
        preflight = Steps(
            title="Check HPA prerequisites",
            steps=(
                CommandTask(
                    title="Check HPA external metric is usable",
                    argv=(
                        "bash",
                        "-lc",
                        # The metric only becomes servable once Prometheus is up,
                        # has scraped the control plane (5s), and the adapter has
                        # relisted (10s) — a live run had it appear 50s after the
                        # HPA was created. Wait on a clock, not on an attempt
                        # count, and keep the loop out of `set -e`'s reach.
                        f"deadline=$((SECONDS + {_HPA_METRIC_WAIT_SECONDS})); "
                        "while [ $SECONDS -lt $deadline ]; do "
                        f"sudo kubectl get --raw {hpa_metric_path!r} >/dev/null 2>&1 && exit 0; "
                        "sleep 2; done; "
                        f"echo 'HPA external metric unavailable after {_HPA_METRIC_WAIT_SECONDS}s:'; "
                        f"sudo kubectl get hpa fn-{target.name} -n {request.namespace} || true; "
                        f"sudo kubectl -n {request.namespace} logs "
                        "deploy/nanofaas-hpa-metrics-adapter --tail=200 2>&1 "
                        "| grep -v healthz | tail -10 || true; "
                        f"sudo kubectl get --raw {control_plane_metrics_path!r} "
                        "| grep '^function_' || true; "
                        f"sudo kubectl -n {request.namespace} exec deploy/nanofaas-prometheus -- "
                        "wget -qO- 'http://localhost:9090/api/v1/query?query=function_dispatch_total' "
                        "|| true; "
                        f"sudo kubectl get --raw {hpa_metric_path!r} || true; exit 1",
                    ),
                    executor=executor,
                    role="stack",
                ),
                *((park_at_zero,) if parks_at_zero else ()),
                preflight,
            ),
        )
    elif parks_at_zero:
        # INTERNAL with the same replica floor of zero: no external metric to wait
        # on, but the same park to wait for.
        preflight = Steps(
            title="Check autoscaling prerequisites",
            steps=(park_at_zero, preflight),
        )
    return preflight


def build_loadtest_plan(
    config: ScenarioConfig,  # NOSONAR (S107): keyword-only inputs mix config, environment and optional overrides
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    control_plane_url: str,
    prometheus_client: PrometheusClient,
    run_dir: Path,
    remote_run_dir: Path | None = None,
    remote_repo_root: Path | None = None,
    fetcher: RemoteFileFetcher | object | None = None,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
    stages: tuple[tuple[str, int], ...] | None = None,
    prebuilt_control_plane_image: str | None = None,
    prebuilt_function_images: Mapping[str, str] | None = None,
) -> Workflow:
    """Compile the loadtest scenario into a Sonata workflow.

    Two halves. The platform — build, push, install the chart, register the
    functions — is `add_platform`, the same code `validate` uses; the legacy
    version reached into the validate module for `k8s_deployment_specs` to get
    it. The load itself is one composite, assembled here because it needs the
    k6 runner, the Prometheus client and the run directory, none of which
    belong in the task catalogue.
    """
    backend = _validate_loadtest(config, environment)
    hpa, replica_floor, scaling_config = _autoscaling_setup(config)
    scaling_config = scaling_config or _concurrency_control_setup(config)
    root = repo_root or Path.cwd()
    functions, prebuilt = _resolve_functions(
        config,
        repo_root,
        tool_root,
        scaling_config,
        prebuilt_control_plane_image,
        prebuilt_function_images,
        concurrency=_CONCURRENCY_CEILING if config.concurrency_control else 4,
        resources=_CONCURRENCY_FUNCTION_RESOURCES if config.concurrency_control else None,
    )
    target = functions[0]
    dedicated = "loadgen" in environment.roles
    remote = environment.provider != "local"
    script_path, summary_path = _resolve_script_and_summary(
        config,
        environment,
        remote=remote,
        dedicated=dedicated,
        remote_run_dir=remote_run_dir,
        run_dir=run_dir,
        tool_root=tool_root,
    )
    additional_modules = _additional_modules(
        config.autoscaling, hpa, config.concurrency_control
    )
    request = _build_platform_request(
        backend=backend,
        build=config.build,
        functions=functions,
        additional_modules=additional_modules,
        prebuilt=prebuilt,
        prebuilt_control_plane_image=prebuilt_control_plane_image,
        root=root,
        remote_repo_root=remote_repo_root,
        hpa=hpa,
    )
    load_role: ExecutionRole = "loadgen" if dedicated else "stack"
    executor = RoleBoundCommandTaskExecutor(bindings)
    platform_requires = _build_platform_requires(
        backend, executor, root, additional_modules
    )
    run_k6 = _build_run_k6(
        executor=executor,
        load_role=load_role,
        control_plane_url=control_plane_url,
        script_path=script_path,
        summary_path=summary_path,
        target=target,
        stages=stages,
        config=config,
    )
    watcher, replica_probe = _build_replica_watcher(
        config=config,
        backend=backend,
        bindings=bindings,
        control_plane_url=control_plane_url,
        target=target,
        request=request,
    )
    concurrency_watcher = _build_concurrency_watcher(
        config=config,
        control_plane_url=control_plane_url,
        target=target,
    )
    after = _build_steps_after(
        remote=remote,
        summary_path=summary_path,
        run_dir=run_dir,
        fetcher=fetcher,
        watcher=watcher,
        concurrency_watcher=concurrency_watcher,
        replica_probe=replica_probe,
        bindings=bindings,
        request=request,
        target=target,
        replica_floor=replica_floor,
        prometheus_client=prometheus_client,
    )
    prepare_argv = _prepare_run_directory_argv(summary_path, remote_run_dir)
    preflight = _build_preflight(
        executor=executor,
        load_role=load_role,
        hpa=hpa,
        parks_at_zero=waits_for_parking(config, backend, replica_floor),
        request=request,
        target=target,
    )

    return build_loadtest_workflow(
        request,
        bindings,
        cwd=root,
        requires=platform_requires,
        local_endpoint=control_plane_url,
        load=loadtest_composite(
            preflight=preflight,
            prepare=CommandTask(
                title="Prepare the run directory",
                argv=prepare_argv,
                executor=executor,
                role=load_role,
            ),
            run_k6=RunK6Task(
                run_k6=run_k6,
                # One sampler slot, and the two never coexist: a concurrency run
                # pins the replica count, an autoscaling run does not govern
                # concurrency.
                watcher=watcher or concurrency_watcher,
                initial_replicas=(
                    VerifyInitialAutoscalingReplicas(
                        replica_probe, expected_replicas=replica_floor
                    )
                    if replica_probe is not None
                    else None
                ),
            ),
            steps_after_run=tuple(after),
        ),
    )


class _AsResult:
    """Adapt a plain summary to the `.result` attribute WriteLoadtestSummary reads.

    That class takes the verifier object and reads `.result` off it. Here the
    numbers arrive as a value through the composite, so this hands them over in
    the shape it expects rather than changing a class three workflows share.
    """

    def __init__(self, result: Any) -> None:
        self.result = result
