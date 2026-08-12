# HPA Scales on a Real Load Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the HPA scaling on `nanofaas_in_flight` — a semaphore counter that reads 0 under full load and saturates when the function is stuck — and scale on request rate instead, which is what actually varies with load.

**Architecture:** nanoFaaS already accepts three scaling metric types (`queue_depth`, `in_flight`, `rps`) and already translates all three into correct `MetricSpec` objects. Only `in_flight` has a prometheus-adapter rule, so the other two produce HPAs referencing external metrics nothing serves. This plan adds the missing rules, adds the test that would have caught their absence, and moves nanolab's autoscaling load test onto `rps`.

**Tech Stack:** Java 25 / Gradle (nanoFaaS), Helm + prometheus-adapter, Python 3.12 / uv / pytest (nanolab), k3s with `HPAScaleToZero`.

## Global Constraints

- **Two repositories.** nanoFaaS lives at `~/Downloads/mcFaas`, nanolab at `~/Downloads/nanolab`. Each task states which. Branch in both; never commit to `main` directly — both have branch protection requiring their checks.
- nanoFaaS gates: `./gradlew test` must pass. nanolab gates: `pytest` per package, `basedpyright`, `ruff check packages`, `lint-imports` — all as listed in nanolab's README CI-gate block.
- nanolab tests need `NANOFAAS_ROOT` pointed at a nanoFaaS checkout whose **git tree is clean**, or `test_build_release_request_requires_credentials_for_execution` fails for an unrelated reason.
- **The external metric target semantics are `desiredReplicas = ceil(currentMetricValue / targetValue)`.** This is not assumed: it is what the 2026-08-12 run did — target `2`, metric `4`, replicas `0` → scaled to `2`. Every target value below is chosen against this formula.
- Do not change `nanofaas.defaults.concurrency` or the queue's dispatch gating. Those are load-bearing elsewhere and are discussed under "what this plan does not do".

---

## The evidence this plan is built on

From a real run on 2026-08-12 (`autoscaling-cycle-k8s-hpa.yaml`, multipass, k3s with `HPAScaleToZero`). Throughput against the metric the HPA scaled on:

| time | req/s | `function_inFlight` | HPA decision |
|---|---|---|---|
| 06:14:32 | 361 | 0 | |
| 06:14:47 | 364 | 0 | scaled 2→1, *"all metrics below target"* |
| 06:14:57 | 368 | 0 | scaled 1→0 |
| 06:15:07 | 2 | 4 | |
| 06:15:17 | 2 | 4 | scaled 0→2, *"above target"* |

Two independent defects in one metric:

**It is inverted.** In-flight concurrency is throughput × service time. At 365 req/s and a p50 of 2.4 ms that is ≈0.88, so a 5-second scrape of an instantaneous gauge reads 0 almost always. When the pods go away, requests block — and a blocked request still holds its slot — so the gauge rises exactly when nothing is being served.

**It is bounded.** `FunctionQueueState.tryAcquireSlot()` refuses to increment past `effectiveConcurrency`, which defaults to 4 (`platform/control-plane/src/main/resources/application.yml:45`). The gauge's range is `[0, 4]` by construction, independent of replica count and of request rate, so it cannot express "twice the load" at all.

---

## File Structure

**nanoFaaS (`~/Downloads/mcFaas`):**
- **Create** `platform/modules/k8s-deployment-provider/src/test/java/.../dispatch/AdapterRuleCoverageTest.java` — asserts every metric name the translator can emit has an adapter rule in the chart.
- **Modify** `deploy/helm/nanofaas/values.yaml:102-114` — add the `nanofaas_rps` and `nanofaas_queue_depth` external rules.

