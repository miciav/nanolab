# Batched Release Image Verification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Verify each release image matrix with one retryable remote command instead of one SSH connection per check.

**Architecture:** Keep image verification inside `run_image_steps`, where the matrix is already one logical release operation. Ask Docker or one remote shell loop for all results at once, validate the complete ordered response locally, and reuse the existing connection-death retry because verification is read-only.

**Tech Stack:** Python 3.12, Sonata command tasks, Docker CLI, Skopeo, pytest.

---

### Task 1: Specify one-command matrix verification

**Files:**
- Modify: `packages/nanolab/tests/release/test_tasks.py`

**Step 1: Write the failing test**

Change the local and registry matrix tests to provide one multi-line response and assert that the executor receives exactly one command for two images. Add a connection-error test whose executor fails once and succeeds once, proving the read-only matrix operation is retried as a unit.

**Step 2: Run test to verify it fails**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/nanofaas/.worktrees/nanolab-release-fixture uv run pytest -q -c packages/nanolab/pyproject.toml packages/nanolab/tests/release/test_tasks.py`

Expected: FAIL because the current implementation invokes the executor once per image, and twice per image when checking architecture.

### Task 2: Batch and validate image inspection

**Files:**
- Modify: `packages/nanolab/src/nanolab/release/tasks.py`
- Test: `packages/nanolab/tests/release/test_tasks.py`

**Step 1: Write the minimal implementation**

Build one command per matrix: Docker inspects all local images in one invocation and emits architecture plus ID per line; a positional-argument shell loop runs Skopeo for registry images in one SSH session. Wrap only this read-only command with `retry_on_connection_death`. Require a successful result, exactly one output line per requested image, the expected architecture when present, and a valid SHA-256 digest for every line.

**Step 2: Run the focused tests**

Run the Task 1 command again.

Expected: PASS.

**Step 3: Run release and workspace verification**

Run:

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/nanofaas/.worktrees/nanolab-release-fixture uv run pytest -q -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run ruff check packages/nanolab/src/nanolab/release/tasks.py packages/nanolab/tests/release/test_tasks.py
uv run basedpyright -p packages/nanolab/pyproject.toml
git diff --check
```

Expected: all commands exit successfully.

### Task 3: Publish and update the release operator

**Files:**
- Commit the plan, implementation, and regression tests.

**Step 1: Commit and push**

Commit with an imperative message, fast-forward `main`, and push `origin/main`.

**Step 2: Pull remotely**

Fast-forward `/home/michele/Documenti/nanolab` on `michele@149.132.179.35`, preserving its intentional local `release.yaml` version change.

**Step 3: Verify synchronization**

Confirm local `main`, `origin/main`, and remote `HEAD` resolve to the same commit.
