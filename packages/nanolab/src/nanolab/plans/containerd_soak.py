"""Containerd soak composition around the shared workload and measurement."""

from dataclasses import replace
from pathlib import Path

from sonata_engine import Workflow
from sonata_tasks.execution.bindings import RoleBindings, RoleBoundCommandTaskExecutor

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import resolve_function, sonata_function
from nanolab.tasks.containerd_maven import repository_for_build
from nanolab.tasks.containerd_rootless import (
    control_plane_resource,
    registry_resource,
    run_for_environment,
)
from nanolab.tasks.platform import PlatformRequest, add_platform
from nanolab.tasks.soak.containerd_runtime import (
    BuildExecutionRecorder,
    ContainerdSoakRun,
)


def _run_image(function, run_id: str):
    """Tag every build for this owned run; the VM registry is shared by address."""
    image = function.image.rsplit(":", 1)[0] + ":soak-" + run_id
    return replace(
        function,
        image=image,
        build_argv=tuple(
            image if arg == function.image else arg for arg in function.build_argv
        ),
        image_build_argv=(
            tuple(
                image if arg == function.image else arg
                for arg in function.image_build_argv
            )
            if function.image_build_argv is not None
            else None
        ),
    )


def build_containerd_soak_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    run_dir: Path,
    repo_root: Path,
    tool_root: Path,
) -> Workflow:
    """Build and own a rootless stack before the shared soak measurement."""
    if config.backend != "containerd" or config.soak is None:
        raise ValueError("containerd soak requires its frozen protocol")
    if (
        config.soak.roles["control-plane"].runtime != "jvm"
        or config.soak.images["control-plane"].variant != "jvm"
    ):
        raise ValueError("containerd soak currently requires a JVM process artifact")
    for role, spec in config.soak.images.items():
        runtime = config.soak.roles[role].runtime
        expected_variant = {"jvm": "jvm", "node": "default"}.get(runtime)
        if (
            spec.mode != "build"
            or spec.variant != expected_variant
            or spec.build_options
            or (role != "control-plane" and spec.modules)
            or spec.artifact_kind
            != ("process" if role == "control-plane" else "oci-image")
        ):
            raise ValueError(f"unsupported containerd soak image recipe for {role}")
    if (
        config.soak.prerequisites.required_coverage
        or any(config.soak.diagnostics.operations.values())
        or any(config.soak.diagnostics.baseline_operations.values())
    ):
        raise ValueError(
            "containerd soak prerequisite and diagnostic adapters are required "
            "before P24 scenarios can run"
        )
    if environment.provider not in {"local", "multipass"}:
        raise ValueError(
            "containerd soak requires a local or owned Multipass environment"
        )
    run = run_for_environment(repo_root, tool_root, environment)
    executor = BuildExecutionRecorder(RoleBoundCommandTaskExecutor(bindings))
    registry = registry_resource(run, executor=executor, role="stack")
    control = control_plane_resource(
        run,
        executor=executor,
        role="stack",
        requires=(registry,),
        mode=config.soak.roles["control-plane"].runtime,
        artifact=run.repo_root / "platform/control-plane/build/libs/app.jar",
        cpu=config.soak.roles["control-plane"].expected_cpu,
        memory_bytes=config.soak.roles["control-plane"].memory_limit_bytes,
    )
    functions = tuple(
        _run_image(
            sonata_function(
                resolve_function(
                    config, key, source_root=repo_root, tool_root=tool_root
                )
            ),
            run.run_id,
        )
        for key in config.functions
    )
    requested_modules = config.soak.images["control-plane"].modules
    provider = "containerd-deployment-provider"
    if provider not in requested_modules:
        raise ValueError("containerd soak must build its declared provider module")
    request = PlatformRequest(
        backend="containerd",
        functions=functions,
        build_images=True,
        build_control_plane=True,
        push_function_images=True,
        containerd_maven_repository=repository_for_build(environment),
        additional_modules=tuple(
            module for module in requested_modules if module != provider
        ),
    )
    workflow = Workflow(workflow_id="containerd-soak")
    platform = add_platform(
        workflow,
        request,
        executor=executor,
        cwd=repo_root,
        control_plane_process=lambda: control,
        requires=(registry,),
    )
    workflow.add(
        ContainerdSoakRun(
            config,
            run,
            environment,
            executor,
            run_dir=run_dir,
            repo_root=repo_root,
            functions=functions,
        ),
        requires=(registry, *platform.resources, *platform.functions),
    )
    return workflow
