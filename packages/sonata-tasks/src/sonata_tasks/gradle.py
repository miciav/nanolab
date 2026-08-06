from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole

from sonata_tasks.command import CommandTask


class GradleTask(CommandTask):
    """Run Gradle targets through the repository's wrapper.

    `properties` is the reason this exists rather than a hand-written argv:
    selecting control-plane modules means `-PcontrolPlaneModules=a,b`, and every
    workflow that builds the control plane was assembling that string itself.

    `--no-daemon` is not optional. These builds run on throwaway VMs and in
    one-shot workflows, where a surviving daemon holds memory nothing reclaims.
    A workflow that genuinely wants the daemon can still use `CommandTask`.

    `role` is required: building on the host and building inside a VM use
    different toolchains and leave the artifacts somewhere else.

    `env` forwards environment variables through the executor to the remote
    process (e.g. NANOFAAS_RUN_K8S_E2E, KUBECONFIG).
    """

    def __init__(
        self,
        *targets: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        properties: Mapping[str, str] | None = None,
        title: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not targets:
            raise ValueError("a Gradle task needs at least one target")
        property_args = tuple(f"-P{name}={value}" for name, value in (properties or {}).items())
        super().__init__(
            title=title or f"Run gradle {' '.join(targets)}",
            argv=("./gradlew", *targets, *property_args, "--no-daemon"),
            executor=executor,
            role=role,
            cwd=cwd,
            env=MappingProxyType(dict(env or {})),
        )
