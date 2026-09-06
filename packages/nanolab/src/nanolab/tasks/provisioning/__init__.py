"""Self-contained provisioning: provider selection, VM resources, bootstrap."""

from nanolab.tasks.provisioning.bootstrap import (
    remote_operations,
    retarget_cloud_operations,
    run_bootstrap_operations,
    scenario_context,
)
from nanolab.tasks.provisioning.environment import ProvisionedRole, provision_roles
from nanolab.tasks.provisioning.providers import command_provider_for, provider_for
from nanolab.tasks.provisioning.resources import VerifiedLifecycle, provisioned_vm

__all__ = [
    "command_provider_for", "provider_for",
    "VerifiedLifecycle", "provisioned_vm",
    "scenario_context", "remote_operations",
    "retarget_cloud_operations", "run_bootstrap_operations",
    "ProvisionedRole", "provision_roles",
]
