from __future__ import annotations

import workflow_tasks


def test_public_api_exports_task_types() -> None:
    assert hasattr(workflow_tasks, "CommandTaskSpec")
    assert hasattr(workflow_tasks, "TaskResult")
    assert hasattr(workflow_tasks, "TaskStatus")
    assert hasattr(workflow_tasks, "ExecutionTarget")


def test_public_api_exports_executors() -> None:
    assert hasattr(workflow_tasks, "HostCommandTaskExecutor")
    assert hasattr(workflow_tasks, "VmCommandTaskExecutor")


def test_public_api_no_longer_exports_the_operation_bridge() -> None:
    # The operation→spec adapters moved into nanolab's provisioning path.
    assert not hasattr(workflow_tasks, "CommandTask")
    assert not hasattr(workflow_tasks, "command_task_from_operation")
    assert not hasattr(workflow_tasks, "operation_to_task_spec")


def test_version_is_set() -> None:
    assert workflow_tasks.__version__ == "0.1.0"


def test_public_api_exports_task_and_workflow() -> None:
    assert hasattr(workflow_tasks, "Task")
    assert hasattr(workflow_tasks, "Workflow")
    assert not hasattr(workflow_tasks, "ResourceTask")


def test_public_api_no_longer_exports_the_moved_clusters() -> None:
    # The event bus, components, VM providers, load-test machinery, shell, and
    # ansible all moved to sonata_tasks; workflow_tasks re-exports only the
    # core engine and the task types.
    for name in (
        "WorkflowEvent", "WorkflowContext", "WorkflowSink",
        "phase", "step", "success", "fail", "workflow_step",
        "VmConfig", "VmInfo", "VmRequest", "VmLifecycle",
        "EnsureVmRunning", "DestroyVm",
        "MultipassVmProvider", "AzureVmProvider", "ProxmoxVmProvider",
        "OrchestratorVmRunner", "VmFileFetcher",
        "VmLifecycleAdapter", "MultipassVmAdapter", "AzureVmAdapter", "ProxmoxVmAdapter",
        "RunPlaybook", "install_k6_task",
        "K6Config", "RunK6", "HttpPrometheusClient",
        "SubprocessShell", "ShellBackend",
    ):
        assert not hasattr(workflow_tasks, name), name