**nanolab (`~/Downloads/nanolab`):**
- **Modify** `packages/nanolab/src/nanolab/plans/loadtest.py:171` (the scaling metric) and `:429` (the preflight probe's metric name).
- **Modify** whichever nanolab test asserts the registration payload — find it in Task 3, do not guess.

---

### Task 1: The test that should have caught this (nanoFaaS)

Written first and expected to fail: two of the three supported types have no rule today.

**Repository:** `~/Downloads/mcFaas`

**Files:**
- Create: `platform/modules/k8s-deployment-provider/src/test/java/it/unimib/datai/nanofaas/modules/k8s/dispatch/AdapterRuleCoverageTest.java`

**Interfaces:**
- Consumes: `KubernetesMetricsTranslator` (package-private — the test must sit in the same package, which the path above already does), `ScalingMetric`, `ScalingConfig`, `FunctionSpec`.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Read the three things the test must agree with**

Read, in this order, and write down the exact strings:
1. `platform/control-plane/src/main/java/it/unimib/datai/nanofaas/controlplane/registry/FunctionSpecResolver.java:28` — `SUPPORTED_INTERNAL_SCALING_METRICS`, the set of types a user may register.
2. `platform/modules/k8s-deployment-provider/src/main/java/.../dispatch/KubernetesMetricsTranslator.java:60-70` — the `case "queue_depth", "in_flight", "rps"` arm that names the metric `"nanofaas_" + type`.
3. `deploy/helm/nanofaas/values.yaml` under `hpa-metrics-adapter.rules.external` — the `name.as` of each rule that exists.

Also read `KubernetesMetricsTranslatorTest.java` first, and construct `ScalingConfig`/`FunctionSpec`/`ScalingMetric` exactly the way it already does. Do not invent constructor shapes.

- [ ] **Step 2: Write the failing test**

```java
package it.unimib.datai.nanofaas.modules.k8s.dispatch;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Every external metric the translator can emit must be servable.
 *
 * A MetricSpec that names a metric no adapter rule produces is accepted by the
 * API server and then silently never satisfied: the HPA reports the metric as
 * unavailable and leaves the replica count alone. The translator's own unit
 * test cannot see this, because it stops at the MetricSpec.
 */
class AdapterRuleCoverageTest {

    /** Types a user may register; mirrors FunctionSpecResolver.SUPPORTED_INTERNAL_SCALING_METRICS. */
    private static final Set<String> SUPPORTED_TYPES = Set.of("queue_depth", "in_flight", "rps");

    @Test
    void everySupportedScalingMetricHasAnAdapterRule() throws Exception {
        Path values = repositoryRoot().resolve("deploy/helm/nanofaas/values.yaml");
        assertTrue(Files.isRegularFile(values), "chart values not found at " + values);
        String chart = Files.readString(values);

        Set<String> missing = new TreeSet<>();
        for (String type : SUPPORTED_TYPES) {
            String metricName = "nanofaas_" + type;
            if (!chart.contains("as: " + metricName)) {
                missing.add(metricName);
            }
        }

        assertEquals(
                Set.of(),
                missing,
                "these metrics are registrable and translated but no adapter rule serves them: " + missing);
    }

    private static Path repositoryRoot() {
        Path path = Path.of("").toAbsolutePath();
        while (path != null && !Files.isDirectory(path.resolve("deploy/helm/nanofaas"))) {
            path = path.getParent();
        }
        if (path == null) {
            throw new IllegalStateException("repository root not found from " + Path.of("").toAbsolutePath());
        }
        return path;
    }
}
```

The `as: <name>` substring match is deliberately crude: it asserts the rule exists without asserting how it is written, so adding a rule satisfies it and reformatting the chart does not break it. If the chart's indentation puts something other than `as: ` before the name, adjust the needle — but keep the assertion about *presence of a rule for the name*, not about the query.

- [ ] **Step 3: Run it and confirm it fails for the right reason**

```bash
cd ~/Downloads/mcFaas
./gradlew :control-plane-modules:k8s-deployment-provider:test --tests '*AdapterRuleCoverageTest*'
```

The project path is `:control-plane-modules:<dir>`, not the directory path: `settings.gradle:44-55` maps every directory under `platform/modules/` onto that coordinate. It includes them unconditionally — the `controlPlaneModules` property further down only selects which are bundled into the control plane, not which exist as projects — so this task needs no module selector.

Expected: FAIL naming `nanofaas_queue_depth` and `nanofaas_rps` as missing. If it fails because the chart or the repository root was not found, fix the test — that is not the defect being demonstrated.

- [ ] **Step 4: Commit the failing test**

```bash
git checkout -b fix/adapter-rules-for-every-scaling-metric
git add platform/modules/k8s-deployment-provider/src/test
git commit -m "test: every registrable scaling metric must have an adapter rule"
```

---

### Task 2: Serve the metrics that were only half-implemented (nanoFaaS)

**Repository:** `~/Downloads/mcFaas` (same branch as Task 1)

**Files:**
- Modify: `deploy/helm/nanofaas/values.yaml`, under `hpa-metrics-adapter.rules.external`

**Interfaces:**
- Consumes: the failing test from Task 1.
- Produces: external metrics `nanofaas_rps` and `nanofaas_queue_depth`, which Task 3 consumes by name.

- [ ] **Step 1: Add the two rules**

Append to the existing `external:` list, after the `function_inFlight` rule, keeping its indentation exactly:

```yaml
      # Request rate, the signal in_flight cannot be. In-flight concurrency is
      # throughput x service time; for a function answering in ~2ms it sits
      # below 1 at any realistic rate, and it is capped at the queue's
      # concurrency limit besides. A rate has neither problem: it tracks load
      # and has no ceiling.
      - seriesQuery: 'function_dispatch_total{function!=""}'
        resources:
          overrides:
            kubernetes_namespace:
              resource: namespace
        name:
          matches: '^function_dispatch_total$'
          as: nanofaas_rps
        # 1m, not 30s: the control plane is scraped every 5s, so a one-minute
        # window averages twelve samples and does not swing on a single slow
        # scrape. It also decays to zero within the 90s the load test allows
        # for scale-down, which is what scale-to-zero needs.
        metricsQuery: 'sum(rate(<<.Series>>{<<.LabelMatchers>>}[1m]))'
      # Backlog. Zero whenever the function keeps up, so it is a saturation
      # signal rather than a load signal — usable as a second metric, not as a
      # sole one. Present here because the type is registrable and translated,
      # and a registrable metric that nothing serves is a trap.
      - seriesQuery: 'function_queue_depth{function!=""}'
        resources:
          overrides:
            kubernetes_namespace:
              resource: namespace
        name:
          matches: '^function_queue_depth$'
          as: nanofaas_queue_depth
        metricsQuery: 'max(<<.Series>>{<<.LabelMatchers>>})'
```

- [ ] **Step 2: Run the test from Task 1**

Same command as Task 1 Step 3. Expected: PASS.

- [ ] **Step 3: Run the module's whole suite**

```bash
cd ~/Downloads/mcFaas
./gradlew :control-plane-modules:k8s-deployment-provider:test
```
Expected: green, with `KubernetesMetricsTranslatorTest` unchanged — this task adds chart rules and touches no Java.

- [ ] **Step 4: Commit**

```bash
git add deploy/helm/nanofaas/values.yaml
git commit -m "fix: serve nanofaas_rps and nanofaas_queue_depth to the HPA"
```

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin fix/adapter-rules-for-every-scaling-metric
gh pr create --title "fix: serve every registrable scaling metric to the HPA" --body "..."
```

The body must state what was observed: `queue_depth` and `rps` were accepted at registration and translated into MetricSpecs naming external metrics no adapter rule produced.

---

### Task 3: Point the autoscaling load test at request rate (nanolab)

**Repository:** `~/Downloads/nanolab`

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/loadtest.py:171` and `:429`
- Test: find the test asserting the registration payload before editing — `grep -rn "in_flight" packages/nanolab/tests`

**Interfaces:**
- Consumes: external metric `nanofaas_rps` from Task 2.
- Produces: nothing later tasks consume.

- [ ] **Step 1: Find every place that names the metric**

```bash
cd ~/Downloads/nanolab
grep -rn "in_flight" packages/
```

Expect at least `plans/loadtest.py:171` (the registered scaling config) and `:429` (the preflight probe URL). Fix every hit; there may be tests asserting the payload.

- [ ] **Step 2: Change the registered metric**

At `packages/nanolab/src/nanolab/plans/loadtest.py:171`, replace the `metrics` entry:

```python
            # rps, not in_flight: in-flight concurrency is throughput x service
            # time, which for this function sits below 1 at any rate the load
            # test produces, and is capped at the queue's concurrency limit
            # anyway. The HPA reads `ceil(value / target)`, so 100 asks for one
            # replica per 100 req/s — about four at the load this test applies,
            # inside the maxReplicas of 5 below.
            "metrics": [{"type": "rps", "target": "100"}],
```

- [ ] **Step 3: Change the preflight probe**

At `packages/nanolab/src/nanolab/plans/loadtest.py:429`, replace `nanofaas_in_flight` with `nanofaas_rps` in `hpa_metric_path`. Read the surrounding comment about why the probe waits, and update it if it names the old metric.

- [ ] **Step 4: Run the nanolab gate**

```bash
NANOFAAS_ROOT=<clean nanoFaaS checkout> uv run --locked --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups ruff check packages
```

Expected: green. Any test that fails because it asserted `in_flight` should be updated to assert `rps` — but read it first: if it asserts the *shape* of the payload rather than the metric name, it should not need changing at all, and a failure means something else moved.

- [ ] **Step 5: Commit, push, open the PR**

```bash
git checkout -b fix/loadtest-scales-on-request-rate
git add packages/nanolab
git commit -m "fix: scale the autoscaling load test on request rate"
git push -u origin fix/loadtest-scales-on-request-rate
gh pr create --title "fix: scale the autoscaling load test on request rate" --body "..."
```

**This PR must not be merged before Task 2's**, or the load test asks for a metric the chart does not yet serve.

---

### Task 4: Prove it on a real cluster (controller, not a subagent)

A subagent cannot usefully own this: it needs a live multipass VM, takes about fifteen minutes, and its verdict is a judgement about a time series. The controller runs it.

**Prerequisites:** Task 2 and Task 3 merged; the nanoFaaS pin in `.github/actions/setup-workspace/action.yml` moved to a commit containing Task 2, if the run is to reflect CI.

- [ ] **Step 1: Run the scenario**

```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab nanolab run \
  packages/nanolab/scenarios-v2/autoscaling-cycle-k8s-hpa.yaml \
  --environment packages/nanolab/environments/multipass-hpa-scale-to-zero.yaml --keep
```

Two environmental notes from the 2026-08-12 run, neither a defect in this work: a fresh VM inherits the router's DNS, which intermittently answers `127.0.0.1` and breaks the Gradle wrapper download — fix with `sudo resolvectl dns enp0s1 1.1.1.1 8.8.8.8 && sudo resolvectl domain enp0s1 ""` as soon as the VM exists. And `--resume` does not exist for loadtest workflows, so any failure restarts from task 1.

- [ ] **Step 2: Read the verdict from the summary, not from the exit code**

```bash
python3 -c "
import json; s=json.load(open('packages/nanolab/runs/latest/summary.json'))['autoscaling']
print('rises_from_zero  :', s['rises_from_zero'])
print('max_replicas     :', s['max_replicas_observed'])
print('final_desired    :', s['final_desired_replicas'])
print('samples          :', [(x['elapsed_seconds'], x['desired']) for x in s['replica_samples']])
"
```

Pass condition: `rises_from_zero == 1`. That field exists because of the fix in PR #29; if it is absent, that PR is not merged and this step cannot conclude anything.

- [ ] **Step 3: Confirm the new metric tracks load**

Compare `max(rate(function_dispatch_total[1m]))` against the replica timeline over the run window, the way the 2026-08-12 investigation did. The metric must **rise with** throughput, not against it. Record the table in the PR or the issue.

- [ ] **Step 4: Record the baseline**

Copy `summary.json`, `k6-summary.json`, `report.html` and `metrics/prometheus-snapshot.json` somewhere durable — `runs/latest/` is overwritten by the next run.

---

## What this plan deliberately does not do

**It does not change the queue's concurrency model.** `FunctionQueueState` gates dispatch at `effectiveConcurrency` (default 4) per function, in the control plane, regardless of replica count. That means replicas beyond the concurrency limit cannot increase throughput — a real ceiling, and arguably a more fundamental one than the metric. It is a separate change with its own blast radius, and conflating it with the metric fix would make both unreviewable.

**It does not remove `in_flight`.** It remains registrable, translated and now still the only metric with a pre-existing rule. It is a defensible signal for a slow function, where concurrency does accumulate. What was wrong was using it for a 2 ms function, not its existence.

**It does not change `maxReplicas: 5` or the k6 load profile.** Both are chosen elsewhere and the target of 100 req/s was picked to fit inside them.

## The risk worth naming

Scale-from-zero now depends on `rate(function_dispatch_total[1m])` being greater than zero while the function has no replicas. The 2026-08-12 data shows the counter does advance at about 0.4/s in that state, so `ceil(0.4/100) = 1` and the function wakes. But that trickle was measured under a specific failure mode, not designed for, and a rate window that has decayed to exactly zero cannot wake anything.

Task 4 Step 2 is the check that matters here: if `rises_from_zero` comes back as 0 with a nonzero `max_replicas_observed`, the function never parked; if the preflight times out waiting for the function to park at zero, the wake path is broken and the metric needs a floor — most likely a second `queue_depth` metric on the same HPA, which is exactly why Task 2 adds its rule too.
