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
        # The same admissions and refusals as above, but split by the door they came
        # in by. Separate series rather than a label on the counters above, because
        # five of those feed control loops - the concurrency governor reads
        # function_latency_ms and function_e2e_latency_ms, the autoscaler
        # function_dispatch_total, the Kubernetes HPA function_inFlight and
        # function_queue_depth - and splitting a series a control loop reads changes
        # what that loop sees.
        *(
            PrometheusQuery(
                f"function_{outcome}_{path}",
                f'function_{outcome}_total{{function="{function_name}",path="{path}"}}',
            )
            for outcome in ("admitted", "refused", "replayed")
            for path in ("sync", "async")
        ),
        # Whether the process measured at the end is the one measured at the start.
        #
        # It was not, on 2026-08-23: under the mixed workload at 3x the liveness probe
        # (timeoutSeconds 1, failureThreshold 3) stopped answering within its second,
        # kubelet killed the container, and the control plane came back with an empty
        # in-memory registry. Every request after that was a 404, for 35 minutes, and
        # nothing in the snapshot said so - the counters simply restarted from zero and
        # a delta read across the window looked merely small.
        #
        # A gauge that only rises, except across a restart, where it falls to nearly
        # nothing. Cheap, and it turns a forensic hunt into a line on a chart.
        # Vincolata al control plane: senza il selettore la query risponde anche per
        # le funzioni e per la seconda porta dello stesso processo, e il lettore
        # prende la prima serie che arriva - che fra due letture puo' non essere la
        # stessa. La mia guardia dal vivo ha suonato un falso allarme esattamente
        # cosi', leggendo 952 s poi 576 s da due processi diversi.
        PrometheusQuery(
            "process_uptime_seconds",
            'max(process_uptime_seconds{app="nanofaas-control-plane"})',
            True,
        ),
        # Quanti record il control plane sta ricordando.
        #
        # La ritenzione delle esecuzioni e' dichiarata nel tempo e illimitata nello
        # spazio, quindi la memoria richiesta e' proporzionale al tasso di arrivo.
        # Senza questa serie, "l'heap sale di 2,79 MB/s" resta un fatto senza un
        # colpevole: si sa che qualcosa cresce, non quale struttura la tiene.
        PrometheusQuery("execution_store_size", "execution_store_size", True),
        # La META' dell'altra mappa. Il rilascio 0.19.0 spezza ExecutionStore in
        # due: gli esiti compatti, che `max-outcomes` limita in NUMERO, e le
        # esecuzioni ancora in volo, che nessun tetto limita. Chiedere solo la
        # prima misurerebbe il tetto e non la memoria: se sotto il tetto la
        # memoria non scende, e' questa serie a dire perche'.
        PrometheusQuery("execution_in_flight_records", "execution_in_flight_records", True),
        # Keys held. Their lifetime is derived from the execution retention, so this is
        # the only reading that says what that derivation costs: at 2x with 5% keyed
        # arrivals a run files roughly 19,000, and whether they are released on
        # schedule is otherwise invisible until the heap says so.
        PrometheusQuery("idempotency_keys_held", "idempotency_keys_held"),
        PrometheusQuery("function_cold_start_total", f"function_cold_start_total{function}"),
        # Not per-function: both are process-wide, and both answer the question
        # the retention rewrite exists for. The store used to be declared in time
        # and unbounded in space, which under sustained load produced a live set
        # the size of the tenured generation - so how many records it actually
        # holds is the reading that says whether that is still true. The key
        # count belongs beside it: a key outliving its record duplicates an
        # execution, and a key store that empties early does the same.
        PrometheusQuery("execution_store_size", f"execution_store_size{CONTROL_PLANE_SELECTOR}"),
        PrometheusQuery(
            "execution_in_flight_records",
            f"execution_in_flight_records{CONTROL_PLANE_SELECTOR}",
        ),
        PrometheusQuery(
            "idempotency_keys_held", f"idempotency_keys_held{CONTROL_PLANE_SELECTOR}"
        ),
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


