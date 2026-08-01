from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from nanolab.config import EnvironmentConfig
from nanolab.release.environment import validate_release_environment
from nanolab.release.versioning import read_project_version


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
# The validator compares this against the checkout's own version, so reading it
# keeps the test from failing on every release rather than on a real defect.
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)
NANOLAB_ROOT = Path(__file__).resolve().parents[2]


def _release_environment(**changes: object) -> EnvironmentConfig:
    data: dict[str, object] = {
        "provider": "azure",
        "roles": {
            "stack": {"name": "nanofaas-azure-release", "disk": "128G"},
            "loadgen": {
                "name": "nanofaas-azure-release-loadgen",
                "disk": "30G",
            },
            "arm-builder": {
                "name": "nanofaas-azure-release-arm",
                "disk": "64G",
            },
        },
        "azure": {
            "resource_group": "nanofaas-rg",
            "location": "westeurope",
            "vm_size": "Standard_D8s_v5",
            "loadgen_vm_size": "Standard_D2s_v5",
            "arm_vm_size": "Standard_D8ps_v5",
            "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
            "arm_image_urn": "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140",
            "operator_source_cidr": "8.8.8.8/32",
        },
    }
    data.update(changes)
    return EnvironmentConfig.model_validate(data)


def test_release_environment_example_requires_real_operator_cidr() -> None:
    path = NANOLAB_ROOT / "environments/azure-release.yaml.example"
    environment = EnvironmentConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    with pytest.raises(ValueError, match="operator source CIDR"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)
    assert environment.roles["stack"].disk == "128G"
    assert environment.roles["loadgen"].disk == "30G"
    assert environment.roles["arm-builder"].disk == "64G"
    assert environment.azure is not None
    assert environment.azure.operator_source_cidr == "203.0.113.0/24"


@pytest.mark.parametrize("provider", ("local", "multipass", "proxmox"))
def test_release_environment_rejects_non_azure_provider(provider: str) -> None:
    changes: dict[str, object] = {"provider": provider, "azure": None}
    if provider == "proxmox":
        changes["proxmox"] = {"host": "pve.example.test", "node": "pve"}
    environment = _release_environment(**changes)

    with pytest.raises(ValueError, match="Azure"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


@pytest.mark.parametrize(
    "roles",
    (
        {"stack": {"disk": "128G"}},
        {"loadgen": {"disk": "30G"}},
        {"stack": {"disk": "128G"}, "loadgen": {"disk": "30G"}},
    ),
)
def test_release_environment_requires_stack_and_loadgen_roles(roles: dict[str, object]) -> None:
    environment = _release_environment(roles=roles)

    with pytest.raises(ValueError, match="stack, loadgen and arm-builder"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


@pytest.mark.parametrize(
    ("role", "name"),
    (
        ("stack", "shared-stack"),
        ("loadgen", "shared-loadgen"),
        ("arm-builder", "shared-arm"),
    ),
)
def test_release_environment_requires_dedicated_vm_names(role: str, name: str) -> None:
    environment = _release_environment()
    environment.roles[role].name = name  # type: ignore[index]

    with pytest.raises(ValueError, match="dedicated release VM"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


@pytest.mark.parametrize(
    "source",
    (None, "*", "0.0.0.0/0", "::/0", "not-a-cidr", "203.0.113.0/24"),
)
def test_release_environment_requires_restricted_operator_source(
    source: str | None,
) -> None:
    environment = _release_environment()
    assert environment.azure is not None
    environment.azure.operator_source_cidr = source

    with pytest.raises(ValueError, match="operator source CIDR"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


@pytest.mark.parametrize(
    "urn",
    (
        "Canonical:ubuntu-24_04-lts:server:latest",
        "Canonical:ubuntu-24_04-lts:server:",
        "Canonical :ubuntu-24_04-lts:server:24.04.202505280",
        "Canonical:ubuntu 24:server:24.04.202505280",
        "Canonical:ubuntu-24_04-lts:ser\nver:24.04.202505280",
        "Canonical:ubuntu-24_04-lts:server:24.04.202505280 ",
    ),
)
def test_release_environment_rejects_unpinned_or_malformed_urn(urn: str) -> None:
    environment = _release_environment(
        azure={
            "resource_group": "nanofaas-rg",
            "location": "westeurope",
            "vm_size": "Standard_D8s_v5",
            "loadgen_vm_size": "Standard_D2s_v5",
            "arm_vm_size": "Standard_D8ps_v5",
            "image_urn": urn,
            "arm_image_urn": "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140",
        }
    )

    with pytest.raises(ValueError, match="image URN"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


@pytest.mark.parametrize("field", ("vm_size", "loadgen_vm_size", "arm_vm_size"))
def test_release_environment_rejects_burstable_vm_size(field: str) -> None:
    azure = {
        "resource_group": "nanofaas-rg",
        "location": "westeurope",
        "vm_size": "Standard_D8s_v5",
        "loadgen_vm_size": "Standard_D2s_v5",
        "arm_vm_size": "Standard_D8ps_v5",
        "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202505280",
        "arm_image_urn": "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202505280",
    }
    azure[field] = "Standard_B2s"
    environment = _release_environment(azure=azure)

    with pytest.raises(ValueError, match="burstable"):
        validate_release_environment(environment, NANOFAAS_ROOT, CURRENT_VERSION)


def test_release_environment_rejects_unprepared_project_version() -> None:
    with pytest.raises(ValueError, match="prepared project version"):
        validate_release_environment(_release_environment(), NANOFAAS_ROOT, "0.18.0")
