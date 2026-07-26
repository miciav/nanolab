"""nanoFaaS workflow tasks built on the Sonata engine."""

from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.process import managed_process_resource
from sonata_tasks.vm import vm_resource

__all__ = [
    "CliFunction",
    "CliWorkflowRequest",
    "CommandTask",
    "build_cli_workflow",
    "managed_process_resource",
    "vm_resource",
]
