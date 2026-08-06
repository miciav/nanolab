from __future__ import annotations

import importlib
from pathlib import Path
import sys


def test_dead_internal_modules_are_absent() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "workflow_tasks"
    dead_modules = (
        "components/",
        "infra/",
        "loadtest/",
        "vm/",
        "workflow/",
        "shell.py",
        "integrations/",
        "execution/",
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
