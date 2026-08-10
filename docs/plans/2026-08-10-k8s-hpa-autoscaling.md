# Kubernetes HPA Autoscaling Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a Kubernetes-only HPA workflow that proves a managed function scales from one replica above one replica and back to one through Prometheus external metrics.

**Architecture:** The NanoFaaS Helm chart declares the upstream Prometheus Adapter as an optional dependency. NanoLab enables it only for a scenario whose functions use `strategy: HPA`; the control plane continues to create the per-function HPA when registering that function. The shared load-test plan selects the requested strategy and verifies its configured replica floor.

**Tech Stack:** Helm 3, prometheus-community/prometheus-adapter, Kubernetes HPA v2, Prometheus external metrics API, Python/Pytest.

---

### Task 1: Add the optional Prometheus Adapter chart dependency

**Files:**
- Modify: `/Users/micheleciavotta/Downloads/mcFaas/deploy/helm/nanofaas/Chart.yaml`
- Modify: `/Users/micheleciavotta/Downloads/mcFaas/deploy/helm/nanofaas/values.yaml`
- Modify: `/Users/micheleciavotta/Downloads/mcFaas/deploy/helm/nanofaas/README.md`
- Test: Helm template/lint for the chart

**Step 1: Define the failing chart expectation**

Render the chart with `hpaMetricsAdapter.enabled=true` and assert that it produces the adapter APIService and maps `function_in_flight` to `nanofaas_in_flight`.

**Step 2: Run it to verify it fails**

Run: `helm dependency build deploy/helm/nanofaas && helm template nanofaas deploy/helm/nanofaas --set hpaMetricsAdapter.enabled=true`

Expected: the chart has no adapter dependency or external-metric rule.

**Step 3: Write minimal implementation**

Add the pinned `prometheus-adapter` dependency, disabled by default. Configure its Prometheus endpoint and the one external rule using the existing `function` label. Keep the adapter disabled unless NanoLab explicitly enables it.

**Step 4: Run chart verification**

Run: `helm dependency build deploy/helm/nanofaas && helm lint deploy/helm/nanofaas && helm template nanofaas deploy/helm/nanofaas --set hpaMetricsAdapter.enabled=true`

Expected: lint succeeds and the rendered adapter configuration contains `nanofaas_in_flight`.

**Step 5: Commit**

```bash
git add deploy/helm/nanofaas
git commit -m "feat: add optional HPA metrics adapter"
```

### Task 2: Select HPA from a NanoLab scenario

**Files:**
- Modify: `packages/nanolab/src/nanolab/config/scenario.py`
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/loadtest/autoscaling.py`
- Test: `packages/nanolab/tests/config/test_scenario.py`
- Test: `packages/nanolab/tests/plans/test_loadtest.py`
- Test: `packages/sonata-tasks/tests/test_autoscaling.py`

**Step 1: Write failing tests**

Define a load-test scenario with `autoscalingStrategy: HPA` and require its registration payload to use `HPA`, its Helm command to enable the adapter, and its checks to expect one initial/final replica.

**Step 2: Run the focused tests to verify they fail**

Run: `uv run pytest packages/nanolab/tests/config/test_scenario.py packages/nanolab/tests/plans/test_loadtest.py packages/sonata-tasks/tests/test_autoscaling.py -q`

Expected: the configuration field and HPA plan behavior are missing.

**Step 3: Write minimal implementation**

Add `autoscalingStrategy` with `INTERNAL` as the backward-compatible default. For `HPA`, request min replicas one, set `hpaMetricsAdapter.enabled=true`, and pass the expected replica floor to the existing verifier. Reject HPA for the container backend.

**Step 4: Run the focused tests to verify they pass**

Run: `uv run pytest packages/nanolab/tests/config/test_scenario.py packages/nanolab/tests/plans/test_loadtest.py packages/sonata-tasks/tests/test_autoscaling.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/nanolab packages/sonata-tasks
git commit -m "feat: support HPA autoscaling scenarios"
```

### Task 3: Add the HPA cycle scenario and validate the adapter API

**Files:**
- Create: `packages/nanolab/scenarios-v2/autoscaling-cycle-k8s-hpa.yaml`
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py`
- Test: `packages/nanolab/tests/plans/test_loadtest.py`

**Step 1: Write the failing test**

Require the HPA plan to check `external.metrics.k8s.io` for `nanofaas_in_flight` before running k6.

**Step 2: Run the focused test to verify it fails**

Run: `uv run pytest packages/nanolab/tests/plans/test_loadtest.py -q`

Expected: no adapter readiness check exists.

**Step 3: Write minimal implementation**

Add the scenario with `backend: k8s`, `autoscaling: true`, and `autoscalingStrategy: HPA`. Poll the Kubernetes external-metrics API for the registered function metric before the load test. Reuse the existing k6 profile and replica watcher; it asserts `1 → N → 1`.

**Step 4: Run verification**

Run: `uv run pytest packages/nanolab/tests -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add packages/nanolab/scenarios-v2 packages/nanolab/src packages/nanolab/tests
git commit -m "test: add Kubernetes HPA autoscaling cycle"
```
