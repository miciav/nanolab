"""The report has to survive the data a real run produces, not an idealised one."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sonata_tasks.loadtest.report import (
    ReportPhase,
    WriteConcurrencyReport,
    counter_delta,
)

PHASES = (
    ReportPhase("A alone", 0, 20),
    ReportPhase("antiphase", 20, 40),
)


def _write_series(directory: Path, function: str, depths: list[int]) -> None:
    samples = [
        {
            "elapsed_seconds": float(index * 10),
            "effective_concurrency": 8 - (index % 3),
            "in_flight": 8,
            # A real series carries nulls: an interval where nothing completed
            # has no service time to report, and the report must not choke on it.
            "mean_latency_ms": None if index == 0 else 4.0 + index,
            "throughput_rps": None if index == 0 else 1200.0,
            "queue_depth": depth,
            "mean_queue_wait_ms": None if index == 0 else 30.0 + index,
            "rejected": float(index * 7),
        }
        for index, depth in enumerate(depths)
    ]
    (directory / f"concurrency-series-{function}.json").write_text(
        json.dumps({"function": function, "dip": None, "errors": [], "samples": samples}),
        encoding="utf-8",
    )


def test_the_report_is_one_self_contained_page(tmp_path: Path) -> None:
    """Inlined rather than linked to a CDN: a record that needs the network to
    draw itself has stopped being a record."""
    _write_series(tmp_path, "alpha", [0, 40, 97, 12, 88])
    _write_series(tmp_path, "beta", [0, 5, 60, 99, 20])
    (tmp_path / "k6-summary.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "http_reqs": {"count": 1000, "rate": 12.5},
                    "http_req_failed": {"value": 0.02, "passes": 20},
                    "http_req_duration": {"avg": 40.0, "p(95)": 88.0},
                }
            }
        ),
        encoding="utf-8",
    )

    destination = WriteConcurrencyReport(
        task_id="",
        title="Concurrency governor — TEST",
        data_dir=tmp_path,
        output_dir=tmp_path,
        queue_size=100,
        phases=PHASES,
    ).run()

    page = destination.read_text(encoding="utf-8")
    assert destination.name == "concurrency-report.html"
    # Not a search for "cdn.plot.ly": the embedded bundle carries that string
    # itself as a config default, so the honest check is that nothing is FETCHED.
    assert not re.search(r'<script[^>]+src=["\']https?://', page), (
        "the library must be embedded, not fetched"
    )
    assert "Plotly.newPlot" in page
    # Both functions and every section reached the page.
    for expected in ("alpha", "beta", "By phase", "Load generator", "antiphase"):
        assert expected in page


def test_a_run_without_series_says_so_instead_of_drawing_nothing(tmp_path: Path) -> None:
    """An empty page would be indistinguishable from a run where the governor
    genuinely did nothing."""
    with pytest.raises(FileNotFoundError, match="nothing to report"):
        WriteConcurrencyReport(
            task_id="", title="t", data_dir=tmp_path, output_dir=tmp_path
        ).run()


def test_a_counter_that_restarted_mid_window_is_not_reported_as_negative() -> None:
    """Prometheus keeps its volume across a redeploy, so a window can open on the
    previous run's value and close on the new process's. Last-minus-first called
    that -10,386 requests served."""
    points = [{"value": v} for v in (500.0, 520_655.0, 0.0, 300.0, 510_269.0)]

    assert counter_delta(points) == pytest.approx(520_155.0 + 510_269.0)


def test_the_whole_run_is_one_phase_when_none_are_given(tmp_path: Path) -> None:
    _write_series(tmp_path, "alpha", [0, 40, 97])

    page = (
        WriteConcurrencyReport(task_id="", title="t", data_dir=tmp_path, output_dir=tmp_path)
        .run()
        .read_text(encoding="utf-8")
    )

    assert "whole run" in page
