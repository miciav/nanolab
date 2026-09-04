"""The catalogue must not fall behind the platform it observes.

A load test that does not ask for a metric records a zero, and a zero is
indistinguishable from "it did not happen". That cost a real reading: a nine-cell
comparison reported the platform refusing nothing while `sync_queue_rejected_total`
stood at 29,555, because the harness only knew `async-queue`'s counters.

So this test reads the platform's own sources — every `Counter/Gauge/Timer.builder`
name, per module — and fails when one appears that the catalogue neither collects
nor explicitly declines.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from nanolab.metrics.catalogue import (
    MODULE_QUERIES,
    MODULES_WITHOUT_QUERIES,
    core_queries,
    queries_for,
    runtime_queries,
)

_METRIC_BUILDER = re.compile(r'(?:Counter|Gauge|Timer|DistributionSummary)\.builder\(\s*"([a-z_0-9]+)"')
_COUNTER_HELPER = re.compile(r'counter\(\s*\w+,\s*"([a-z_0-9]+)"')
# Enough of the call to know how Prometheus will name the result: the builder
# type decides the suffix and `baseUnit` inserts a unit before it.
_TYPED_BUILDER = re.compile(
    r'(Counter|FunctionCounter|Gauge|Timer|DistributionSummary)\.builder\('
    r'\s*"([a-z_0-9]+)"(?P<tail>.{0,400}?)\.register\(',
    re.DOTALL,
)
_BASE_UNIT = re.compile(r'\.baseUnit\(\s*"([a-z]+)"\s*\)')


def exported_name(kind: str, name: str, base_unit: str | None) -> tuple[str, ...]:
    """What Prometheus will call a meter, which is not what the code called it.

    Micrometer renames on export and the difference is invisible in the source:
    a counter gains `_total`, a declared base unit is inserted before it, and a
    timer becomes a `_seconds_count`/`_seconds_sum` pair. Querying the registered
    name returns nothing at all — silently, because an empty series is
    indistinguishable from an idle one.
    """
    def suffixed(stem: str, suffix: str) -> str:
        # Verified against a live /actuator/prometheus: a name that already ends
        # in the suffix keeps it once. `function_dispatch_total` is served as
        # itself, not `function_dispatch_total_total`, and the Timer
        # `sync_queue_wait_seconds` becomes `sync_queue_wait_seconds_count`.
        return stem if stem.endswith(suffix) else f"{stem}{suffix}"

    stem = suffixed(name, f"_{base_unit}") if base_unit else name
    if kind in ("Counter", "FunctionCounter"):
        return (suffixed(stem, "_total"),)
    if kind == "Timer":
        timer = suffixed(name, "_seconds")
        return (f"{timer}_count", f"{timer}_sum")
    if kind == "DistributionSummary":
        return (f"{stem}_count", f"{stem}_sum")
    return (name,)


def _exported_metrics(source_dir: Path) -> dict[str, tuple[str, ...]]:
    """Registered name -> the names Prometheus will actually serve."""
    exported: dict[str, tuple[str, ...]] = {}
    for path in source_dir.rglob("*.java"):
        if "/build/" in str(path) or "/test/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _TYPED_BUILDER.finditer(text):
            kind, name, tail = match.group(1), match.group(2), match.group("tail")
            unit = _BASE_UNIT.search(tail)
            exported[name] = exported_name(kind, name, unit.group(1) if unit else None)
    return exported

# Names the catalogue deliberately does not collect in a run snapshot, with the
# reason. Anything not listed here and not queried is a gap, not a decision.
_NOT_COLLECTED: dict[str, str] = {
    # Sampled continuously by the concurrency watcher: a post-run snapshot would
    # record the limit the governor settled on and lose the trajectory.
    "function_target_inflight_per_pod": "sampled by the concurrency watcher",
    "function_concurrency_controller_mode": "sampled by the concurrency watcher",
    # The admin API's own behaviour, not the platform's under load.
    "controlplane_runtime_config_revision": "admin API, not a load reading",
    "controlplane_runtime_config_updates_total": "admin API, not a load reading",
    "controlplane_runtime_config_apply_duration_seconds": "admin API, not a load reading",
    # Published by the HPA-facing adapter, read through kube-state-metrics instead.
    "gateway_service_target_load": "read via kube-state-metrics",
    # An info gauge that is always 1; it labels the profile rather than measuring it.
    "nanofaas_metrics_profile_info": "an info gauge, not a measurement",
    # Registered on a private SimpleMeterRegistry that Prometheus never scrapes:
    # a sink for the meters of deregistered functions, so their samples stop
    # accumulating without the timers becoming null at every call site.
    "removed_function_latency_ms": "private registry, never scraped",
    "removed_function_init_duration_ms": "private registry, never scraped",
    "removed_function_queue_wait_ms": "private registry, never scraped",
    "removed_function_e2e_latency_ms": "private registry, never scraped",
}


def _nanofaas_root() -> Path:
    root = os.environ.get("NANOFAAS_ROOT", "")
    if not root:  # pragma: no cover - conftest already requires it
        pytest.skip("NANOFAAS_ROOT is not set")
    return Path(root)


def _published_metrics(source_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in source_dir.rglob("*.java"):
        if "/build/" in str(path) or "/test/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        names.update(_METRIC_BUILDER.findall(text))
        names.update(_COUNTER_HELPER.findall(text))
    return names


def _queried_names(function: str, modules: tuple[str, ...]) -> set[str]:
    """Every metric name the catalogue would ask Prometheus about."""
    queries = queries_for(function, modules=modules, hpa=True)
    return {name for query in queries for name in re.findall(r"[a-z_0-9]+", query.expr)}


def test_every_module_is_classified() -> None:
    """A module is either queried or explicitly declared as publishing nothing.

    Silence here is how `sync-queue` went unobserved for its whole existence.
    """
    modules_dir = _nanofaas_root() / "platform" / "modules"
    present = {
        child.name
        for child in modules_dir.iterdir()
        if child.is_dir() and (child / "build.gradle").is_file()
    }
    classified = set(MODULE_QUERIES) | set(MODULES_WITHOUT_QUERIES)

    assert present <= classified, (
        "these control-plane modules are neither queried nor declared as "
        f"publishing nothing: {sorted(present - classified)}"
    )


@pytest.mark.parametrize("module", sorted(MODULE_QUERIES))
def test_a_queried_module_has_every_metric_it_publishes_collected(module: str) -> None:
    published = _published_metrics(_nanofaas_root() / "platform" / "modules" / module / "src" / "main")
    queried = _queried_names("word-stats-java", (module,))
    missing = {
        name
        for name in published
        if name not in _NOT_COLLECTED and not any(name in q for q in queried)
    }

    assert not missing, f"{module} publishes metrics the catalogue never asks for: {sorted(missing)}"


def test_the_core_metrics_are_collected() -> None:
    published = _published_metrics(_nanofaas_root() / "platform" / "control-plane" / "src" / "main")
    catalogue = core_queries("word-stats-java") + runtime_queries("word-stats-java")
    queried = {n for q in catalogue for n in re.findall(r"[a-z_0-9]+", q.expr)}
    # Core timers are exported by Micrometer with _seconds_count/_sum suffixes,
    # so match on the stem rather than the literal registered name.
    missing = {
        name
        for name in published
        if name not in _NOT_COLLECTED
        and not any(name in q or q.startswith(name) for q in queried)
    }

    assert not missing, f"the core publishes metrics nothing collects: {sorted(missing)}"


def test_async_queue_snapshot_collects_dispatch_capacity() -> None:
    queries = {
        query.name: query.expr
        for query in queries_for("word-stats-java", modules=("async-queue",))
    }

    assert queries["function_inFlight"] == 'function_inFlight{function="word-stats-java"}'
    assert queries["function_effective_concurrency"] == (
        'function_effective_concurrency{function="word-stats-java"}'
    )


def test_async_queue_snapshot_collects_dispatch_diagnostics() -> None:
    queries = {
        query.name: query.expr
        for query in queries_for("word-stats-java", modules=("async-queue",))
    }
    function = '{function="word-stats-java"}'

    assert queries["function_dispatchable_backlog"] == f"function_dispatchable_backlog{function}"
    assert queries["function_queue_offer_duration_count"] == (
        f"function_queue_offer_duration_seconds_count{function}"
    )
    assert queries["function_queue_offer_duration_sum"] == (
        f"function_queue_offer_duration_seconds_sum{function}"
    )
    assert queries["function_queue_poll_duration_count"] == (
        f"function_queue_poll_duration_seconds_count{function}"
    )
    assert queries["function_queue_poll_duration_sum"] == (
        f"function_queue_poll_duration_seconds_sum{function}"
    )
    assert queries["function_scheduler_wakeup_delay_count"] == (
        f"function_scheduler_wakeup_delay_seconds_count{function}"
    )
    assert queries["function_scheduler_wakeup_delay_sum"] == (
        f"function_scheduler_wakeup_delay_seconds_sum{function}"
    )
    assert queries["function_scheduler_poll_delay_count"] == (
        f"function_scheduler_poll_delay_seconds_count{function}"
    )
    assert queries["function_scheduler_poll_delay_sum"] == (
        f"function_scheduler_poll_delay_seconds_sum{function}"
    )
    assert queries["function_scheduler_activation_bookkeeping_duration_count"] == (
        f"function_scheduler_activation_bookkeeping_duration_seconds_count{function}"
    )
    assert queries["function_scheduler_activation_bookkeeping_duration_sum"] == (
        f"function_scheduler_activation_bookkeeping_duration_seconds_sum{function}"
    )
    assert queries["function_scheduler_signal_enqueue_duration_count"] == (
        f"function_scheduler_signal_enqueue_duration_seconds_count{function}"
    )
    assert queries["function_scheduler_signal_enqueue_duration_sum"] == (
        f"function_scheduler_signal_enqueue_duration_seconds_sum{function}"
    )
    assert queries["function_scheduler_dispatch_submit_duration_count"] == (
        f"function_scheduler_dispatch_submit_duration_seconds_count{function}"
    )
    assert queries["function_scheduler_dispatch_submit_duration_sum"] == (
        f"function_scheduler_dispatch_submit_duration_seconds_sum{function}"
    )
    assert queries["function_scheduler_dispatch_submit_duration_all_count"] == (
        "sum(function_scheduler_dispatch_submit_duration_seconds_count)"
    )
    assert queries["function_scheduler_dispatch_submit_duration_all_sum"] == (
        "sum(function_scheduler_dispatch_submit_duration_seconds_sum)"
    )
    assert queries["function_dispatch_slot_hold_seconds_total"] == (
        f"function_dispatch_slot_hold_seconds_total{function}"
    )
    assert queries["function_dispatch_slot_hold_events_total"] == (
        f"function_dispatch_slot_hold_events_total{function}"
    )
    assert queries["function_dispatch_slot_hold_distribution_series"] == (
        'count({__name__=~"function_dispatch_slot_hold_.*(_max|_bucket)",'
        'function="word-stats-java"} or '
        '{__name__=~"function_dispatch_slot_hold_.*",'
        'function="word-stats-java",quantile=~".+"}) or vector(0)'
    )
    assert "function_dispatch_slot_hold_duration_count" not in queries
    assert "function_dispatch_slot_hold_duration_sum" not in queries
    # The four reacquisition series are gone on purpose: the probe was falsified
    # and the meters deleted from QueueManager, so asking for them collected an
    # empty series that reads as "it never happened".
    assert not [k for k in queries if "reacquisition" in k]
    assert queries["function_scheduler_batch_limit_total"] == (
        f"function_scheduler_batch_limit_total{function}"
    )
    assert queries["function_scheduler_slot_blocked_total"] == (
        f"function_scheduler_slot_blocked_total{function}"
    )
    assert queries["function_scheduler_slot_blocked_all_total"] == (
        "sum(function_scheduler_slot_blocked_total)"
    )
    assert queries["function_scheduler_signal_coalesced_total"] == (
        f"function_scheduler_signal_coalesced_total{function}"
    )
    assert queries["function_scheduler_signal_coalesced_all_total"] == (
        "sum(function_scheduler_signal_coalesced_total)"
    )


def test_queries_use_the_name_prometheus_serves_not_the_one_the_code_registers() -> None:
    """Micrometer renames on export, and the rename is invisible in the source.

    The first version of this catalogue asked for `jvm_gc_collection_count`, which
    Prometheus serves as `jvm_gc_collection_count_total`. Three GC metrics were
    requested and one arrived, and nothing said so: a query for a name that does
    not exist returns an empty series, exactly like a metric that never moved.
    """
    root = _nanofaas_root()
    exported = _exported_metrics(root / "platform" / "control-plane" / "src" / "main")
    for module in MODULE_QUERIES:
        exported |= _exported_metrics(root / "platform" / "modules" / module / "src" / "main")

    catalogue = queries_for("word-stats-java", modules=tuple(MODULE_QUERIES), hpa=True)
    expressions = " ".join(query.expr for query in catalogue)

    wrong = {
        registered: names
        for registered, names in exported.items()
        if registered not in _NOT_COLLECTED
        and any(name in expressions for name in names) is False
        and registered in expressions
    }

    assert not wrong, (
        "these queries use the registered name where Prometheus serves a "
        f"suffixed one: {sorted(wrong)}"
    )


def _required(queries, name: str) -> bool:
    query = next(query for query in queries if query.name == name)
    return query.required


def test_a_run_without_heap_pools_still_has_to_answer_for_its_cpu() -> None:
    """The two used to share one flag, so a release that could not publish heap
    pools also stopped requiring process_cpu_usage - and a broken actuator scrape
    would have read as a normal G1 run. Measured on the v0.20.0 release: the G1
    native control plane publishes process_cpu_usage and no heap series."""
    queries = queries_for("word-stats-java", modules=(), heap_metrics_required=False)

    assert _required(queries, "process_cpu_usage") is True
    assert _required(queries, "jvm_heap_used_bytes") is False


def test_a_build_that_keeps_heap_pools_is_held_to_them() -> None:
    queries = queries_for("word-stats-java", modules=(), heap_metrics_required=True)

    assert _required(queries, "process_cpu_usage") is True
    assert _required(queries, "jvm_heap_used_bytes") is True
