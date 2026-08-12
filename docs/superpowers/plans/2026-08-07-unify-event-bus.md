# Unify the Event Bus onto sonata-engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete `sonata_tasks/workflow/` (the duplicated event bus) and point every consumer at the native `sonata_engine.workflow` bus — one sink, one event vocabulary, ~600 lines of duplication removed.

**Architecture:** The migration left two twin event buses. `sonata_engine.workflow` (the pinned external dependency) has the same `context`/`events`/`models`/`event_builders` modules with identical types (verified: `events.py` and `models.py` byte-identical, `event_builders.py` behavior-identical, `context.py` same API) and a smaller `reporting` API: `workflow_log` (same signature), `status`, and `subtask` — the engine's equivalent of the legacy `workflow_step` (same started→passed/failed lifecycle, same task_id/title args; the legacy `parent_task_id`/`detail` params are unused by every caller). The legacy-only `phase`/`step`/`success`/`warning`/`skip`/`fail` one-shot emitters have zero consumers outside the module. The TUI is already bilingual: its event aggregator maps both `task.running`/`task.completed` (legacy) and `task.started`/`task.passed` (engine) vocabularies — so translating consumers changes only which vocabulary is emitted, not what the TUI renders.

**Tech Stack:** Python 3.12, sonata-engine (pinned git dep), pytest (90% coverage gate), import-linter, uv workspace.

## Global Constraints

- `sonata_tasks` must not import `nanolab` or `tui_toolkit` (contract in `packages/sonata-tasks/.importlinter`).
- Coverage gate is 90% per package (`--cov-fail-under=90` in sonata-tasks pyproject).
- Run tests per package: `pytest -c packages/<pkg>/pyproject.toml packages/<pkg>/tests`.
- The pinned sonata-engine revision (`818d53e` in root pyproject `[tool.uv.sources]`) is fixed — no pin bump, no changes to the external repo. All mapping happens on the consumer side.
- Behavior must be preserved: provisioning tests (`packages/nanolab/tests/test_provisioning.py`, 14 tests) and the shell sink test (`packages/sonata-tasks/tests/test_shell.py`) keep passing unchanged in behavior (import re-points allowed; assertion changes NOT allowed).
- The TUI event-aggregator tests pin the bilingual vocabulary mapping — they keep passing unchanged.

---

### Task 1: Translate sonata_tasks consumers onto the engine bus

