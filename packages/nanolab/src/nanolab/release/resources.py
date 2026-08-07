"""Sonata-owned VM lifecycle for the Azure release workflow."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Generic, TypeVar

from sonata_engine import Resource, TaskInputs
from sonata_tasks.provisioning.resources import provisioned_vm
from sonata_tasks.compensation import best_effort
from sonata_tasks.components.bootstrap import (
    plan_assets_sync_to_vm,
    plan_k3s_configure_registry,
    plan_k3s_install,
    plan_loadtest_install_k6,
    plan_registry_ensure_container,
    plan_vm_provision_base,
)
from sonata_tasks.vm.models import VmInfo, VmRequest

from sonata_tasks.provisioning import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config.environment import EnvironmentConfig, ExecutionRole
from nanolab.images.bake import render_bake_json
from nanolab.images.plan import ImagePlan
from nanolab.release.environment import secure_release_endpoints, verify_release_vm_facts
from nanolab.release.build import create_source_archive, stage_source_archive
from nanolab.release.model import ArtifactEvidence
from nanolab.release.secrets import (
    RemoteCosignCredentials,
    RemoteDockerCredentials,
    stage_cosign_credentials,
    stage_ghcr_credentials,
)
from nanolab.workspace.paths import discover_tool_root


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ReleaseEndpoints:
    control_plane: str
    prometheus: str


@dataclass(frozen=True, slots=True)
class ReleaseResources:
    stack: Resource[VmInfo]
    loadgen: Resource[VmInfo]
    arm_builder: Resource[VmInfo]
    endpoints: Resource[ReleaseEndpoints]

    @property
    def vms(self) -> tuple[Resource[VmInfo], ...]:
        return self.stack, self.loadgen, self.arm_builder


@dataclass(frozen=True, slots=True)
class ReleaseSourceResources:
    local: Resource[ArtifactEvidence]
    stack: Resource[str]
    arm: Resource[str]


@dataclass(frozen=True, slots=True)
class BuildInputs:
    bake_file: Path
    buildkit_config: Path
    remote_bake_file: str
    remote_buildkit_config: str


@dataclass(frozen=True, slots=True)
class CredentialLease(Generic[T]):
    value: T
    _manager: AbstractContextManager[T]

    def close(self) -> None:
        self._manager.__exit__(None, None, None)


def release_execution_guard(
    credentials: object | None,
    *,
    repo_roots: tuple[Path, ...],
) -> Resource[None]:
    """Fail before cloud resources when an executable release lacks credentials."""

    def acquire(_inputs: TaskInputs) -> None:
        if credentials is None:
            raise ValueError("release credential config is required for execution")
        validate = getattr(credentials, "validate")
        for root in repo_roots:
            validate(repo_root=root)

    return Resource(
        title="Acquire validated release execution credentials",
        acquire=acquire,
        # No always_release: its release does nothing, so retaining it is a no-op.
        # The declaration belongs to resources that actually stage a secret.
        release=lambda _inputs, _value: None,
    )


def _credential_resource(
    title: str,
    manager: Callable[[], AbstractContextManager[T]],
    requires: tuple[Resource[Any], ...],
) -> Resource[CredentialLease[T]]:
    def acquire(_inputs: TaskInputs) -> CredentialLease[T]:
        context = manager()
        return CredentialLease(context.__enter__(), context)

    return Resource(
        title=title,
        acquire=acquire,
        release=lambda _inputs, lease: lease.close(),
        requires=requires,
        always_release=True,
    )


def ghcr_credentials_resource(
    *,
    provider: object,
    request: object,
    username: str,
    token_file: Path,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[CredentialLease[RemoteDockerCredentials]]:
    return _credential_resource(
        "Acquire staged GHCR credentials",
        lambda: stage_ghcr_credentials(
            provider, request, username=username, token_file=token_file
        ),
        requires,
    )


def cosign_credentials_resource(
    *,
    provider: object,
    request: object,
    key_file: Path,
    password_file: Path,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[CredentialLease[RemoteCosignCredentials]]:
    return _credential_resource(
        "Acquire staged Cosign credentials",
        lambda: stage_cosign_credentials(
            provider, request, key_file=key_file, password_file=password_file
        ),
        requires,
    )


def _release_remote_root(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or path == PurePosixPath("/")
        or path.parent.name != "nanofaas-release"
        or path.parent.parent == PurePosixPath("/")
        or not path.name
    ):
        raise ValueError("release remote root must be an absolute versioned nanofaas-release path")
    return path


def _release_remote_source_paths(source: str, archive: str) -> tuple[str, str]:
    source_path = PurePosixPath(source)
    archive_path = PurePosixPath(archive)
    if (
        ".." in source_path.parts
        or ".." in archive_path.parts
        or str(source_path) != source
        or str(archive_path) != archive
        or source_path.name != "source"
        or archive_path.name != "source.tar"
        or source_path.parent != archive_path.parent
    ):
        raise ValueError("release remote source paths are unsafe")
    _release_remote_root(str(source_path.parent))
    return str(source_path), str(archive_path)


def _require_remote_success(result: object, action: str) -> None:
    return_code = int(getattr(result, "return_code", 0))
    if return_code != 0:
        detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        raise RuntimeError(detail or f"{action} failed (exit {return_code})")


def _bootstrap_role(
    provider: object,
    repo_root: Path,
    role: ExecutionRole,
    request: VmRequest,
    info: VmInfo,
) -> None:
    resolved = request.model_copy(
        update={"lifecycle": "external", "host": info.host, "user": info.user, "home": info.home}
    )
    context = scenario_context(repo_root, resolved, discover_tool_root() / "assets")
    if role == "stack":
        raw = (
            *plan_vm_provision_base(context),
            *plan_k3s_install(context),
            *plan_registry_ensure_container(context),
            *plan_k3s_configure_registry(context),
        )
    elif role == "loadgen":
        raw = (*plan_loadtest_install_k6(context), *plan_assets_sync_to_vm(context))
    else:
        raw = plan_vm_provision_base(context)
    operations = retarget_cloud_operations(provider, context, remote_operations(raw))
    run_bootstrap_operations(provider, operations, role=role)


def build_release_resources(
    environment: EnvironmentConfig,
    repo_root: Path,
    provider: object,
    *,
    requires: tuple[Resource[Any], ...] = (),
) -> ReleaseResources:
    """Build three ordered VM resources without discovering cloud state."""
    stack_request = vm_request_for_role(environment, "stack", loadtest=True)
    loadgen_request = vm_request_for_role(environment, "loadgen", loadtest=True)
    arm_request = vm_request_for_role(environment, "arm-builder")

    def after_stack(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "stack", stack_request)
        secure_release_endpoints(environment, provider, stack_request, None)
        _bootstrap_role(provider, repo_root, "stack", stack_request, info)

    def after_loadgen(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "loadgen", loadgen_request)
        secure_release_endpoints(environment, provider, stack_request, loadgen_request)
        _bootstrap_role(provider, repo_root, "loadgen", loadgen_request, info)

    def after_arm(info: VmInfo) -> None:
        verify_release_vm_facts(environment, provider, "arm-builder", arm_request)
        provider.restrict_inbound_sources(  # type: ignore[attr-defined]
            stack_request,
            ports=(5000,),
            source_cidrs=(f"{info.host}/32",),
            priority_base=1020,
        )
        _bootstrap_role(provider, repo_root, "arm-builder", arm_request, info)

    stack = provisioned_vm(
        title="Acquire release stack VM",
        request=stack_request,
        provider=provider,
        after_ensure=after_stack,
        requires=requires,
    )
    loadgen = provisioned_vm(
        title="Acquire release loadgen VM",
        request=loadgen_request,
        provider=provider,
        after_ensure=after_loadgen,
        requires=requires,
    )
    arm_builder = provisioned_vm(
        title="Acquire release ARM builder VM",
        request=arm_request,
        provider=provider,
        after_ensure=after_arm,
        requires=requires,
    )

    def acquire_endpoints(inputs: TaskInputs) -> ReleaseEndpoints:
        stack_info = inputs.resource(stack)
        _ = inputs.resource(loadgen)
        return ReleaseEndpoints(
            control_plane=f"http://{stack_info.host}:30080",
            prometheus=f"http://{stack_info.host}:30090",
        )

    endpoints = Resource(
        title="Acquire release endpoints",
        acquire=acquire_endpoints,
        release=lambda _inputs, _value: None,
        requires=(stack, loadgen),
    )
    return ReleaseResources(stack, loadgen, arm_builder, endpoints)


def build_release_source_resources(
    *,
    repo_root: Path,
    commit: str,
    run_dir: Path,
    remote_source_dir: str,
    remote_archive: str,
    provider: object,
    stack_request: object,
    arm_request: object,
    stack_requires: tuple[Resource[Any], ...] = (),
    arm_requires: tuple[Resource[Any], ...] = (),
) -> ReleaseSourceResources:
    """Create one immutable archive and verify that digest on both release VMs."""
    remote_source_dir, remote_archive = _release_remote_source_paths(
        remote_source_dir, remote_archive
    )
    archive = Path(run_dir) / "source.tar"

    def acquire_local(_inputs: TaskInputs) -> ArtifactEvidence:
        archive.unlink(missing_ok=True)
        return create_source_archive(repo_root, commit, archive)

    local = Resource(
        title="Acquire immutable release source archive",
        acquire=acquire_local,
        release=lambda _inputs, _value: archive.unlink(missing_ok=True),
    )

    def remote(request: object, requires: tuple[Resource[Any], ...]) -> Resource[str]:
        def cleanup() -> None:
            result = provider.exec_argv(  # type: ignore[attr-defined]
                request, ("rm", "-rf", "--", remote_source_dir, remote_archive)
            )
            _require_remote_success(result, "release source cleanup")

        def acquire(inputs: TaskInputs) -> str:
            evidence = inputs.resource(local)
            try:
                stage_source_archive(
                    provider,
                    request,
                    archive=archive,
                    remote_archive=remote_archive,
                    remote_source_dir=remote_source_dir,
                    expected_digest=evidence.digest,
                )
            except BaseException as error:
                best_effort(error, cleanup, what="release source failed acquire")
                raise
            return remote_source_dir

        return Resource(
            title=f"Acquire verified source on {getattr(request, 'name', 'release VM')}",
            acquire=acquire,
            release=lambda _inputs, _value: cleanup(),
            requires=(local, *requires),
        )

    return ReleaseSourceResources(
        local=local,
        stack=remote(stack_request, stack_requires),
        arm=remote(arm_request, arm_requires),
    )


def build_inputs_resource(
    *,
    image_plan: ImagePlan,
    max_parallelism: int,
    run_dir: Path,
    remote_root: str,
    provider: object,
    request: object,
    architecture: str,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[BuildInputs]:
    """Stage the two generated inputs consumed by a Buildx builder.

    The filenames carry the architecture because both builders stage into the
    same local run directory: a shared name would let one architecture's
    cleanup delete the other's file.
    """
    remote_root = str(_release_remote_root(remote_root))
    bake = Path(run_dir) / f"docker-bake-{architecture}.json"
    buildkit = Path(run_dir) / f"buildkitd-{architecture}.toml"
    remote_bake = f"{remote_root}/{bake.name}"
    remote_buildkit = f"{remote_root}/{buildkit.name}"

    def cleanup() -> None:
        error: BaseException | None = None
        try:
            result = provider.exec_argv(  # type: ignore[attr-defined]
                request,
                ("rm", "-f", "--", remote_bake, remote_buildkit),
            )
            _require_remote_success(result, f"{architecture} release input cleanup")
        except BaseException as cleanup_error:
            error = cleanup_error
        finally:
            bake.unlink(missing_ok=True)
            buildkit.unlink(missing_ok=True)
        if error is not None:
            raise error

    def acquire(_inputs: TaskInputs) -> BuildInputs:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        bake.write_text(render_bake_json(image_plan), encoding="utf-8")
        buildkit.write_text(
            f"[worker.oci]\n  max-parallelism = {max_parallelism}\n",
            encoding="utf-8",
        )
        try:
            result = provider.exec_argv(  # type: ignore[attr-defined]
                request, ("mkdir", "-p", remote_root)
            )
            if int(getattr(result, "return_code", 0)) != 0:
                raise RuntimeError(f"create {architecture} release input directory failed")
            for source, destination in ((bake, remote_bake), (buildkit, remote_buildkit)):
                result = provider.transfer_to(  # type: ignore[attr-defined]
                    request, source=source, destination=destination
                )
                if int(getattr(result, "return_code", 0)) != 0:
                    raise RuntimeError(f"transfer {source.name} failed")
        except BaseException as error:
            best_effort(error, cleanup, what=f"{architecture} release inputs failed acquire")
            raise
        return BuildInputs(bake, buildkit, remote_bake, remote_buildkit)

    return Resource(
        title=f"Acquire {architecture.upper()} Bake and BuildKit inputs",
        acquire=acquire,
        release=lambda _inputs, _value: cleanup(),
        requires=requires,
    )
