"""One page that puts every control-plane build next to every other.

Separate from `report.py`, which describes a single run: the questions differ.
A run report asks what happened; this asks whether the difference between two
builds is larger than the difference between two runs of the same build. With
three repetitions that question can be answered badly in one obvious way —
comparing means and calling the bigger one better — so every headline number here
is carried with its spread, and a gap that falls inside the spread is labelled as
such rather than reported as a finding.

Reads a matrix directory: `comparison-manifest.json` at the root, then
`<variant>/run-<n>/` for each cell. Missing cells are skipped rather than fatal,
because a matrix that lost one run to a flaky VM is still worth reading.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

from sonata_tasks.loadtest.report import _STYLE, _scalar, _table_html

_SNAPSHOT = "metrics/prometheus-snapshot.json"
_K6 = "k6-summary.json"
_PALETTE = ("#2563eb", "#db2777", "#059669", "#d97706", "#7c3aed", "#0891b2")
_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CellData:
    """One run of one build, reduced to what the comparison asks of it."""

    variant: str
    repetition: int
    requests: float
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    failed_rate: float
    series: dict[str, pd.DataFrame] = field(default_factory=dict)

    def peak(self, metric: str) -> float:
        frame = self.series.get(metric)
        return _scalar(frame["value"].max()) if frame is not None and not frame.empty else float("nan")

    def mean(self, metric: str) -> float:
        frame = self.series.get(metric)
        return _scalar(frame["value"].mean()) if frame is not None and not frame.empty else float("nan")


def _k6_values(summary: dict[str, Any], name: str) -> dict[str, Any]:
    entry = summary.get("metrics", {}).get(name, {})
    # k6 has moved this shape between versions: the values sat under a "values"
    # key in one and at the top level in another. Accept both rather than pin a
    # version, since a wrong guess here reports every metric as zero.
    return entry.get("values", entry) if isinstance(entry, dict) else {}


def _series(snapshot: dict[str, Any], name: str) -> pd.DataFrame:
    entry = snapshot.get("queries", {}).get(name)
    if not entry or not entry.get("points"):
        return pd.DataFrame(columns=["elapsed", "value"])
    start = datetime.fromisoformat(snapshot["start"])
    rows = [
        {
            "elapsed": (datetime.fromisoformat(point["timestamp"]) - start).total_seconds(),
            "value": float(point["value"]),
        }
        for point in entry["points"]
    ]
    return pd.DataFrame(rows)


def read_cell(root: Path, variant: str, repetition: int) -> CellData | None:
    """One cell, or None when it never produced a summary."""
    cell = root / variant / f"run-{repetition}"
    k6_path = cell / _K6
    if not k6_path.is_file():
        return None
    summary = json.loads(k6_path.read_text(encoding="utf-8"))
    reqs = _k6_values(summary, "http_reqs")
    duration = _k6_values(summary, "http_req_duration")
    failed = _k6_values(summary, "http_req_failed")

    snapshot: dict[str, Any] = {}
    snapshot_path = cell / _SNAPSHOT
    if snapshot_path.is_file():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    series = {
        name: _series(snapshot, name)
        for name in snapshot.get("queries", {})
        if name.startswith("container_")
    }
    return CellData(
        variant=variant,
        repetition=repetition,
        requests=float(reqs.get("count", 0.0)),
        rps=float(reqs.get("rate", 0.0)),
        p50_ms=float(duration.get("p(50)", duration.get("med", 0.0))),
        p95_ms=float(duration.get("p(95)", 0.0)),
        p99_ms=float(duration.get("p(99)", 0.0)),
        failed_rate=float(failed.get("value", 0.0)),
        series=series,
    )


def _spread(values: list[float]) -> tuple[float, float]:
    """Mean and the half-range, not the standard deviation.

    With three samples a standard deviation is barely a statistic, and reporting
    one invites it to be read as a confidence interval. The half-range says
    exactly what it is: how far the extremes of this build sat from its middle.
    """
    if not values:
        return float("nan"), float("nan")
    if len(values) == 1:
        return values[0], 0.0
    return statistics.fmean(values), (max(values) - min(values)) / 2


def _fmt(mean: float, half_range: float, digits: int = 1) -> str:
    if mean != mean:  # NaN
        return "—"
    return f"{mean:.{digits}f} ± {half_range:.{digits}f}"


def aggregate_table(cells: list[CellData], labels: dict[str, str]) -> pd.DataFrame:
    rows = []
    for variant in labels:
        runs = [cell for cell in cells if cell.variant == variant]
        if not runs:
            continue
        memory = [cell.peak("container_memory_bytes@control-plane") / _MIB for cell in runs]
        cpu = [cell.mean("container_cpu_cores@control-plane") for cell in runs]
        rows.append(
            {
                "Build": labels[variant],
                "Runs": len(runs),
                "Throughput (rps)": _fmt(*_spread([c.rps for c in runs])),
                "p50 (ms)": _fmt(*_spread([c.p50_ms for c in runs])),
                "p95 (ms)": _fmt(*_spread([c.p95_ms for c in runs])),
                "p99 (ms)": _fmt(*_spread([c.p99_ms for c in runs])),
                "Shed (%)": _fmt(*_spread([c.failed_rate * 100 for c in runs]), digits=2),
                "Control plane peak (MiB)": _fmt(*_spread(memory)),
                "Control plane CPU (cores)": _fmt(*_spread(cpu), digits=2),
            }
        )
    return pd.DataFrame(rows)


def _verdict(cells: list[CellData], labels: dict[str, str], metric: str, lower_is_better: bool) -> str:
    """Say whether the best build is actually distinguishable from the next one.

    The comparison that matters is not between two means but between the gap and
    the noise: if the best build's spread overlaps the runner-up's, three runs
    did not separate them, and saying so is the finding.
    """
    ranked = []
    single_run = False
    for variant in labels:
        values = [getattr(cell, metric) for cell in cells if cell.variant == variant]
        if values:
            single_run = single_run or len(values) == 1
            ranked.append((variant, min(values), max(values), statistics.fmean(values)))
    if len(ranked) < 2:
        return ""
    if single_run:
        # With one run per build the min equals the max, so the overlap test below
        # always reports a clean separation — it would turn a single sample into a
        # confident verdict. A build compared once has not been compared.
        return (
            "<p class='verdict warn'>At least one build has a single run, so nothing "
            "here separates a difference between builds from a difference between "
            "runs. Read the numbers, not a winner.</p>"
        )
    ranked.sort(key=lambda row: row[3], reverse=not lower_is_better)
    best, runner_up = ranked[0], ranked[1]
    overlaps = not (best[2] < runner_up[1] or runner_up[2] < best[1])
    if overlaps:
        return (
            f"<p class='verdict warn'><strong>{labels[best[0]]}</strong> leads on this metric, "
            f"but its runs overlap those of <strong>{labels[runner_up[0]]}</strong>: "
            "three repetitions did not separate them.</p>"
        )
    return (
        f"<p class='verdict'><strong>{labels[best[0]]}</strong> is ahead of "
        f"<strong>{labels[runner_up[0]]}</strong> by more than either build's own spread.</p>"
    )


def _over_time(
    cells: list[CellData], labels: dict[str, str], metric: str, title: str, y_title: str, scale: float = 1.0
) -> go.Figure:
    """One trace per build, every repetition drawn.

    Repetitions are drawn individually rather than averaged: an average of three
    runs hides whether they agreed, and agreement is most of what three runs are
    for. The first run of each build carries the legend entry so the chart does
    not grow twelve of them.
    """
    figure = go.Figure()
    for index, variant in enumerate(labels):
        colour = _PALETTE[index % len(_PALETTE)]
        first = True
        for cell in [c for c in cells if c.variant == variant]:
            frame = cell.series.get(metric)
            if frame is None or frame.empty:
                continue
            figure.add_trace(
                go.Scatter(
                    x=frame["elapsed"],
                    y=frame["value"] * scale,
                    name=labels[variant],
                    legendgroup=variant,
                    showlegend=first,
                    line={"color": colour, "width": 1.6},
                    opacity=1.0 if first else 0.55,
                    hovertemplate=f"{labels[variant]} run {cell.repetition}<br>%{{x:.0f}}s: %{{y:.1f}}<extra></extra>",
                )
            )
            first = False
    figure.update_layout(
        title=title,
        xaxis_title="seconds into the run",
        yaxis_title=y_title,
        template="plotly_white",
        height=420,
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
        legend={"orientation": "h", "y": -0.2},
    )
    return figure


def _spread_chart(cells: list[CellData], labels: dict[str, str]) -> go.Figure:
    """Every run as a point, so the reader sees the sample rather than a bar.

    A bar chart of three means would present the same data as if it were exact.
    """
    figure = go.Figure()
    for index, variant in enumerate(labels):
        runs = [cell for cell in cells if cell.variant == variant]
        if not runs:
            continue
        figure.add_trace(
            go.Scatter(
                x=[labels[variant]] * len(runs),
                y=[cell.rps for cell in runs],
                mode="markers",
                name=labels[variant],
                marker={"size": 13, "color": _PALETTE[index % len(_PALETTE)], "opacity": 0.8},
                text=[f"run {cell.repetition}" for cell in runs],
                hovertemplate="%{text}<br>%{y:.0f} rps<extra></extra>",
            )
        )
    figure.update_layout(
        title="Throughput, every run shown",
        yaxis_title="requests per second",
        template="plotly_white",
        height=380,
        showlegend=False,
        margin={"l": 60, "r": 30, "t": 60, "b": 50},
    )
    return figure


def _per_run_table(cells: list[CellData], labels: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Build": labels.get(cell.variant, cell.variant),
                "Run": cell.repetition,
                "Requests": f"{cell.requests:,.0f}",
                "rps": f"{cell.rps:.1f}",
                "p95 (ms)": f"{cell.p95_ms:.1f}",
                "Shed (%)": f"{cell.failed_rate * 100:.2f}",
                "CP peak (MiB)": f"{cell.peak('container_memory_bytes@control-plane') / _MIB:.1f}",
            }
            for cell in cells
        ]
    )


_EXTRA_STYLE = """
.verdict { margin: 0.4rem 0 1.4rem; padding: 0.7rem 1rem; border-left: 3px solid #059669;
           background: rgba(5, 150, 105, 0.07); border-radius: 0 6px 6px 0; }