def runtime_queries(_function_name: str, *, heap_required: bool = True) -> Queries:
    """The process itself, published by Spring rather than by nanoFaaS.

    `heap_required` is a property of the run, not of the metric: measured here,
    the JVM and both serial-collector native builds publish
    `jvm_memory_used_bytes` for heap pools, and the G1 build publishes none of it
    — SubstrateVM registers no heap MemoryPoolMXBean under G1, so the series does
    not exist. An earlier version of this docstring named that trap and left the
    flag on anyway. The matrix then failed on its first G1 cell with "required
    query 'jvm_heap_used_bytes' returned no data", which was the truth and not a
    fault.

    One flag used to govern `process_cpu_usage` too, on the belief that G1
    registers no MXBeans at all. It is not so: the v0.20.0 release, whose control
    plane is the G1 native build, published process_cpu_usage throughout while
    only the heap series was missing — nanoFaaS registers ProcessorMetrics for
    native images on purpose. Accepting its absence bought nothing and cost the
    one reading that distinguishes a broken actuator scrape from a collector that
    keeps no pools, so it stays required on every run.
    """
    return (
        PrometheusQuery(
            "process_cpu_usage", f"process_cpu_usage{CONTROL_PLANE_SELECTOR}", True
        ),
        PrometheusQuery(
            "jvm_heap_used_bytes",
            'jvm_memory_used_bytes{app="nanofaas-control-plane",area="heap"}',
            heap_required,
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
        # Whether the build can see its own collector AT ALL, asked as presence
        # rather than as a value: the gauge always reads 1.0 and carries its
        # answer in a tag, which the snapshot's per-timestamp sum would erase.
        # Pinning source="mxbean" turns the tag into the presence of a series.
        #
        # This exists because the previous campaign left the GC row of the native
        # G1 build empty and could not say whether that meant "no collections" or
        # "no instrument". Silence now has a witness.
        PrometheusQuery(
            "gc_metrics_source_mxbean",
            "nanofaas_gc_metrics_source"
            + CONTROL_PLANE_SELECTOR.replace("}", ',source="mxbean"}'),
        ),
        # Native G1 registers no collector MXBean, so its pauses are read from
        # JFR VM operations instead. Two queries on purpose: the total says
        # whether the JFR stream is alive, the filtered one says how much of it
        # was collection. Both may legitimately answer nothing - on a JVM because
        # the stream is never opened, on the filtered one because SubstrateVM's
        # operation names may not match. An empty filtered query beside a
        # non-empty total is a reading about the names, not about the collector.
        PrometheusQuery(
            "jfr_vm_operation_count",
            f"nanofaas_jfr_vm_operation_count_total{CONTROL_PLANE_SELECTOR}",
        ),
        PrometheusQuery(
            "jfr_gc_operation_time",
            "nanofaas_jfr_vm_operation_time_seconds_total"
            + CONTROL_PLANE_SELECTOR.replace(
                "}", ',operation=~".*(G|g)arbage.*|.*GC.*|.*[Cc]ollect.*"}'
            ),
        ),
        # The same counters split by generation, because the snapshot sums a
        # query's series together: `_merge_samples` adds every label dimension at
        # each timestamp, so the aggregate above is Copy plus MarkSweepCompact
        # with no way back. That difference is the whole question when a smaller
        # heap makes a build faster - fewer young collections and fewer full ones
        # mean opposite things.
        #
        # The names are the MXBean's, per collector, and they are NOT the same on
        # every VM: HotSpot serial calls them Copy and MarkSweepCompact, G1 calls
        # them G1 Young Generation and G1 Old/Concurrent, and SubstrateVM's serial
        # collector calls them "young generation scavenger" and "complete
        # scavenger". Read off a running 0.19.0 native image on 2026-08-26; the
        # first version of these queries knew only the HotSpot names, so the
        # per-generation split came back empty for every native build and the
        # August campaign could not say whether a smaller heap meant fewer young
        # collections or fewer full ones - which is the whole question there.
        #
        # Matching on names rather than asking for `by (gc)` keeps one series per
        # query, which is what a snapshot entry holds.
        PrometheusQuery(
            "jvm_gc_young_count",
            "jvm_gc_collection_count_total"
            + CONTROL_PLANE_SELECTOR.replace("}", ',gc=~"Copy|PS Scavenge|G1 Young Generation|young generation scavenger"}'),
        ),
        PrometheusQuery(
            "jvm_gc_full_count",
            "jvm_gc_collection_count_total"
            + CONTROL_PLANE_SELECTOR.replace(
                "}", ',gc=~"MarkSweepCompact|PS MarkSweep|G1 Old Generation|G1 Concurrent GC|complete scavenger"}'
            ),
        ),
        PrometheusQuery(
            "jvm_gc_young_time",
            "jvm_gc_collection_time_seconds_total"
            + CONTROL_PLANE_SELECTOR.replace("}", ',gc=~"Copy|PS Scavenge|G1 Young Generation|young generation scavenger"}'),
        ),
        PrometheusQuery(
            "jvm_gc_full_time",
            "jvm_gc_collection_time_seconds_total"
            + CONTROL_PLANE_SELECTOR.replace(
                "}", ',gc=~"MarkSweepCompact|PS MarkSweep|G1 Old Generation|G1 Concurrent GC|complete scavenger"}'
            ),
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
            'jvm_threads_states_threads{app="nanofaas-control-plane",state="runnable"}',
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
            'sum(http_server_requests_seconds_count{app="nanofaas-control-plane",status="200"})',
        ),
        PrometheusQuery(
            "http_server_ok_sum",
            'sum(http_server_requests_seconds_sum{app="nanofaas-control-plane",status="200"})',
        ),
        PrometheusQuery(
            "http_server_rejected_count",
            'sum(http_server_requests_seconds_count{app="nanofaas-control-plane",status="429"})',
        ),
        PrometheusQuery(
            "http_server_rejected_sum",
            'sum(http_server_requests_seconds_sum{app="nanofaas-control-plane",status="429"})',
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
        # How much of that one backlog is work nobody is waiting for. The depth above
        # is a mixture whenever both doors are used: with sync-queue off the sync path
        # calls the same enqueue the async path does, so one FunctionQueueState holds
        # both. These two say which is which.
        PrometheusQuery(
            "function_queue_depth_sync",
            f'function_queue_depth_by_path{{function="{function_name}",path="sync"}}',
        ),
        PrometheusQuery(
            "function_queue_depth_async",
            f'function_queue_depth_by_path{{function="{function_name}",path="async"}}',
        ),
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
            "function_dispatch_slot_hold_seconds_total",
            f"function_dispatch_slot_hold_seconds_total{function}",
        ),
        PrometheusQuery(
            "function_dispatch_slot_hold_events_total",
            f"function_dispatch_slot_hold_events_total{function}",
        ),
        PrometheusQuery(
            "function_dispatch_slot_hold_distribution_series",
            'count({__name__=~"function_dispatch_slot_hold_.*(_max|_bucket)",'
            f'function="{function_name}"}} or '
            '{__name__=~"function_dispatch_slot_hold_.*",'
            f'function="{function_name}",quantile=~".+"}}) or vector(0)',
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
_SUBSTRATE_G1_NO_GC_MXBEAN = "SubstrateVM under G1 registers no GarbageCollectorMXBean"

_MAY_BE_ABSENT: Mapping[str, str] = {
    "jvm_gc_collection_count": _SUBSTRATE_G1_NO_GC_MXBEAN,
    "jvm_gc_collection_time": _SUBSTRATE_G1_NO_GC_MXBEAN,
    # Same VM, same reason, and they were left out of this list by oversight: a
    # split of counters that do not exist cannot exist either. Declaring the
    # aggregates absent-able while requiring the per-generation split made the
    # 0.19.0 campaign fail on its second cell, after the native G1 build had run
    # its whole load test, with "required query returned no data".
    #
    # Silence here is no longer ambiguous the way it was in August: whether a
    # build can see its own collector at all is now answered by
    # gc_metrics_source_mxbean, and native G1 publishes its pauses through JFR.
    "jvm_gc_young_count": _SUBSTRATE_G1_NO_GC_MXBEAN,
    "jvm_gc_young_time": _SUBSTRATE_G1_NO_GC_MXBEAN,
    "jvm_gc_full_count": _SUBSTRATE_G1_NO_GC_MXBEAN,
    "jvm_gc_full_time": _SUBSTRATE_G1_NO_GC_MXBEAN,
    "jvm_gc_pause_count": "Micrometer's notification binder does not exist on a native image",
    "jvm_gc_pause_sum": "Micrometer's notification binder does not exist on a native image",
    "jvm_gc_time_fraction": "polled binder, absent when the collector publishes no MXBean",
    # Spring registers http_server_requests per observed outcome, so the
    # status="429" series does not exist until something has actually been
    # refused. Requiring it made the run fail precisely when the control plane
    # rejected nothing - the best possible result read as a missing measurement.
    # A1b died that way on its fourteenth cell of fifteen, on jvm-c2 at two
    # cores, after two hours and fifty minutes.
    #
    # Absence here is a reading: nobody got a 429. It is not a blind spot either,
    # because http_server_ok_count stays required - if the whole
    # http_server_requests family were missing, that is what would fail.
    "http_server_rejected_count": "no 429 was served, so Spring never created the series",
    "http_server_rejected_sum": "no 429 was served, so Spring never created the series",
    "gc_metrics_source_mxbean": "absent IS the reading: the build has no usable collector MXBean",
    "jfr_vm_operation_count": "the JFR stream is opened only where the MXBeans cannot answer",
    "jfr_gc_operation_time": "no JFR stream, or SubstrateVM names its GC operation otherwise",
    # jvm_heap_used_bytes is governed by the run instead, through
    # runtime_queries(heap_required=...): absent means the build's collector keeps
    # no heap pools, which is what G1 does under SubstrateVM. process_cpu_usage is
    # not on this list and is required everywhere: every build measured publishes
    # it, so its absence only ever means the actuator scrape is broken.
    "jvm_heap_used_bytes": "governed per run by heap_metrics_required",
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
    heap_metrics_required: bool = True,
) -> Queries:
    """Everything this run can meaningfully be asked, and nothing it cannot.

    Unknown module names are ignored rather than rejected: nanolab selects
    modules by string and a platform may grow one before this catalogue learns
    about it. The coverage test is what turns that silence into a failure, at a
    moment when someone can act on it.
    """
    selected = tuple(modules)
    queries = list(core_queries(function_name)) + list(
        runtime_queries(function_name, heap_required=heap_metrics_required)
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
        # The co-tenant is driven through both doors too, so without these a mixed run
        # sees which door displaced which on one function and nothing on the other.
        # The queue depths arrive by the module loop below; these do not, because they
        # are core meters and this list is written out rather than derived.
        *(
            PrometheusQuery(
                f"function_{outcome}_{path}@{neighbour}",
                f'function_{outcome}_total{{function="{neighbour}",path="{path}"}}',
            )
            for outcome in ("admitted", "refused", "replayed")
            for path in ("sync", "async")
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
