from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonata_tasks.loadtest.comparison_report import (
    WriteComparisonReport,
    _spread,
    _verdict,
    aggregate_table,
    read_cell,
)


def _k6(rate: float, p95: float, failed: float = 0.0) -> dict:
    return {
        "metrics": {
            "http_reqs": {"count": rate * 100, "rate": rate},
            "http_req_duration": {"p(50)": 1.0, "p(95)": p95, "p(99)": p95 * 2},
            "http_req_failed": {"value": failed},
        }
    }


def _snapshot(memory_mib: list[float]) -> dict:
    return {
        "start": "2026-08-17T09:00:00+00:00",
        "end": "2026-08-17T09:07:30+00:00",
        "queries": {
            "container_memory_bytes@control-plane": {
                "query": "…",
                "points": [
                    {
                        "timestamp": f"2026-08-17T09:0{i}:00+00:00",
                        "value": value * 1024 * 1024,
                    }
                    for i, value in enumerate(memory_mib)
                ],
            }
        },
    }


def _matrix(root: Path, data: dict[str, list[tuple[float, float, list[float]]]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "comparison-manifest.json").write_text(
        json.dumps(
            {
                "repetitions": max(len(runs) for runs in data.values()),
                "functions": ["word-stats-java", "word-stats-javascript"],
                "order": [],
                "variants": [
                    {"key": key, "label": key.upper(), "rationale": "because", "build_env": {}, "image": "i"}
                    for key in data
                ],
            }
        ),
        encoding="utf-8",
    )
    for variant, runs in data.items():
        for index, (rate, p95, memory) in enumerate(runs, start=1):
            cell = root / variant / f"run-{index}"
            (cell / "metrics").mkdir(parents=True, exist_ok=True)
            (cell / "k6-summary.json").write_text(json.dumps(_k6(rate, p95)), encoding="utf-8")
            (cell / "metrics" / "prometheus-snapshot.json").write_text(
                json.dumps(_snapshot(memory)), encoding="utf-8"
            )


def test_spread_reports_half_range_not_standard_deviation() -> None:
    """Three samples do not make a standard deviation; the half-range says what it is."""
    mean, half = _spread([10.0, 20.0, 30.0])

    assert mean == 20.0
    assert half == 10.0


def test_a_single_run_has_no_spread() -> None:
    assert _spread([42.0]) == (42.0, 0.0)


def test_a_missing_cell_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    """A matrix that lost one run to a flaky VM is still worth reading."""
    _matrix(tmp_path, {"jvm": [(400.0, 100.0, [500.0])]})

    assert read_cell(tmp_path, "jvm", 2) is None
    assert read_cell(tmp_path, "jvm", 1) is not None


def test_peak_memory_comes_from_the_series_not_the_last_sample(tmp_path: Path) -> None:
    _matrix(tmp_path, {"jvm": [(400.0, 100.0, [200.0, 900.0, 300.0])]})
    cell = read_cell(tmp_path, "jvm", 1)

    assert cell is not None
    assert cell.peak("container_memory_bytes@control-plane") == 900.0 * 1024 * 1024


def test_a_gap_inside_the_spread_is_reported_as_undecided(tmp_path: Path) -> None:
    """Overlapping runs mean three repetitions did not separate the builds.

    Reporting the higher mean as a winner would present noise as a result.
    """
    _matrix(
        tmp_path,
        {
            "jvm": [(400.0, 100.0, [500.0]), (500.0, 100.0, [500.0])],
            "native": [(420.0, 100.0, [40.0]), (520.0, 100.0, [40.0])],
        },
    )
    cells = [c for v in ("jvm", "native") for r in (1, 2) if (c := read_cell(tmp_path, v, r))]

    verdict = _verdict(cells, {"jvm": "JVM", "native": "NATIVE"}, "rps", lower_is_better=False)

    assert "did not separate them" in verdict


def test_a_gap_beyond_the_spread_is_reported_as_a_result(tmp_path: Path) -> None:
    _matrix(
        tmp_path,
        {
            "jvm": [(400.0, 100.0, [500.0]), (410.0, 100.0, [500.0])],
            "native": [(900.0, 100.0, [40.0]), (910.0, 100.0, [40.0])],
        },
    )
    cells = [c for v in ("jvm", "native") for r in (1, 2) if (c := read_cell(tmp_path, v, r))]

    verdict = _verdict(cells, {"jvm": "JVM", "native": "NATIVE"}, "rps", lower_is_better=False)

    assert "more than either build's own spread" in verdict
    assert "NATIVE" in verdict


def test_lower_is_better_flips_the_ranking(tmp_path: Path) -> None:
    """Latency and throughput cannot share a comparison direction."""
    _matrix(
        tmp_path,
        {
            "jvm": [(400.0, 300.0, [500.0]), (400.0, 310.0, [500.0])],
            "native": [(400.0, 20.0, [40.0]), (400.0, 25.0, [40.0])],
        },
    )
    cells = [c for v in ("jvm", "native") for r in (1, 2) if (c := read_cell(tmp_path, v, r))]

    assert "NATIVE" in _verdict(cells, {"jvm": "JVM", "native": "NATIVE"}, "p95_ms", True)


def test_aggregate_carries_every_headline_with_its_spread(tmp_path: Path) -> None:
    _matrix(tmp_path, {"jvm": [(400.0, 100.0, [500.0]), (500.0, 120.0, [700.0])]})
    cells = [c for r in (1, 2) if (c := read_cell(tmp_path, "jvm", r))]

    table = aggregate_table(cells, {"jvm": "JVM"})

    assert table.loc[0, "Throughput (rps)"] == "450.0 ± 50.0"
    assert table.loc[0, "Control plane peak (MiB)"] == "600.0 ± 100.0"


def test_a_run_without_container_metrics_reads_as_missing_not_zero(tmp_path: Path) -> None:
    """Zero would claim the control plane used no memory; it means nobody asked."""
    _matrix(tmp_path, {"jvm": [(400.0, 100.0, [500.0])]})
    (tmp_path / "jvm" / "run-1" / "metrics" / "prometheus-snapshot.json").write_text(
        json.dumps({"start": "2026-08-17T09:00:00+00:00", "end": "…", "queries": {}}),
        encoding="utf-8",
    )
    cells = [c for r in (1,) if (c := read_cell(tmp_path, "jvm", r))]

    assert aggregate_table(cells, {"jvm": "JVM"}).loc[0, "Control plane peak (MiB)"] == "—"


def test_the_page_is_self_contained(tmp_path: Path) -> None:
    """No CDN: these pages are read from a run directory, often offline."""
    _matrix(
        tmp_path,
        {
            "jvm": [(400.0, 100.0, [500.0])],
            "native": [(900.0, 20.0, [40.0])],
        },
    )

    output = WriteComparisonReport(task_id="", title="Comparison", root=tmp_path).run()
    html = output.read_text(encoding="utf-8")

    assert "<script src=\"http" not in html
    assert "JVM" in html and "NATIVE" in html


def test_an_empty_matrix_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no comparison manifest"):
        WriteComparisonReport(task_id="", title="Comparison", root=tmp_path).run()
