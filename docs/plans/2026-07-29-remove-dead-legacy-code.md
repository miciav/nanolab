# Remove Dead Legacy Code Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove legacy workflow code that no production path can execute while preserving active transitional infrastructure.

**Architecture:** Keep Sonata as the only scenario workflow engine. Move the two reusable values out of dead builder modules, delete obsolete implementations and tests, then collapse constant routing branches.

**Tech Stack:** Python 3.12, pytest, Sonata, Ruff, basedpyright, import-linter.

---

### Task 1: Lock the package boundary

**Files:**
- Modify: `packages/nanolab/tests/test_import_contracts.py`

1. Add a test asserting production code does not import `workflow_tasks.workflows`
   or `workflow_tasks.components.container`.
2. Run the test and confirm it fails on current imports.
3. Leave the failing test in place for the following tasks.

### Task 2: Remove the old workflow builders

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/validate.py`
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py`
- Delete: `packages/workflow-tasks/src/workflow_tasks/workflows/*.py`
- Delete: `packages/workflow-tasks/tests/workflows/*.py`

1. Define the resolved-function record beside `_resolve_function`.
2. Define the default Prometheus queries beside the load-test plan.
3. Redirect the two remaining imports.
4. Delete the old workflow builders and their dedicated tests.
5. Run focused nanolab plan tests and the workflow-tasks suite.

### Task 3: Remove standalone dead compatibility code

**Files:**
- Delete: `packages/workflow-tasks/src/workflow_tasks/components/container.py`
- Delete: `packages/workflow-tasks/tests/components/test_container.py`
- Modify: `packages/workflow-tasks/src/workflow_tasks/loadtest/tasks.py`
- Modify: `packages/workflow-tasks/src/workflow_tasks/loadtest/__init__.py`
- Modify: `packages/workflow-tasks/src/workflow_tasks/__init__.py`
- Modify: `packages/workflow-tasks/tests/loadtest/test_loadtest_tasks.py`
- Modify: `packages/workflow-tasks/tests/test_public_api.py`
- Modify: `packages/nanolab/src/nanolab/functions/catalog.py`

1. Remove `InstallK6`, its exports, and tests.
2. Remove the unused scenario runtime allowlist and accessor.
3. Remove the unused legacy container resource and test.
4. Run focused workflow-tasks and nanolab tests.

### Task 4: Collapse dead engine routing

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/tui/app.py`
- Modify: affected CLI and TUI tests.

1. Remove `uses_sonata`, `_render`, `_slice`, and their unreachable branches.
2. Render and run scenario workflows directly through Sonata.
3. Update stale test fixtures and comments.
4. Run CLI and TUI tests.

### Task 5: Verify the repository

1. Run all four package test suites.
2. Run Ruff, basedpyright, and all import-linter contracts.
3. Run `git diff --check`.
4. Generate representative plans and build all wheels.
5. Inspect the final diff and confirm active legacy infrastructure was preserved.

