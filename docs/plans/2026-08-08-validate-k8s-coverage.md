# Validate K8s Coverage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `validate-k8s` the black-box replacement for the deleted `K8sE2eTest`.

**Architecture:** Keep lifecycle ownership in Sonata resources, and add small public-HTTP/Kubernetes/Prometheus tasks only where the current primitives cannot express an old observable assertion. The scenario remains a single K8s validation workflow: its standard function covers registration, endpoint metadata, invocation, asynchronous execution, deletion and resource disappearance; two dedicated functions exercise queue pressure and cold/warm metrics.

**Tech Stack:** Python 3.12, Sonata Engine tasks/resources, curl, kubectl, Prometheus exposition format, pytest.

---

### Task 1: Express HTTP function API assertions

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/http_function.py`
- Test: `packages/sonata-tasks/tests/test_http_function.py`

**Step 1: Write failing tests** for a task that verifies the registration response and provider-derived endpoint, and tasks that list/get a function, enqueue twice with one idempotency key, and poll the execution to success.

**Step 2: Run the focused tests**

Run: `uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_http_function.py -q`

Expected: FAIL because the tasks do not exist.

**Step 3: Implement the smallest public-HTTP tasks** using curl and JSON parsing in the task verifier. Return the execution identifier from enqueue so the idempotency comparison and polling use task inputs rather than a file or mutable global.

**Step 4: Run the focused tests**

Run: `uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_http_function.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/sonata-tasks/src/sonata_tasks/http_function.py packages/sonata-tasks/tests/test_http_function.py
git commit -m "feat: add HTTP lifecycle validation tasks"
```

### Task 2: Express Kubernetes cleanup and Prometheus value checks

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/kubectl.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/metrics.py`
- Test: `packages/sonata-tasks/tests/test_kubectl.py`
- Test: `packages/sonata-tasks/tests/test_metrics.py`

**Step 1: Write failing tests** for waiting until a named Deployment and Service are absent, and for asserting an exposition metric has a numeric sample at least a requested value (including one-of alternate metric names).

**Step 2: Run focused tests**

Run: `uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_kubectl.py packages/sonata-tasks/tests/test_metrics.py -q`

Expected: FAIL because these checks do not exist.

**Step 3: Implement minimal checks.** Reuse `KubectlTask` and scrape once per check; parse only Prometheus sample lines, not a client or a new dependency.

**Step 4: Run focused tests**

Run: `uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_kubectl.py packages/sonata-tasks/tests/test_metrics.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/sonata-tasks/src/sonata_tasks/kubectl.py packages/sonata-tasks/src/sonata_tasks/metrics.py packages/sonata-tasks/tests/test_kubectl.py packages/sonata-tasks/tests/test_metrics.py
git commit -m "feat: add Kubernetes lifecycle assertion tasks"
```

### Task 3: Build the complete K8s validation graph

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/validate.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/platform.py`
- Modify: `packages/nanolab/src/nanolab/plans/validate.py`
- Test: `packages/sonata-tasks/tests/test_validate.py`
- Test: `packages/nanolab/tests/plans/test_validate.py`

**Step 1: Write failing plan tests** that require the K8s graph to include: response metadata/endpoint, async idempotency and success, deletion followed by Deployment/Service disappearance, queue pressure, and cold/warm/init metrics. Keep the container graph unchanged.

**Step 2: Run focused tests**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --package nanolab pytest packages/nanolab/tests/plans/test_validate.py && uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_validate.py -q`

Expected: FAIL because `validate-k8s` currently ends after synchronous invoke/resource inspection.

**Step 3: Implement the graph.** Use the scenario function for the normal lifecycle. Register two narrow fixture functions only for queue pressure and cold/warm measurements, explicitly order their assertions before resource release, and preserve automatic release for all registrations and Helm.

**Step 4: Run focused tests**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --package nanolab pytest packages/nanolab/tests/plans/test_validate.py && uv run --package sonata-tasks pytest packages/sonata-tasks/tests/test_validate.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/sonata-tasks/src/sonata_tasks/validate.py packages/sonata-tasks/src/sonata_tasks/platform.py packages/nanolab/src/nanolab/plans/validate.py packages/sonata-tasks/tests/test_validate.py packages/nanolab/tests/plans/test_validate.py
git commit -m "feat: cover Kubernetes E2E validation workflow"
```

### Task 4: Verify the migrated behavior

**Files:**
- Modify: `docs/superpowers/specs/2026-07-31-nanofaas-e2e-workflows-in-nanolab.md`

**Step 1: Update the migration specification** to mark the K8s behavior implemented and list the retained black-box checks.

**Step 2: Run all local tests**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --package nanolab pytest packages/nanolab/tests -q && uv run --package sonata-tasks pytest packages/sonata-tasks/tests -q && git diff --check`

Expected: PASS.

**Step 3: Run the real workflow**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --package nanolab nanolab run packages/nanolab/scenarios-v2/validate-k8s.yaml --environment packages/nanolab/environments/multipass.yaml`

Expected: successful K8s lifecycle, queue, metrics and cleanup checks.

**Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-31-nanofaas-e2e-workflows-in-nanolab.md
git commit -m "docs: record K8s E2E migration"
```
