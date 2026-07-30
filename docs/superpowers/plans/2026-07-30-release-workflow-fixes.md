# Release Workflow Bug Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 bugs in the release workflow: path confusion, loadgen provisioning, credential hardcoding, and evidence plumbing.

**Architecture:** 4 simple fixes (repo_root→nanofaas, credentials, provision_environment, loadgen wiring) are mechanical. 2 complex fixes (benchmark prebuilt images, publish/attest evidence) use a pragmatic v1 approach: benchmarks build from scratch, publish/attest stay in the DAG as documented stubs.

**Tech Stack:** Python 3.12, Sonata Engine, Azure VM provider

## Global Constraints

- No changes to `ReleaseJournal`, `release/run.py` (procedural release), or `release/versioning.py`
- Tests follow `RecordingExecutor` pattern
- Azure only
- All new code in existing files unless specified

---

### Task 1: Fix repo_root → nanofaas_root in 3 places

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py:110,203,226`

**Interfaces:**
- Produces: `nanofaas` already exists as local variable at line 93

- [ ] **Step 1: Make the edits**

Line 110 — `source_archive_resource(repo_root=request.repo_root, ...)`:
```python
    archive = source_archive_resource(
        repo_root=nanofaas,  # was request.repo_root
```

Line 203 — `build_arm64_image_plan(request.repo_root, ...)`:
```python
    arm_plan = build_arm64_image_plan(
        nanofaas,  # was request.repo_root
```

Line 226 — `build_publish_plan(request.repo_root, ...)`:
```python
    pub_plan = build_publish_plan(
        nanofaas,  # was request.repo_root
```

- [ ] **Step 2: Run linter + type checker**

```bash
uv run ruff check packages/nanolab/src/nanolab/plans/release.py
uv run --locked basedpyright --project packages/nanolab
```

- [ ] **Step 3: Run tests**

```bash
uv run pytest packages/nanolab/tests/plans/test_release.py -v
```

- [ ] **Step 4: Commit**

```bash
git add packages/nanolab/src/nanolab/plans/release.py
git commit -m "fix: use nanofaas root for archive, arm64 plan, publish plan

These operations read the nanoFaaS repo structure, not the nanolab tool root.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Fix credential hardcoding

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py:252-262`

- [ ] **Step 1: Update attest_composite call to use request.credentials**

```python
    # --- Phase 12: Attest ---
    cosign_key = "/secrets/cosign-key"
    cosign_password = "/secrets/cosign-password"
    if request.credentials is not None:
        cosign_key = str(getattr(request.credentials, "cosign_key", cosign_key))
        cosign_password = str(getattr(request.credentials, "cosign_password", cosign_password))
    attest = attest_composite(
        images=(),
        predicate_remote=Path(f"{remote_root}/predicate.json"),
        sbom_dir_remote=Path(f"{remote_root}/sboms"),
        cosign_key=cosign_key,
        password_file=cosign_password,
        docker_config="/tmp/ghcr-auth",
        executor=executor,
        role="stack",
    )
```

- [ ] **Step 2: Run linter + type checker**

```bash
uv run ruff check packages/nanolab/src/nanolab/plans/release.py
uv run --locked basedpyright --project packages/nanolab
```

- [ ] **Step 3: Commit**

```bash
git add packages/nanolab/src/nanolab/plans/release.py
git commit -m "fix: use request.credentials for cosign key paths

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Provision loadgen VM for release workflow

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py:217-218`
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py:169`

- [ ] **Step 1: Add "release" to loadtest_workflow condition (line 217)**

```python
    loadtest_workflow = scenario.workflow in ("loadtest", "offload-loadtest", "release")
```

This makes `dedicated_loadgen = True` when `"loadgen" in environment.roles`, provisioning the loadgen VM.

- [ ] **Step 2: Add "release" to k3s+registry installation condition (line 169)**

```python
    if scenario.backend == "k8s" or scenario.workflow in ("loadtest", "release"):
        planners.extend(
            [
                plan_k3s_install,
                plan_registry_ensure_container,
                plan_k3s_configure_registry,
            ]
        )
```

This installs k3s and local registry on the stack VM for the benchmark phases.

- [ ] **Step 3: Add loadgen k6+assets+repo for release workflow (line 177)**

```python
    if scenario.workflow in ("loadtest", "release") and not dedicated_loadgen:
        planners.extend([plan_loadtest_install_k6, plan_assets_sync_to_vm])
```

Wait — for release, `dedicated_loadgen` is True (since loadgen is in roles), so this branch is never taken. The loadgen provisioning is handled by lines 304-321, which provision k6+assets+repo on the loadgen VM. But lines 304-321 are guarded by `if loadgen is not None:` which is already satisfied by Step 1.

No change needed here — the existing loadgen provisioning (lines 304-321) already installs k6 and syncs assets.

- [ ] **Step 4: Run linter + type checker**

```bash
uv run ruff check packages/nanolab/src/nanolab/cli/provisioning.py
uv run --locked basedpyright --project packages/nanolab
```

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/cli/provisioning.py
git commit -m "fix: provision loadgen and k3s for release workflow

Add 'release' to loadtest_workflow condition so the loadgen VM is
created, and to the k3s/registry condition so the stack gets k8s.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Fix benchmark prebuilt images (v1: skip)

**Files:**
- No changes (pragmatic v1 approach)

**Rationale:** For v1, benchmarks build images from scratch inside the VM instead of using prebuilt images from the local registry. This is slower but correct. The `build_loadtest_plan()` already handles the no-prebuilt case — it calls `add_platform()` with `build_images=True` which builds everything locally.

**No code changes needed.** Documented as a known limitation for a future optimization pass.

---

### Task 5: Fix publish/attest empty data (v1: document as stub)

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py:224-262`

- [ ] **Step 1: Add TODO markers on publish/attest sections**

```python
    # --- Phase 11: Publish ---
    # TODO: plumb digest evidence from registry push + arm64 build phases.
    # Currently these phases run with empty data and will fail at runtime.
    # Use --until arm64-smoke to stop before publish when testing.
```

- [ ] **Step 2: Commit**

```bash
git add packages/nanolab/src/nanolab/plans/release.py
git commit -m "docs: document publish/attest evidence plumbing as TODO

These phases need digest data from prior phases. Document as known
limitation for v1.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: Add release to CLI provisioning guard

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py:288`

- [ ] **Step 1: Allow --provision for release workflow**

The guard `if provision and environment_config.provider == "local"` already covers this — release requires Azure so the local guard is fine. But let's add an explicit check:

No change needed. The existing `if provision and environment_config.provider == "local"` already ensures release can't be provisioned locally.

- [ ] **Step 2: Verify the import of release_config test works**

```bash
uv run python -c "from nanolab.config.scenario import ScenarioConfig; c = ScenarioConfig.model_validate({'workflow': 'release', 'functions': ['word-stats-java'], 'release': {'version': 'v0.18.1'}}); print(c.workflow)"
```

Expected: `release`

- [ ] **Step 3: Full test suite**

```bash
uv run pytest packages/nanolab/tests/plans/test_release.py packages/nanolab/tests/test_import_contracts.py -v
```

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: verify release scenario validation works end-to-end

Co-Authored-By: Claude <noreply@anthropic.com>"
```
