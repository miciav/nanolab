"""Provider selection: request lifecycle -> concrete managed VM provider."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


def provider_for(request: VmRequest, repo_root: Path) -> object:
    """Return the managed VM provider for a request's lifecycle.

    `external` has no managed provider: the caller already owns the VM.
    """
    if request.lifecycle == "multipass":
        return VmOrchestrator(repo_root)
    if request.lifecycle == "azure":
        return AzureVmProvider(repo_root)
    if request.lifecycle == "proxmox":
        return ProxmoxVmProvider(repo_root)
    raise ValueError(f"{request.lifecycle} does not use a managed VM provider")
