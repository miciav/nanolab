from __future__ import annotations

import os

from sonata_tasks.deployment import CONTROL_PLANE_NODE_PORT, PROMETHEUS_NODE_PORT
from sonata_tasks.vm.models import VmRequest

from nanolab.config.environment import EnvironmentConfig, ExecutionRole


_DEFAULT_NAMES = {
    ("azure", "stack"): "nanofaas-azure",
    ("azure", "loadgen"): "nanofaas-azure-loadgen",
    ("azure", "arm-builder"): "nanofaas-azure-arm",
    ("proxmox", "stack"): "nanofaas-proxmox",
    ("proxmox", "loadgen"): "nanofaas-proxmox-loadgen",
}


def vm_request_for_role(
    environment: EnvironmentConfig,
    role: ExecutionRole,
    *,
    loadtest: bool = False,
) -> VmRequest:
    provider = environment.provider
    if provider == "local":
        raise ValueError("local environments do not have a VM request")

    target = environment.target(role)
    user = target.user
    if provider == "azure" and "user" not in target.model_fields_set:
        user = "azureuser"

    common = {
        "lifecycle": provider,
        "name": target.name or _DEFAULT_NAMES.get((provider, role)),
        "host": target.host,
        "user": user,
        "home": target.home,
        "cpus": target.cpus,
        "memory": target.memory,
        "disk": target.disk,
        "hpa_scale_to_zero": target.hpa_scale_to_zero,
    }
    if provider == "azure":
        azure = environment.azure
        assert azure is not None
        if role == "loadgen":
            vm_size = azure.loadgen_vm_size
        elif role == "arm-builder":
            vm_size = azure.arm_vm_size
        else:
            vm_size = azure.vm_size
        return VmRequest(
            **common,
            azure_vm_size=vm_size,
            azure_resource_group=azure.resource_group,
            azure_location=azure.location,
            azure_image_urn=(
                azure.arm_image_urn if role == "arm-builder" else azure.image_urn
            ),
            azure_ssh_key_path=azure.ssh_key_path,
            # An environment that declares an operator CIDR gets its NodePort
            # ingress from secure_release_endpoints, bounded to that CIDR plus the
            # load generator. Opening the ports at VM creation would publish them
            # to 0.0.0.0/0 first, so it must not happen here.
            azure_open_ports=(
                (CONTROL_PLANE_NODE_PORT, 30081, PROMETHEUS_NODE_PORT)
                if loadtest and role == "stack" and azure.operator_source_cidr is None
                else None
            ),
        )
    if provider == "proxmox":
        proxmox = environment.proxmox
        assert proxmox is not None
        return VmRequest(
            **common,
            proxmox_host=proxmox.host,
            proxmox_node=proxmox.node,
            proxmox_user=proxmox.user,
            proxmox_password=os.getenv(proxmox.password_env),
            proxmox_template_id=proxmox.template_id,
            proxmox_ssh_key_path=proxmox.ssh_key_path,
        )
    return VmRequest(**common)
