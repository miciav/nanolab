from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from workflow_tasks.vm.models import VmRequest


class ResolvedFunctionView(Protocol):
    """Structural view of a resolved function, as read by components."""

    key: str
    family: str | None
    runtime: str
    image: str | None


class ResolvedScenarioView(Protocol):
    """Structural view of a resolved scenario, as read by components.

    A product-specific resolved scenario satisfies this protocol by shape.
    """

    namespace: str | None
    functions: Sequence[ResolvedFunctionView]


@dataclass(frozen=True, slots=True)
class ScenarioExecutionContext:
    """Neutral execution context consumed by scenario components.

    Deliberately free of product request types (E2eRequest/CliTestRequest): the
    factory that builds it lives in nanolab.
    """

    repo_root: Path
    scenario_name: str
    # str rather than a Literal/RuntimeKind to keep this library module free of
    # nanolab type aliases (do not narrow this back — it would re-introduce coupling).
    runtime: str
    namespace: str | None
    local_registry: str
    resolved_scenario: ResolvedScenarioView | None
    vm_request: VmRequest
    cleanup_vm: bool
    manifest_path: Path | None = None
    release: str | None = None
    loadgen_vm_request: VmRequest | None = None
    # nanolab verification commands, injected by the nanolab context factory.
    # Empty by default so this library module stays nanolab-agnostic.
    k3s_curl_verify_command: tuple[str, ...] = ()
    loadtest_run_command: tuple[str, ...] = ()
    autoscaling_command: tuple[str, ...] = ()
