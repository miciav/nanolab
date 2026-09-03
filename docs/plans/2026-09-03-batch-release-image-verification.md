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

### Task 4: Reuse the Azure SSH transport across workflow commands

The first implementation removed the post-build inspection burst, but a resumed release proved that the registry push composite contains another per-image command burst. Batching every present and future composite would duplicate a network constraint throughout release code, so the durable boundary is the Azure transport.

**Files:**
- Modify in `azure-vm-sdk`: `src/azure_vm/vm.py`
- Test in `azure-vm-sdk`: `tests/unit/test_vm.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/vm/azure.py`
- Test: `packages/sonata-tasks/tests/vm/test_azure_provider.py`
- Modify: `packages/sonata-tasks/pyproject.toml`
- Modify: `uv.lock`

**Step 1: Specify SDK connection reuse**

Add a failing SDK test that executes two commands through one `AzureVM` and requires one connected `SSHClient`. Add a failing reconnection test for an inactive cached transport.

**Step 2: Implement the SDK lifecycle**

Cache one authenticated active client per `AzureVM`, close and replace an inactive client, invalidate it on transport errors, and expose `close()` for deterministic lifecycle cleanup.

**Step 3: Specify provider VM reuse**

Add a failing provider test that executes two commands for the same request and requires a single `AzureClient.get_vm()` result to serve both.

**Step 4: Implement provider ownership and update the pin**

Cache `AzureVM` handles by immutable connection identity inside `AzureVmProvider`; use the handle for command execution and upload, and close it before teardown. Pin NanoLab to the published SDK commit and refresh `uv.lock`.

**Step 5: Verify and publish both repositories**

Run the complete unit, lint, and type-check suites in each repository; commit and push the SDK first, then NanoLab. Fast-forward both remote checkouts and confirm the release journal and retained VM remain available.
