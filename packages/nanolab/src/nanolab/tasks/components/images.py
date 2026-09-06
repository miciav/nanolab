from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from nanolab.tasks.components.context import ScenarioExecutionContext
from nanolab.tasks.components.operations import RemoteCommandOperation


def control_image(local_registry: str) -> str:
    return f"{local_registry}/nanofaas/control-plane:e2e"


def warm_echo_image(local_registry: str) -> str:
    return f"{local_registry}/nanofaas/java-warm-echo:e2e"


def function_image_specs(
    resolved_scenario,
    fallback_runtime_image: str,
) -> list[tuple[str, str, str, str]]:
    """Return (image, runtime_kind, family, fn_key) for each buildable function."""
    if resolved_scenario is None:
        return []

    function_specs: list[tuple[str, str, str, str]] = []
    for function in resolved_scenario.functions:
        if function.runtime == "fixture" or function.family is None:
            continue
        image = function.image or fallback_runtime_image
        function_specs.append((image, function.runtime, function.family, function.key))
    return function_specs


def _frozen_env() -> Mapping[str, str]:
    return MappingProxyType({})


_RUST_CP_DIR = (
    "experiments/control-plane-staging/versions"
    "/control-plane-rust-m3-20260222-200159/snapshot/control-plane-rust"
)


def plan_build_core(context: ScenarioExecutionContext) -> tuple[RemoteCommandOperation, ...]:
    control_plane_image = control_image(context.local_registry)
    warm_echo = warm_echo_image(context.local_registry)
    if context.runtime == "rust":
        control_context = _RUST_CP_DIR
        control_dockerfile = f"{_RUST_CP_DIR}/Dockerfile"
    else:
        control_context = "platform/control-plane"
        control_dockerfile = "platform/control-plane/Dockerfile"

    operations: list[RemoteCommandOperation] = []

    if context.runtime != "rust":
        # Rust Dockerfile is a self-contained multi-stage build (cargo runs inside Docker);
        # no pre-build step is needed on the VM.
        operations.append(
            RemoteCommandOperation(
                operation_id="images.build_core.boot_jars",
                summary="Build core JVM artifacts",
                argv=(
                    "./gradlew",
                    ":control-plane:bootJar",
                    ":services:java:warm-echo:bootJar",
                    "--no-daemon",
                    "-q",
                ),
                env=_frozen_env(),
                execution_target="vm",
            )
        )

    operations.extend(
        [
            RemoteCommandOperation(
                operation_id="images.build_core.control_image",
                summary="Build control-plane image",
                argv=(
                    "docker",
                    "build",
                    "-f",
                    control_dockerfile,
                    "-t",
                    control_plane_image,
                    control_context,
                ),
                env=_frozen_env(),
                execution_target="vm",
            ),
            RemoteCommandOperation(
                operation_id="images.build_core.warm_echo_image",
                summary="Build warm-echo image",
                argv=(
                    "docker",
                    "build",
                    "-f",
                    "services/java/warm-echo/Dockerfile",
                    "-t",
                    warm_echo,
                    "services/java/warm-echo",
                ),
                env=_frozen_env(),
                execution_target="vm",
            ),
            RemoteCommandOperation(
                operation_id="images.build_core.push_control_image",
                summary="Push control-plane image",
                argv=("docker", "push", control_plane_image),
                env=_frozen_env(),
                execution_target="vm",
            ),
            RemoteCommandOperation(
                operation_id="images.build_core.push_warm_echo_image",
                summary="Push warm-echo image",
                argv=("docker", "push", warm_echo),
                env=_frozen_env(),
                execution_target="vm",
            ),
        ]
    )
    return tuple(operations)