The three `sonata_tasks` modules that import `sonata_tasks.workflow` re-point to `sonata_engine.workflow`, with `workflow_step` → `subtask` at the three call sites.

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/shell.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/provisioning/bootstrap.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/provisioning/environment.py`
- Modify: `packages/sonata-tasks/tests/test_shell.py` (import re-point only)

**Interfaces:**
- Consumes: `sonata_engine.workflow.context.has_workflow_sink`, `sonata_engine.workflow.reporting.workflow_log`, `sonata_engine.workflow.reporting.subtask` — all exist at the pinned rev (verified in site-packages).
- Produces: no new public API — same functions, new import homes.

- [ ] **Step 1: Re-point shell.py**

In `packages/sonata-tasks/src/sonata_tasks/shell.py`, replace:

```python
from sonata_tasks.workflow.context import has_workflow_sink
from sonata_tasks.workflow.reporting import workflow_log
```

with:

```python
from sonata_engine.workflow.context import has_workflow_sink
from sonata_engine.workflow.reporting import workflow_log
```

The docstring line "Routes each output line to workflow_log when a workflow sink is active" stays valid.

- [ ] **Step 2: Re-point bootstrap.py and environment.py**

In `packages/sonata-tasks/src/sonata_tasks/provisioning/bootstrap.py`:
- Replace `from sonata_tasks.workflow.reporting import workflow_step` with `from sonata_engine.workflow.reporting import subtask`.
- At the call site (currently `with workflow_step(task_id=task.task_id, title=task.title):`): replace `workflow_step(` with `subtask(`. The arguments are exactly `task_id=`/`title=` — the engine's `subtask(*, task_id, title="")` accepts them.

In `packages/sonata-tasks/src/sonata_tasks/provisioning/environment.py`: same two changes at BOTH call sites (the ensure wrapper and the destroy wrapper — currently `with workflow_step(task_id=task.task_id, title=task.title):`).

Verify no `parent_task_id=`/`detail=` kwargs are passed anywhere (grep `workflow_step(` in the package — expected: only the three `task_id=`/`title=` sites).

- [ ] **Step 3: Re-point test_shell.py**

In `packages/sonata-tasks/tests/test_shell.py`, replace the imports `from sonata_tasks.workflow.context import bind_workflow_sink` and `from sonata_tasks.workflow.events import WorkflowEvent` with the `sonata_engine.workflow.*` equivalents. The test binds a sink and asserts workflow_log output — behavior identical.

- [ ] **Step 4: Run the sonata suite**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q`
Expected: PASS, coverage ≥ 90% (shell/provisioning modules still covered by their tests).

- [ ] **Step 5: Commit**

```bash
git add packages/sonata-tasks
git commit -m "refactor: sonata_tasks reports through the engine event bus"
```

---

### Task 2: Re-point nanolab consumers onto the engine bus

The TUI, progress, and product modules re-point their `sonata_tasks.workflow.*` imports to `sonata_engine.workflow.*`; nanolab declares sonata-engine as a direct dependency (it currently relies on it transitively via sonata-tasks).

**Files:**
- Modify: `packages/nanolab/src/nanolab/tui/event_aggregator.py`
- Modify: `packages/nanolab/src/nanolab/tui/models.py`
- Modify: `packages/nanolab/src/nanolab/tui/workflow_controller.py`
- Modify: `packages/nanolab/src/nanolab/tui/workflow.py`
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/cli/progress.py`
- Modify: `packages/nanolab/pyproject.toml` (add `"sonata-engine"` to dependencies)
- Modify: nanolab tests (conftest.py, test_tui_app.py, test_fake_sink_fixture.py, test_workflow_events.py, test_workflow_sink_location.py, test_tui_workflow.py, test_tui_event_aggregator.py, test_tui_workflow_controller.py, cli/test_progress.py — all re-points of the same import lines)

**Interfaces:**
- Consumes: `sonata_engine.workflow.{context,events,models,event_builders}` — same module layout, same names (`bind_workflow_sink`, `WorkflowEvent`, `WorkflowState`, `build_task_event`, `build_log_event`, `build_phase_event`). The engine's `event_builders.build_task_event` has the same signature (its `resolved_task_id` logic is a behavior-identical refactor — verify one call site after re-pointing).

- [ ] **Step 1: Re-point the six src files**

For each of `tui/event_aggregator.py`, `tui/models.py`, `tui/workflow_controller.py`, `tui/workflow.py`, `cli/product.py`, `cli/progress.py`: replace every `from sonata_tasks.workflow.` import with the `sonata_engine.workflow.` equivalent (mechanical sed: `s/from sonata_tasks\.workflow\./from sonata_engine.workflow./`).

Then handle the two special cases:
- `tui/workflow_controller.py` currently has `from sonata_engine.workflow.context import bind_workflow_sink as bind_sonata_sink` (line ~13) AND `from sonata_tasks.workflow.context import bind_workflow_sink` (line ~15). After the sed both names import the same function — delete the `bind_sonata_sink` alias and the second `with bind_workflow_sink(sink), bind_sonata_sink(sink):` binding at its call site (now just `with bind_workflow_sink(sink):`).
- `cli/product.py` has the same pair (`bind_sonata_sink` alias + double binding at line ~523) — same cleanup: single `with bind_workflow_sink(sink):`.
- `tui/app.py` (imports `step, workflow_log` from `sonata_tasks.workflow.reporting`) — check whether `step`/`workflow_log` are actually called in the file; if the imports are unused, delete them; if used, re-point to `sonata_engine.workflow.reporting` and translate: `step(label, detail)` has no engine equivalent — if used, replace the call with `workflow_log(...)` or restructure per what the call does (report in the fix notes; the TUI test suite is the contract). **If the calls are used and cannot map 1:1, STOP and report BLOCKED with the usage — do not silently change TUI output.**

- [ ] **Step 2: Add the direct dependency**

In `packages/nanolab/pyproject.toml`, add `"sonata-engine",` to `dependencies` (alphabetical: after `"rich>=13.8",`).

- [ ] **Step 3: Re-point the nanolab tests**

Mechanical sed over the listed test files: `s/from sonata_tasks\.workflow\./from sonata_engine.workflow./`. The aggregator tests pin the bilingual vocabulary — they keep passing because the engine emits the same event types.

- [ ] **Step 4: Run the nanolab suite**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: PASS (the pre-existing `test_help_does_not_require_nanofaas_root` environment failure is allowed).

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab
git commit -m "refactor: nanolab consumes the engine event bus"
```

---

### Task 3: Delete sonata_tasks/workflow and the dead reporting API

Remove the duplicated bus and the legacy-only one-shot emitters, and update the import-linter contract that pinned them.

**Files:**
- Delete: `packages/sonata-tasks/src/sonata_tasks/workflow/` (whole directory: `__init__.py`, `context.py`, `events.py`, `models.py`, `reporting.py`, `event_builders.py`)
- Delete: `packages/sonata-tasks/tests/workflow/` (whole directory: the tests tested the deleted modules — their subject now lives in the external sonata-engine repo, which owns its own tests)
- Modify: `packages/sonata-tasks/.importlinter` (remove the `pure_types_are_logic_free` contract — its source modules `sonata_tasks.workflow.*` no longer exist)

- [ ] **Step 1: Verify zero remaining consumers**

Run: `grep -rn "sonata_tasks.workflow" packages/*/src packages/*/tests --include="*.py" | grep -v "\.egg"`
Expected: no output. If any remain, they are Task 1/2 oversights — fix them before deleting.

- [ ] **Step 2: Delete and update the contract**

```bash
git rm -r packages/sonata-tasks/src/sonata_tasks/workflow packages/sonata-tasks/tests/workflow
```

In `packages/sonata-tasks/.importlinter`, delete the `[importlinter:contract:pure_types_are_logic_free]` block (its `source_modules` referenced the deleted `sonata_tasks.workflow.events/models/context`).

- [ ] **Step 3: Run all gates**

Run: `pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q` (coverage rises — fewer lines measured), `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`, `ruff check packages`, `lint-imports --config packages/sonata-tasks/.importlinter --no-cache`, `lint-imports --config packages/nanolab/.importlinter --no-cache`.
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: delete the duplicated event bus from sonata_tasks"
```

---

### Task 4: Stale-comment and vocabulary cleanup

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/progress.py` (comment "Two engines, one console renderer: workflow_tasks says running/completed..." — now one engine; update)
- Modify: `packages/nanolab/src/nanolab/tui/event_aggregator.py` (comment at ~line 95 "workflow_tasks emits running/completed, Sonata emits started/passed" — the legacy vocabulary is gone; update to state the single engine vocabulary)

- [ ] **Step 1: Update the comments**

`progress.py`: the comment describes the two-vocabulary renderer. Reword to reflect that only the engine bus exists (one vocabulary, `started`/`passed`/`failed`).
`event_aggregator.py`: same — the bilingual mapping is now single-vocabulary; reword the comment, and if the aggregator still has dead branches for the legacy kinds (`task.running`/`task.completed`), remove them ONLY if the aggregator tests still pass unchanged (the tests pin the mapping — check `packages/nanolab/tests/test_tui_event_aggregator.py`; if a test feeds legacy kinds, that test is testing dead vocabulary — delete that test only if nothing can produce those kinds anymore, and say so in the commit).

- [ ] **Step 2: Run the nanolab suite**

Run: `NANOFAAS_ROOT=$HOME/Downloads/mcFaas pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add packages/nanolab
git commit -m "chore: single event vocabulary after the bus unification"
```

---

## Self-Review

**Spec coverage:** The goal — one sink, one vocabulary, `sonata_tasks/workflow/` deleted — maps to Tasks 1-3; the vocabulary/comments cleanup is Task 4. The fixed-pin constraint is honored (all mapping is consumer-side; no engine repo changes). The behavior contract (14 provisioning tests, shell test, TUI aggregator tests) is preserved by construction: `subtask` is the drop-in for `workflow_step` at the three `task_id`/`title`-only call sites, `workflow_log`/`has_workflow_sink`/`bind_workflow_sink`/event types are identical, and the aggregator already handles both vocabularies.

**Placeholder scan:** Task 2 Step 1 contains one conditional (tui/app.py's `step` usage) with an explicit STOP-and-report rule rather than a silent change — that is a deliberate checkpoint, not a placeholder: the grep evidence collected during planning showed no direct `step(`/`workflow_log(` calls in tui/app.py, so the expected outcome is "imports unused → delete", with the checkpoint covering the unexpected case.

**Type consistency:** `subtask(*, task_id, title="")` matches the three call sites' `task_id=`/`title=` kwargs exactly. `sonata_engine.workflow.context` provides `bind_workflow_sink`/`has_workflow_sink`; `sonata_engine.workflow.reporting` provides `workflow_log`/`subtask`; `sonata_engine.workflow.events/models/event_builders` provide the same names as the deleted modules (verified against site-packages during planning). The `bind_sonata_sink` alias removal in Task 2 is consistent because both names resolve to the same function after the sed.
