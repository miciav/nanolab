"""Provider selection: request lifecycle -> concrete managed VM provider."""

from __future__ import annotations

from pathlib import Path

from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.ports import VmCommandProvider
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


def provider_for(request: VmRequest, repo_root: Path) -> VmCommandProvider:
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


def command_provider_for(request: VmRequest, repo_root: Path) -> VmCommandProvider:
    """Return the provider that *runs commands* on a request's VM.

    Distinct from `provider_for`, which answers who creates and destroys the
    VM. An `external` VM has no owner to create it, but commands still have to
    reach it, and the orchestrator's exec path is what reaches a VM it did not
    create. Callers that only run commands want this one; callers that manage a
    lifecycle want `provider_for` and its refusal.
    """
    if request.lifecycle == "external":
        return VmOrchestrator(repo_root)
    return provider_for(request, repo_root)
