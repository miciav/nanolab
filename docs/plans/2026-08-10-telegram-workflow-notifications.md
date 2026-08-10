# Telegram Workflow Notifications Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Send one local Telegram notification after each NanoLab workflow succeeds or fails, including workflow, scenario, and elapsed time.

**Architecture:** Add a small, generic `WorkflowObserver` protocol to Sonata Engine. `Workflow.run()` calls it once after all workflow cleanup has completed, with a terminal success/failure result. NanoLab supplies a Telegram implementation only when both environment variables are present and `CI` is unset; notification transport failures are deliberately ignored so they cannot change the workflow result.

**Tech Stack:** Python 3.12, Sonata Engine, stdlib `urllib.request`, pytest.

---

### Task 1: Add the engine lifecycle observer contract

**Files:**
- Modify: Sonata Engine `src/sonata_engine/core/workflow.py`
- Create/Modify: Sonata Engine workflow observer public API module and exports
- Test: Sonata Engine workflow tests

**Step 1: Write failing tests**

Assert that an observer receives exactly one completed notification after a successful run and after a failed run; failure is reported only after resource cleanup.

**Step 2: Run the focused tests to verify they fail**

Run: `pytest tests/... -k observer -q`

Expected: FAIL because `WorkflowObserver` and the `observers` run argument do not exist.

**Step 3: Implement the minimal observer API**

Define a protocol with `started(workflow_id, started_at)` and `finished(workflow_id, started_at, finished_at, error)`. Extend `Workflow.run()` with an optional immutable observer sequence, emit `started` once, then emit `finished` once from a `finally` path after engine cleanup. Preserve the original workflow exception if an observer raises by swallowing observer failures.

**Step 4: Run focused engine tests**

Run: `pytest tests/... -k observer -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/sonata_engine tests/
git commit -m "feat: add workflow lifecycle observers"
```

### Task 2: Add a best-effort Telegram observer

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/telegram.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Test: `packages/sonata-tasks/tests/test_telegram.py`

**Step 1: Write failing tests**

Use an injected sender to assert the exact success and failure messages include workflow id, scenario label, duration, and error detail. Assert sender errors are swallowed.

**Step 2: Run the focused test to verify it fails**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_telegram.py -q`

Expected: FAIL because the Telegram observer does not exist.

**Step 3: Implement the minimal observer**

Read `NANOLAB_TELEGRAM_BOT_TOKEN` and `NANOLAB_TELEGRAM_CHAT_ID` only in the explicit factory. Use `urllib.request` to post Telegram `sendMessage`; do not log secrets. Return no observer when credentials are incomplete or `CI` is non-empty.

**Step 4: Run focused tests**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_telegram.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/sonata-tasks/src packages/sonata-tasks/tests
git commit -m "feat: add Telegram workflow observer"
```

### Task 3: Attach the observer at NanoLab workflow construction

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/tui/app.py`
- Test: NanoLab CLI and TUI workflow tests
- Modify: `packages/nanolab/README.md`

**Step 1: Write failing tests**

Assert CLI and TUI pass the Telegram observer to their built workflow only when the local configuration is available; assert a workflow error remains the error reported to the caller.

**Step 2: Run focused tests to verify they fail**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/cli packages/nanolab/tests/test_tui_app.py -q`

Expected: FAIL because `Workflow.run()` does not yet receive the observer.

**Step 3: Implement the minimal wiring and documentation**

Build the observer once per run, pass it through each invocation of `Workflow.run()`, and document the two local-only variables. Do not add YAML configuration or CI secrets.

**Step 4: Run focused tests**

Run: `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/cli packages/nanolab/tests/test_tui_app.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/nanolab/src packages/nanolab/tests packages/nanolab/README.md
git commit -m "feat: notify local workflows on Telegram"
```

### Task 4: Verify the complete integration

**Files:**
- No new files

**Step 1: Run source and quality checks**

Run: `uv run --locked --all-packages --all-groups ruff check packages && uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests && uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests`

Expected: PASS, except for any explicitly identified pre-existing failure.

**Step 2: Verify no credentials are tracked**

Run: `git diff --check && git status --short`

Expected: no token or chat id appears in tracked content.

**Step 3: Commit documentation-only fixes if needed**

```bash
git add -p
git commit -m "docs: document local Telegram notifications"
```
