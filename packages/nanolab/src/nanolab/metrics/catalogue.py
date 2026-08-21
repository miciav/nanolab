"""What to read out of Prometheus, grouped by whichever component publishes it.

A load test used to ask for one hand-written list of 29 queries, in a plan that
had no idea which modules were compiled into the control plane under test. That
failed in both directions, and silently in both:

* **Incomplete.** `sync-queue` publishes five metrics and none of them were in
  the list. A nine-cell comparison therefore recorded zero rejections while the
  platform was refusing 29,555 requests — the run reported a healthy system that
  was in fact shedding 13% of its load at a queue sitting at 100 of 100.
* **Over-complete.** Autoscaler and HPA series were requested on runs with
  neither, so an empty result meant "nobody could have answered" and looked
  exactly like "it did not happen".

Grouping by publisher fixes both: a run asks for what its modules can answer,
and an empty series is a fact about the run rather than about the question.

The coverage test beside this module is the part that keeps it honest. It scans
the platform sources for metric names, per module, and fails when one appears
that this catalogue neither collects nor explicitly declines — so the list can no
longer fall behind the code it observes without anyone noticing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping

from sonata_tasks.loadtest.models import PrometheusQuery

CONTROL_PLANE_SELECTOR = '{app="nanofaas-control-plane"}'

Queries = tuple[PrometheusQuery, ...]
QueryBuilder = Callable[[str], Queries]


def _fn(function_name: str) -> str:
    return f"{{function={json.dumps(function_name)}}}"


def core_queries(function_name: str) -> Queries:
    """What the control plane publishes on its own, with no module loaded."""
    function = _fn(function_name)
    return (
        PrometheusQuery("function_dispatch_total", f"function_dispatch_total{function}", True),
        PrometheusQuery("function_success_total", f"function_success_total{function}", True),
        PrometheusQuery("function_error_total", f"function_error_total{function}"),
        PrometheusQuery("function_retry_total", f"function_retry_total{function}"),
        PrometheusQuery("function_timeout_total", f"function_timeout_total{function}"),
        PrometheusQuery(
            "function_queue_rejected_total", f"function_queue_rejected_total{function}"
        ),
        PrometheusQuery("function_cold_start_total", f"function_cold_start_total{function}"),
        PrometheusQuery("function_warm_start_total", f"function_warm_start_total{function}"),
        PrometheusQuery(
            "function_latency_count", f"function_latency_ms_seconds_count{function}", True
        ),
        PrometheusQuery(
            "function_latency_sum", f"function_latency_ms_seconds_sum{function}", True
        ),
        PrometheusQuery(
            "function_init_duration_count",
            f"function_init_duration_ms_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_init_duration_sum", f"function_init_duration_ms_seconds_sum{function}"
        ),
        PrometheusQuery(
            "function_queue_wait_count", f"function_queue_wait_ms_seconds_count{function}"
        ),
        PrometheusQuery(
            "function_queue_wait_sum", f"function_queue_wait_ms_seconds_sum{function}"
        ),
        # The mean of a wait is the number a queue is least honest about: a
        # buffer that is empty most of the time and full occasionally reports a
        # comfortable average while the callers who arrived during the burst
        # waited for the whole of it. The timer runs with a percentile histogram
        # under the advanced metrics profile, so the tail is recoverable.
        PrometheusQuery(
            "function_queue_wait_p95_ms",
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(function_queue_wait_ms_seconds_bucket{function}[30s]))) * 1000",
        ),
        PrometheusQuery(
            "function_e2e_latency_p95_ms",
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(function_e2e_latency_ms_seconds_bucket{function}[30s]))) * 1000",
        ),
        PrometheusQuery(
            "function_e2e_latency_count", f"function_e2e_latency_ms_seconds_count{function}"
        ),
        PrometheusQuery(
            "function_e2e_latency_sum", f"function_e2e_latency_ms_seconds_sum{function}"
        ),
    )


def runtime_queries(_function_name: str, *, required: bool = True) -> Queries:
    """The process itself, published by Spring rather than by nanoFaaS.

    `required` is a property of the run, not of the metric. On a single-build run
    the absence of these means the actuator scrape is broken and the run should
    say so. On a comparison it means nothing of the sort: measured here, the JVM
    and both serial-collector native builds publish `jvm_memory_used_bytes`, and
    the G1 build publishes none of it — SubstrateVM registers no MXBeans at all
    under G1, so there is no MemoryMXBean for Micrometer to read.

    An earlier version of this docstring named that trap and left the flag on
    anyway. The matrix then failed on its first G1 cell with "required query
    'jvm_heap_used_bytes' returned no data", which was the truth and not a fault.
    """
    return (
        PrometheusQuery(
            "process_cpu_usage", f"process_cpu_usage{CONTROL_PLANE_SELECTOR}", required
        ),
        PrometheusQuery(
            "jvm_heap_used_bytes",
            'jvm_memory_used_bytes{app="nanofaas-control-plane",area="heap"}',
            required,
        ),
        # Collection, published by a polling binder because a native image emits
        # no GC notifications and, under G1, registers no GarbageCollectorMXBean
        # at all. Not required for that reason. Worth asking for on every run: a
        # comparison of a JIT against two natively compiled builds turns on how
        # much of the wall clock the collector took, and the nine-cell matrix that
        # first raised the question could not answer it, because the platform
        # published these and the harness never asked.
        # `_total` and `_seconds` are added by the Prometheus exporter, not by the
        # code: both are FunctionCounters and the second declares baseUnit
        # "seconds". Querying the names as registered returns nothing at all,
        # which is how the first version of this catalogue asked for three GC
        # metrics and collected one.
        PrometheusQuery(
            "jvm_gc_collection_count",
            f"jvm_gc_collection_count_total{CONTROL_PLANE_SELECTOR}",
        ),
        PrometheusQuery(
            "jvm_gc_collection_time",
            f"jvm_gc_collection_time_seconds_total{CONTROL_PLANE_SELECTOR}",
        ),
        # Micrometer's own binder, which reads GC notifications rather than
        # polling the MXBean. Present on a JVM, absent on a native image — the
        # difference is itself a reading worth having in a build comparison.
        PrometheusQuery(
            "jvm_gc_pause_count", f"jvm_gc_pause_seconds_count{CONTROL_PLANE_SELECTOR}"
        ),
        PrometheusQuery(
            "jvm_gc_pause_sum", f"jvm_gc_pause_seconds_sum{CONTROL_PLANE_SELECTOR}"
        ),
        PrometheusQuery(
            "jvm_gc_time_fraction", f"jvm_gc_time_fraction{CONTROL_PLANE_SELECTOR}"
        ),
    )


def _async_queue_queries(function_name: str) -> Queries:
    """Depth of the per-function queue that `async-queue` owns.

    Blind to synchronous traffic whenever `sync-queue` is also loaded: the sync
    path never calls `QueueManager.enqueue`, so this reads 0 while the other
    queue fills. See nanofaas#196.
    """
    return (
        PrometheusQuery("function_queue_depth", f"function_queue_depth{_fn(function_name)}"),
        PrometheusQuery("function_inFlight", f"function_inFlight{_fn(function_name)}"),
        PrometheusQuery(
            "function_effective_concurrency",
            f"function_effective_concurrency{_fn(function_name)}",
        ),
    )


def _sync_queue_queries(function_name: str) -> Queries:
    """The sync queue's own counters, which nothing else republishes.

    Absent from the harness until a comparison run reported zero rejections
    against 29,555 real ones: the platform's `function_queue_rejected_total`
    belongs to `async-queue`, and a reader who checks only that one concludes
    the platform refused nothing.
    """
    function = _fn(function_name)
    return (
        PrometheusQuery("sync_queue_depth", f"sync_queue_depth{function}"),
        PrometheusQuery("sync_queue_admitted_total", f"sync_queue_admitted_total{function}"),
        PrometheusQuery("sync_queue_rejected_total", f"sync_queue_rejected_total{function}"),
        PrometheusQuery("sync_queue_timedout_total", f"sync_queue_timedout_total{function}"),
        PrometheusQuery(
            "sync_queue_wait_count", f"sync_queue_wait_seconds_count{function}"
        ),
        PrometheusQuery("sync_queue_wait_sum", f"sync_queue_wait_seconds_sum{function}"),
    )


def _autoscaler_queries(function_name: str) -> Queries:
    """What the internal scaler decided, which no other source records."""
    function = _fn(function_name)
    return (
        PrometheusQuery(
            "internal_scaling_recommended_replicas",
            f"function_scaling_recommended_replicas{function}",
        ),
        PrometheusQuery(
            "internal_scaling_desired_replicas",
            f"function_scaling_desired_replicas{function}",
        ),
        PrometheusQuery("internal_scaling_limited", f"function_scaling_limited{function}"),
        PrometheusQuery("internal_scaling_ratio_milli", f"function_scaling_ratio_milli{function}"),
    )


MODULE_QUERIES: Mapping[str, QueryBuilder] = {
    "async-queue": _async_queue_queries,
    "sync-queue": _sync_queue_queries,
    "autoscaler": _autoscaler_queries,
}

# Modules that publish nothing a snapshot should collect, listed so the coverage
# test can tell "deliberately not collected" from "nobody has looked yet".
MODULES_WITHOUT_QUERIES: frozenset[str] = frozenset(
    {
        # Sampled continuously by the concurrency watcher instead: a snapshot
        # taken after the run would record the limit the governor settled on and
        # lose the trajectory, which is the whole reading.
        "concurrency-control",
        "build-metadata",
        "runtime-config",
        "offload",
        "k8s-deployment-provider",
        "container-deployment-provider",
    }
)


def hpa_queries(function_name: str) -> Queries:
    """The HPA controller's own verdict, published by kube-state-metrics.

    Labelled by object name, not by the `function` label the control plane's
    series carry; the HPA is named after the deployment.
    """
    selector = f"{{horizontalpodautoscaler={json.dumps(f'fn-{function_name}')}}}"
    return (
        PrometheusQuery(
            "hpa_desired_replicas",
            f"kube_horizontalpodautoscaler_status_desired_replicas{selector}",
        ),
        PrometheusQuery(
            "hpa_current_replicas",
            f"kube_horizontalpodautoscaler_status_current_replicas{selector}",
        ),
        PrometheusQuery(
            "hpa_scaling_active",
            f'kube_horizontalpodautoscaler_status_condition{{horizontalpodautoscaler='
            f'{json.dumps(f"fn-{function_name}")},condition="ScalingActive",status="true"}}',
        ),
        PrometheusQuery(
            "hpa_scaling_limited",
            f'kube_horizontalpodautoscaler_status_condition{{horizontalpodautoscaler='
            f'{json.dumps(f"fn-{function_name}")},condition="ScalingLimited",status="true"}}',
        ),
    )


def queries_for(
    function_name: str,
    *,
    modules: Iterable[str],
    neighbour: str | None = None,
    hpa: bool = False,
    jvm_metrics_required: bool = True,
) -> Queries:
    """Everything this run can meaningfully be asked, and nothing it cannot.

    Unknown module names are ignored rather than rejected: nanolab selects
    modules by string and a platform may grow one before this catalogue learns
    about it. The coverage test is what turns that silence into a failure, at a
    moment when someone can act on it.
    """
    selected = tuple(modules)
    queries = list(core_queries(function_name)) + list(
        runtime_queries(function_name, required=jvm_metrics_required)
    )
    for module in selected:
        builder = MODULE_QUERIES.get(module)
        if builder is not None:
            queries.extend(builder(function_name))
    if hpa:
        queries.extend(hpa_queries(function_name))
    if neighbour is not None:
        queries.extend(neighbour_queries(neighbour, modules=selected))
    return tuple(queries)


def neighbour_queries(neighbour: str, *, modules: Iterable[str]) -> Queries:
    """The same queue readings for the second function of a two-function run.

    Suffixed so the primary series stay exactly where every existing reader looks
    for them. Without these a two-function run records one side of a question
    that is entirely about the relationship between two.
    """
    function = _fn(neighbour)
    queries = [
        PrometheusQuery(f"function_dispatch_total@{neighbour}", f"function_dispatch_total{function}"),
        PrometheusQuery(
            f"function_queue_rejected_total@{neighbour}",
            f"function_queue_rejected_total{function}",
        ),
        PrometheusQuery(
            f"function_queue_wait_count@{neighbour}",
            f"function_queue_wait_ms_seconds_count{function}",
        ),
        PrometheusQuery(
            f"function_queue_wait_sum@{neighbour}",
            f"function_queue_wait_ms_seconds_sum{function}",
        ),
        PrometheusQuery(
            f"function_queue_wait_p95_ms@{neighbour}",
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(function_queue_wait_ms_seconds_bucket{function}[30s]))) * 1000",
        ),
        PrometheusQuery(
            f"function_latency_count@{neighbour}",
            f"function_latency_ms_seconds_count{function}",
        ),
        PrometheusQuery(
            f"function_latency_sum@{neighbour}",
            f"function_latency_ms_seconds_sum{function}",
        ),
        PrometheusQuery(
            f"function_e2e_latency_p95_ms@{neighbour}",
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(function_e2e_latency_ms_seconds_bucket{function}[30s]))) * 1000",
        ),
    ]
    for module in modules:
        builder = MODULE_QUERIES.get(module)
        if builder is None:
            continue
        queries.extend(
            PrometheusQuery(f"{query.name}@{neighbour}", query.expr, query.required)
            for query in builder(neighbour)
        )
    return tuple(queries)
