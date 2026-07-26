import json
import urllib.request
from pathlib import Path

from sonata_engine import Resource, Workflow
from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.process import managed_process_resource
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.execution.roles import ExecutionRole

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import _resolve_function

LOCAL_ENDPOINT = "http://127.0.0.1:18080"
LOCAL_HEALTH_URL = "http://127.0.0.1:18081/actuator/health"
LOCAL_CONTROL_PLANE_BUILD_ARGV = (
    "./gradlew",
    ":control-plane:bootJar",
    "-PcontrolPlaneModules=container-deployment-provider",
    "--no-daemon",
)


def _local_control_plane(repo_root: Path) -> Resource:
    """The control plane running on this machine, deploying functions with Docker.

    The health probe deliberately targets the management port: the actuator never
    lives on the API port, which is what made the old `cli` preflight impossible
    to satisfy.
    """

    def ready() -> bool:
        try:
            with urllib.request.urlopen(LOCAL_HEALTH_URL, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    return managed_process_resource(
        title="Acquire local control plane",
        argv=(
            "java",
            "-jar",
            str(repo_root / "platform/control-plane/build/libs/app.jar"),
            "--server.port=18080",
            "--management.server.port=18081",
            "--sync-queue.enabled=false",
            "--nanofaas.deployment.default-backend=container-local",
            "--nanofaas.container-local.runtime-adapter=docker",
            "--nanofaas.container-local.bind-host=127.0.0.1",
        ),
        cwd=repo_root,
        ready=ready,
    )


def build_cli_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    cli_role: ExecutionRole = "host",
    endpoint: str | None = None,
    namespace: str = "nanofaas-e2e",
    repo_root: Path | None = None,
) -> Workflow:
    if config.workflow != "cli":
        raise ValueError("CLI plan requires a cli scenario")
    if config.backend not in ("container", "k8s"):
        raise ValueError(f"cli workflow supports container or k8s, not {config.backend!r}")
    root = repo_root or Path.cwd()
    local = config.backend == "container"
    if local and cli_role != "host":
        raise ValueError("container cli workflow must run on the host role")
    if not local and endpoint is None:
        raise ValueError("k8s cli workflow requires an explicit control-plane URL")
    target_endpoint = LOCAL_ENDPOINT if local else endpoint
    assert target_endpoint is not None
    functions = tuple(
        CliFunction(
            name=resolved.name,
            image=resolved.image,
            payload=json.dumps(json.loads(resolved.payload)["input"], separators=(",", ":")),
            resources=resolved.resources,
            build_argv=resolved.build_argv if local else None,
        )
        for key in config.functions
        for resolved in (_resolve_function(config, key),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role=cli_role,
        endpoint=target_endpoint,
        namespace=namespace,
    )
    requires = (_local_control_plane(root),) if local else ()
    return build_cli_workflow(
        request,
        bindings,
        cwd=root,
        control_plane_build_argv=LOCAL_CONTROL_PLANE_BUILD_ARGV if local else None,
        requires=requires,
    )
