"""Executable local soak wiring and native producer acceptance manifests.

All constructors are inert. Docker/process/HTTP observations happen only inside
Sonata task/resource acquisition. Missing evidence is never inferred from policy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_engine.journal import JournalConfig

from nanolab.config.soak import SoakConfig
from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.manifest import FunctionManifest
from nanolab.tasks.platform import PlatformFunction, PlatformRequest
from nanolab.tasks.soak.adapters import RoleBinding, RoleBoundProbe, SubprocessTransport
from nanolab.tasks.soak.artifacts import (
    describe_artifact,
    enforce_limit,
    fingerprint,
    measure_tree,
)
from nanolab.tasks.soak.collector import _docker_get, collect_procfs, read_bounded
from nanolab.tasks.soak.diagnostic_helper import (
    DockerHelperSpec,
    LocalDockerDiagnosticProvisioner,
)
from nanolab.tasks.soak.diagnostics import DiagnosticBudget, supported_operations
from nanolab.tasks.soak.evaluate import combine_results, evaluate_run
from nanolab.tasks.soak.helper_build import HELPER_BUILDER
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.observer import Observer, SystemClock
from nanolab.tasks.soak.preflight import preflight
from nanolab.tasks.soak.preparation import (
    PreparationOptions,
    PreparedSoak,
    prepare_soak,
)
from nanolab.tasks.soak.report import write_report
from nanolab.tasks.soak.workflow import (
    LifecycleHooks,
    LifecycleState,
    SoakLifecycle,
    TerminalSoakLifecycle,
    make_workload_driver_factory,
    observer_startup_timeout,
    run_prerequisite_gate,
    write_policy_input,
    write_terminal_receipt,
)

if TYPE_CHECKING:
    from nanolab.tasks.soak.containerd_runtime import (
        ContainerdDeployment,
        PreparedContainerdSoak,
    )


@dataclass(frozen=True)
class RuntimeDeployment:
    """Owned deployment contract; discovery/observations must query live state."""

    project: DockerComposeProject
    request: PlatformRequest
    ownership: Resource[Any]
    api_endpoint: str
    metrics_endpoints: dict[str, str | None]
    discover: Callable[[], tuple[Target, ...]]
    observations: Callable[[tuple[Target, ...]], dict[str, Any]]
    diagnostic_inputs: dict[str, Any] | None = None


@dataclass(frozen=True)
class RuntimeOptions:
    """Inject real operational adapters without changing the acceptance policy."""

    preparation: PreparationOptions = field(default_factory=PreparationOptions)
    prepared: PreparedSoak | None = None
    deployment_factory: Callable[[PreparedSoak, Path], RuntimeDeployment] | None = None
    observations: (
        Callable[[PreparedSoak, tuple[Target, ...]], dict[str, Any]] | None
    ) = None
    prerequisite_inputs: dict[str, object] | None = None
    docker_socket: str = "/var/run/docker.sock"
    memory_helper_image: str | None = None
    helper_builder: str = HELPER_BUILDER
    allow_diagnostic_target_stop_on_cancel: bool = False
    prerequisite_parent_artifact_bytes: int | None = 32 * 1024 * 1024
    prerequisite_lifetime_artifact_bytes: int | None = 64 * 1024 * 1024
    prerequisite_recovery_artifact_bytes: int | None = 32 * 1024 * 1024
    prerequisite_acquire_timeout_s: float = 60
    prerequisite_release_timeout_s: float = 35

    def __post_init__(self) -> None:
        """Require explicit immutable helper selection without probing Docker."""
        _validate_memory_helper_image(self.memory_helper_image)
        if type(self.allow_diagnostic_target_stop_on_cancel) is not bool:
            raise ValueError("diagnostic target-stop permission must be explicit")
        for quota in (
            self.prerequisite_parent_artifact_bytes,
            self.prerequisite_lifetime_artifact_bytes,
            self.prerequisite_recovery_artifact_bytes,
        ):
            if quota is not None and (type(quota) is not int or quota <= 0):
                raise ValueError(
                    "prerequisite artifact quotas must be positive integers"
                )
        for timeout in (
            self.prerequisite_acquire_timeout_s,
            self.prerequisite_release_timeout_s,
        ):
            if (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("prerequisite timeouts must be finite and positive")


_NODE_CONTROLLER = "/opt/nanolab/node-diagnostic-control.cjs"
_NODE_PRELOAD = "--require=" + _NODE_CONTROLLER
_DIAGNOSTIC_RECEIPT_BYTES = 65536
_PREREQUISITE_SETTLEMENT_MARGIN_S = 5.0


def _close_all(leases: list[socket.socket]) -> None:
    """Release every reserved port, so none of them leaks past the run."""
    for lease in leases:
        lease.close()


def _runtime_preparation_options(config, options: RuntimeOptions) -> PreparationOptions:
    """Declare available wiring before build, never actual target capabilities."""
    preparation = options.preparation
    if (
        config.prerequisites.mode == "run"
        and config.prerequisites.required_coverage
        and preparation.prerequisite_runner is None
    ):
        _check_prerequisite_resources(config, options)
        preparation = replace(preparation, prerequisite_provider_available=True)
    if (
        (
            not any(config.diagnostics.operations.values())
            and not any(config.diagnostics.baseline_operations.values())
        )
        or preparation.diagnostic_adapter is not None
        or options.deployment_factory is not None
    ):
        return preparation
    if not options.allow_diagnostic_target_stop_on_cancel:
        raise ValueError("diagnostics require explicit owned-target stop permission")
    for role in set(config.diagnostics.operations) | set(
        config.diagnostics.baseline_operations
    ):
        # Both checkpoints' declarations, because the provider routes whichever
        # checkpoint asks. Reading only the final one let a baseline reading
        # reach preparation and fail there, before any measurement.
        operations = [
            *config.diagnostics.operations.get(role, []),
            *config.diagnostics.baseline_operations.get(role, []),
        ]
        if not operations:
            continue
        policy = config.roles[role]
        # The provider's own declaration, not a second copy of it: an operation
        # this set does not carry is one it cannot dispatch, and naming the set
        # here is how the two drifted apart before.
        if set(operations) - supported_operations(policy.runtime):
            raise ValueError(
                "local diagnostic provider cannot route requested operations"
            )
        image = config.diagnostics.helper_images.get(role)
        if image is None:
            raise ValueError(f"diagnostic helper_images missing role {role}")
        _validate_memory_helper_image(image)
        if policy.runtime == "node" and _NODE_PRELOAD not in policy.runtime_options:
            raise ValueError("Node diagnostic preload must be declared before build")
    return replace(preparation, diagnostic_provider_available=True)


def _check_prerequisite_resources(config, options: RuntimeOptions) -> None:
    """Check bounded provider resources without requiring post-build identities."""
    if options.docker_socket != "/var/run/docker.sock":
        raise ValueError(
            "automatic prerequisites require the local default Docker socket"
        )
    parent, lifetime, recovery = (
        options.prerequisite_parent_artifact_bytes,
        options.prerequisite_lifetime_artifact_bytes,
        options.prerequisite_recovery_artifact_bytes,
    )
    if any(
        type(quota) is not int or quota <= 0 for quota in (parent, lifetime, recovery)
    ):
        raise ValueError(
            "automatic prerequisites require explicit parent/lifetime/recovery quotas"
        )
    assert parent is not None and lifetime is not None and recovery is not None  # nosec B101 - validated invariant/type narrowing
    count = len(config.prerequisites.required_coverage)
    if parent + count * (lifetime + recovery) > config.artifact_limit_bytes:
        raise ValueError("prerequisite reservations exceed the global artifact budget")
    if (
        options.prerequisite_acquire_timeout_s < 60
        or options.prerequisite_release_timeout_s <= 30
    ):
        raise ValueError(
            "prerequisite acquisition requires 60s and release strictly above 30s"
        )


def _population_retention(config, population: str) -> float:
    """Map retained owner populations to the declared authoritative lifetime."""
    if population in {"outcomes", "expiry_queue_depth"}:
        return (
            config.retention_s["unkeyed-sync-outcome"]
            + _PREREQUISITE_SETTLEMENT_MARGIN_S
        )
    if population == "idempotency_entries":
        return (
            config.retention_s["terminal-key-and-readable-outcome"]
            + _PREREQUISITE_SETTLEMENT_MARGIN_S
        )
    return 0


def _freeze_prerequisite_inputs(prepared) -> dict[str, Any]:
    """Derive the built-in sync gate only after images and payloads are frozen."""
    from nanolab.tasks.soak.prerequisites import (
        _inputs,
        _required_populations,
        select_relevant_config,
    )

    config = prepared.config
    coverage = frozenset(config.prerequisites.required_coverage)
    if coverage != {"sync"}:
        raise ValueError(
            "built-in prerequisites support only sync; other profiles require "
            "explicit fault-capable recipes"
        )
    roles = [role for role in config.roles if role != "control-plane"]
    if not roles:
        raise ValueError("sync prerequisite requires an SDK function role")
    role = next(
        (name for name in roles if config.roles[name].runtime == "jvm"), roles[0]
    )
    case = prepared.payloads[role][0]
    payload = prepared.writer.write_json("prerequisite-payload.json", case["input"])
    script = prepared.writer.write_json(
        "prerequisite-script.json",
        {
            "schema": "nanolab-soak-prerequisite-script-v1",
            "implementation": "nanolab.tasks.soak.prerequisite_runtime",
            "coverage": ["sync"],
        },
    )
    populations = _required_populations(
        prepared.images, coverage, config.metrics_profile
    )
    profile: dict[str, Any] = {
        "function": role,
        "role": role,
        "request": {"input": deepcopy(case["input"])},
        "expected_output": deepcopy(case["expected"]),
        "request_timeout_s": 3,
        "exercise_timeout_s": 30,
        "poll_interval_s": 0.05,
    }
    # The scenario declares which configuration the prerequisite run had to be
    # frozen against, and acceptance compares each of those projections against
    # the live policy. A profile that carries none of them cannot pass that gate,
    # so the sections are copied here, at the only place the built-in sync recipe
    # is derived.
    normalized = config.model_dump(mode="json")
    for key in config.prerequisites.relevant_config_keys["sync"]:
        profile[key] = select_relevant_config(normalized, key)
    frozen = {
        "images": dict(prepared.images),
        "metrics_profile": config.metrics_profile,
        "relevant_config": {"sync": profile},
        "payload": describe_artifact(payload),
        "script": describe_artifact(script),
        "settlement": {
            owner: {
                population: {
                    "limit": 0,
                    "retention_s": _population_retention(config, population),
                }
                for population in sorted(required)
            }
            for owner, required in populations.items()
        },
    }
    _inputs(frozen, coverage)
    return frozen


def _make_runtime_prerequisites(prepared, options, bindings, run_dir):
    """Bind the real factory after build, retaining reservations in the parent."""
    # The factory imports runtime and plans: importing it at module scope cycles.
    from nanolab.tasks.soak.prerequisite_platform import (
        freeze_effective_config,
        make_prerequisite_platform_factory,
    )
    from nanolab.tasks.soak.prerequisite_runtime import (
        make_live_runner,
        required_body_budget,
    )
    from nanolab.tasks.soak.prerequisites import _inputs

    _check_prerequisite_resources(prepared.config, options)
    frozen = (
        deepcopy(options.prerequisite_inputs)
        if options.prerequisite_inputs is not None
        else _freeze_prerequisite_inputs(prepared)
    )
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError(
            "automatic prerequisites require explicit frozen recipe inputs"
        )
    selected_profile = prepared.config.metrics_profile
    if frozen.get("metrics_profile", selected_profile) != selected_profile:
        raise ValueError("frozen prerequisite metrics profile differs from policy")
    frozen["metrics_profile"] = selected_profile
    if "images" in frozen and frozen["images"] != prepared.images:
        raise ValueError("frozen prerequisite images differ from build receipts")
    frozen["images"] = dict(prepared.images)
    expected = freeze_effective_config(
        prepared, retention_s=prepared.config.retention_s
    )
    for profile in frozen.get("relevant_config", {}).values():
        if "effective_config" in profile and profile["effective_config"] != expected:
            raise ValueError("frozen prerequisite settings differ from declared policy")
        profile["effective_config"] = deepcopy(expected)
    coverage = frozenset(prepared.config.prerequisites.required_coverage)
    _inputs(frozen, coverage)
    body_timeout_s = required_body_budget(frozen)
    factory_root = prepared.evidence_dir / "prerequisite-platforms"
    parent_root = prepared.evidence_dir / "prerequisites"
    factory = make_prerequisite_platform_factory(
        prepared=prepared,
        ownership_root=factory_root,
        bindings=bindings,
        acquire_timeout_s=options.prerequisite_acquire_timeout_s,
        release_timeout_s=options.prerequisite_release_timeout_s,
    )
    if not callable(getattr(factory, "assign_lifetime_budget", None)) or not callable(
        getattr(factory, "recover", None)
    ):
        raise ValueError(
            "prerequisite factory lacks quota enforcement or parent recovery capability"
        )
    factory.validate_inputs(deepcopy(frozen))
    runner = make_live_runner(inputs=deepcopy(frozen), factory=factory)
    parent_quota = options.prerequisite_parent_artifact_bytes
    lifetime_quota = options.prerequisite_lifetime_artifact_bytes
    recovery_quota = options.prerequisite_recovery_artifact_bytes
    reserved = 0
    assigned = set()

    def before_fork(lifetime_id, writer):
        nonlocal reserved
        if (
            writer.root.absolute() != parent_root.absolute()
            or writer.limit_bytes != parent_quota
        ):
            raise ValueError(
                "prerequisite parent writer does not enforce its reserved quota"
            )
        if lifetime_id in assigned:
            raise ValueError("prerequisite lifetime reservation is immutable")
        # Count other run artifacts dynamically. The two owned subtrees are
        # already charged at their FULL reservations, never actual-use/refunded.
        other = (
            _artifact_bytes(run_dir)
            - _owned_bytes(factory_root)
            - _owned_bytes(parent_root)
        )
        next_reserved = reserved + lifetime_quota + recovery_quota
        if other + parent_quota + next_reserved > prepared.config.artifact_limit_bytes:
            raise ValueError(
                "remaining global artifact budget cannot reserve "
                "prerequisite lifetime/recovery"
            )
        assigned.add(lifetime_id)
        reserved = next_reserved
        writer.write_json(
            f"reservation-{lifetime_id}.json",
            {
                "schema": "nanolab-soak-prerequisite-reservation-v1",
                "lifetime_id": lifetime_id,
                "global_limit_bytes": prepared.config.artifact_limit_bytes,
                "other_run_artifact_bytes": other,
                "parent_artifact_bytes": parent_quota,
                "artifact_limit_bytes": lifetime_quota,
                "recovery_limit_bytes": recovery_quota,
                "cumulative_lifetime_reservations_bytes": reserved,
                "refund_permitted": False,
            },
        )
        factory.assign_lifetime_budget(
            lifetime_id,
            artifact_limit_bytes=lifetime_quota,
            recovery_limit_bytes=recovery_quota,
        )

    def recover_after_reap(lifetime_id, worker):
        if (
            lifetime_id not in assigned
            or worker.get("reaped") is not True
            or type(worker.get("pid")) is not int
            or worker["pid"] <= 0
            or type(worker.get("exit_code")) is not int
        ):
            raise ValueError(
                "parent recovery requires an assigned lifetime "
                "and actual supervisor reap"
            )
        return factory.recover(lifetime_id, worker_reaped=True)

    prepared.writer.write_json(
        "prerequisite-provider-inputs.json",
        {
            "schema": "nanolab-soak-prerequisite-provider-inputs-v1",
            "provider_available": True,
            "capabilities_verified": False,
            "frozen_inputs_sha256": fingerprint(frozen),
            "parent_artifact_bytes": parent_quota,
            "lifetime_artifact_bytes": lifetime_quota,
            "recovery_artifact_bytes": recovery_quota,
            "acquire_timeout_s": options.prerequisite_acquire_timeout_s,
            "release_timeout_s": options.prerequisite_release_timeout_s,
            "body_timeout_s": body_timeout_s,
            "global_artifact_limit_bytes": prepared.config.artifact_limit_bytes,
        },
    )
    return (
        runner,
        frozen,
        {
            "before_fork": before_fork,
            "recover_after_reap": recover_after_reap,
            "parent_artifact_bytes": parent_quota,
            "body_timeout_s": body_timeout_s,
            "acquire_timeout_s": options.prerequisite_acquire_timeout_s,
            "release_timeout_s": options.prerequisite_release_timeout_s,
        },
    )


def _validate_natural_samples(config, target: Target, rows, phase: str) -> None:
    """Require actual observations, not merely a nonempty scrape result.

    `phase` is the checkpoint the rows were taken for. It is not cosmetic: the
    metric family is the same at every checkpoint, so validating the wrong
    phase's criteria against these rows would pass and hold the wrong set.
    """
    if not rows or any(row.target != target or row.phase != phase for row in rows):
        raise RuntimeError("owned natural checkpoint samples unavailable")

    def require(metric, selector, unit=None):
        selected = [
            row
            for row in rows
            if row.metric == metric
            and all(
                dict(row.labels).get(key) == value for key, value in selector.items()
            )
        ]
        if not selected or any(
            row.availability != "observed"
            or isinstance(row.value, bool)
            or not isinstance(row.value, (int, float))
            or not math.isfinite(row.value)
            or (unit is not None and row.unit != unit)
            for row in selected
        ):
            raise RuntimeError(
                f"natural checkpoint observation unavailable: {target.role}/{metric} "
                f"selector={selector!r}"
            )

    for metric in config.roles[target.role].required_metrics:
        require(metric, {})
    for criterion in config.criteria:
        if criterion.role == target.role and criterion.phase == phase:
            require(criterion.metric, criterion.label_selector, criterion.unit)


_artifact_bytes = measure_tree


def _owned_bytes(root: Path) -> int:
    """Measure an owned subtree, which is absent until its first lifetime.

    The reservation hook runs before the factory's first acquisition creates its
    ownership root, and a run directory that has not had a prerequisite yet has
    no parent subtree either. A missing subtree contributes nothing to the run
    total, so charging it zero is what the subtraction above means.
    """
    return 0 if not root.exists() else _artifact_bytes(root)


# The natural checkpoints a run freezes and reads from, in the order they happen.
# Each one names both the window its samples were taken over and the phase its
# captures are gated on, so the two can never be told apart.
_CHECKPOINTS = ("baseline", "drain")


def _diagnostic_resource_inputs(prepared, *, allow_target_stop: bool) -> dict:
    """Derive finite reservations from frozen policy; never clamp application limits."""
    config, policy = prepared.config, prepared.config.diagnostics
    # Both checkpoints' declarations, in capture order. A reading declared at
    # both is two captures and reserves twice, so these are concatenated rather
    # than unioned -- and the run-wide dump ceiling counts captures too, which is
    # what acceptance re-sums from the entries it receives.
    declared = {
        role: [
            *policy.operations.get(role, []),
            *policy.baseline_operations.get(role, []),
        ]
        for role in set(policy.operations) | set(policy.baseline_operations)
    }
    requested = {role: ops for role, ops in declared.items() if ops}
    count = sum(len(ops) for ops in declared.values())
    dumps = sum(ops.count("heap_dump") for ops in declared.values())
    if not count:
        return {}
    if not allow_target_stop:
        raise ValueError("diagnostics require explicit owned-target stop permission")
    if dumps > policy.max_dumps:
        raise ValueError("requested heap dumps exceed max_dumps")
    committed = _artifact_bytes(prepared.evidence_dir)
    terminal = min(4096, config.artifact_limit_bytes // 8)
    available = config.artifact_limit_bytes - committed - terminal
    payload_budget = available - _DIAGNOSTIC_RECEIPT_BYTES * count
    roles = {}
    for index, (role, application) in enumerate(config.roles.items()):
        if role not in requested:
            continue
        operations = requested[role]
        supported = supported_operations(application.runtime)
        if application.runtime not in {"jvm", "node"} or set(operations) - supported:
            raise ValueError(
                "local diagnostic helper does not support requested operations"
            )
        if (
            application.runtime == "node"
            and _NODE_PRELOAD not in application.runtime_options
        ):
            raise ValueError(
                f"Node diagnostics require declared runtime_options {_NODE_PRELOAD}"
            )
        image = policy.helper_images.get(role)
        if image is None:
            raise ValueError(f"diagnostic helper_images missing role {role}")
        _validate_memory_helper_image(image)
        memory = application.memory_limit_bytes
        if type(memory) is not int or memory <= 0:
            raise ValueError("diagnostic role requires a finite memory limit")
        bounds = [payload_budget // count, memory]
        if "heap_dump" in operations:
            bounds.append(policy.max_dump_bytes // dumps)
        quota = min(bounds) // 4096 * 4096
        if quota < 4096:
            raise ValueError("diagnostic budget cannot reserve one page per capture")
        # This is a provisioner contract, not a policy clamp. Keep the requested
        # calculation intact and reject an unsupported volume instead of shrinking it.
        if quota > 1024**3:
            raise ValueError(
                "derived diagnostic quota exceeds helper volume contract (1 GiB)"
            )
        headroom = max(64 * 1024**2, memory // 4 // 4096 * 4096)
        key = f"diagnostic-tmp-{index}"
        roles[role] = {
            "runtime": application.runtime,
            "operations": list(operations),
            "helper_image": image,
            "memory_limit_bytes": memory,
            "quota_bytes": quota,
            "helper_headroom_bytes": headroom,
            "helper_memory_bytes": quota + headroom,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "volume_key": key,
            "target_tmp_volume": f"{prepared.run_id}_{key}",
        }
    return {
        "schema": "nanolab-soak-diagnostic-resource-inputs-v1",
        "run_id": prepared.run_id,
        "policy_sha256": fingerprint(config.model_dump(mode="json")),
        "allow_target_stop_on_cancel": True,
        "formula": (
            "Q=align_down(min(payload_budget/N,role_memory,max_dump_bytes/D if dump)); "
            "helper=Q+max(64MiB,align_down(role_memory/4))"
        ),
        "page_bytes": 4096,
        "operation_count": count,
        "dump_count": dumps,
        "max_dumps": policy.max_dumps,
        "max_dump_bytes": policy.max_dump_bytes,
        "artifact_limit_bytes": config.artifact_limit_bytes,
        "committed_artifact_bytes": committed,
        "terminal_reserve_bytes": terminal,
        "receipt_reserve_bytes": _DIAGNOSTIC_RECEIPT_BYTES * count,
        "available_artifact_bytes": available,
        "payload_budget_bytes": payload_budget,
        "roles": roles,
    }


def _with_built_helper(
    config: SoakConfig, *, run_dir: Path, options: RuntimeOptions
) -> SoakConfig:
    """Return the protocol with this run's freshly built helper digest in it.

    A digest written into a scenario names bytes in whichever registry built
    them, so it is unpullable on any other machine and a prune breaks it even
    locally. The helper is built per run instead and pinned to the digest that
    build reported; the inputs stay pinned in assets/soak/*.lock.json.

    A scenario that still pins `helper_images`, or an injected
    `memory_helper_image`, is honoured as-is and skips the build.
    """
    from nanolab.tasks.soak.helper_build import HelperImageRequest, build_helper_image

    policy = config.diagnostics
    # Every role that asks for any reading at any checkpoint. Reading only the
    # final map left a baseline-only role without a helper digest, and a policy
    # that declares nothing but baseline readings built no helper at all -- so
    # the reading the run was configured for had nothing to run it.
    roles = [
        role
        for role in set(policy.operations) | set(policy.baseline_operations)
        if policy.operations.get(role) or policy.baseline_operations.get(role)
    ]
    if not roles or policy.helper_images or options.memory_helper_image is not None:
        return config
    # The build writes its log and metadata into the run root and refuses a
    # run_dir that does not exist. Only a scenario carrying a policy file would
    # have created it by now (`write_policy_input`), and the default diagnostic
    # protocols carry none, so the root is created here for the same reason
    # heap analysis creates it: the build needs it and nothing else does it.
    run_dir.mkdir(parents=True, exist_ok=True)
    digest = build_helper_image(
        HelperImageRequest(
            run_dir=run_dir,
            run_id=run_dir.name,
            registry=options.preparation.registry.split("/", 1)[0] + "/nanolab",
            builder=options.helper_builder,
        )
    )
    return config.model_copy(
        update={
            "diagnostics": policy.model_copy(
                update={"helper_images": dict.fromkeys(roles, digest)}
            )
        }
    )


def _validate_memory_helper_image(image: str | None) -> None:
    if image is not None and (
        not isinstance(image, str)
        or re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) is None
        or image.startswith("-")
    ):
        raise ValueError("memory helper requires an explicit image RepoDigest digest")


def _target_effective_ids(target: Target) -> tuple[int, int]:
    """Read the actual target credentials, not host credentials or image defaults."""
    with (Path("/proc") / str(target.process_id) / "status").open("rb") as stream:
        status = read_bounded(stream).decode()
    values = {}
    for line in status.splitlines():
        if line.startswith(("Uid:", "Gid:")):
            key, value = line.split(":", 1)
            fields = value.split()
            if (
                key in values
                or len(fields) != 4
                or not all(x.isdecimal() for x in fields)
            ):
                raise ValueError("target effective UID/GID is unavailable")
            values[key] = int(fields[1])
    if set(values) != {"Uid", "Gid"}:
        raise ValueError("target effective UID/GID is unavailable")
    return values["Uid"], values["Gid"]


class _MemoryHelperTransport:
    """Supply same-UID procfs to the existing probe without changing other sources.

    Preparation is explicit and occurs only after deployment. The existing
    RoleBoundProbe parses RSS/PSS independently and preserves missing values.
    Neither failed helpers nor inaccessible smaps fall back to host RSS/PSS.
    """

    def __init__(
        self, base, *, images, project_name, output_root, docker_socket, cancelled
    ):
        for image in images.values():
            _validate_memory_helper_image(image)
        self.base = base
        self.images = dict(images)
        self.project_name = project_name
        self.output_root = output_root
        self.docker_socket = docker_socket
        self.cancelled = cancelled
        self.helpers = {}

    def prepare(
        self,
        targets: tuple[Target, ...],
        *,
        diagnostic_inputs=None,
        observations=None,
        diagnostic_root=None,
    ) -> None:
        """Provision readers using observed credentials and architecture."""
        self.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        provisioner = LocalDockerDiagnosticProvisioner(
            assets_dir=Path(__file__).resolve().parents[4] / "assets/soak",
            cancelled=self.cancelled,
        )
        try:
            for target in targets:
                if target.role not in self.images:
                    continue
                if target.role in self.helpers:
                    raise ValueError("duplicate memory helper target role")
                uid, gid = _target_effective_ids(target)
                image = _docker_get(
                    f"/images/{quote(target.image_digest, safe='')}/json",
                    self.docker_socket,
                    5,
                )
                decision = (diagnostic_inputs or {}).get("roles", {}).get(target.role)
                if decision is not None:
                    actual = (observations or {}).get("roles", {}).get(target.role, {})
                    if (
                        actual.get("memory_bytes") != decision["memory_limit_bytes"]
                        or not actual.get("limit_sources", {}).get("memory_bytes")
                        or (uid, gid) != (decision["uid"], decision["gid"])
                        or target.runtime != decision["runtime"]
                    ):
                        raise ValueError(
                            "diagnostic effective memory/credentials "
                            "differ or are unavailable"
                        )
                    controller = decision.get("controller")
                    if controller is not None:
                        path = Path(controller["path"])
                        if path.is_symlink() or describe_artifact(path) != controller:
                            raise ValueError(
                                "owned Node controller changed since deployment"
                            )
                        container = _docker_get(
                            f"/containers/{quote(target.container_id, safe='')}/json",
                            self.docker_socket,
                            5,
                        )
                        mounts = [
                            m
                            for m in container.get("Mounts", [])
                            if m.get("Destination") == _NODE_CONTROLLER
                        ]
                        if len(mounts) != 1 or any(
                            mounts[0].get(key) != value
                            for key, value in {
                                "Type": "bind",
                                "Source": str(path),
                                "RW": False,
                            }.items()
                        ):
                            raise ValueError(
                                "actual Node controller mount differs from owned input"
                            )
                        actual["diagnostic_controller"] = {
                            **controller,
                            "mount": mounts[0],
                        }
                spec = DockerHelperSpec(
                    target=target,
                    helper_image=self.images[target.role],
                    owner_label="com.docker.compose.project",
                    owner_value=self.project_name,
                    uid=uid,
                    gid=gid,
                    output_root=(
                        diagnostic_root
                        if decision and diagnostic_root is not None
                        else self.output_root
                    ).resolve(),
                    quota_bytes=decision["quota_bytes"] if decision else 4096,
                    helper_memory_bytes=decision["helper_memory_bytes"]
                    if decision
                    else 64 * 1024 * 1024,
                    allow_target_stop_on_cancel=decision is not None,
                    memory_only=decision is None,
                    target_tmp_volume=decision["target_tmp_volume"]
                    if decision
                    else None,
                    docker_host="unix://" + self.docker_socket,
                    architecture=str(image["Architecture"]),
                )
                helper = (
                    provisioner.prepare(spec, timeout_s=30)
                    if decision
                    else provisioner.prepare_memory(spec, timeout_s=30)
                )
                self.helpers[target.role] = (target, helper)
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"memory helper cleanup unconfirmed: {cleanup_error}")
            raise

    def collect(self, target: Target, endpoint: str | None, timeout_s: float) -> dict:
        """Share one scrape deadline across the ordinary collector and procfs reader."""
        deadline = time.monotonic() + timeout_s
        data = dict(self.base.collect(target, endpoint, timeout_s))
        if target.role not in self.images:
            return data
        errors = dict(data.get("errors") or {})
        errors.pop("procfs", None)
        data["errors"] = errors
        data["procfs"] = {"status": None, "smaps_rollup": None}
        try:
            bound, helper = self.helpers[target.role]
            if bound != target:
                raise ValueError("memory helper target identity changed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("memory scrape deadline exhausted")
            sample = helper.read_memory(timeout_s=min(remaining, 300))
            if (
                sample.get("schema") != "nanolab-soak-memory-helper-v1"
                or sample.get("target") != asdict(target)
                or sample.get("before") != sample.get("after")
            ):
                raise ValueError("memory helper sample identity changed")
            data["procfs"] = {
                "status": sample.get("status"),
                "smaps_rollup": sample.get("smaps_rollup"),
            }
            if sample.get("errors"):
                errors["procfs"] = str(sample["errors"])[:1024]
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            errors["procfs"] = str(error)[:1024]
        return data

    def close(self) -> None:
        """Attempt every helper release, retaining failed handles for a later retry."""
        errors = []
        for role, (_, helper) in tuple(self.helpers.items()):
            try:
                helper.close()
            except BaseException as error:
                errors.append(error)
            else:
                del self.helpers[role]
        if errors:
            raise BaseExceptionGroup("memory helper cleanup unconfirmed", errors)


@dataclass(frozen=True, slots=True)
class _ExternalManifest(FunctionManifest):
    endpoint: str = ""

    def body(self) -> dict[str, Any]:
        # The control plane's FunctionSpec names this endpointUrl, and its
        # record rejects unknown properties: "endpoint" is a 400, not a
        # silently ignored field.
        return {**FunctionManifest.body(self), "endpointUrl": self.endpoint}


@dataclass(frozen=True, slots=True)
class _ExternalFunction(PlatformFunction):
    endpoint: str = ""

    def manifest(self) -> FunctionManifest:
        return _ExternalManifest(
            name=self.name,
            image=self.image,
            execution_mode="EXTERNAL",
            endpoint=self.endpoint,
            timeout_ms=self.timeout_ms,
            concurrency=self.concurrency,
            queue_size=self.queue_size,
            max_retries=self.max_retries,
        )


def _reference(root: Path, path: Path) -> dict[str, Any]:
    path = path.absolute()
    if path.is_symlink() or not path.is_relative_to(root.absolute()):
        raise ValueError("evidence reference must remain within this run")
    return {
        **describe_artifact(path),
        "path": path.relative_to(root.absolute()).as_posix(),
    }


def _read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        body = stream.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("runtime receipt exceeds its bounded JSON limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("runtime receipt must be a JSON object")
    return value


def create_local_deployment(
    prepared: PreparedSoak,
    run_dir: Path,
    *,
    docker_socket: str = "/var/run/docker.sock",
    allow_diagnostic_target_stop_on_cancel: bool = False,
) -> RuntimeDeployment:
    """Create an exclusive digest-only Compose project, including SDK services.

    Functions use EXTERNAL endpoints on this project's private bridge. This
    avoids managed containers escaping Compose ownership or sharing a bridge.
    Each service gets Docker-enforced limits and loopback-only metrics ports.
    """
    import os

    config = prepared.config
    diagnostic_inputs = _diagnostic_resource_inputs(
        prepared, allow_target_stop=allow_diagnostic_target_stop_on_cancel
    )
    volumes = {}
    controller = None
    for decision in diagnostic_inputs.get("roles", {}).values():
        volumes[decision["volume_key"]] = {
            "driver": "local",
            "driver_opts": {
                "type": "tmpfs",
                "device": "tmpfs",
                "o": (
                    f"size={decision['quota_bytes']},uid={decision['uid']},"
                    f"gid={decision['gid']},mode=1777,noexec,nosuid,nodev"
                ),
            },
            "labels": {
                "nanolab.run": prepared.run_id,
                "nanolab.diagnostic.tmp": "true",
            },
        }
        if decision["runtime"] == "node":
            if controller is None:
                source = (
                    Path(__file__).resolve().parents[4]
                    / "assets/soak/node-diagnostic-control.cjs"
                )
                with source.open("rb") as stream:
                    body = stream.read(65537)
                if not body or len(body) > 65536:
                    raise ValueError(
                        "Node controller exceeds bounded owned input limit"
                    )
                destination = run_dir.absolute() / "node-diagnostic-control.cjs"
                with destination.open("xb") as output:
                    output.write(body)
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o444)
                controller = describe_artifact(destination)
            decision["controller"] = controller
    if diagnostic_inputs:
        prepared.writer.write_json("diagnostic-resource-inputs.json", diagnostic_inputs)
    leases: list[socket.socket] = []
    ports: dict[str, int] = {}
    for name in ("api", *config.roles):
        lease = socket.socket()
        lease.bind(("127.0.0.1", 0))
        leases.append(lease)
        ports[name] = lease.getsockname()[1]
    network = prepared.run_id + "_owned"
    services = {}
    service_roles = {}
    for index, (role, policy) in enumerate(config.roles.items()):
        service = "control-plane" if role == "control-plane" else f"function-{index}"
        env = {
            "MANAGEMENT_SERVER_PORT": "8081",
            "MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE": (
                "health,prometheus,info,configprops"
            ),
            "MANAGEMENT_ENDPOINT_CONFIGPROPS_SHOW_VALUES": "always",
            # The image entrypoint carries
            # -Dmanagement.endpoints.enabled-by-default=false, so exposure
            # alone leaves configprops/info at 404: per-endpoint access has to
            # override it. Read-only observes the effective configuration and
            # sets nothing about the run.
            "MANAGEMENT_ENDPOINT_CONFIGPROPS_ACCESS": "read-only",
            "MANAGEMENT_ENDPOINT_INFO_ACCESS": "read-only",
        }
        if policy.runtime == "jvm":
            env["NANOFAAS_METRICS_PROFILE"] = config.metrics_profile
        if policy.runtime == "jvm" and policy.runtime_options:
            env["JAVA_TOOL_OPTIONS"] = " ".join(policy.runtime_options)
        elif policy.runtime == "node" and policy.runtime_options:
            env["NODE_OPTIONS"] = " ".join(policy.runtime_options)
        metrics_port = 8081 if role == "control-plane" else 8080
        exposed = [f"127.0.0.1:{ports[role]}:{metrics_port}"]
        if role == "control-plane":
            exposed.append(f"127.0.0.1:{ports['api']}:8080")
        service_roles[service] = role
        services[service] = {
            "user": f"{os.getuid()}:{os.getgid()}",
            "image": prepared.images[role],
            "cpus": policy.expected_cpu,
            "mem_limit": policy.memory_limit_bytes,
            "environment": env,
            "ports": exposed,
            "networks": ["owned"],
            "restart": "no",
            # Declare the grace teardown budgets for, instead of inheriting
            # Docker's default and hoping the cleanup timeout covers it.
            "stop_grace_period": f"{int(config.cancellation_timeout_s)}s",
        }
        if role == "control-plane":
            # The container runs as the host user so procfs stays readable,
            # but the image ships /var/lib/nanofaas owned by its own distroless
            # user: the catalog write at startup is denied and the platform
            # never becomes ready. Only this declared path is made writable.
            catalog = run_dir.absolute() / "control-plane-data"
            catalog.mkdir(mode=0o700)
            services[service]["volumes"] = [
                {
                    "type": "bind",
                    "source": str(catalog),
                    "target": "/var/lib/nanofaas",
                    "bind": {"create_host_path": False},
                }
            ]
        if role in diagnostic_inputs.get("roles", {}):
            decision = diagnostic_inputs["roles"][role]
            services[service]["cap_drop"] = ["ALL"]
            services[service]["security_opt"] = ["no-new-privileges:true"]
            services[service].setdefault("volumes", []).append(
                {
                    "type": "volume",
                    "source": decision["volume_key"],
                    "target": "/tmp",  # nosec B108 - isolated container path
                    "volume": {"nocopy": True},
                }
            )
            if decision.get("controller"):
                services[service]["volumes"].append(
                    {
                        "type": "bind",
                        "source": decision["controller"]["path"],
                        "target": _NODE_CONTROLLER,
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                )
    compose_path = run_dir.absolute() / "soak-compose.json"
    try:
        with compose_path.open("x") as stream:
            json.dump(
                {"services": services, "networks": {"owned": {}}, "volumes": volumes},
                stream,
            )
    except BaseException:
        for lease in leases:
            lease.close()
        raise
    prepared.writer.write_json(
        "ownership-intent.json",
        {
            "schema": "nanolab-soak-creation-intent-v1",
            "run_id": prepared.run_id,
            "state": "intent-before-provisioning",
            "project_name": prepared.run_id,
            "compose_file": str(compose_path),
            "compose_sha256": describe_artifact(compose_path)["sha256"],
            "network_name": network,
            "images": prepared.images,
            "ports": ports,
            "labels": {"com.docker.compose.project": prepared.run_id},
            "automatic_partial_acquire_recovery": False,
        },
    )
    api = f"http://127.0.0.1:{ports['api']}"
    endpoints: dict[str, str | None] = {
        role: f"http://127.0.0.1:{ports[role]}"
        + ("/actuator/prometheus" if role == "control-plane" else "/metrics")
        for role in config.roles
    }
    project = DockerComposeProject(
        prepared.run_id,
        compose_path,
        f"http://127.0.0.1:{ports['control-plane']}/actuator/health/readiness",
        build=False,
    )
    functions = tuple(
        _ExternalFunction(
            role,
            prepared.images[role],
            json.dumps(prepared.payloads[role][0]["input"]),
            (),
            endpoint=f"http://function-{index}:8080/invoke",
        )
        for index, role in enumerate(config.roles)
        if role != "control-plane"
    )

    def acquire(inputs):
        # Port leases prevent other local preparers choosing these ports. Docker
        # performs the final exclusive bind; a collision is a deployment failure.
        for lease in leases:
            lease.close()
        return {
            "schema": "nanolab-soak-endpoints-v1",
            "project": project.name,
            "ports": ports,
        }

    owner = Resource(
        title="Own exclusive soak endpoint allocation",
        acquire=acquire,
        release=lambda inputs, value: _close_all(leases),
        always_release=True,
    )

    def discover():
        targets = []
        for service in services:
            name = f"{project.name}-{service}-1"
            container = _docker_get(
                f"/containers/{quote(name, safe='')}/json", docker_socket, 5
            )
            state = container["State"]
            labels = container["Config"].get("Labels", {})
            role = service_roles[service]
            if (
                labels.get("com.docker.compose.project") != prepared.run_id
                or not state["Running"]
            ):
                raise ValueError("running process does not belong to this soak")
            image = _docker_get(
                f"/images/{quote(container['Image'], safe='')}/json", docker_socket, 5
            )
            if prepared.images[role] not in image.get("RepoDigests", []):
                raise ValueError("deployed image differs from frozen digest")
            targets.append(
                Target(
                    role,
                    container["Id"],
                    state["Pid"],
                    state["StartedAt"],
                    prepared.images[role],
                    config.roles[role].runtime,
                )
            )
        return tuple(targets)

    def observations(targets):
        observed = {"roles": {}, "free_bytes": shutil.disk_usage(run_dir).free}
        effective = observe_local_configuration(
            prepared,
            f"http://127.0.0.1:{ports['control-plane']}",
            api_url=f"http://127.0.0.1:{ports['api']}",
        )
        observed.update(effective)
        for target in targets:
            actual: dict[str, Any] = {
                "image_digest": target.image_digest,
                "diagnostics": [],
            }
            # Docker configuration is retained, but is not substituted for actual
            # cgroup limits or observable runtime flags/retention configuration.
            try:
                actual.update(observe_local_process(target))
            except (OSError, ValueError) as error:
                actual["unavailable"] = str(error)
            if target.role == "control-plane" and "modules" in effective:
                actual["modules"] = effective["modules"]
            observed["roles"][target.role] = actual
        try:
            observed["generator"] = inspect_generator(prepared, api, ("k6",))
        except (OSError, ValueError) as error:
            observed["generator"] = {"available": False, "reason": str(error)}
        return observed

    return RuntimeDeployment(
        project,
        PlatformRequest(
            backend="container",
            functions=functions,
            build_images=False,
            build_control_plane=False,
            control_plane_image=prepared.images["control-plane"],
        ),
        owner,
        api,
        endpoints,
        discover,
        observations,
        diagnostic_inputs,
    )


def _capture_owned_diagnostic(
    adapter, helper, target, operation, output, timeout_s, cancelled
):
    """Stop an exact owned remote writer on external cancellation, not just its CLI."""
    finished, cancellation_started = Event(), Event()
    cancellation_errors = []

    def watch():
        while not finished.wait(0.05):
            if cancelled.is_set():
                cancellation_started.set()
                try:
                    helper.cancel(timeout_s=min(timeout_s, 10))
                except BaseException as error:
                    cancellation_errors.append(error)
                return

    watcher = Thread(target=watch, name="soak-diagnostic-cancel", daemon=True)
    watcher.start()
    try:
        try:
            receipt = adapter.capture(target, operation, output, timeout_s)
        except BaseException as error:
            if not isinstance(error, Exception) and not cancellation_started.is_set():
                try:
                    helper.cancel(timeout_s=min(timeout_s, 10))
                except BaseException as cleanup_error:
                    error.add_note(
                        f"remote diagnostic cancellation unconfirmed: {cleanup_error}"
                    )
            raise
        if cancelled.is_set():
            if not cancellation_started.is_set():
                cancellation_started.set()
                helper.cancel(timeout_s=min(timeout_s, 10))
            raise KeyboardInterrupt("soak cancelled during diagnostic capture")
        return receipt
    finally:
        finished.set()
        watcher.join(timeout=min(timeout_s, 10) + 1)
        if watcher.is_alive():
            raise RuntimeError("remote diagnostic cancellation thread did not finish")
        if cancellation_errors:
            raise BaseExceptionGroup(
                "remote diagnostic cancellation unconfirmed", cancellation_errors
            )


def create_soak_lifecycle(
    prepared: PreparedSoak | PreparedContainerdSoak,
    *,
    deployment: RuntimeDeployment | ContainerdDeployment,
    run_dir: Path,
    observations: Callable[[tuple[Target, ...]], dict[str, Any]] | None = None,
    prerequisite_runner: Any = None,
    prerequisite_inputs: dict[str, object] | None = None,
    prerequisite_supervision: dict[str, Any] | None = None,
    diagnostic_adapter: Any = None,
    generator_command: tuple[str, ...] = ("k6",),
    transport: Any = None,
    clock: Any = None,
    cancelled: Event | None = None,
    defer_terminal: bool = False,
    memory_helper_image: str | None = None,
    docker_socket: str = "/var/run/docker.sock",
    allow_diagnostic_target_stop_on_cancel: bool = False,
) -> TerminalSoakLifecycle:
    """Bind real probes and producer receipts after the deployment was acquired."""
    config, root, writer = prepared.config, prepared.evidence_dir, prepared.writer
    writer.write_json(
        "frozen-workload-inputs.json",
        {
            "base_url": deployment.api_endpoint.rstrip("/"),
            "function_rates": config.workload.rates,
            "payloads": prepared.payloads,
            "image_digests": prepared.images,
            "config": config.model_dump(mode="json"),
            "vus": config.workload.preallocated_vus,
            "max_vus": config.workload.max_vus,
        },
    )
    script_path = Path(__file__).resolve().parents[4] / "assets/k6/soak-workload.js"
    with script_path.open("rb") as source:
        script = source.read(128 * 1024 + 1)
    if len(script) > 128 * 1024 or len(script) >= config.artifact_limit_bytes:
        raise ValueError("workload script exceeds its frozen artifact budget")
    with (root / "frozen-workload.js").open("xb") as output:
        output.write(script)
    clock = clock or SystemClock()
    targets = deployment.discover()
    cancelled = cancelled if cancelled is not None else Event()
    diagnostic_inputs = getattr(deployment, "diagnostic_inputs", None) or {}
    automatic_diagnostics = (
        any(config.diagnostics.operations.values())
        or any(config.diagnostics.baseline_operations.values())
    ) and diagnostic_adapter is None
    if automatic_diagnostics and (
        not allow_diagnostic_target_stop_on_cancel
        or not diagnostic_inputs
        or diagnostic_inputs.get("policy_sha256")
        != fingerprint(config.model_dump(mode="json"))
        or diagnostic_inputs.get("run_id") != prepared.run_id
    ):
        raise ValueError(
            "diagnostics require authorized owned deployment inputs "
            "for this frozen policy"
        )
    diagnostic_adapters = {}
    diagnostic_budget = (
        DiagnosticBudget(
            config.diagnostics.max_dumps,
            config.diagnostics.max_dump_bytes,
            diagnostic_inputs["available_artifact_bytes"],
        )
        if automatic_diagnostics
        else None
    )
    memory_transport = None
    collection_transport = transport or SubprocessTransport(docker_socket=docker_socket)
    helper_images = dict(config.diagnostics.helper_images)
    if memory_helper_image is not None:
        _validate_memory_helper_image(memory_helper_image)
        if any(image != memory_helper_image for image in helper_images.values()):
            raise ValueError("memory helper option conflicts with frozen helper_images")
        helper_images.update({target.role: memory_helper_image for target in targets})
    if helper_images:
        project = getattr(deployment, "project", None)
        if project is None:
            raise ValueError("memory helper images require a Docker deployment")
        writer.write_json(
            "memory-helper-inputs.json",
            {
                "schema": "nanolab-soak-memory-helper-inputs-v1",
                "images": {
                    role: image
                    for role, image in helper_images.items()
                    if not automatic_diagnostics
                    or role not in diagnostic_inputs["roles"]
                },
                "shared_diagnostic_roles": sorted(diagnostic_inputs.get("roles", {}))
                if automatic_diagnostics
                else [],
                "memory_only": True,
                "allow_target_stop_on_cancel": False,
                "quota_bytes": 4096,
                "helper_memory_bytes": 64 * 1024 * 1024,
            },
        )
        memory_transport = _MemoryHelperTransport(
            collection_transport,
            images=helper_images,
            project_name=project.name,
            output_root=root / "memory-helpers",
            docker_socket=docker_socket,
            cancelled=cancelled,
        )
        collection_transport = memory_transport
    units = {(c.role, c.metric): c.unit for c in config.criteria}
    bindings = []
    for target in targets:
        metrics = {
            name: units.get(
                (target.role, name), "bytes" if name.endswith("_bytes") else "unknown"
            )
            for name in config.roles[target.role].required_metrics
        }
        if target.role == "control-plane":
            metrics["function_admitted_total"] = "count"
        bindings.append(
            RoleBinding(target, deployment.metrics_endpoints.get(target.role), metrics)
        )
    probe = RoleBoundProbe(
        tuple(bindings),
        collection_transport,
        timeout_s=config.scrape_timeout_s,
    )
    observer = Observer(
        probe,
        clock,
        writer,
        config.sample_interval_s,
        startup_timeout_s=observer_startup_timeout(config),
        # The whole run directory, so journals, command logs, k6 output and
        # diagnostic dumps are charged against the budget they share, and a
        # run that would overrun it stops while its evidence is still valid.
        budget=lambda: enforce_limit(run_dir, config.artifact_limit_bytes),
    )
    binding = {
        "schema": "nanolab-soak-v1",
        "run_id": prepared.run_id,
        "policy_sha256": fingerprint(config.model_dump(mode="json")),
        "backend": (
            "containerd"
            if config.images["control-plane"].artifact_kind == "process"
            else "container"
        ),
    }
    results: tuple = ()
    diagnostic_entries: list[dict] = []
    final_targets: tuple[Target, ...] = ()
    observed_start: float | None = None
    observed_end: float | None = None
    natural_drain_completed = False

    class TimedObserver:
        def start(self, phase):
            nonlocal observed_start
            observed_start = clock.monotonic()
            observer.start(phase)

        def set_phase(self, phase):
            observer.set_phase(phase)

        def label_diagnostic(self):
            """Label the samples a capture perturbs, when the observer is live.

            Through `set_phase` so a caller that owns phase transitions still
            does. A run whose observer already died still gets its diagnostics:
            this label changes how those ticks are judged, never whether the
            capture runs.
            """
            with suppress(RuntimeError):
                self.set_phase("diagnostic")

        def stop(self, timeout_s):
            nonlocal observed_end
            observer.stop(timeout_s)
            observed_end = clock.monotonic()

    timed_observer = TimedObserver()

    def check():
        actual = (observations or deployment.observations)(targets)
        if memory_transport is not None:
            memory_transport.prepare(
                targets,
                diagnostic_inputs=diagnostic_inputs if automatic_diagnostics else None,
                observations=actual,
                diagnostic_root=root,
            )
        if automatic_diagnostics:
            preparations = []
            for target in targets:
                if target.role not in diagnostic_inputs["roles"]:
                    continue
                decision = diagnostic_inputs["roles"][target.role]
                if memory_transport is None:
                    raise RuntimeError("memory transport was not initialized")
                _, helper = memory_transport.helpers[target.role]
                for phase in _CHECKPOINTS:
                    # One adapter per checkpoint: the checkpoint it accepts and
                    # the phase it is gated on are the same declaration, so a
                    # capture can never take a reading against a window it was
                    # not measured from.
                    adapter = helper.adapter(
                        budget=diagnostic_budget,
                        natural_checkpoint=root / f"natural-{phase}-{target.role}.json",
                        max_capture_bytes=decision["quota_bytes"],
                        natural_phase=phase,
                    )
                    diagnostic_adapters[(target.role, phase)] = adapter
                adapter = diagnostic_adapters[(target.role, "drain")]
                receipt = _read_json(helper.receipt)
                role = actual.setdefault("roles", {}).setdefault(target.role, {})
                role["diagnostics"] = sorted(adapter.capabilities(target))
                role["gc_completion_evidence"] = receipt.get("full_gc_source")
                role["diagnostic_preparation"] = _reference(root, helper.receipt)
                preparations.append(
                    {
                        "target": asdict(target),
                        "receipt": role["diagnostic_preparation"],
                        "effective_memory_bytes": role.get("memory_bytes"),
                        "decision": decision,
                    }
                )
            writer.write_json(
                "diagnostic-preparation.json",
                {"schema": "nanolab-soak-v1", "entries": preparations},
            )
        actual["snapshot_fingerprint"] = prepared.snapshot.fingerprint
        actual["build_receipts"] = {
            receipt.role: asdict(receipt) for receipt in prepared.receipts
        }
        samples = []
        for target in targets:
            rows = probe.sample(target, "preflight", clock.monotonic())
            samples.extend(rows)
            role = actual.setdefault("roles", {}).setdefault(target.role, {})
            role["metrics"] = sorted(
                {row.metric for row in rows if row.availability == "observed"}
            )
            role["collection_sources"] = sorted(
                set(role.get("collection_sources", []))
                | _observed_collection_sources(rows)
            )
        for row in samples:
            writer.append(
                "preflight-samples", {"schema": "nanolab-soak-v1", **asdict(row)}
            )
        checks = preflight(config, targets, actual, writer)
        missing = [
            item.criterion_id + ": " + item.reason
            for item in checks
            if item.status != "PASS"
        ]
        if missing:
            raise RuntimeError(
                "effective preflight INCONCLUSIVE: " + "; ".join(missing)
            )

    def prerequisites():
        inputs = prerequisite_inputs or {}
        writer.write_json("prerequisite-inputs.json", inputs)
        if prerequisite_supervision is not None:
            import asyncio

            from nanolab.tasks.soak.artifacts import ArtifactWriter
            from nanolab.tasks.soak.prerequisites import run_prerequisites

            profile_writer = ArtifactWriter(
                root / "prerequisites",
                prerequisite_supervision["parent_artifact_bytes"],
            )
            try:
                receipt = asyncio.run(
                    run_prerequisites(
                        inputs=inputs,
                        required_coverage=frozenset(
                            config.prerequisites.required_coverage
                        ),
                        runner=prerequisite_runner,
                        writer=profile_writer,
                        timeout_s=prerequisite_supervision.get(
                            "body_timeout_s", config.diagnostics.timeout_s
                        ),
                        **{
                            key: value
                            for key, value in prerequisite_supervision.items()
                            if key not in {"parent_artifact_bytes", "body_timeout_s"}
                        },
                    )
                )
            finally:
                profile_writer.close()
            if receipt.get("status") != "PASS":
                raise RuntimeError(
                    f"{receipt.get('status', 'INCONCLUSIVE')}: "
                    "prerequisites did not pass"
                )
        else:
            receipt = run_prerequisite_gate(
                config,
                inputs=inputs,
                runner=prerequisite_runner,
                run_dir=root,
                receipt_root=run_dir,
                timeout_s=config.diagnostics.timeout_s,
            )
        writer.write_json("prerequisites.json", receipt)

    def capture_checkpoint(phase, window, deadline, declared):
        """Freeze one natural checkpoint, then take the readings it declares.

        Shared by the baseline checkpoint and the final drain one: the same
        freeze-then-perturb order and the same per-capture reservation, a
        different window and a different declared set. The caller validates the
        window, so this never captures from a checkpoint that was not a natural
        one.
        """
        # Relabel before the first perturbing read. Without this every tick taken
        # here keeps the previous label with a timestamp past its window's end,
        # and `evaluate` judges each one a boundary crossing -- so a run passed or
        # failed by whether a tick happened to land in a capture that takes far
        # longer than one sample interval.
        timed_observer.label_diagnostic()
        checkpoints = []

        def remaining_checkpoint_time():
            if cancelled.is_set():
                raise KeyboardInterrupt("soak cancelled during natural checkpoints")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("shared diagnostic deadline exhausted")
            return min(config.scrape_timeout_s, remaining)

        # Freeze ALL natural checkpoints before perturbing any role.
        for target in targets:
            if not declared.get(target.role):
                continue
            ordinary = next(item for item in bindings if item.target == target)
            checkpoint_binding = RoleBinding(
                target,
                ordinary.endpoint,
                {
                    **ordinary.required_metrics,
                    **{
                        c.metric: c.unit
                        for c in config.criteria
                        if c.role == target.role and c.phase == phase
                    },
                },
            )
            # The existing transport owns/kills its collector on timeout. A new
            # probe propagates the shared remainder, not its usual timeout.
            checkpoint_probe = RoleBoundProbe(
                (checkpoint_binding,),
                collection_transport,
                timeout_s=remaining_checkpoint_time(),
            )
            rows = checkpoint_probe.sample(target, phase, clock.monotonic())
            remaining_checkpoint_time()
            _validate_natural_samples(config, target, rows, phase)
            checkpoints.append((target, rows))
        # No completed marker is published until every role has valid evidence.
        for target, rows in checkpoints:
            remaining_checkpoint_time()
            evidence = writer.write_json(
                f"natural-samples-{phase}-{target.role}.json",
                {
                    **binding,
                    "target": asdict(target),
                    "rows": [asdict(row) for row in rows],
                },
            )
            remaining_checkpoint_time()
            writer.write_json(
                f"natural-{phase}-{target.role}.json",
                {
                    **binding,
                    "kind": "natural_checkpoint",
                    "phase": phase,
                    "completed": True,
                    "target": asdict(target),
                    "window": window,
                    "ended_s": clock.monotonic(),
                    "artifacts": [_reference(root, evidence)],
                },
            )
        for target in targets:
            for operation in declared.get(target.role, []):
                if cancelled.is_set():
                    raise KeyboardInterrupt("soak cancelled before diagnostic capture")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("shared diagnostic deadline exhausted")
                # The phase is in the path because the same reading at both
                # checkpoints is the point of the split, and one directory per
                # capture name would collide on the second.
                output = root / f"diagnostic-{phase}-{target.role}-{operation}"
                if automatic_diagnostics:
                    quota = diagnostic_inputs["roles"][target.role]["quota_bytes"]
                    if (
                        _artifact_bytes(root) + quota + _DIAGNOSTIC_RECEIPT_BYTES
                        > config.artifact_limit_bytes
                        - diagnostic_inputs["terminal_reserve_bytes"]
                    ):
                        raise RuntimeError(
                            "remaining artifact budget cannot reserve "
                            "diagnostic capture"
                        )
                    if memory_transport is None:
                        raise RuntimeError("memory transport was not initialized")
                    receipt = _capture_owned_diagnostic(
                        diagnostic_adapters[(target.role, phase)],
                        memory_transport.helpers[target.role][1],
                        target,
                        operation,
                        output,
                        remaining,
                        cancelled,
                    )
                else:
                    if diagnostic_adapter is None:
                        raise RuntimeError("diagnostic adapter was not initialized")
                    receipt = diagnostic_adapter.capture(
                        target, operation, output, remaining
                    )
                diagnostic_entries.append(
                    {
                        "role": target.role,
                        "phase": phase,
                        "operation": operation,
                        "receipt": _reference(root, receipt),
                    }
                )

    def baseline_capture(state: LifecycleState, timeout_s: float):
        """Take the readings declared for the baseline checkpoint's close.

        Reads nothing when none are declared, so the step can stay an
        unconditional part of the phase sequence and a run's phase keys stay
        independent of its policy.
        """
        declared = config.diagnostics.baseline_operations
        if not any(declared.values()):
            return
        window = state.windows.get("baseline", ())
        if (
            state.primary_error is not None
            or cancelled.is_set()
            or len(window) != 2
            or not all(math.isfinite(t) for t in window)
            or window[1] - window[0] < config.phases.baseline_window_s
        ):
            raise RuntimeError(
                "completed owned natural baseline checkpoint unavailable"
            )
        capture_checkpoint("baseline", window, time.monotonic() + timeout_s, declared)

    def final_capture(state: LifecycleState, timeout_s: float):
        nonlocal final_targets
        # Discovery repeats while resources remain alive. Identity differences
        # are handed to acceptance, never silently rebound to fresh processes.
        final_targets = deployment.discover()
        if any(config.diagnostics.operations.values()) or any(
            config.diagnostics.baseline_operations.values()
        ):
            window = state.windows.get("drain", ())
            if (
                not natural_drain_completed
                or state.primary_error is not None
                or cancelled.is_set()
                or final_targets != targets
                or len(window) != 2
                or not all(math.isfinite(t) for t in window)
                or window[1] - window[0] < config.phases.drain_s
            ):
                raise RuntimeError(
                    "completed owned natural final checkpoint unavailable"
                )
            capture_checkpoint(
                "drain",
                window,
                time.monotonic() + timeout_s,
                config.diagnostics.operations,
            )
            writer.write_json(
                "diagnostics.json", {**binding, "entries": diagnostic_entries}
            )

    def evaluate(state: LifecycleState):
        nonlocal results
        windows = {
            name.replace("-", "_"): {"start_s": start, "end_s": end}
            for name, (start, end) in state.windows.items()
        }
        writer.write_json(
            "evaluation-input.json",
            {
                "schema": "nanolab-soak-v1",
                "scope": "numerical-projection",
                "purpose": config.purpose,
                "criteria": [c.model_dump(mode="json") for c in config.criteria],
                "targets": [asdict(target) for target in targets],
                "windows": {
                    name: windows[name]
                    for name in ("baseline", "steady", "drain")
                    if name in windows
                },
                # The observer has one "baseline" label for the settling drain
                # and the measurement window that follows it, so a sample can
                # carry that label outside the narrower criterion window above.
                # Criteria keep using "windows"; only sample placement uses these.
                "phase_extents": {
                    name: {
                        "start_s": windows[first]["start_s"],
                        "end_s": windows[name]["end_s"],
                    }
                    for name, first in (
                        ("baseline", "baseline_drain"),
                        ("steady", "steady"),
                        ("drain", "drain"),
                    )
                    if name in windows and first in windows
                },
                "sample_interval_s": config.sample_interval_s,
                "max_observation_gap_s": config.max_observation_gap_s,
                "perturbations": [],
            },
        )
        manifest = {
            **binding,
            "completed": state.primary_error is None and "drain" in windows,
            "aborted": state.aborted,
            "phases": windows,
            "frozen_at_s": prepared.frozen_at_s,
            "builds_finished_s": prepared.builds_finished_s,
            "observation_started_s": observed_start,
            "observation_ended_s": observed_end,
            "preflight_started_s": windows.get("preflight", {}).get("start_s"),
            "preflight_ended_s": windows.get("preflight", {}).get("end_s"),
            "traffic_stopped_s": windows.get("steady", {}).get("end_s"),
            "final_targets": [asdict(target) for target in final_targets],
            "restarts": [],
            "natural_drain": True,
            "builds": {
                recipe.role: _reference(root, root / f"builds/build-{index}.json")
                for index, recipe in enumerate(prepared.recipes)
            },
            "recipes": {
                recipe.role: _reference(root, root / f"recipe-{index}.json")
                for index, recipe in enumerate(prepared.recipes)
            },
        }
        for key, name in {
            "config": "config.json",
            "source": "source/snapshot.json",
            "remote_source": "remote-source.json",
            "preflight": "preflight.json",
            "prerequisites": "prerequisites.json",
            "prerequisite_inputs": "prerequisite-inputs.json",
            "diagnostics": "diagnostics.json",
        }.items():
            if (root / name).is_file():
                manifest[key] = _reference(root, root / name)
        receipt = state.workload_receipts.get("steady")
        if receipt is not None:
            manifest["workload"] = _reference(root, receipt)
            manifest["workload_inputs"] = _reference(
                root, root / "frozen-workload-inputs.json"
            )
            manifest["workload_script"] = _reference(root, root / "frozen-workload.js")
            workload = _read_json(receipt)
            manifest["traffic_stopped_s"] = workload.get("generator_end_s")
        # The captured source tree is already inventoried, file by file, in
        # source-manifest.jsonl, which snapshot.json seals with manifest_sha256
        # and both of which stay in this inventory. Listing its thousands of
        # files again only duplicates that chain, and it overflows the
        # single-record limit this document has to fit in.
        entries = [
            _reference(root, path)
            for path in sorted(root.rglob("*"))
            if (
                path.is_file()
                and not path.is_symlink()
                and not path.name.startswith(".")
                and not any(part.startswith("workspace-") for part in path.parts)
                and not path.is_relative_to(root / "source" / "tree")
            )
        ]
        inventory = writer.write_json(
            "artifacts.json",
            {
                **binding,
                "complete": state.primary_error is None,
                "budget_exhausted": sum(item["size_bytes"] for item in entries)
                > config.artifact_limit_bytes,
                "entries": entries,
            },
        )
        manifest["artifacts"] = _reference(root, inventory)
        writer.write_json("acceptance-manifest.json", manifest)
        results = evaluate_run(root)
        return {"status": combine_results(results, state.aborted)}

    def report(state):
        return write_report(root, results, state.aborted)

    drivers = make_workload_driver_factory(
        config,
        base_url=deployment.api_endpoint,
        payloads=prepared.payloads,
        image_digests=prepared.images,
        command=generator_command,
    )

    def driver_factory(phase):
        driver = drivers(phase)

        class EvidenceDriver:
            def run(self, output_dir, duration_s, cancelled):
                return driver.run(root / phase, duration_s, cancelled)

            def stop(self, timeout_s):
                driver.stop(timeout_s)

        return EvidenceDriver()

    class DeferredTerminalLifecycle(TerminalSoakLifecycle):
        def run(self, inputs):
            result = SoakLifecycle.run(self, inputs)
            evaluation = self.state.evaluation
            status = evaluation.get("status") if isinstance(evaluation, dict) else None
            if status != "PASS":
                raise RuntimeError(f"{status or 'INCONCLUSIVE'}: soak did not pass")
            return result

    lifecycle_type = (
        DeferredTerminalLifecycle if defer_terminal else TerminalSoakLifecycle
    )

    class MemoryOwnedLifecycle(lifecycle_type):  # pyright: ignore[reportGeneralTypeIssues] - selected by runtime mode
        def _natural(self, phase, duration):
            nonlocal natural_drain_completed
            super()._natural(phase, duration)
            if phase == "drain" and not self.cancelled.is_set():
                natural_drain_completed = True

        def stop_observer(self):
            try:
                super().stop_observer()
            except BaseException as error:
                if memory_transport is not None:
                    try:
                        memory_transport.close()
                    except BaseException as cleanup_error:
                        error.add_note(
                            f"memory helper cleanup unconfirmed: {cleanup_error}"
                        )
                raise
            else:
                if memory_transport is not None:
                    memory_transport.close()

    return MemoryOwnedLifecycle(
        config,
        observer=timed_observer,
        clock=clock,
        driver_factory=driver_factory,
        hooks=LifecycleHooks(
            check, prerequisites, baseline_capture, final_capture, evaluate, report
        ),
        run_dir=run_dir,
        cancelled=cancelled,
    )


class RunSingleVersionSoak(Task):
    """An indivisible deferred public workflow; prepare before acquiring runtime."""

    title = "Prepare deploy and measure a single-version soak"
    idempotent = False

    def __init__(
        self,
        config,
        bindings,
        *,
        run_dir,
        repo_root,
        tool_root,
        options=None,
        keep=lambda: False,
    ):
        """Freeze execution inputs without acquiring files, processes or resources."""
        self.config, self.bindings = config, bindings
        self.run_dir, self.repo_root, self.tool_root = run_dir, repo_root, tool_root
        self.options = options or RuntimeOptions()
        self.keep = keep
        self._entered = False

    def run(self, inputs: TaskInputs) -> TaskOutcome:
        """Prepare once, run the owned platform, and persist every terminal exit."""
        from nanolab.plans.soak import compose_frozen_soak_workflow
        from nanolab.tasks.soak.teardown import cleanup_timeout_s

        if self._entered:
            raise RuntimeError("single-version soak cannot resume or restart")
        self._entered = True
        prepared = None
        holder = {}
        writer_close_attempted = False
        try:
            write_policy_input(self.run_dir, self.config)
            prepared = self.options.prepared
            soak = _with_built_helper(
                self.config.soak,
                run_dir=self.run_dir,
                options=self.options,
            )
            if prepared is None:
                prepared = prepare_soak(
                    soak,
                    run_dir=self.run_dir,
                    repo_root=self.repo_root,
                    tool_root=self.tool_root,
                    options=_runtime_preparation_options(soak, self.options),
                )
            elif (
                prepared.evidence_dir.absolute()
                != (self.run_dir / "evidence").absolute()
                or prepared.config.model_dump(mode="json")
                != self.config.soak.model_dump(mode="json")
                or _read_json(prepared.evidence_dir / "config.json")
                != self.config.soak.model_dump(mode="json")
            ):
                raise ValueError(
                    "prepared source/images belong to another run or policy"
                )
            prerequisite_runner = self.options.preparation.prerequisite_runner
            prerequisite_inputs = self.options.prerequisite_inputs
            prerequisite_supervision = None
            if (
                prerequisite_runner is None
                and prepared.config.prerequisites.mode == "run"
                and prepared.config.prerequisites.required_coverage
            ):
                prerequisite_runner, prerequisite_inputs, prerequisite_supervision = (
                    _make_runtime_prerequisites(
                        prepared, self.options, self.bindings, self.run_dir
                    )
                )
            factory = self.options.deployment_factory or (
                lambda prepared, run_dir: create_local_deployment(
                    prepared,
                    run_dir,
                    docker_socket=self.options.docker_socket,
                    allow_diagnostic_target_stop_on_cancel=self.options.allow_diagnostic_target_stop_on_cancel,
                )
            )
            deployment = factory(prepared, self.run_dir)
            holder = {}
            outer = self

            class DeferredMeasurement(Task):
                title = "Run complete soak measurement"
                observer = None

                def stop_observer(self):
                    if "measurement" in holder:
                        holder["measurement"].stop_observer()

                def run(self, inputs):
                    # Bound once so the lambda closes over a non-optional hook.
                    observations_hook = outer.options.observations
                    observation = (
                        None
                        if observations_hook is None
                        else lambda targets: observations_hook(prepared, targets)
                    )
                    measurement = create_soak_lifecycle(
                        prepared,
                        deployment=deployment,
                        run_dir=outer.run_dir,
                        observations=observation,
                        prerequisite_runner=prerequisite_runner,
                        prerequisite_inputs=prerequisite_inputs,
                        prerequisite_supervision=prerequisite_supervision,
                        diagnostic_adapter=outer.options.preparation.diagnostic_adapter,
                        generator_command=outer.options.preparation.generator_command,
                        defer_terminal=True,
                        memory_helper_image=outer.options.memory_helper_image,
                        docker_socket=outer.options.docker_socket,
                        allow_diagnostic_target_stop_on_cancel=outer.options.allow_diagnostic_target_stop_on_cancel,
                    )
                    holder["measurement"] = measurement
                    return measurement.run(inputs)

            workflow = compose_frozen_soak_workflow(
                deployment.request,
                self.bindings,
                project=deployment.project,
                measurement=DeferredMeasurement(),
                api_endpoint=deployment.api_endpoint,
                ownership=deployment.ownership,
                cwd=self.repo_root,
                release_timeout_s=cleanup_timeout_s(
                    prepared.config.cancellation_timeout_s
                ),
            )
            workflow.keep = self.keep()
            result = workflow.run(
                journal=JournalConfig(path=self.run_dir / "runtime-journal.jsonl")
            )
            state = holder["measurement"].state
            writer_close_attempted = True
            prepared.writer.close()
            write_terminal_receipt(self.run_dir, "PASS", report_path=state.report)
            return TaskOutcome(value=result)
        except BaseException as error:
            if prepared is not None and not writer_close_attempted:
                writer_close_attempted = True
                try:
                    prepared.writer.close()
                except BaseException as close_error:
                    error.add_note(f"evidence writer close failed: {close_error}")
            if not (self.run_dir / "terminal.json").exists():
                state = getattr(holder.get("measurement"), "state", None)
                evaluation = getattr(state, "evaluation", None)
                status = (
                    "ABORTED"
                    if not isinstance(error, Exception)
                    else "FAIL"
                    if isinstance(evaluation, dict)
                    and evaluation.get("status") == "FAIL"
                    else "INCONCLUSIVE"
                )
                write_terminal_receipt(
                    self.run_dir,
                    status,
                    report_path=getattr(state, "report", None),
                    reason="; ".join((str(error), *getattr(error, "__notes__", ()))),
                )
            raise


def _duration_seconds(value: object) -> float:
    """Parse observed Spring Duration values without assuming an absent default."""
    import re

    if not isinstance(value, str):
        raise ValueError("effective duration must be an observed string")
    match = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value
    )
    if match and any(match.groups()):
        return sum(
            float(part or 0) * factor
            for part, factor in zip(match.groups(), (3600, 60, 1), strict=True)
        )
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if match:
        return float(match[1]) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match[2]]
    raise ValueError("effective duration format is unsupported")


def retention_from_configprops(document: dict) -> dict[str, float]:
    """Read the actual bound ExecutionStoreProperties bean, including clamping."""
    matches = []
    for context in document.get("contexts", {}).values():
        for name, bean in context.get("beans", {}).items():
            if name.endswith("ExecutionStoreProperties"):
                matches.append(bean.get("properties", {}))
    if len(matches) != 1:
        raise ValueError("effective execution-store bean is absent or ambiguous")
    properties = matches[0]
    return {
        "unkeyed-sync-outcome": _duration_seconds(properties.get("syncTtl")),
        "terminal-key-and-readable-outcome": _duration_seconds(properties.get("ttl")),
        "live-key-and-execution": _duration_seconds(properties.get("maxLifetime")),
    }


def _read_owned_argfile(root: Path, name: str, limit: int) -> tuple[bytes, dict]:
    """Read a bounded regular file beneath the target root without symlink traversal."""
    path = Path(name)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise ValueError(
            "JVM argfile requires an absolute container path without traversal"
        )
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)
    with os.fdopen(file_descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("JVM argfile exceeds its regular-file observation bound")
        body = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(body) > limit or any(
        getattr(before, key) != getattr(after, key) for key in fields
    ):
        raise ValueError("JVM argfile changed during bounded observation")
    return body, {key: getattr(after, key) for key in fields}


def _observed_java_options(
    directory: Path, argv: list[str], env: dict, proof: dict
) -> list[str]:
    """Expand a conservative launcher grammar, preserving every option and override.

    Evidence describes observed launch inputs, not a live VM.flags query. The
    current argfile bytes and metadata are retained for audit, not claimed to
    prove that mutable files have been unchanged since process startup.
    """

    def environment(name):
        value = env.get(name, "")
        if not value.isascii() or any(char in value for char in "'\"\\"):
            raise ValueError("unsupported JVM environment quoting or encoding")
        return value.split()

    options = environment("JAVA_TOOL_OPTIONS")
    trailing = environment("_JAVA_OPTIONS")
    if any(not item.startswith("-") for item in (*options, *trailing)):
        raise ValueError("unsupported JVM environment argument")
    pending = environment("JDK_JAVA_OPTIONS") + argv[1:]
    used = 0
    index = 0
    while index < len(pending):
        item = pending[index]
        if item.startswith("@"):
            if len(proof["argfiles"]) >= 16:
                raise ValueError("JVM argfile count exceeds observation bound")
            body, metadata = _read_owned_argfile(
                directory / "root", item[1:], 65536 - used
            )
            used += len(body)
            proof["argfiles"].append(
                {
                    "path": item[1:],
                    "bytes_base64": base64.b64encode(body).decode(),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "stat": metadata,
                }
            )
            text = body.decode("ascii")
            content = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            if any(char in content for char in "'\"\\") or any(
                ord(char) < 32 and char not in " \t\r\n\f" for char in text
            ):
                raise ValueError(
                    "unsupported JVM argfile quoting, escape or control character"
                )
            expanded = content.split()
            if any(token.startswith("@") for token in expanded):
                raise ValueError("nested JVM argfiles are unsupported")
            pending[index : index + 1] = expanded
            continue
        if item == "-jar" or not item.startswith("-"):
            break
        if (
            item in {"-cp", "-classpath", "-p", "-m", "--disable-@files"}
            or (
                item.startswith("--") and "=" not in item and item != "--enable-preview"
            )
            or item.startswith(("-XX:Flags=", "-XX:VMOptionsFile="))
        ):
            raise ValueError("unsupported JVM launcher option or indirect options file")
        options.append(item)
        index += 1
    options.extend(trailing)
    if any(item.startswith(("-XX:Flags=", "-XX:VMOptionsFile=")) for item in options):
        raise ValueError("indirect JVM options files are unsupported")
    return options


def observe_local_process(target: Target) -> dict[str, Any]:
    """Observe local PID identity, cgroup-v2 limits and actual runtime arguments."""
    import shlex

    from nanolab.tasks.soak.preflight import effective_cpu_limit

    before = collect_procfs(target.process_id, target.container_id)
    directory = Path("/proc") / str(target.process_id)

    def read(path):
        with path.open("rb") as stream:
            body = stream.read(65536 + 1)
        if len(body) > 65536:
            raise ValueError("process configuration exceeds observation bound")
        return body.decode()

    cgroup = directory / "root/sys/fs/cgroup"
    raw_cpu = read(cgroup / "cpu.max").split()
    if len(raw_cpu) != 2:
        raise ValueError("effective cgroup CPU quota is malformed")
    quota = None if raw_cpu[0] == "max" else int(raw_cpu[0])
    cpuset = read(cgroup / "cpuset.cpus.effective").strip()
    memory = read(cgroup / "memory.max").strip()
    argv = [arg for arg in read(directory / "cmdline").split("\0") if arg]
    env = dict(
        item.split("=", 1)
        for item in read(directory / "environ").split("\0")
        if "=" in item
    )
    executable = (directory / "exe").readlink().name
    runtime = (
        "jvm" if executable == "java" else "node" if executable == "node" else None
    )
    options = []
    argument_evidence = {}
    if runtime == "jvm":
        argument_evidence = {
            "target": asdict(target),
            "process_start_ticks": before["start_ticks"],
            "source": "observed launch inputs; not live VM flags",
            "argv": argv,
            "environment": {
                name: env.get(name, "")
                for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
            },
            "argfiles": [],
        }

        def owned_container():
            container = _docker_get(
                f"/containers/{target.container_id}/json",
                "/var/run/docker.sock",
                5,
            )
            state = container.get("State", {})
            if (
                container.get("Id") != target.container_id
                or state.get("Pid") != target.process_id
                or state.get("StartedAt") != target.process_started_at
                or state.get("Running") is not True
                or container.get("Config", {}).get("Image") != target.image_digest
            ):
                raise ValueError("owned JVM container identity changed")

        try:
            owned_container()
            options = _observed_java_options(directory, argv, env, argument_evidence)
            owned_container()
        except (OSError, ValueError) as error:
            options = None
            argument_evidence["unavailable"] = str(error)
    elif runtime == "node":
        options.extend(shlex.split(env.get("NODE_OPTIONS", "")))
        for arg in argv[1:]:
            if not arg.startswith("-"):
                break
            options.append(arg)
    after = collect_procfs(target.process_id, target.container_id)
    if before["start_ticks"] != after["start_ticks"]:
        raise ValueError(
            "process identity changed while observing effective configuration"
        )
    return {
        "cpu": effective_cpu_limit(quota, int(raw_cpu[1]), cpuset),
        "memory_bytes": None if memory == "max" else int(memory),
        "limit_sources": {
            "cpu": str(cgroup / "cpu.max"),
            "memory_bytes": str(cgroup / "memory.max"),
        },
        "runtime": runtime,
        "runtime_options": options if runtime is not None else None,
        "runtime_source": "procfs/exe,cmdline,environ",
        "runtime_argument_evidence": argument_evidence,
        "capabilities": ["procfs", "docker-engine"],
        "collection_sources": ["procfs", "docker-engine"],
    }


def _observed_collection_sources(rows: Iterable[Any]) -> set[str]:
    sources = set()
    for row in rows:
        if getattr(row, "availability", None) != "observed":
            continue
        source = getattr(row, "source", None)
        if isinstance(source, str) and source:
            sources.add(source.partition("/")[0])
    return sources


def observe_local_configuration(
    prepared: PreparedSoak | PreparedContainerdSoak,
    management_url: str,
    api_url: str | None = None,
) -> dict[str, Any]:
    """Query bounded actuator documents; missing bound values stay unavailable."""
    from urllib.request import ProxyHandler, Request, build_opener

    from nanolab.tasks.soak.collector import _NoRedirect, read_bounded

    result: dict[str, Any] = {"unavailable": {}}
    # configprops is an actuator document; the compiled module list is not.
    # Only the build-metadata module reports it, at /modules/build-metadata on
    # the application port, so /actuator/info never carried it.
    sources = {"configprops": management_url + "/actuator/configprops"}
    if api_url is not None:
        sources["build-metadata"] = api_url + "/modules/build-metadata"
    for name, url in sources.items():
        try:
            request = Request(url, headers={"Accept": "application/json"})
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(
                request, timeout=prepared.config.scrape_timeout_s
            ) as response:
                document = json.loads(read_bounded(response))
            if not isinstance(document, dict):
                raise ValueError("actuator observation must be an object")
            prepared.writer.write_json("effective-" + name + ".json", document)
            if name == "configprops":
                result["retention_s"] = retention_from_configprops(document)
                result["retention_source"] = (
                    "effective-configprops.json:ExecutionStoreProperties"
                )
            else:
                raw = document.get("modules")
                if not isinstance(raw, (list, str)):
                    raise ValueError(
                        "effective module information is absent or ambiguous"
                    )
                result["modules"] = (
                    [item.strip() for item in raw.split(",") if item.strip()]
                    if isinstance(raw, str)
                    else raw
                )
                result["modules_source"] = "effective-build-metadata.json:modules"
        except (OSError, ValueError, TypeError, KeyError) as error:
            result["unavailable"][name] = str(error)
    return result


def inspect_generator(
    prepared: PreparedSoak, base_url: str, command: tuple[str, ...]
) -> dict[str, Any]:
    """Ask k6 for execution requirements without starting workload traffic."""
    import os

    from nanolab.tasks.soak.processes import run_owned_command
    from nanolab.tasks.soak.workload import allocate_vus, constant_arrival_options

    config = prepared.config
    allocation = allocate_vus(
        config.workload.rates, config.workload.preallocated_vus, config.workload.max_vus
    )
    effective = {
        "base_url": base_url.rstrip("/"),
        "request_timeout_s": 30.0,
        "functions": [
            {
                "name": name,
                "scenario": f"fn_{index}",
                "options": {
                    **constant_arrival_options(
                        rate,
                        config.phases.steady_s,
                        allocation[name]["preAllocatedVUs"],
                        allocation[name]["maxVUs"],
                    ),
                    "exec": "invoke",
                    "gracefulStop": "30s",
                },
                "payloads": prepared.payloads[name],
            }
            for index, (name, rate) in enumerate(config.workload.rates.items())
        ],
    }
    path = prepared.writer.write_json("generator-inspect-input.json", effective)
    log = prepared.evidence_dir / "generator-inspect.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("K6_")}
    env.update(NANOLAB_SOAK_CONFIG=str(path), K6_NO_USAGE_REPORT="true")
    argv = (
        *command,
        "inspect",
        "--execution-requirements",
        # k6 run exposes the process environment to __ENV, k6 inspect does not,
        # so the script's own configuration has to be named on the command line.
        f"--env=NANOLAB_SOAK_CONFIG={path}",
        str(prepared.evidence_dir / "frozen-workload.js"),
    )
    result = run_owned_command(
        argv,
        cwd=prepared.evidence_dir,
        env=env,
        log_path=log,
        timeout_s=30,
        cancelled=Event(),
        output_limit_bytes=min(1024 * 1024, config.artifact_limit_bytes // 16),
    )
    if (
        result.returncode != 0
        or not result.reaped
        or result.errors
        or result.timed_out
        or result.quota_exceeded
        or result.forced_stop
    ):
        raise ValueError("k6 execution requirements could not be observed")
    requirements = _read_json(log)
    maximum = requirements.get("maxVUs")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("k6 did not report its effective VU capacity")
    prepared.writer.write_json(
        "generator-inspection.json",
        {
            "schema": "nanolab-soak-v1",
            "command": list(argv),
            "requirements": requirements,
            "process": asdict(result),
        },
    )
    return {
        "available": True,
        "max_vus": maximum,
        "source": "generator-inspection.json",
    }


def capture_function_owner(name, api_endpoint, control_plane_image, project_name, cwd):
    """Bind function ownership to the running CP container and catalog lifetime."""
    from nanolab.tasks.soak.owned_functions import FunctionOwnership

    container = _docker_get(
        f"/containers/{quote(project_name + '-control-plane-1', safe='')}/json",
        "/var/run/docker.sock",
        5,
    )
    owner = FunctionOwnership(
        name,
        api_endpoint,
        control_plane_image,
        project_name,
        cwd,
        container["Id"],
        container["State"]["StartedAt"],
    )
    verify_function_owner(owner)
    return owner


def verify_function_owner(owner) -> None:
    """Reject a replaced/restarted CP before interpreting any function HTTP status."""
    from urllib.parse import urlsplit

    container = _docker_get(
        f"/containers/{owner.control_plane_container_id}/json",
        "/var/run/docker.sock",
        5,
    )
    labels = container.get("Config", {}).get("Labels", {})
    state = container.get("State", {})
    endpoint = urlsplit(owner.api_endpoint)
    ports = (
        container.get("NetworkSettings", {}).get("Ports", {}).get("8080/tcp", []) or []
    )
    if (
        container.get("Id") != owner.control_plane_container_id
        or state.get("StartedAt") != owner.control_plane_started_at
        or state.get("Running") is not True
        or labels.get("com.docker.compose.project") != owner.project_name
        or labels.get("com.docker.compose.service") != "control-plane"
        or container.get("Config", {}).get("Image") != owner.control_plane_image
        or not any(
            port.get("HostIp") == endpoint.hostname
            and port.get("HostPort") == str(endpoint.port)
            for port in ports
        )
    ):
        raise ValueError("owned control-plane catalog continuity is unconfirmed")
