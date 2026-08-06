"""workflow-tasks — the legacy workflow engine.

The task primitives, components, VM providers, load-test machinery, and the
event bus moved to `sonata_tasks`; this package keeps the legacy `core`
engine (`Task`/`Workflow`) for the remaining legacy execution paths, plus
the task-type re-exports for API stability.
"""
from __future__ import annotations

__version__ = "0.1.0"

from workflow_tasks.core.task import Task
from workflow_tasks.core.workflow import Workflow
from sonata_tasks.tasks.executors import HostCommandTaskExecutor, VmCommandTaskExecutor
from sonata_tasks.tasks.models import CommandTaskSpec, ExecutionTarget, TaskResult, TaskStatus

__all__ = [
    "__version__",
    # core
    "Task", "Workflow",
    # tasks
    "CommandTaskSpec", "ExecutionTarget", "TaskResult", "TaskStatus",
    "HostCommandTaskExecutor", "VmCommandTaskExecutor",
]