.verdict.warn { border-left-color: #d97706; background: rgba(217, 119, 6, 0.08); }
"""


@dataclass
class WriteComparisonReport:
    """Renders a whole matrix into one self-contained page."""

    task_id: str
    title: str
    root: Path
    output_path: Path | None = None

    def run(self) -> Path:
        manifest_path = self.root / "comparison-manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"no comparison manifest under {self.root}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        labels = {entry["key"]: entry["label"] for entry in manifest["variants"]}
        repetitions = int(manifest["repetitions"])

        cells = [
            cell
            for variant in labels
            for repetition in range(1, repetitions + 1)
            if (cell := read_cell(self.root, variant, repetition)) is not None
        ]
        if not cells:
            raise FileNotFoundError(f"no completed runs under {self.root}")

        charts = [
            _spread_chart(cells, labels),
            _over_time(
                cells,
                labels,
                "container_memory_bytes@control-plane",
                "Control-plane memory over the run",
                "working set (MiB)",
                scale=1 / _MIB,
            ),
            _over_time(
                cells,
                labels,
                "container_cpu_cores@control-plane",
                "Control-plane CPU over the run",
                "cores",
            ),
        ]
        body = "".join(
            pio.to_html(figure, include_plotlyjs=(index == 0), full_html=False)
            for index, figure in enumerate(charts)
        )
        missing = len(labels) * repetitions - len(cells)
        note = (
            f"<p class='verdict warn'>{missing} of {len(labels) * repetitions} cells "
            "produced no summary and are absent from every figure.</p>"
            if missing
            else ""
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{self.title}</title>
<style>{_STYLE}{_EXTRA_STYLE}</style></head>
<body>
<h1>{self.title}</h1>
<p>{len(labels)} control-plane builds, {repetitions} runs each, one load profile.
Functions held fixed: {", ".join(manifest["functions"])}.</p>
{note}
{_verdict(cells, labels, "rps", lower_is_better=False)}
{_verdict(cells, labels, "p95_ms", lower_is_better=True)}
{_table_html(aggregate_table(cells, labels), "Aggregate, mean ± half-range over runs")}
{body}
{_table_html(_per_run_table(cells, labels), "Every run")}
<h3>What each build is</h3>
<ul>{"".join(f"<li><strong>{e['label']}</strong> — {e['rationale']}</li>" for e in manifest["variants"])}</ul>
</body></html>
"""
        output = self.output_path or self.root / "comparison-report.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html, encoding="utf-8")
        return output
