from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import shlex
import socket
import subprocess
import time
from typing import Any, cast
from urllib.parse import urlsplit

from multipass import MultipassClient
from sonata_tasks.execution.bindings import RetargetingCommandTaskExecutor, RoleBindings
from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.tasks.executors import (
    HostCommandRunner,
    HostCommandTaskExecutor,
    VmCommandResult,
    VmCommandTaskExecutor,
)
from sonata_tasks.shell import SubprocessShell
from sonata_tasks.vm.models import VmRequest, vm_remote_home
from sonata_tasks.vm.multipass import resolve_connection_host
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.runners import VmFileFetcher

from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config.environment import EnvironmentConfig, RoleTarget
from nanolab.config.scenario import BackendName
from nanolab.workspace.paths import default_tool_paths


StackHostResolver = Callable[[RoleTarget], str]


def resolve_loadtest_urls(
    environment: EnvironmentConfig,
    *,
    backend: BackendName = "k8s",
    control_plane_url: str | None = None,
    prometheus_url: str | None = None,
    dry_run: bool = False,
    host_resolver: StackHostResolver | None = None,
    vm_provider: object | None = None,
) -> tuple[str, str]:
    if control_plane_url is not None and prometheus_url is not None:
        return control_plane_url, prometheus_url

    if backend == "container":
        if environment.provider != "local":
            raise ValueError("container load-test requires a local environment")
        return (
            control_plane_url or "http://127.0.0.1:8080",
            prometheus_url or "http://127.0.0.1:9090",
        )

    target = environment.target("stack")
    if environment.provider == "local":
        host = "127.0.0.1"
    elif environment.provider == "multipass":
        if host_resolver is not None:
            host = host_resolver(target)
        else:
            host = resolve_connection_host(
                VmRequest(lifecycle="multipass", name=target.name),
                MultipassClient(),
                dry_run=dry_run,
            )
    elif environment.provider in {"azure", "proxmox"}:
        request = vm_request_for_role(environment, "stack", loadtest=True)
        if dry_run:
            if environment.provider == "azure":
                host = f"<azure-ip:{request.name}>"
                discovered_prometheus = f"http://{host}:30090"
            else:
                host = f"<proxmox-guest-ip:{request.name}>"
                discovered_prometheus = f"http://<proxmox-prometheus:{request.name}>"
        else:
            provider = vm_provider or provider_for(
                request, default_tool_paths().nanofaas_root
            )
            if environment.provider == "azure":
                host = provider.connection_host(request)  # type: ignore[attr-defined]
                discovered_prometheus = f"http://{host}:30090"
            else:
                host = provider.guest_host(request)  # type: ignore[attr-defined]
                metrics_host, metrics_port = provider.publish_port(  # type: ignore[attr-defined]
                    request, service="PROMETHEUS_HTTP", guest_port=30090
                )
                discovered_prometheus = f"http://{metrics_host}:{metrics_port}"
        return (
            control_plane_url or f"http://{host}:30080",
            prometheus_url or discovered_prometheus,
        )
    elif target.host:
        host = target.host
    else:
        raise ValueError(f"{environment.provider} stack requires a host or explicit load-test URLs")

    return (
        control_plane_url or f"http://{host}:30080",
        prometheus_url or f"http://{host}:30090",
    )


