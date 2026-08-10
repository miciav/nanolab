# HPA Scale-to-Zero Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the Kubernetes HPA workflow scale a function from zero replicas up under external metrics and back to zero in an explicitly alpha-enabled Multipass environment.

**Architecture:** A role-level `hpaScaleToZero` setting flows from NanoLab environment YAML through the VM request into the k3s playbook. Only when true, the playbook writes k3s configuration enabling `HPAScaleToZero` in both the API server and controller manager, then reapplies k3s. The HPA scenario opts in with `hpaScaleToZero: true`, producing min/final replicas zero.

**Tech Stack:** Pydantic, Ansible, k3s, Kubernetes HPA v2, Prometheus Adapter, Pytest.

---

### Task 1: Carry the dedicated k3s scale-to-zero setting to provisioning

**Files:**
- Modify: `packages/nanolab/src/nanolab/config/environment.py`
- Modify: `packages/nanolab/src/nanolab/cli/vm_provider.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/vm/models.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/components/bootstrap.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/infra/ansible_assets/playbooks/provision-k3s.yml`
- Test: `packages/nanolab/tests/cli/test_vm_provider.py`
- Test: `packages/sonata-tasks/tests/components/test_bootstrap.py`

**Step 1: Write failing tests**

Require a role with `hpaScaleToZero: true` to produce a VM request and Ansible extra variable with that value; ordinary roles must preserve false.

**Step 2: Run focused tests and verify red**

Run: `uv run --all-packages pytest packages/nanolab/tests/cli/test_vm_provider.py packages/sonata-tasks/tests/components/test_bootstrap.py -q`

Expected: the models and bootstrap planner do not expose the setting.

**Step 3: Write minimal implementation**

Add one boolean from `RoleTarget` to `VmRequest`. Pass it only as `hpa_scale_to_zero` to the existing k3s playbook. When true, the playbook writes both k3s component feature gates and re-runs k3s if that configuration changed.

**Step 4: Run focused tests and verify green**

Run: `uv run --all-packages pytest packages/nanolab/tests/cli/test_vm_provider.py packages/sonata-tasks/tests/components/test_bootstrap.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/config/environment.py packages/nanolab/src/nanolab/cli/vm_provider.py packages/sonata-tasks/src/sonata_tasks/vm/models.py packages/sonata-tasks/src/sonata_tasks/components/bootstrap.py packages/sonata-tasks/src/sonata_tasks/infra/ansible_assets/playbooks/provision-k3s.yml packages/nanolab/tests/cli/test_vm_provider.py packages/sonata-tasks/tests/components/test_bootstrap.py
git commit -m "feat: support scale-to-zero k3s environments"
```

### Task 2: Make the HPA scenario opt in to scale-to-zero

**Files:**
- Modify: `packages/nanolab/src/nanolab/config/scenario.py`
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py`
- Modify: `packages/nanolab/scenarios-v2/autoscaling-cycle-k8s-hpa.yaml`
- Create: `packages/nanolab/environments/multipass-hpa-scale-to-zero.yaml`
- Modify: `packages/nanolab/README.md`
- Test: `packages/nanolab/tests/config/test_scenario.py`
- Test: `packages/nanolab/tests/plans/test_loadtest.py`

**Step 1: Write failing tests**

Require `hpaScaleToZero: true` to be valid only with `autoscalingStrategy: HPA`, and require the HPA plan to register min replicas zero and verify an initial/final zero floor.

**Step 2: Run focused tests and verify red**

Run: `uv run --all-packages pytest packages/nanolab/tests/config/test_scenario.py packages/nanolab/tests/plans/test_loadtest.py -q`

Expected: the scenario setting is rejected and the plan retains one replica.

**Step 3: Write minimal implementation**

Add `hpaScaleToZero` defaulting to false. Its only effect is to select zero for HPA min/initial/final replicas. Add the dedicated Multipass environment with both k3s feature gates enabled and update the command documentation.

**Step 4: Run focused tests and verify green**

Run: `uv run --all-packages pytest packages/nanolab/tests/config/test_scenario.py packages/nanolab/tests/plans/test_loadtest.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/nanolab
git commit -m "test: exercise HPA scale to zero"
```

### Task 3: Verify the complete NanoLab contract

**Files:**
- Test: `packages/nanolab/tests`
- Test: `packages/sonata-tasks/tests`

**Step 1: Run full verification**

Run: `NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --package nanolab pytest packages/nanolab/tests -q && uv run --package sonata-tasks pytest packages/sonata-tasks/tests -q`

Expected: PASS.

**Step 2: Check formatting and commit scope**

Run: `git diff --check && git status --short`

Expected: only the environment, provisioning, HPA workflow, documentation, and their tests are changed.
