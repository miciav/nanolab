from __future__ import annotations

import importlib
import sys


def _fresh_import(module: str) -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("nanolab"):
            del sys.modules[key]
    importlib.import_module(module)
    assert not any(k.startswith("nanolab") for k in sys.modules), (
        f"{module} imported nanolab"
    )


def test_vm_subpackage_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.vm")


def test_loadtest_subpackage_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.loadtest")


def test_vm_multipass_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.vm.multipass")


def test_vm_azure_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.vm.azure")


def test_shell_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.shell")


def test_components_subpackage_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.components.operations")


def test_infra_subpackage_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.infra.ansible")


def test_vm_orchestrator_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.vm.orchestrator")


def test_components_context_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.components.context")


def test_components_images_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.components.images")


def test_loadtest_two_vm_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.loadtest.two_vm")


def test_components_helm_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.components.helm")


def test_components_bootstrap_does_not_import_nanolab() -> None:
    _fresh_import("sonata_tasks.components.bootstrap")
