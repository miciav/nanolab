import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from sonata_engine import Resource, Workflow
from sonata_tasks.process import managed_process_resource
from sonata_tasks.validate import ValidateFunction as SonataFunction
from sonata_tasks.validate import ValidateWorkflowRequest, build_validate_workflow
from workflow_tasks.components.helm import control_plane_helm_values
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.workflows.validate import ValidateFunction

from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import FunctionDefinition, resolve_function_definition
from nanolab.plans import _local_control_plane
from nanolab.workspace.paths import discover_tool_root


def _container_control_plane(repo_root: Path) -> Resource[Any]:
    """The control plane running on this machine, deploying functions with Docker."""
    return managed_process_resource(
        title="Acquire local control plane",
        argv=_local_control_plane.argv(repo_root),
        cwd=repo_root,
        ready=_local_control_plane.ready,
    )


def _function_image(definition: FunctionDefinition) -> str:
    if definition.default_image is None:
        raise ValueError(f"function {definition.key!r} has no image")
    return definition.default_image


def _build_argv(definition: FunctionDefinition, image: str) -> tuple[str, ...]:
    family = definition.family
    runtime = definition.runtime
    if runtime == "java":
        return (
            "./gradlew",
            f":functions:java:{family}:bootJar",
            "--quiet",
        )
    runtime_dir = {"java-lite": "java", "exec": "bash"}.get(runtime, runtime)
    suffix = "-lite" if runtime == "java-lite" else ""
    return (
        "docker",
        "build",
        "-t",
        image,
        "-f",
        f"functions/{runtime_dir}/{family}{suffix}/Dockerfile",
        ".",
    )


def _image_build_argv(definition: FunctionDefinition, image: str) -> tuple[str, ...] | None:
    if definition.runtime != "java":
        return None
    family = definition.family
    return (
        "docker", "build", "-t", image, "-f", f"functions/java/{family}/Dockerfile", f"functions/java/{family}"
    )


def _function_name(definition: FunctionDefinition) -> str:
    if definition.example_dir is None:
        return definition.key
    manifest = yaml.safe_load(
        (definition.example_dir / "function.yaml").read_text(encoding="utf-8")
    )
    return str(manifest.get("name", definition.key))


def _payload(definition: FunctionDefinition, tool_root: Path | None = None) -> str:
    if definition.default_payload_file is None:
        return '{"input":{}}'
    product_root = tool_root or discover_tool_root()
    payload_path = (
        product_root / "scenarios" / "payloads" / definition.default_payload_file
    )
    if not payload_path.exists():
        return '{"input":{}}'
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return json.dumps({"input": payload}, separators=(",", ":"))


def _resolve_function(
    config: ScenarioConfig,
    key: str,
    *,
    tool_root: Path | None = None,
) -> ValidateFunction:
    definition = resolve_function_definition(key)
    image = _function_image(definition)
    resource = config.resources.get(key)
    return ValidateFunction(
        key=key,
        name=_function_name(definition),
        image=image,
        build_argv=_build_argv(definition, image),
        image_build_argv=_image_build_argv(definition, image),
        payload=_payload(definition, tool_root),
        resources=(
            resource.model_dump(by_alias=True, exclude_none=True) if resource is not None else None
        ),
    )


def _sonata_function(resolved: ValidateFunction) -> SonataFunction:
    """The same resolved function in the Sonata catalogue's shape.

    `loadtest`, `offload` and `offload-loadtest` still consume the legacy type,
    so resolution keeps producing it and only the workflow that has moved
    converts. This goes away with the last of them; the dropped `key` existed
    only to spell task ids by hand.
    """
    return SonataFunction(
        name=resolved.name,
        image=resolved.image,
        payload=resolved.payload,
        build_argv=resolved.build_argv,
        image_build_argv=resolved.image_build_argv,
        resources=resolved.resources,
        scaling_config=resolved.scaling_config,
        timeout_ms=resolved.timeout_ms,
        concurrency=resolved.concurrency,
        queue_size=resolved.queue_size,
        max_retries=resolved.max_retries,
    )


def _set_args(values: dict[str, str]) -> tuple[str, ...]:
    args: list[str] = []
    for key, value in values.items():
        args.extend(["--set", f"{key}={value}"])
    return tuple(args)


def build_validate_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
) -> Workflow:
    """Compile the validate scenario into a Sonata workflow.

    The legacy builder had to take the rendered specs apart to work: it filtered
    out `container.start.control-plane` by task id, built the rest, then inserted
    a resource back at the index the removed spec had occupied, and finally
    attached a separate cleanup list the runner had to remember to execute. The
    control plane is now a resource the workflow is given, and the teardown is
    the release half of the resources it holds.
    """
    if config.workflow != "validate" or config.backend is None:
        raise ValueError("validate plan requires a validate scenario with a backend")
    root = repo_root or Path.cwd()
    kubernetes = config.backend == "k8s"
    request = ValidateWorkflowRequest(
        backend=config.backend,
        build=config.build,
        functions=tuple(
            _sonata_function(_resolve_function(config, key, tool_root=tool_root))
            for key in config.functions
        ),
        additional_modules=("async-queue", "sync-queue") if kubernetes else (),
    )
    if kubernetes:
        # Both settings are what this workflow exists to exercise: the JUnit queue
        # contracts need admission on, and the metric assertions need the advanced
        # profile. Derived from the request so the chart and the pushed image can
        # never name different things.
        request = replace(
            request,
            helm_values=_set_args(
                control_plane_helm_values(
                    namespace=request.namespace,
                    control_plane_image=request.control_plane_image_reference(),
                    metrics_profile="advanced",
                    sync_queue_admission_enabled=True,
                )
            ),
        )
    return build_validate_workflow(
        request,
        bindings,
        cwd=root,
        control_plane_process=(
            None if kubernetes else lambda: _container_control_plane(root)
        ),
        local_endpoint=_local_control_plane.ENDPOINT,
    )
