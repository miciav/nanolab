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


def test_public_api_exports_workflow_types() -> None:
    assert hasattr(workflow_tasks, "WorkflowEvent")
    assert hasattr(workflow_tasks, "WorkflowContext")
    assert hasattr(workflow_tasks, "WorkflowSink")


def test_public_api_exports_reporting_helpers() -> None:
    assert hasattr(workflow_tasks, "phase")
    assert hasattr(workflow_tasks, "step")
    assert hasattr(workflow_tasks, "success")
    assert hasattr(workflow_tasks, "fail")
    assert hasattr(workflow_tasks, "workflow_step")


def test_version_is_set() -> None:
    assert workflow_tasks.__version__ == "0.1.0"


def test_public_api_exports_task_and_workflow() -> None:
    assert hasattr(workflow_tasks, "Task")
    assert hasattr(workflow_tasks, "Workflow")
    assert not hasattr(workflow_tasks, "ResourceTask")


def test_public_api_exports_vm_tasks() -> None:
    assert hasattr(workflow_tasks, "VmConfig")
    assert hasattr(workflow_tasks, "VmInfo")
    assert hasattr(workflow_tasks, "VmLifecycle")
    assert hasattr(workflow_tasks, "EnsureVmRunning")
    assert hasattr(workflow_tasks, "DestroyVm")


def test_public_api_exports_loadtest_tasks() -> None:
    # The k6/autoscaling/prometheus/offload-conservation cluster moved to
    # sonata_tasks.loadtest; the legacy package re-exports nothing anymore.
    assert not hasattr(workflow_tasks, "K6Config")
    assert not hasattr(workflow_tasks, "RunK6")
    assert not hasattr(workflow_tasks, "HttpPrometheusClient")
    assert hasattr(workflow_tasks, "RunPlaybook")
    assert hasattr(workflow_tasks, "install_k6_task")


def test_public_api_exports_vm_infrastructure() -> None:
    assert hasattr(workflow_tasks, "VmRequest")
    assert hasattr(workflow_tasks, "MultipassVmProvider")
    assert hasattr(workflow_tasks, "AzureVmProvider")
    assert hasattr(workflow_tasks, "OrchestratorVmRunner")
    assert hasattr(workflow_tasks, "VmFileFetcher")
    assert hasattr(workflow_tasks, "VmLifecycleAdapter")
    assert hasattr(workflow_tasks, "MultipassVmAdapter")
    assert hasattr(workflow_tasks, "AzureVmAdapter")
    assert not hasattr(workflow_tasks, "HttpPrometheusClient")


def test_public_api_exports_proxmox_provider() -> None:
    assert hasattr(workflow_tasks, "ProxmoxVmProvider")
    assert hasattr(workflow_tasks, "ProxmoxVmAdapter")
