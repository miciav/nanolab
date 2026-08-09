from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Literal
import urllib.request

from multipass import MultipassClient
from sonata_engine import Steps, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.loadtest import FetchResultsTask, RunK6Task
from sonata_tasks.offload_loadtest import (
    EvaluateConservationTask,
    OffloadLoadtestRequest,
    build_offload_loadtest_workflow,
)
from sonata_tasks.platform import PlatformFunction, PlatformRequest
from sonata_tasks.components.helm import control_plane_helm_values
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.k6 import K6Task
from sonata_tasks.loadtest.models import K6Config
from sonata_tasks.loadtest.offload_conservation import evaluate_conservation
from sonata_tasks.loadtest.ports import RemoteFileFetcher
from sonata_tasks.loadtest.tasks import FetchVmResults
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.multipass import resolve_connection_host

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.workspace.paths import discover_tool_root
from nanolab.plans.validate import _resolve_function, _set_args, _sonata_function

_ACTUATOR_PORT = 30081
_CONTROL_PLANE_PORT = 30080
# The pressure-offload trigger fires in bursts correlated with the control
# plane's 10s throughput window, not smoothly — a lower offloadable rate keeps
# each burst small enough for the cloud's own admission control to absorb.
_OFFLOADABLE_RATE = "10"
_CONTROL_RATE = "20"
_K6_DURATION = "60s"

Role = Literal["stack", "cloud"]


def _role_host(environment: EnvironmentConfig, role: Role, *, dry_run: bool) -> str:
    target = environment.target(role)
    if environment.provider == "local":
        return "127.0.0.1"
    if environment.provider == "multipass":
        return resolve_connection_host(
            VmRequest(lifecycle="multipass", name=target.name),
            MultipassClient(),
            dry_run=dry_run,
        )
    if target.host:
        return target.host
    raise ValueError(f"{environment.provider} {role} requires a host or explicit URLs")


