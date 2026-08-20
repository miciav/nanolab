"""The comparison environment must provision two VMs, not three.

`provisioning.py` creates the arm-builder VM whenever that role is declared —
it does not ask what the workflow is — so reusing the release environment would
launch a Standard_D8ps_v5 the comparison has no use for and wait for it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.config.environment import EnvironmentConfig

# The .example, not the working copy: `.gitignore` excludes
# `environments/azure*.yaml` because a real one carries a resource group, a key
# path and the operator's own address. A test that read the working copy would
# pass here and fail on every clean checkout.
ENVIRONMENT = Path("packages/nanolab/environments/azure-comparison.yaml.example")


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        yaml.safe_load(ENVIRONMENT.read_text(encoding="utf-8"))
    )


def test_it_declares_a_load_generator_and_no_arm_builder() -> None:
    """The loadgen role is the whole mechanism: `dedicated = "loadgen" in roles`."""
    roles = set(_environment().roles)

    assert roles == {"stack", "loadgen"}


def test_the_node_ports_are_not_opened_at_creation() -> None:
    """Declaring an operator CIDR is what keeps them off 0.0.0.0/0.

    `vm_provider` opens the ports at creation only when no CIDR is declared;
    with one, `provisioning` opens them afterwards bounded to that CIDR plus the
    load generator. A control plane with no authentication must never be briefly
    reachable from the whole internet.
    """
    environment = _environment()
    request = vm_request_for_role(environment, "stack", loadtest=True)
    azure = environment.azure

    assert request.azure_open_ports is None
    assert azure is not None
    assert azure.operator_source_cidr, "the example must ship a CIDR to be replaced, not none"


def test_the_stack_is_sized_for_a_native_build() -> None:
    """The G1 build was OOM-killed on 12GB and unfinished after 94 minutes bounded."""
    azure = _environment().azure

    assert azure is not None
    assert azure.vm_size == "Standard_D8s_v5"
