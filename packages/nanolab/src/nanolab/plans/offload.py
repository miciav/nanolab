"""Assemble the two-instance offload scenario into an executable workflow."""

import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Workflow
from sonata_tasks.offload import (
    OffloadFunction,
    OffloadWorkflowRequest,
    build_offload_workflow,
)
from sonata_tasks.process import managed_process_resource
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor
from sonata_tasks.registry import docker_registry_resource

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import _resolve_function

JAR_PATH = "platform/control-plane/build/libs/app.jar"

# Two control planes on one machine, so the ports are split rather than
# discovered. The edge reuses validate's pair because a container-backed
# nanoFaaS always answers there; the cloud takes the next block.
EDGE_ENDPOINT = "http://127.0.0.1:18080"
EDGE_MANAGEMENT = "http://127.0.0.1:18081"
CLOUD_ENDPOINT = "http://127.0.0.1:19090"
CLOUD_MANAGEMENT = "http://127.0.0.1:19091"


def _health_probe(management_url: str) -> Callable[[], bool]:
    """Readiness on the management port: the actuator never lives on the API one."""
    health_url = f"{management_url}/actuator/health"

    def ready() -> bool:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    return ready


def _cloud_argv(repo_root: Path) -> tuple[str, ...]:
    """The instance that actually runs the function, on the container backend."""
    return (
        "java",
        "-jar",
        str(repo_root / JAR_PATH),
        "--server.port=19090",
        "--management.server.port=19091",
        "--sync-queue.enabled=false",
        "--nanofaas.deployment.default-backend=container-local",
        "--nanofaas.container-local.runtime-adapter=docker",
        "--nanofaas.container-local.bind-host=127.0.0.1",
    )


def _edge_argv(repo_root: Path) -> tuple[str, ...]:
    """The instance that holds no implementation and proxies to the cloud."""
    return (
        "java",
        "-jar",
        str(repo_root / JAR_PATH),
        "--server.port=18080",
        "--management.server.port=18081",
        "--sync-queue.enabled=false",
        f"--nanofaas.offload.target-url={CLOUD_ENDPOINT}",
    )


def _plane(
    title: str, argv: tuple[str, ...], management_url: str, repo_root: Path
) -> Callable[[], Resource[Any]]:
    def factory() -> Resource[Any]:
        return managed_process_resource(
            title=title,
            argv=argv,
            cwd=repo_root,
            ready=_health_probe(management_url),
        )

    return factory


def build_offload_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    repo_root: Path | None = None,
) -> Workflow:
    """Compile the offload scenario into a Sonata workflow.

    The legacy builder rendered every task as a spec, then took the result apart
    to inject the two control planes: it filtered the start ids out, built the
    rest, and inserted resources back at the indices the removed specs had
    occupied. They are resources the workflow is handed now, and the teardown —
    two registrations and two processes — is the release half of what it holds.
    """
    if config.workflow != "offload":
        raise ValueError("offload plan requires an offload scenario")
    root = repo_root or Path.cwd()
    request = OffloadWorkflowRequest(
        functions=tuple(
            OffloadFunction(
                name=resolved.name,
                image=resolved.image,
                payload=resolved.payload,
                build_argv=resolved.build_argv,
                timeout_ms=resolved.timeout_ms,
                concurrency=resolved.concurrency,
                queue_size=resolved.queue_size,
                max_retries=resolved.max_retries,
            )
            for key in config.functions
            for resolved in (_resolve_function(config, key),)
        ),
        cloud_endpoint=CLOUD_ENDPOINT,
        edge_endpoint=EDGE_ENDPOINT,
        edge_management=EDGE_MANAGEMENT,
    )
    registry = docker_registry_resource(
        executor=RoleBoundCommandTaskExecutor(bindings), role="host"
    )
    return build_offload_workflow(
        request,
        bindings,
        cwd=root,
        cloud=_plane(
            "Acquire cloud control plane", _cloud_argv(root), CLOUD_MANAGEMENT, root
        ),
        edge=_plane("Acquire edge control plane", _edge_argv(root), EDGE_MANAGEMENT, root),
        push_function_images=True,
        push_requires=(registry,),
        function_requires=(registry,),
    )
