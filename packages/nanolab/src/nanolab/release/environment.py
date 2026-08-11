"""Validation for the one comparable Azure release environment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import tempfile

from nanolab.config import EnvironmentConfig
from nanolab.config.environment import ExecutionRole
from nanolab.release.versioning import normalize_version, verify_version_consistency
from sonata_tasks.deployment import CONTROL_PLANE_NODE_PORT, PROMETHEUS_NODE_PORT
from sonata_tasks.vm.models import VmRequest


_STACK_VM_SIZE = "Standard_D8s_v5"
_LOADGEN_VM_SIZE = "Standard_D2s_v5"
_ARM_VM_SIZE = "Standard_D8ps_v5"
_LOCATION = "westeurope"
_STACK_DISK = "128G"
_LOADGEN_DISK = "30G"
_ARM_DISK = "64G"
_STACK_NAME = "nanofaas-azure-release"
_LOADGEN_NAME = "nanofaas-azure-release-loadgen"
_ARM_NAME = "nanofaas-azure-release-arm"
_URN_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")


class ReleaseRunInProgressError(RuntimeError):
    """Another coordinator already holds the lock for these Azure release VMs."""


def release_lock_path(environment: EnvironmentConfig) -> Path:
    """One lock per Azure VM identity, independent of version and run directory."""
    azure = environment.azure
    assert azure is not None
    identity = json.dumps(
        (
            azure.resource_group.casefold(),
            (environment.target("stack").name or "").casefold(),
            (environment.target("loadgen").name or "").casefold(),
        ),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"nanofaas-release-locks-{os.getuid()}" / f"{digest}.lock"


@contextmanager
def release_run_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseRunInProgressError("release run is already in progress") from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def validate_release_environment(
    environment: EnvironmentConfig,
    repo_root: Path,
    requested_version: str,
) -> None:
    """Reject any environment that cannot produce comparable Azure evidence."""
    if environment.provider != "azure" or environment.azure is None:
        raise ValueError("release requires an Azure environment")
    if not {"stack", "loadgen", "arm-builder"}.issubset(environment.roles):
        raise ValueError("release environment requires stack, loadgen and arm-builder roles")

    azure = environment.azure
    _require_exact("Azure location", azure.location, _LOCATION)
    _require_exact("stack VM size", azure.vm_size, _STACK_VM_SIZE)
    _require_exact("loadgen VM size", azure.loadgen_vm_size, _LOADGEN_VM_SIZE)
    _require_exact("arm-builder VM size", azure.arm_vm_size, _ARM_VM_SIZE)
    _require_exact("stack disk", environment.roles["stack"].disk, _STACK_DISK)
    _require_exact("loadgen disk", environment.roles["loadgen"].disk, _LOADGEN_DISK)
    _require_exact("arm-builder disk", environment.roles["arm-builder"].disk, _ARM_DISK)
    _require_exact(
        "stack dedicated release VM name",
        environment.roles["stack"].name or "",
        _STACK_NAME,
    )
    _require_exact(
        "loadgen dedicated release VM name",
        environment.roles["loadgen"].name or "",
        _LOADGEN_NAME,
    )
    _require_exact(
        "arm-builder dedicated release VM name",
        environment.roles["arm-builder"].name or "",
        _ARM_NAME,
    )
    _validate_image_urn(azure.image_urn)
    _validate_image_urn(azure.arm_image_urn)
    _validate_operator_source(azure.operator_source_cidr)

    requested_plain, _ = normalize_version(requested_version)
    if verify_version_consistency(repo_root) != requested_plain:
        raise ValueError("requested version does not match the prepared project version")


def _require_exact(name: str, actual: str, expected: str) -> None:
    if actual.lower().startswith("standard_b"):
        raise ValueError(f"{name} must not use a burstable VM size")
    if actual != expected:
        raise ValueError(f"{name} must be {expected}")


def _validate_image_urn(urn: str | None) -> None:
    parts = urn.split(":") if urn is not None else ()
    if (
        len(parts) != 4
        or any(_URN_COMPONENT.fullmatch(part) is None for part in parts)
        or parts[-1].lower() == "latest"
    ):
        raise ValueError("Azure image URN must be an exact, non-latest four-part URN")


def _validate_operator_source(source: str | None) -> None:
    try:
        network = ipaddress.ip_network(source, strict=True) if source is not None else None
    except ValueError:
        network = None
    if network is None or network.prefixlen == 0 or not network.is_global:
        raise ValueError("Azure operator source CIDR must be a restricted network")


def verify_release_vm_facts(
    environment: EnvironmentConfig,
    provider: object,
    role: ExecutionRole,
    request: VmRequest,
) -> None:
    azure = environment.azure
    assert azure is not None
    facts = provider.release_vm_facts(request)  # type: ignore[attr-defined]
    target = environment.target(role)
    expected_size = (
        azure.loadgen_vm_size
        if role == "loadgen"
        else azure.arm_vm_size
        if role == "arm-builder"
        else azure.vm_size
    )
    expected = {
        "location": azure.location,
        "vm_size": expected_size,
        "disk_size_gb": int(target.disk.removesuffix("G")),
        "image_urn": azure.arm_image_urn if role == "arm-builder" else azure.image_urn,
    }
    mismatches = tuple(
        name for name, value in expected.items() if getattr(facts, name, None) != value
    )
    if mismatches:
        raise RuntimeError(f"Azure release VM facts mismatch for {role}: {', '.join(mismatches)}")


def secure_release_endpoints(
    environment: EnvironmentConfig,
    provider: object,
    stack_request: VmRequest,
    loadgen_request: VmRequest | None,
) -> tuple[str, str]:
    azure = environment.azure
    assert azure is not None and azure.operator_source_cidr is not None
    stack_host = provider.connection_host(stack_request)  # type: ignore[attr-defined]
    sources = (azure.operator_source_cidr,)
    if loadgen_request is not None:
        loadgen_address = ipaddress.ip_address(
            provider.connection_host(loadgen_request)  # type: ignore[attr-defined]
        )
        sources = tuple(
            dict.fromkeys((f"{loadgen_address}/{loadgen_address.max_prefixlen}", *sources))
        )
    provider.restrict_inbound_sources(  # type: ignore[attr-defined]
        stack_request,
        ports=(CONTROL_PLANE_NODE_PORT, 30081, PROMETHEUS_NODE_PORT),
        source_cidrs=sources,
    )
    return (
        f"http://{stack_host}:{CONTROL_PLANE_NODE_PORT}",
        f"http://{stack_host}:{PROMETHEUS_NODE_PORT}",
    )
