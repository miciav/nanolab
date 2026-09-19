"""Multipass-backed VM orchestrator that also drives Ansible and repo sync."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from multipass import MultipassCommandError, VmNotFoundError
from multipass.models import VmState
from shellcraft.backend import ShellExecutionResult
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.providers.multipass import MultipassVmProvider
from sonata_tasks.vm.results import successful_result

from nanolab.tasks.deployment import LOCAL_REGISTRY, REGISTRY_CONTAINER_NAME
from nanolab.tasks.vm.sync import repo_rsync_command, repo_sync_ssh_rsh

if TYPE_CHECKING:
    from multipass import MultipassClient
    from shellcraft.backend import ShellBackend

    from nanolab.tasks.infra.ansible import AnsibleAdapter

__all__ = ["VmOrchestrator"]


class VmOrchestrator(MultipassVmProvider):
    """A Multipass provider that adds Ansible provisioning and repo sync to VMs."""

    def __init__(
        self,
        repo_root: Path,
        shell: ShellBackend | None = None,
        ansible: AnsibleAdapter | None = None,
        multipass_client: MultipassClient | None = None,
    ) -> None:
        """Set up the provider and the Ansible adapter used for provisioning."""
        self.repo_root = Path(repo_root)
        super().__init__(
            workspace_root=self.repo_root,
            # MultipassVmProvider's ShellBackend argument, not the subprocess
            # shell flag bandit's B604 looks for.
            shell=shell,  # nosec B604
            multipass_client=multipass_client,
        )
        self._owns_ansible = ansible is None
        if ansible is None:
            from nanolab.tasks.infra.ansible import AnsibleAdapter

            ansible = AnsibleAdapter(
                self.repo_root,
                # AnsibleAdapter's ShellBackend argument, not the subprocess
                # shell flag bandit's B604 looks for.
                shell=self.shell,  # nosec B604
                host_resolver=self.connection_host,
            )
        self.ansible = ansible

    def ensure_running(
        self, request: VmRequest, *, dry_run: bool = False
    ) -> ShellExecutionResult:
        """Ensure the VM is up, refreshing the Ansible key to the one just installed."""
        result = super().ensure_running(request, dry_run=dry_run)
        if self._owns_ansible and not dry_run:
            self.ansible.private_key_path = self._ssh_credentials()[1]
        return result

    def vm_exists(self, request: VmRequest) -> bool:
        """Check Multipass before ensure; a deleted VM must not trigger purge."""
        if not request.name:
            raise ValueError("managed Multipass VM requires a name")
        try:
            info = self._client.get_vm(request.name).info()
        except VmNotFoundError:
            return False
        if info.state == VmState.DELETED:
            raise RuntimeError(
                f"Multipass VM {request.name} is deleted; refusing global purge"
            )
        return True

    def remote_project_dir(self, request: VmRequest) -> str:
        """Return the in-VM directory the repository is synced into."""
        return f"{self._remote_home(request)}/nanofaas"

    def kubeconfig_path(self, request: VmRequest) -> str:
        """Return the path of the k3s kubeconfig inside the VM."""
        return f"{self._remote_home(request)}/.kube/config"

    def remote_path_for_local(
        self,
        request: VmRequest,
        local_path: Path,
        *,
        local_root: Path | None = None,
        fallback_subdir: str | None = None,
    ) -> str:
        """Map `local_path` onto where it lands inside the synced project dir."""
        path = Path(local_path).resolve()
        root = Path(local_root or self.repo_root).resolve()
        remote_dir = self.remote_project_dir(request)

        try:
            relative = path.relative_to(root)
            return f"{remote_dir}/{relative.as_posix()}"
        except ValueError:
            if fallback_subdir:
                fallback = fallback_subdir.strip("/")
                return f"{remote_dir}/{fallback}/{path.name}"
            return f"{remote_dir}/{path.name}"

    def sync_project(
        self,
        request: VmRequest,
        *,
        source_dir: Path | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Rsync the source tree into the VM, over SSH for external VMs."""
        source = Path(source_dir or self.repo_root)
        destination = remote_dir or self.remote_project_dir(request)

        if request.lifecycle == "external":
            return self._shell_run(
                repo_rsync_command(
                    source=source,
                    user=request.user,
                    host=str(request.host),
                    destination=destination,
                ),
                dry_run=dry_run,
            )

        host = self.connection_host(request, dry_run=dry_run)
        private_key = None if dry_run else self._ssh_credentials()[1]
        return self._shell_run(
            repo_rsync_command(
                source=source,
                user=request.user,
                host=host,
                destination=destination,
                ssh_rsh=repo_sync_ssh_rsh(private_key),
            ),
            dry_run=dry_run,
        )

    def install_dependencies(
        self,
        request: VmRequest,
        *,
        install_helm: bool = False,
        helm_version: str = "3.16.4",
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Install base packages, and Helm when `install_helm` is set, on the VM."""
        return self.ansible.provision_base(
            request,
            install_helm=install_helm,
            helm_version=helm_version,
            dry_run=dry_run,
        )

    def install_k3s(
        self,
        request: VmRequest,
        *,
        kubeconfig_path: str | None = None,
        k3s_version: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Install k3s on the VM, writing its kubeconfig to the VM-side path."""
        return self.ansible.provision_k3s(
            request,
            kubeconfig_path=kubeconfig_path or self.kubeconfig_path(request),
            k3s_version=k3s_version,
            dry_run=dry_run,
        )

    def setup_registry(
        self,
        request: VmRequest,
        *,
        registry: str = LOCAL_REGISTRY,
        container_name: str = REGISTRY_CONTAINER_NAME,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Start the registry container, then point k3s at it.

        Returns the container failure unchanged when the registry could not be
        started, so k3s is never configured against a registry that is not there.
        """
        ensure_result = self.ensure_registry_container(
            request,
            registry=registry,
            container_name=container_name,
            dry_run=dry_run,
        )
        if ensure_result.return_code != 0:
            return ensure_result
        return self.configure_k3s_registry(
            request,
            registry=registry,
            dry_run=dry_run,
        )

    def ensure_registry_container(
        self,
        request: VmRequest,
        *,
        registry: str = LOCAL_REGISTRY,
        container_name: str = REGISTRY_CONTAINER_NAME,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run the playbook that starts the local registry container on the VM."""
        return self.ansible.ensure_registry_container(
            request,
            registry=registry,
            container_name=container_name,
            dry_run=dry_run,
        )

    def configure_k3s_registry(
        self,
        request: VmRequest,
        *,
        registry: str = LOCAL_REGISTRY,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run the playbook that configures k3s to pull from the in-VM registry."""
        return self.ansible.configure_k3s_registry(
            request,
            registry=registry,
            dry_run=dry_run,
        )

    def export_kubeconfig(
        self,
        request: VmRequest,
        *,
        destination: Path,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Copy the VM's kubeconfig to `destination` on the host."""
        kubeconfig_path = self.kubeconfig_path(request)
        if request.lifecycle == "external":
            return self._shell_run(
                [
                    "scp",
                    f"{request.user}@{request.host}:{kubeconfig_path}",
                    str(destination),
                ],
                dry_run=dry_run,
            )

        name = self._vm_name(request)
        transfer_cmd = [
            "multipass",
            "transfer",
            f"{name}:{kubeconfig_path}",
            str(destination),
        ]
        if dry_run:
            return successful_result(transfer_cmd)

        try:
            self._client.get_vm(name).transfer(
                f"{name}:{kubeconfig_path}", str(destination)
            )
        except MultipassCommandError as e:
            return ShellExecutionResult(
                command=e.args_list,
                return_code=e.returncode,
                stdout=e.stdout,
                stderr=e.stderr,
            )
        return successful_result(transfer_cmd)
