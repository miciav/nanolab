"""Parsing the Engine's stats payload, which is where the arithmetic can go wrong."""

from __future__ import annotations

import json
from pathlib import Path

from sonata_tasks.loadtest.resources import (
    ResourceSample,
    ResourceWatcher,
    cpu_percent,
    working_set_bytes,
)


def _stats(usage: int, inactive: int, cpu: int, precpu: int, system: int, presystem: int) -> dict:
    return {
        "memory_stats": {
            "usage": usage,
            "limit": 536_870_912,
            "stats": {"inactive_file": inactive},
        },
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu},
            "system_cpu_usage": system,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": precpu},
            "system_cpu_usage": presystem,
        },
    }


def test_page_cache_is_not_counted_as_memory_the_container_needs() -> None:
    """Raw usage counts the page cache, so a container that has merely read its
    own jar looks like one that needs the memory. Kubernetes decides OOM kills
    on the working set, so that is the honest number."""
    stats = _stats(usage=200 * 1024**2, inactive=120 * 1024**2, cpu=0, precpu=0, system=0, presystem=0)

    assert working_set_bytes(stats) == 80 * 1024**2


def test_cgroup_v1_naming_is_understood_too() -> None:
    stats = {"memory_stats": {"usage": 100, "stats": {"total_inactive_file": 40}}}

    assert working_set_bytes(stats) == 60


def test_cpu_is_a_share_of_the_interval_not_a_running_total() -> None:
    """The Engine reports cumulative nanoseconds plus the previous read, so the
    interval travels with the sample and no state is kept here. Four cores fully
    busy reads as 400%."""
    stats = _stats(usage=0, inactive=0, cpu=400, precpu=0, system=1000, presystem=0)

    assert cpu_percent(stats) == 160.0


def test_the_first_reading_of_a_container_reports_no_cpu_rather_than_a_spike() -> None:
    """There is no predecessor to subtract, and inventing one would put a false
    spike at the start of every series."""
    stats = {"cpu_stats": {"cpu_usage": {"total_usage": 500}}, "precpu_stats": {}}

    assert cpu_percent(stats) == 0.0


def test_the_series_is_written_even_when_some_snapshots_failed(tmp_path: Path) -> None:
    """A failed snapshot costs one reading; the run's other readings are still
    the record."""
    watcher = ResourceWatcher.__new__(ResourceWatcher)
    watcher._samples = [  # type: ignore[attr-defined]
        ResourceSample(
            elapsed_seconds=1.0,
            container="nanofaas-word-stats-java-r1",
            memory_bytes=199_229_440.0,
            memory_limit_bytes=536_870_912.0,
            cpu_percent=210.5,
        )
    ]
    watcher.errors = ["docker engine stats failed: timeout"]
    path = tmp_path / "nested" / "resource-series.json"

    watcher.write_series(path)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["errors"] == ["docker engine stats failed: timeout"]
    assert written["samples"][0]["container"] == "nanofaas-word-stats-java-r1"
    assert written["samples"][0]["cpu_percent"] == 210.5
