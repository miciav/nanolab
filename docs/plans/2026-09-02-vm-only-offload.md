# VM-only Offload Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Run the offload load test only across two separate virtual machines, using either Multipass or Azure.

**Architecture:** The `offload-loadtest` plan rejects local environments and requires explicit, distinct `stack` and `cloud` roles. Multipass and Azure environment files declare only those two VMs; k6 runs on `stack`. Azure resolves both VM addresses through its provider and restricts each VM's NodePorts to the operator and the edge VM.

**Tech Stack:** Python 3.12, Pydantic, Sonata VM providers, pytest, YAML, Kubernetes/Helm.

---

### Task 1: Enforce the two-VM contract

**Files:**
- Modify: `packages/nanolab/tests/plans/test_offload_loadtest.py`
- Modify: `packages/nanolab/src/nanolab/plans/offload_loadtest.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/offload_loadtest.py`

**Steps:**
1. Add tests that reject `local`, missing `cloud`, and identical VM names.
2. Run the focused tests and verify they fail for the missing contract.
3. Add the validation at the start of `build_offload_loadtest_plan`.
4. Delete the Compose-only imports, ports, resources, backend branches, and endpoint parameters.
5. Run the focused tests and verify they pass.

### Task 2: Define exactly two VMs for both providers

**Files:**
- Modify: `packages/nanolab/tests/config/test_scenario_offload_loadtest.py`
- Modify: `packages/nanolab/environments/multipass-offload.yaml`
- Create: `packages/nanolab/environments/azure-offload.yaml.example`

**Steps:**
1. Change the environment contract test to require exactly `stack` and `cloud` for both providers.
2. Run it and verify the Azure case fails and Multipass still has an extra role.
3. Remove `loadgen` from the Multipass environment and add the minimal Azure counterpart.
4. Run the environment tests and verify they pass.

### Task 3: Resolve and secure both Azure control planes

**Files:**
- Modify: `packages/nanolab/tests/plans/test_offload_loadtest.py`
- Modify: `packages/nanolab/tests/test_provisioning.py`
- Modify: `packages/nanolab/src/nanolab/plans/offload_loadtest.py`
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py`

**Steps:**
1. Add tests for Azure edge/cloud address resolution and per-VM restricted NodePorts.
2. Run them and verify they fail.
3. Reuse the existing Azure provider request and connection lookup for both roles.
4. Restrict `stack` and `cloud` ingress to the operator plus the stack VM address.
5. Run the focused plan and provisioning tests and verify they pass.

### Task 4: Remove the unsupported NanoFaaS Compose artifact

**Files:**
- Delete: `deploy/compose/offload-loadtest.yaml`
- Delete: `scripts/tests/test_offload_loadtest_compose.py`

**Steps:**
1. Verify NanoLab no longer references `offload-loadtest.yaml`.
2. Delete the unused Compose file and its contract test.
3. Run repository searches and Compose/test gates to ensure no dangling reference remains.

### Task 5: Verify and commit

**Steps:**
1. Run the complete NanoLab offload, provisioning, CLI, and Sonata offload suites.
2. Run Ruff and Pyright on changed Python files.
3. Run GitNexus change detection in both repositories.
4. Review both diffs and run `git diff --check`.
5. Commit each repository with short imperative messages.