def _fetch_text(url: str, *, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return response.read().decode("utf-8")


def format_offload_summary(numbers: dict[str, float]) -> str:
    no_offloads = numbers["k6_offloaded_requests"] == 0
    rows = (
        ("k6_offloadable_requests", ""),
        (
            "edge_function_success_offloadable",
            "Edge served all requests." if no_offloads else "",
        ),
        ("k6_offloaded_requests", ""),
        ("edge_offload_total", "Edge did not offload any requests." if no_offloads else ""),
        (
            "cloud_function_success_offloadable",
            "Cloud did not process any offloaded requests." if no_offloads else "",
        ),
    )
    width = max(len(label) for label, _ in rows)
    return "\n".join(
        ["Offload load-test summary"]
        + [
            f"{label + ':':<{width + 2}} {numbers[label]:g}{'  ' + note if note else ''}"
            for label, note in rows
        ]
    )


@dataclass
class EvaluateOffloadConservation:
    task_id: str
    title: str
    k6_summary_path: Path
    edge_metrics_url: str
    cloud_metrics_url: str
    offloadable: str
    control: str
    output_path: Path

    def run(self) -> dict[str, object]:
        k6_summary = json.loads(self.k6_summary_path.read_text(encoding="utf-8"))
        edge_metrics = _fetch_text(self.edge_metrics_url)
        cloud_metrics = _fetch_text(self.cloud_metrics_url)
        report = evaluate_conservation(
            k6_summary=k6_summary,
            edge_metrics=edge_metrics,
            cloud_metrics=cloud_metrics,
            offloadable=self.offloadable,
            control=self.control,
        )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(
                {
                    "passed": report.passed,
                    "failures": list(report.failures),
                    "numbers": report.numbers,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if not report.passed:
            raise RuntimeError(
                "offload conservation check failed: " + "; ".join(report.failures)
            )
        return {"passed": report.passed}




def _offload_target(values: dict[str, str], target_url: str) -> dict[str, str]:
    """Tell the edge's control plane where to offload to.

    The chart takes extra environment as an indexed list, so the entry goes
    after whatever `control_plane_helm_values` already put there — appending at
    a fixed index would overwrite one of them.
    """
    used = [
        int(key.split("[", 1)[1].split("]", 1)[0])
        for key in values
        if key.startswith("controlPlane.extraEnv[")
    ]
    index = max(used) + 1 if used else 0
    return {
        **values,
        f"controlPlane.extraEnv[{index}].name": "NANOFAAS_OFFLOAD_TARGETURL",
        f"controlPlane.extraEnv[{index}].value": target_url,
    }


def _platform(
    functions: tuple[PlatformFunction, ...],
    *,
    label: str,
    role: Literal["stack", "cloud"],
    build: str,
    offload_target: str | None = None,
) -> PlatformRequest:
    request = PlatformRequest(
        backend="k8s",
        build=build,  # pyright: ignore[reportArgumentType]
        functions=functions,
        additional_modules=("offload", "async-queue", "sync-queue"),
        execution_role=role,
        label=label,
    )
    values = control_plane_helm_values(
        namespace=request.namespace,
        control_plane_image=request.control_plane_image_reference(),
        expose_node_port=True,
        metrics_profile="advanced",
    )
    if offload_target is not None:
        values = _offload_target(values, offload_target)
    return replace(request, helm_values=_set_args(values))


def build_offload_loadtest_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
    fetcher: RemoteFileFetcher | None = None,
    dry_run: bool = False,
) -> Workflow:
    """Compile the offload-loadtest scenario into a Sonata workflow.

    Two clusters, each `add_platform` — the same code validate and loadtest use.
    The legacy builder had its own `cloud_deployment_specs` and
    `edge_deployment_specs`, both re-roled copies of validate's, plus a cleanup
    list the runner had to remember for four registrations across two clusters.
    """
    if config.workflow != "offload-loadtest":
        raise ValueError("offload load-test plan requires an offload-loadtest scenario")
    if len(config.functions) != 2:
        raise ValueError(
            "offload-loadtest requires exactly two functions: [offloadable, control]"
        )
    offloadable_key, control_key = config.functions
    # timeout_ms is also the offload gateway's remote-call budget (edge gives up
    # locally once it elapses) — the default 5s is too tight for a cross-VM hop
    # to a function pod that may still be warming up under real load.
    offloadable = replace(
        _sonata_function(_resolve_function(config, offloadable_key, tool_root=tool_root)),
        concurrency=2,
        queue_size=8,
        timeout_ms=15000,
    )
    control = replace(
        _sonata_function(_resolve_function(config, control_key, tool_root=tool_root)),
        concurrency=2,
        queue_size=8,
        # The control must never be offloaded: if it were, the conservation
        # check could not tell offloaded traffic from ordinary traffic.
        offload={"enabled": False},
    )

    root = repo_root or Path.cwd()
    edge_host = _role_host(environment, "stack", dry_run=dry_run)
    cloud_host = _role_host(environment, "cloud", dry_run=dry_run)
    edge_url = f"http://{edge_host}:{_CONTROL_PLANE_PORT}"
    cloud_url = f"http://{cloud_host}:{_CONTROL_PLANE_PORT}"

    request = OffloadLoadtestRequest(
        # The cloud absorbs whatever the edge sheds, so it must not reproduce the
        # edge's own admission pressure: registering it with the edge's tight
        # concurrency made the cloud reject most offloaded calls itself.
        cloud=_platform(
            (replace(offloadable, concurrency=20, queue_size=100),),
            label="cloud",
            role="cloud",
            build=config.build,
        ),
        edge=_platform(
            (offloadable, control),
            label="edge",
            role="stack",
            build=config.build,
            offload_target=cloud_url,
        ),
    )

    dedicated_loadgen = "loadgen" in environment.roles
    k6_role: Literal["stack", "loadgen"] = "loadgen" if dedicated_loadgen else "stack"
    remote = dedicated_loadgen and environment.provider != "local"
    if remote:
        role_target = environment.target("loadgen")
        home = role_target.home or (
            "/root" if role_target.user == "root" else f"/home/{role_target.user}"
        )
        script_path = Path(home) / "nanolab-assets/k6/offload-mixed.js"
        summary_path = Path(home) / "nanofaas-loadtest/k6-summary.json"
    else:
        product_root = tool_root or discover_tool_root()
        script_path = product_root / "assets/k6/offload-mixed.js"
        summary_path = run_dir / "k6-summary.json"

    executor = RoleBoundCommandTaskExecutor(bindings)
    steps: list[Any] = [
        CommandTask(
            title="Check k6 is usable",
            argv=("k6", "version"),
            executor=executor,
            role=k6_role,
            cwd=root,
        ),
        CommandTask(
            title="Prepare the run directory",
            argv=("mkdir", "-p", str(summary_path.parent)),
            executor=executor,
            role=k6_role,
            cwd=root,
        ),
        RunK6Task(
            run_k6=K6Task(
                executor=executor,
                role=k6_role,
                title="Run k6",
                config=K6Config(
                    script_path=script_path,
                    target_url=edge_url,
                    summary_output_path=summary_path,
                    env={
                        "NANOFAAS_URL": edge_url,
                        "OFFLOADABLE_FUNCTION": offloadable.name,
                        "CONTROL_FUNCTION": control.name,
                        "OFFLOADABLE_RATE": _OFFLOADABLE_RATE,
                        "CONTROL_RATE": _CONTROL_RATE,
                        "DURATION": _K6_DURATION,
                    },
                ),
                remote_dir=".",
            ),
            title="Run the mixed-policy k6 script",
        ),
    ]

    local_summary_path = summary_path
    if remote:
        if fetcher is None:
            raise ValueError("fetcher is required to retrieve remote k6 results")
        steps.append(
            FetchResultsTask(
                fetch=FetchVmResults(
                    task_id="",
                    title="Fetch k6 results",
                    fetcher=fetcher,
                    remote_source=str(summary_path),
                    local_dest=run_dir,
                )
            )
        )
        local_summary_path = run_dir / summary_path.name

    steps.append(
        EvaluateConservationTask(
            evaluate=EvaluateOffloadConservation(
                task_id="",
                title="Evaluate offload conservation",
                k6_summary_path=local_summary_path,
                edge_metrics_url=f"http://{edge_host}:{_ACTUATOR_PORT}/actuator/prometheus",
                cloud_metrics_url=f"http://{cloud_host}:{_ACTUATOR_PORT}/actuator/prometheus",
                offloadable=offloadable.name,
                control=control.name,
                output_path=run_dir / "offload-report.json",
            )
        )
    )

    return build_offload_loadtest_workflow(
        request,
        bindings,
        cwd=root,
        load=Steps(title="Run the offload load test", steps=tuple(steps)),
    )
