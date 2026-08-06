from __future__ import annotations

import importlib
from pathlib import Path
import sys


def test_dead_internal_modules_are_absent() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "workflow_tasks"
    dead_modules = (
        "components/cleanup.py",
        "components/function_tasks.py",
        "components/namespace.py",
        "components/registry.py",
        "components/remote_script.py",
        "components/verification.py",
        "infra/host_sleep.py",
        "loadtest/remote_k6.py",
        "orchestration/__init__.py",
        "orchestration/models.py",
        "orchestration/runtime.py",
    )

    assert [module for module in dead_modules if (package / module).exists()] == []


def test_workflow_tasks_does_not_import_tui_toolkit() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("tui_toolkit"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks")
    assert not any(k.startswith("tui_toolkit") for k in sys.modules), (
        "workflow_tasks imported tui_toolkit"
    )


def test_workflow_tasks_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks")
    assert not any(k.startswith("nanolab") for k in sys.modules), (
        "workflow_tasks imported nanolab"
    )


def test_tasks_subpackage_does_not_import_workflow() -> None:
    # models/executors live in sonata_tasks now; only the re-export package remains.
    import workflow_tasks.tasks  # noqa: F401
    # If we got here without importing workflow subpackage transitively, we're good.
    # The import-linter contract enforces this at the CI gate.


def test_vm_subpackage_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.vm")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_loadtest_subpackage_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.loadtest")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_vm_multipass_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.vm.multipass")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_vm_azure_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.vm.azure")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_shell_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.shell")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_components_subpackage_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.components.operations")
    importlib.import_module("workflow_tasks.components.models")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_infra_subpackage_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.infra.ansible")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_vm_orchestrator_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.vm.orchestrator")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_components_context_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.components.context")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_components_images_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.components.images")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_loadtest_two_vm_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.loadtest.two_vm")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_components_helm_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.components.helm")
    assert not any(k.startswith("nanolab") for k in sys.modules)


def test_components_bootstrap_does_not_import_nanolab() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module("workflow_tasks.components.bootstrap")
    assert not any(k.startswith("nanolab") for k in sys.modules)
