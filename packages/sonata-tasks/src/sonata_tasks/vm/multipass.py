from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from multipass import MultipassClient, MultipassCommandError, VmNotFoundError, find_ssh_public_key
from shellcraft.backend import ShellBackend, ShellExecutionResult, SubprocessShell

from sonata_tasks.deployment import DEFAULT_NAMESPACE
from sonata_tasks.vm.models import VmRequest, vm_remote_home


REPO_SYNC_EXCLUDE_PATTERNS = (
    ".git", ".git/", ".gitnexus", ".gradle/", ".gradle-local/", ".DS_Store",
    ".idea/", ".vscode/", ".env", "*.log", "*.class", ".worktrees/",
    "__pycache__/", "*.egg-info/", "*.pyc", "*.pyo", "*.pyd",
    ".pytest_cache/", ".venv/", ".uv/", "node_modules/", "dist/",
    "/building/", "out/", "target/", "building-test/", "k6/results/",
    "experiments/k6/results/", "experiments/loadtest/results/",
    "experiments/.image-cache/", "tools/controlplane/runs/",
    "recovery/",
)
_SSH_CREDENTIALS_UNRESOLVED = object()


def _vm_name_default(request: VmRequest) -> str:
    return request.name or DEFAULT_NAMESPACE


def _ok(command: list[str], *, stdout: str = "") -> ShellExecutionResult:
    return ShellExecutionResult(command=command, return_code=0, stdout=stdout)


def _sdk_error(e: MultipassCommandError) -> ShellExecutionResult:
    return ShellExecutionResult(
        command=e.args_list,
        return_code=e.returncode,
        stdout=e.stdout,
        stderr=e.stderr,
    )


def _find_ssh_private_key_path(public_key: str | None = None) -> Path | None:
    ssh_dir = Path.home() / ".ssh"
    normalized_public_key = public_key.strip() if public_key else None
    for name in ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"):
        pub = ssh_dir / f"{name}.pub"
        priv = ssh_dir / name
        if pub.exists() and priv.exists():
            if normalized_public_key is not None:
                if pub.read_text(encoding="utf-8").strip() == normalized_public_key:
                    return priv
                continue
            return priv
    return None


def resolve_connection_host(
    request: VmRequest,
    client: MultipassClient,
    *,
    dry_run: bool = False,
) -> str:
    if request.lifecycle == "external":
        if not request.host:
            raise RuntimeError("external VM lifecycle requires a host")
        return request.host
    if dry_run:
        return f"<multipass-ip:{_vm_name_default(request)}>"
    try:
        info = client.get_vm(_vm_name_default(request)).info()
    except VmNotFoundError:
        raise RuntimeError(f"Unable to resolve Multipass VM '{_vm_name_default(request)}'")
    if info.ipv4:
        return info.ipv4[0]
    raise RuntimeError(f"Multipass VM '{_vm_name_default(request)}' has no IPv4 address")


def repo_sync_ssh_rsh(
    private_key_path: Path | None = None,
    *,
    port: int | None = None,
) -> str:
    parts = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    if port is not None:
        parts.extend(["-p", str(port)])
    if private_key_path is not None:
        parts.extend(["-i", str(private_key_path)])
    return shlex.join(parts)


def repo_rsync_command(
    *,
    source: Path,
    user: str,
    host: str,
    destination: str,
    ssh_rsh: str | None = None,
) -> list[str]:
    command = [
        "rsync", "-az", "--delete", "--delete-excluded",
        *(f"--exclude={pattern}" for pattern in REPO_SYNC_EXCLUDE_PATTERNS),
    ]
    if ssh_rsh is not None:
        command.extend(["-e", ssh_rsh])
    command.extend([f"{source}/", f"{user}@{host}:{destination.rstrip('/')}/"])
    return command


