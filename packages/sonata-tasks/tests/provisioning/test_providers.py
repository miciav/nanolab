from __future__ import annotations

from pathlib import Path

import pytest

from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.vm.azure import AzureVmProvider
from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.orchestrator import VmOrchestrator
from sonata_tasks.vm.proxmox import ProxmoxVmProvider


def test_provider_for_multipass_returns_orchestrator(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="multipass"), tmp_path)
    assert isinstance(provider, VmOrchestrator)


def test_provider_for_azure_returns_azure_provider(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="azure"), tmp_path)
    assert isinstance(provider, AzureVmProvider)


def test_provider_for_proxmox_returns_proxmox_provider(tmp_path: Path) -> None:
    provider = provider_for(VmRequest(lifecycle="proxmox"), tmp_path)
    assert isinstance(provider, ProxmoxVmProvider)


def test_provider_for_external_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="external"):
        provider_for(VmRequest(lifecycle="external"), tmp_path)
