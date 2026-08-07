"""Self-contained provisioning: provider selection, VM resources, bootstrap."""

from sonata_tasks.provisioning.providers import provider_for

__all__ = ["provider_for"]