def _free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _accepts_connections(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


@contextmanager
def prometheus_over_ssh(
    environment: EnvironmentConfig,
    url: str,
    *,
    enabled: bool = True,
    spawn: Callable[..., Any] = subprocess.Popen,
    ready: Callable[[int], bool] = _accepts_connections,
    timeout_seconds: float = 15.0,
) -> Iterator[str]:
    """Yield a Prometheus URL the host process can actually open.

    On a Multipass environment the VM's address is a local-network peer, and
    macOS refuses those to binaries it has not been granted Local Network
    access — which the uv-managed Python is not. Every host-side query then
    fails instantly with EHOSTUNREACH, so retrying is pointless: the snapshot
    simply never worked from a Mac, while the same scenario passed on `local`
    because there the URL is loopback.

    Forwarding the port makes it loopback for everyone, on any machine, without
    asking each developer to grant a permission or to remember a wrapper.

    Yields `url` untouched when there is nothing to forward: a non-Multipass
    provider, or a URL the caller chose explicitly and may already be tunnelling
    themselves.
    """
    parts = urlsplit(url)
    if not enabled or environment.provider != "multipass" or not parts.port:
        yield url
        return

    # The address is taken from the URL rather than resolved again: whoever
    # produced it already asked Multipass where the VM answers, and asking twice
    # is how the two would drift apart.
    host = parts.hostname
    if not host:
        yield url
        return
    target = environment.target("stack")
    local_port = _free_local_port()
    process = spawn(
        [
            "ssh",
            "-N",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            # Without this ssh stays up after failing to bind, and the readiness
            # wait below would blame the timeout instead of the port.
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{parts.port}",
            f"{target.user}@{host}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                detail = ""
                if process.stderr is not None:
                    detail = process.stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"Prometheus tunnel to {host} exited: {detail or 'no output'}"
                )
            if ready(local_port):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(
                f"Prometheus tunnel to {host} never accepted a connection "
                f"within {timeout_seconds:g}s"
            )
        yield f"http://127.0.0.1:{local_port}"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                _ = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                _ = process.wait(timeout=5)


def _home(target: RoleTarget) -> str:
    return target.home or ("/root" if target.user == "root" else f"/home/{target.user}")


def _command(argv: tuple[str, ...], env: dict[str, str], cwd: str) -> str:
    command = f"cd {shlex.quote(cwd)} && "
    if env:
        assignments = shlex.join([f"{key}={value}" for key, value in env.items()])
        command += f"env {assignments} "
    return command + shlex.join(argv)


class _RemoteRunner:
    def __init__(
        self,
        runner: HostCommandRunner,
        target: RoleTarget,
        provider: str,
        default_env: dict[str, str] | None = None,
    ) -> None:
        self._runner = runner
        self._target = target
        self._provider = provider
        self._default_env = default_env or {}

    def run_vm_command(self, argv, *, env, remote_dir, dry_run):
        command = _command(
            argv,
            {**self._default_env, **env},
            remote_dir or f"{_home(self._target)}/nanofaas",
        )
        if self._provider == "multipass":
            if not self._target.name:
                raise ValueError("Multipass role requires an instance name")
            outer = ["multipass", "exec", self._target.name, "--", "bash", "-lc", command]
        else:
            if not self._target.host:
                raise ValueError(f"{self._provider} role requires a reachable SSH host")
            outer = [
                "ssh",
                "-o",
                "BatchMode=yes",
                f"{self._target.user}@{self._target.host}",
                f"bash -lc {shlex.quote(command)}",
            ]
        return self._runner.run(outer, cwd=None, env={}, dry_run=dry_run)


class _RemoteFetcher:
    def __init__(self, runner: HostCommandRunner, target: RoleTarget, provider: str) -> None:
        self._runner = runner
        self._target = target
        self._provider = provider

    def fetch_from(self, remote: str, local: Path) -> None:
        if self._provider == "multipass":
            argv = ["multipass", "transfer", f"{self._target.name}:{remote}", str(local)]
        else:
            argv = ["scp", f"{self._target.user}@{self._target.host}:{remote}", str(local)]
        result = self._runner.run(argv, cwd=None, env={}, dry_run=False)
        if result.return_code != 0:
            raise RuntimeError(result.stderr or result.stdout or "remote file transfer failed")


class _ProviderRunner:
    """A VM provider as a VmCommandRunner, carrying the role's defaults.

    Calls the provider directly: the adapter that used to sit in between did
    nothing but rename a keyword, one layer below the one that supplies the
    defaults.
    """

    def __init__(
        self,
        provider: object,
        request: object,
        *,
        default_env: dict[str, str],
        default_dir: str,
    ) -> None:
        self._provider = provider
        self._request = request
        self._default_env = default_env
        self._default_dir = default_dir

    def run_vm_command(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        remote_dir: str | None,
        dry_run: bool,
    ) -> VmCommandResult:
        merged = {**self._default_env, **env}
        return cast(
            VmCommandResult,
            self._provider.exec_argv(  # type: ignore[attr-defined]
                self._request,
                argv,
                env=merged or None,
                remote_dir=remote_dir or self._default_dir,
                dry_run=dry_run,
            ),
        )


def build_role_bindings(
    environment: EnvironmentConfig,
    *,
    runner: HostCommandRunner | None = None,
    vm_provider: object | None = None,
    repo_root: Path | None = None,
) -> tuple[RoleBindings, _RemoteFetcher | VmFileFetcher | None]:
    command_runner = runner or SubprocessShell()
    host = HostCommandTaskExecutor(command_runner)
    if environment.provider == "local":
        return RoleBindings(host=host, stack=host, loadgen=host, cloud=host, arm_builder=host), None

    if environment.provider in {"multipass", "azure", "proxmox"}:
        if environment.provider == "multipass":
            provider = vm_provider or VmOrchestrator(
                repo_root or default_tool_paths().nanofaas_root,
                shell=command_runner,
            )
        else:
            provider = vm_provider or provider_for(
                vm_request_for_role(environment, "stack", loadtest=True),
                repo_root or default_tool_paths().nanofaas_root,
            )

        def provider_remote(role: str):
            request = vm_request_for_role(environment, role)  # type: ignore[arg-type]
            default_env = (
                {
                    "KUBECONFIG": environment.target(role).kubeconfig
                    or f"{vm_remote_home(request)}/.kube/config"
                }
                if role in ("stack", "cloud")
                else {}
            )
            executor = VmCommandTaskExecutor(
                _ProviderRunner(
                    provider,
                    request,
                    default_env=default_env,
                    default_dir=(
                        f"{vm_remote_home(request)}/nanofaas"
                        if environment.provider == "multipass"
                        else vm_remote_home(request)
                    ),
                )
            )
            return RetargetingCommandTaskExecutor(executor, "vm"), request

        stack, stack_request = provider_remote("stack")
        loadgen_result = provider_remote("loadgen") if "loadgen" in environment.roles else None
        loadgen = loadgen_result[0] if loadgen_result else None
        cloud_result = provider_remote("cloud") if "cloud" in environment.roles else None
        cloud = cloud_result[0] if cloud_result else None
        arm_builder_result = provider_remote("arm-builder") if "arm-builder" in environment.roles else None
        arm_builder = arm_builder_result[0] if arm_builder_result else None
        fetch_request = loadgen_result[1] if loadgen_result else stack_request
        return (
            RoleBindings(host=host, stack=stack, loadgen=loadgen, cloud=cloud, arm_builder=arm_builder),
            VmFileFetcher(provider, fetch_request),
        )

    def remote(role: str):
        target = environment.target(role)  # type: ignore[arg-type]
        default_env = (
            {"KUBECONFIG": target.kubeconfig or f"{_home(target)}/.kube/config"}
            if role in ("stack", "cloud")
            else None
        )
        executor = VmCommandTaskExecutor(
            _RemoteRunner(command_runner, target, environment.provider, default_env)
        )
        return RetargetingCommandTaskExecutor(executor, "vm")

    stack = remote("stack")
    loadgen = remote("loadgen") if "loadgen" in environment.roles else None
    cloud = remote("cloud") if "cloud" in environment.roles else None
    arm_builder = remote("arm-builder") if "arm-builder" in environment.roles else None
    fetch_target = environment.target("loadgen" if loadgen is not None else "stack")
    return (
        RoleBindings(host=host, stack=stack, loadgen=loadgen, cloud=cloud, arm_builder=arm_builder),
        _RemoteFetcher(command_runner, fetch_target, environment.provider),
    )