class MultipassVmProvider:
    """Generic multipass VM provider: lifecycle, command execution, file transfer.

    Subclass to add project-specific operations (see VmOrchestrator in sonata_tasks.vm.orchestrator).
    Takes workspace_root directly — no ToolPaths dependency.
    """

    def __init__(
        self,
        workspace_root: Path,
        shell: ShellBackend | None = None,
        multipass_client: MultipassClient | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.shell = shell or SubprocessShell()
        self._client = multipass_client or MultipassClient()
        self._ssh_public_key: str | None | object = _SSH_CREDENTIALS_UNRESOLVED
        self._private_key_path: Path | None = None

    def _ssh_credentials(self) -> tuple[str | None, Path | None]:
        if self._ssh_public_key is _SSH_CREDENTIALS_UNRESOLVED:
            public_key = find_ssh_public_key()
            self._ssh_public_key = public_key
            self._private_key_path = _find_ssh_private_key_path(public_key)
        return cast(str | None, self._ssh_public_key), self._private_key_path

    def _vm_name(self, request: VmRequest) -> str:
        return _vm_name_default(request)

    def _remote_home(self, request: VmRequest) -> str:
        return vm_remote_home(request)

    def _shell_run(self, command: list[str], *, dry_run: bool = False) -> ShellExecutionResult:
        # cwd here is the *local* working directory of the ssh/scp process, which
        # is not where the caller stands. Any local path in `command` must
        # therefore already be absolute — see _local_path — or it silently
        # resolves against the workspace instead of against the caller.
        return self.shell.run(command, cwd=self.workspace_root, dry_run=dry_run)

    def _ssh_options(self) -> list[str]:
        _, private_key = self._ssh_credentials()
        options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        if private_key is not None:
            options.extend(("-i", str(private_key)))
        return options

    def _ssh_target(self, request: VmRequest, *, dry_run: bool) -> str:
        return f"{request.user}@{self.connection_host(request, dry_run=dry_run)}"

    @staticmethod
    def _build_exec_script(
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
    ) -> str:
        parts: list[str] = []
        if remote_dir:
            parts.append(f"cd {shlex.quote(remote_dir)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={shlex.quote(v)}")
        parts.append(shlex.join(list(argv)))
        return " && ".join(parts)

    def vm_name(self, request: VmRequest) -> str:
        return self._vm_name(request)

    def remote_home(self, request: VmRequest) -> str:
        return self._remote_home(request)

    def resolve_multipass_ipv4(self, request: VmRequest, *, dry_run: bool = False) -> str:
        return resolve_connection_host(request, self._client, dry_run=dry_run)

    def connection_host(self, request: VmRequest, *, dry_run: bool = False) -> str:
        return resolve_connection_host(request, self._client, dry_run=dry_run)

    def ensure_running(self, request: VmRequest, *, dry_run: bool = False) -> ShellExecutionResult:
        if request.lifecycle == "external":
            return self._shell_run(
                ["ssh", f"{request.user}@{request.host}", "true"], dry_run=dry_run
            )
        name = self._vm_name(request)
        launch_cmd = [
            "multipass", "launch", "--name", name,
            "--cpus", str(request.cpus), "--memory", request.memory, "--disk", request.disk,
        ]
        if dry_run:
            return _ok(launch_cmd)
        public_key, _ = self._ssh_credentials()
        cloud_init_config = (
            {"ssh_authorized_keys": [public_key]} if public_key else None
        )
        self._client.ensure_running(
            name, cpus=request.cpus, memory=request.memory,
            disk=request.disk, cloud_init_config=cloud_init_config,
        )
        return _ok(launch_cmd)

    def teardown(self, request: VmRequest, *, dry_run: bool = False) -> ShellExecutionResult:
        if request.lifecycle == "external":
            return self._shell_run(
                ["echo", "Skipping teardown for external VM lifecycle"], dry_run=dry_run
            )
        name = self._vm_name(request)
        if dry_run:
            return _ok(["multipass", "delete", name])
        try:
            self._client.get_vm(name).delete()
        except (VmNotFoundError, MultipassCommandError) as e:
            if isinstance(e, MultipassCommandError):
                return _sdk_error(e)
        return _ok(["multipass", "delete", name])

    def inspect(self, request: VmRequest, *, dry_run: bool = False) -> ShellExecutionResult:
        if request.lifecycle == "external":
            return self._shell_run(
                ["ssh", f"{request.user}@{request.host}", "hostname"], dry_run=dry_run
            )
        name = self._vm_name(request)
        if dry_run:
            return _ok(["multipass", "info", name])
        try:
            info = self._client.get_vm(name).info()
            stdout = (
                f"Name:  {info.name}\n"
                f"State: {info.state.value}\n"
                f"IPv4:  {', '.join(info.ipv4) or '-'}\n"
            )
            return _ok(["multipass", "info", name], stdout=stdout)
        except MultipassCommandError as e:
            return _sdk_error(e)

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        command = self._build_exec_script(argv, env=env, remote_dir=remote_dir)
        return self.remote_exec(request, command=command, dry_run=dry_run)

    def remote_exec(
        self,
        request: VmRequest,
        *,
        command: str,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        return self._shell_run(
            ["ssh", *self._ssh_options(), self._ssh_target(request, dry_run=dry_run), shlex.join(("bash", "-lc", command))],
            dry_run=dry_run,
        )

    @staticmethod
    def _local_path(path: Path) -> str:
        """A local path the scp process will read the same way its caller meant.

        scp runs from the workspace, not from wherever the caller stands, so a
        relative path would quietly mean a different file. It cost a silent bug:
        load-test results "transferred" successfully into the checkout under
        test, named after the run directory, while the run directory stayed
        empty.
        """
        return str(Path(path).resolve())

    def transfer_to(
        self,
        request: VmRequest,
        *,
        source: Path,
        destination: str,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        return self._shell_run(
            [
                "scp",
                *self._ssh_options(),
                self._local_path(source),
                f"{self._ssh_target(request, dry_run=dry_run)}:{destination}",
            ],
            dry_run=dry_run,
        )

    def transfer_from(
        self,
        request: VmRequest,
        *,
        source: str,
        destination: Path,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        return self._shell_run(
            [
                "scp",
                *self._ssh_options(),
                f"{self._ssh_target(request, dry_run=dry_run)}:{source}",
                self._local_path(destination),
            ],
            dry_run=dry_run,
        )
