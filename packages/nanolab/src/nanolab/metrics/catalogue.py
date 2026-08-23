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
        # What happens to a request before the application sees it. At the peak of
        # the 2026-08-23 sweep a rejected request waited 675 ms for its 429 while
        # the control plane accounted for 6.55 ms of it, and nothing was looking at
        # the rest.
        #
        # Not the accept queue: http_req_connecting averaged 0.01 ms and one
        # connection per VU accounts for 0.4-0.5% of requests, so ~99% of them
        # arrive on a connection that was established long before. What is left in
        # front of the application is per-connection: bytes sitting read but
        # unhandled in the socket buffer, and a read event queued on an event loop.
        # That is what the two series below are for.
        #
        # Names verified against a live /actuator/prometheus, because the first
        # attempt guessed both: `connections_total` does not exist - the total is
        # `connections`, unsuffixed - and the selector was a `job` regex when every
        # other application metric here selects on `app`. Both mistakes return an
        # empty series rather than an error, so a whole Azure sweep collected
        # nothing and said nothing about it.
        PrometheusQuery(
            "netty_connections_active",
            f"reactor_netty_http_server_connections_active{CONTROL_PLANE_SELECTOR}",
        ),
        PrometheusQuery(
            "netty_connections",
            f"reactor_netty_http_server_connections{CONTROL_PLANE_SELECTOR}",
        ),
        # Work queued on the event loops themselves: the backlog upstream of every
        # meter the application owns.
        #
        # Summed AND unsummed, because the sum alone threw away the count. The
        # 2026-08-23 verify run read 1,660 pending tasks and could not say whether
        # that was four loops of 415 or sixteen of 104 - and without the number of
        # loops there is no per-task cost, which is the quantity that decides
        # whether this queue can hold 900ms. The unsummed series is tagged per
        # loop thread, so its cardinality is the thread count.
        PrometheusQuery(
            "netty_eventloop_pending",
            f"sum(reactor_netty_eventloop_pending_tasks{CONTROL_PLANE_SELECTOR})",
        ),
        PrometheusQuery(
            "netty_eventloop_pending_per_loop",
            f"reactor_netty_eventloop_pending_tasks{CONTROL_PLANE_SELECTOR}",
        ),
        # How many threads exist and how many want a core. Reactor Netty runs
        # max(availableProcessors, 4) event loops - four inside a two-CPU cgroup -
        # while Schedulers.boundedElastic() sizes itself at 10 x availableProcessors
        # and creates them lazily, so twenty more appear only once load arrives.
        # Four loops against twenty workers on two cores is a scheduling story that
        # no application meter can tell, and the JVM has been publishing the two
        # numbers that test it since the first run.
        #
        # Verified against a live /actuator/prometheus, not remembered: the state
        # tag is spelled "timed-waiting", and jvm_threads_live_threads is a gauge
        # with no suffix beyond the unit.
        PrometheusQuery(
            "jvm_threads_live", f"jvm_threads_live_threads{CONTROL_PLANE_SELECTOR}"
        ),
        PrometheusQuery(
            "jvm_threads_runnable",
            f'jvm_threads_states_threads{{app="nanofaas-control-plane",state="runnable"}}',
        ),
        # Time spent INSIDE the application, split by what the caller got. Spring
        # Boot publishes this already and nobody had asked for it.
        #
        # It is the missing half of the arithmetic: k6 says a rejected request
        # waited 675 ms and that http_req_waiting - server think time - is 100% of
        # it, while queue wait plus service accounts for 6.55 ms. This says how
        # much of the rest was inside the handler. Whatever remains was in front of
        # it, on an already-open connection waiting to be read.
        #
        # Summed by status because the raw series also carries method, outcome,
        # exception and a templated uri, and only the status matters here.
        PrometheusQuery(
            "http_server_ok_count",
            f'sum(http_server_requests_seconds_count{{app="nanofaas-control-plane",status="200"}})',
        ),
        PrometheusQuery(
            "http_server_ok_sum",
            f'sum(http_server_requests_seconds_sum{{app="nanofaas-control-plane",status="200"}})',
        ),
        PrometheusQuery(
            "http_server_rejected_count",
            f'sum(http_server_requests_seconds_count{{app="nanofaas-control-plane",status="429"}})',
        ),
        PrometheusQuery(
            "http_server_rejected_sum",
            f'sum(http_server_requests_seconds_sum{{app="nanofaas-control-plane",status="429"}})',
        ),
    )


def _async_queue_queries(function_name: str) -> Queries:
    """Depth of the per-function queue that `async-queue` owns.

    Blind to synchronous traffic whenever `sync-queue` is also loaded: the sync
    path never calls `QueueManager.enqueue`, so this reads 0 while the other
    queue fills. See nanofaas#196.
    """
    function = _fn(function_name)
    return (
        PrometheusQuery("function_queue_depth", f"function_queue_depth{function}"),
        PrometheusQuery("function_inFlight", f"function_inFlight{function}"),
        PrometheusQuery(
            "function_effective_concurrency",
            f"function_effective_concurrency{function}",
        ),
        PrometheusQuery(
            "function_dispatchable_backlog", f"function_dispatchable_backlog{function}"
        ),
        PrometheusQuery(
            "function_queue_offer_duration_count",
            f"function_queue_offer_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_queue_offer_duration_sum",
            f"function_queue_offer_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_queue_poll_duration_count",
            f"function_queue_poll_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_queue_poll_duration_sum",
            f"function_queue_poll_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_wakeup_delay_count",
            f"function_scheduler_wakeup_delay_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_scheduler_wakeup_delay_sum",
            f"function_scheduler_wakeup_delay_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_poll_delay_count",
            f"function_scheduler_poll_delay_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_scheduler_poll_delay_sum",
            f"function_scheduler_poll_delay_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_activation_bookkeeping_duration_count",
            f"function_scheduler_activation_bookkeeping_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_scheduler_activation_bookkeeping_duration_sum",
            f"function_scheduler_activation_bookkeeping_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_signal_enqueue_duration_count",
            f"function_scheduler_signal_enqueue_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_scheduler_signal_enqueue_duration_sum",
            f"function_scheduler_signal_enqueue_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_dispatch_submit_duration_count",
            f"function_scheduler_dispatch_submit_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_scheduler_dispatch_submit_duration_sum",
            f"function_scheduler_dispatch_submit_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_dispatch_submit_duration_all_count",
            "sum(function_scheduler_dispatch_submit_duration_seconds_count)",
        ),
        PrometheusQuery(
            "function_scheduler_dispatch_submit_duration_all_sum",
            "sum(function_scheduler_dispatch_submit_duration_seconds_sum)",
        ),
        PrometheusQuery(
            "function_dispatch_slot_hold_duration_count",
            f"function_dispatch_slot_hold_duration_seconds_count{function}",
        ),
        PrometheusQuery(
            "function_dispatch_slot_hold_duration_sum",
            f"function_dispatch_slot_hold_duration_seconds_sum{function}",
        ),
        PrometheusQuery(
            "function_scheduler_batch_limit_total",
            f"function_scheduler_batch_limit_total{function}",
        ),
        PrometheusQuery(
            "function_scheduler_slot_blocked_total",
            f"function_scheduler_slot_blocked_total{function}",
        ),
        PrometheusQuery(
            "function_scheduler_slot_blocked_all_total",
            "sum(function_scheduler_slot_blocked_total)",
        ),
        PrometheusQuery(
            "function_scheduler_signal_coalesced_total",
            f"function_scheduler_signal_coalesced_total{function}",
        ),
        PrometheusQuery(
            "function_scheduler_signal_coalesced_all_total",
            "sum(function_scheduler_signal_coalesced_total)",
        ),
        # Untagged on purpose: one scheduler thread serves every function, so
        # these two partition its wall clock and a `function` selector would
        # return nothing. Their sum over a window must not exceed the window.
        PrometheusQuery(
            "scheduler_visit_duration_count", "scheduler_visit_duration_seconds_count"
        ),
        PrometheusQuery(
            "scheduler_visit_duration_sum", "scheduler_visit_duration_seconds_sum"
        ),
        PrometheusQuery(
            "scheduler_idle_duration_count", "scheduler_idle_duration_seconds_count"
        ),
        PrometheusQuery(
            "scheduler_idle_duration_sum", "scheduler_idle_duration_seconds_sum"
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


# A query that answers nothing is not a fact about the run, it is a hole in it -
# and holes have been expensive here. The Netty series were absent for a whole
# Azure night because the image predated the config that publishes them, and
# nothing said so; four reacquisition probes went on being asked for months after
# the meters were deleted, returning empty results that read as "it never
# happened". Measured over the 114 archived cells, 69 of 80 query names never
# came back empty where they were asked, so silence is the exception and belongs
# where exceptions belong: declared, with its reason.
#
# Keyed by query name, valued by why that name may legitimately answer nothing.
_MAY_BE_ABSENT: Mapping[str, str] = {
    "jvm_gc_collection_count": "SubstrateVM under G1 registers no GarbageCollectorMXBean",
    "jvm_gc_collection_time": "SubstrateVM under G1 registers no GarbageCollectorMXBean",
    "jvm_gc_pause_count": "Micrometer's notification binder does not exist on a native image",
    "jvm_gc_pause_sum": "Micrometer's notification binder does not exist on a native image",
    "jvm_gc_time_fraction": "polled binder, absent when the collector publishes no MXBean",
    # process_cpu_usage and jvm_heap_used_bytes are governed by the run instead,
    # through runtime_queries(required=...): on a single-build run their absence
    # means the actuator scrape is broken, on a build comparison it means G1.
    "process_cpu_usage": "governed per run by jvm_metrics_required",
    "jvm_heap_used_bytes": "governed per run by jvm_metrics_required",
}


def _require_everything_that_can_answer(queries: Queries) -> Queries:
    """Flip the default: a query is required unless it is declared absent-able.

    The flag was opt-in, and five of seventy-five queries had opted in. Every
    other one could return nothing and the run would still pass, which is how a
    matrix reported a healthy system while the platform shed 13% of its load.
    """
    return tuple(
        query
        if query.name in _MAY_BE_ABSENT or query.required
        else PrometheusQuery(query.name, query.expr, True)
        for query in queries
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
    return _require_everything_that_can_answer(tuple(queries))


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
