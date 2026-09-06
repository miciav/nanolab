"""The experiment record: one page that says what the controller did and what it cost.

The existing k6 report lists metric names and their values, which is enough to
confirm a run happened and not enough to understand one. Every question this
profile exists to answer is about a relationship over time — the limit against
the queue it produced, the service time against the wait it hid, one function's
share against its neighbour's arrival — and none of those survive being reduced
to a summary row.

The page is self-contained: Plotly is inlined rather than fetched, so a report
opened in a year on a machine with no network still draws its charts. Reports
that depend on a CDN stop being records and become bookmarks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

_PROMETHEUS_SNAPSHOT = "prometheus-snapshot.json"
# Colours held constant per function across every chart, so a reader tracks one
# series through the page instead of re-reading a legend at each figure.
_PALETTE = ("#2563eb", "#db2777", "#059669", "#d97706")
_PHASE_FILL = "rgba(99, 102, 241, 0.07)"


@dataclass(frozen=True, slots=True)
class ReportPhase:
    """A window of the run that answers its own question."""

    name: str
    start_seconds: float
    end_seconds: float


def counter_delta(points: list[dict[str, Any]]) -> float:
    """Total increase across a window, counting a restart as its own increment.

    A plain last-minus-first was wrong here and wrong in a way that looked like
    data rather than an error: Prometheus keeps its volume across a redeploy, so
    a window could open on the previous run's value and close on the new
    process's, reporting a NEGATIVE number of requests served.
    """
    values = [float(point["value"]) for point in points if "value" in point]
    total = 0.0
    for previous, current in zip(values, values[1:]):
        total += current - previous if current >= previous else current
    return total


def _scalar(value: object) -> float:
    """A pandas reduction is typed as possibly-a-Series; at these call sites it is
    always one number, and saying so once beats a cast at every use."""
    return float(cast(Any, value))


def _series_frame(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(payload["samples"])
    frame["function"] = payload["function"]
    return frame


def _phase_of(elapsed: float, phases: tuple[ReportPhase, ...]) -> str:
    for phase in phases:
        if phase.start_seconds <= elapsed < phase.end_seconds:
            return phase.name
    return "—"


def _shade_phases(figure: go.Figure, phases: tuple[ReportPhase, ...]) -> None:
    """Alternate bands behind the traces so a change can be read against the
    phase that caused it rather than against a bare time axis."""
    for index, phase in enumerate(phases):
        if index % 2:
            continue
        figure.add_vrect(
            x0=phase.start_seconds,
            x1=phase.end_seconds,
            fillcolor=_PHASE_FILL,
            layer="below",
            line_width=0,
        )
    for phase in phases:
        figure.add_annotation(
            x=(phase.start_seconds + phase.end_seconds) / 2,
            y=1.06,
            yref="paper",
            text=phase.name,
            showarrow=False,
            font={"size": 11, "color": "#6b7280"},
        )


def _layout(figure: go.Figure, title: str, y_title: str, y2_title: str | None = None) -> None:
    figure.update_layout(
        title=title,
        template="plotly_white",
        height=380,
        margin={"l": 60, "r": 60, "t": 70, "b": 45},
        legend={"orientation": "h", "y": -0.18},
        hovermode="x unified",
        xaxis_title="elapsed (s)",
        yaxis_title=y_title,
    )
    if y2_title:
        figure.update_layout(yaxis2={"title": y2_title, "overlaying": "y", "side": "right"})


def _limit_and_queue(frame: pd.DataFrame, phases: tuple[ReportPhase, ...], queue_size: int) -> go.Figure:
    """The whole argument in one chart: what the controller granted, and how much
    work that decision parked in the buffer."""
    figure = go.Figure()
    for index, (name, group) in enumerate(frame.groupby("function", sort=True)):
        colour = _PALETTE[index % len(_PALETTE)]
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["queue_depth"],
                name=f"{name} — queue depth",
                line={"color": colour, "width": 1.5},
                fill="tozeroy",
                opacity=0.35,
            )
        )
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["effective_concurrency"],
                name=f"{name} — limit",
                line={"color": colour, "width": 2.5, "dash": "dot"},
                yaxis="y2",
            )
        )
    figure.add_hline(
        y=queue_size,
        line={"color": "#dc2626", "width": 1, "dash": "dash"},
        annotation_text=f"queue full ({queue_size})",
        annotation_position="top left",
    )
    _shade_phases(figure, phases)
    _layout(figure, "Granted limit and the queue it produced", "queue depth", "effective concurrency")
    return figure


def _wait_against_service(frame: pd.DataFrame, phases: tuple[ReportPhase, ...]) -> go.Figure:
    """Where the caller's latency actually goes. The controller steers the solid
    line; the caller experiences the sum, and the dashed line is usually the
    larger half."""
    figure = go.Figure()
    for index, (name, group) in enumerate(frame.groupby("function", sort=True)):
        colour = _PALETTE[index % len(_PALETTE)]
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["mean_latency_ms"],
                name=f"{name} — service time",
                line={"color": colour, "width": 2},
            )
        )
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["mean_queue_wait_ms"],
                name=f"{name} — queue wait",
                line={"color": colour, "width": 2, "dash": "dash"},
            )
        )
    _shade_phases(figure, phases)
    _layout(figure, "Service time against queue wait", "milliseconds")
    return figure


def _throughput_and_rejections(frame: pd.DataFrame, phases: tuple[ReportPhase, ...]) -> go.Figure:
    figure = go.Figure()
    for index, (name, group) in enumerate(frame.groupby("function", sort=True)):
        colour = _PALETTE[index % len(_PALETTE)]
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["throughput_rps"],
                name=f"{name} — served",
                line={"color": colour, "width": 2},
            )
        )
        figure.add_trace(
            go.Bar(
                x=group["elapsed_seconds"],
                y=group["rejected"],
                name=f"{name} — refused",
                marker={"color": colour, "opacity": 0.35},
                yaxis="y2",
            )
        )
    _shade_phases(figure, phases)
    _layout(figure, "Throughput and refusals", "requests/s", "refused in interval")
    return figure


def _depth_distribution(frame: pd.DataFrame, queue_size: int) -> go.Figure:
    """How often the buffer was near full, which the mean hides: a queue that is
    empty most of the time and full occasionally reports a comfortable average
    while the callers who arrived during the burst waited for all of it."""
    figure = go.Figure()
    for index, (name, group) in enumerate(frame.groupby("function", sort=True)):
        figure.add_trace(
            go.Box(
                y=group["queue_depth"],
                name=str(name),
                marker={"color": _PALETTE[index % len(_PALETTE)]},
                boxmean="sd",
            )
        )
    figure.add_hline(
        y=queue_size,
        line={"color": "#dc2626", "width": 1, "dash": "dash"},
        annotation_text="queue full",
    )
    figure.update_layout(
        title="Queue depth distribution",
        template="plotly_white",
        height=340,
        margin={"l": 60, "r": 30, "t": 60, "b": 40},
        yaxis_title="queue depth",
        showlegend=False,
    )
    return figure


def _resource_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    payload = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(payload.get("samples", []))
    if frame.empty:
        return frame
    frame["memory_mib"] = frame["memory_bytes"] / 1024**2
    return frame


def _footprint(frame: pd.DataFrame, phases: tuple[ReportPhase, ...]) -> go.Figure:
    """Resident memory and CPU per container.

    Both on one chart because the interesting comparisons are between
    containers, not between a container and itself: a JVM function and a native
    one differ here by a factor of four while doing the same work.
    """
    figure = go.Figure()
    for index, (name, group) in enumerate(frame.groupby("container", sort=True)):
        colour = _PALETTE[index % len(_PALETTE)]
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["memory_mib"],
                name=f"{name} — memory",
                line={"color": colour, "width": 2},
            )
        )
        figure.add_trace(
            go.Scatter(
                x=group["elapsed_seconds"],
                y=group["cpu_percent"],
                name=f"{name} — CPU",
                line={"color": colour, "width": 1.5, "dash": "dot"},
                yaxis="y2",
            )
        )
    _shade_phases(figure, phases)
    _layout(figure, "Container footprint", "resident memory (MiB)", "CPU (% of one core)")
    return figure


def _footprint_table(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = frame.groupby("container", sort=True).agg(
        memory_mean_mib=("memory_mib", "mean"),
        memory_peak_mib=("memory_mib", "max"),
        memory_limit_mib=("memory_limit_bytes", lambda values: values.max() / 1024**2),
        cpu_mean_pct=("cpu_percent", "mean"),
        cpu_peak_pct=("cpu_percent", "max"),
    )
    return grouped.round(1).reset_index()


def _phase_table(frame: pd.DataFrame, phases: tuple[ReportPhase, ...]) -> pd.DataFrame:
    working = frame.copy()
    working["phase"] = working["elapsed_seconds"].map(lambda value: _phase_of(value, phases))
    grouped = working.groupby(["function", "phase"], sort=False).agg(
        limit_mean=("effective_concurrency", "mean"),
        limit_min=("effective_concurrency", "min"),
        limit_max=("effective_concurrency", "max"),
        depth_mean=("queue_depth", "mean"),
        depth_p95=("queue_depth", lambda values: values.quantile(0.95)),
        depth_max=("queue_depth", "max"),
        wait_mean_ms=("mean_queue_wait_ms", "mean"),
        wait_max_ms=("mean_queue_wait_ms", "max"),
        service_ms=("mean_latency_ms", "mean"),
        throughput_rps=("throughput_rps", "mean"),
        refused=("rejected", "sum"),
    )
    return grouped.round(2).reset_index()


def _k6_table(k6_summary: dict[str, Any]) -> pd.DataFrame:
    metrics = k6_summary.get("metrics", {})
    rows: list[dict[str, Any]] = []
    for name, entry in sorted(metrics.items()):
        if not isinstance(entry, dict):
            continue
        nested = entry.get("values")
        values: dict[str, Any] = nested if isinstance(nested, dict) else entry
        rows.append(
            {
                "metric": name,
                "values": " · ".join(
                    f"{key} {value:.4g}" if isinstance(value, (int, float)) else f"{key} {value}"
                    for key, value in values.items()
                    if not isinstance(value, dict)
                ),
            }
        )
    return pd.DataFrame(rows)


def _tail_table(queries: dict[str, Any]) -> pd.DataFrame:
    """The percentiles, which are the numbers a queue is least honest about in
    the mean."""
    rows: list[dict[str, Any]] = []
    for key, entry in sorted(queries.items()):
        if "p95" not in key or not isinstance(entry, dict):
            continue
        values = [
            float(point["value"])
            for point in entry.get("points", [])
            # NaN is what histogram_quantile returns for a window with no
            # observations, and averaging it in would poison the whole column.
            if point.get("value") == point.get("value") and float(point["value"]) > 0
        ]
        if not values:
            continue
        frame = pd.Series(values)
        rows.append(
            {
                "metric": key,
                "median over run (ms)": round(frame.median(), 1),
                "p90 of windows (ms)": round(frame.quantile(0.9), 1),
                "worst window (ms)": round(frame.max(), 1),
            }
        )
    return pd.DataFrame(rows)


def _counter_table(queries: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for key, entry in sorted(queries.items()):
        if not isinstance(entry, dict) or not key.startswith("function_"):
            continue
        if not any(marker in key for marker in ("_total", "dispatch")):
            continue
        points = entry.get("points") or []
        if not points:
            continue
        rows.append({"counter": key, "increase over run": f"{counter_delta(points):,.0f}"})
    return pd.DataFrame(rows)


def _table_html(frame: pd.DataFrame, caption: str) -> str:
    if frame.empty:
        return ""
    return (
        f'<h3>{caption}</h3>\n'
        + frame.to_html(index=False, border=0, classes="data", justify="left", na_rep="—")
    )


_STYLE = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       max-width: 1180px; margin: 0 auto; padding: 32px 24px 72px; line-height: 1.55;
       color: #111827; background: #ffffff; }
h1 { font-size: 1.85rem; margin-bottom: 4px; }
h2 { font-size: 1.3rem; margin-top: 44px; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }
h3 { font-size: 1.02rem; margin-top: 26px; color: #374151; }
p.lede { color: #6b7280; margin-top: 0; }
p.note { color: #4b5563; background: #f9fafb; border-left: 3px solid #d1d5db;
         padding: 10px 16px; margin: 18px 0; }
table.data { border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 0.88rem; }
table.data th, table.data td { border-bottom: 1px solid #e5e7eb; padding: 7px 10px; text-align: left; }
table.data th { background: #f3f4f6; font-weight: 600; }
table.data tr:hover td { background: #f9fafb; }
.cards { display: flex; flex-wrap: wrap; gap: 14px; margin: 20px 0 8px; }
.card { flex: 1 1 170px; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 16px; }
.card .value { font-size: 1.5rem; font-weight: 650; }
.card .label { color: #6b7280; font-size: 0.8rem; text-transform: uppercase;
               letter-spacing: 0.04em; }
@media (prefers-color-scheme: dark) {
  body { color: #e5e7eb; background: #0b1020; }
  h2 { border-bottom-color: #1f2937; }
  h3 { color: #cbd5e1; }
  p.note { color: #cbd5e1; background: #111827; border-left-color: #374151; }
  table.data th { background: #111827; }
  table.data th, table.data td { border-bottom-color: #1f2937; }
  table.data tr:hover td { background: #111827; }
  .card { border-color: #1f2937; }
}
"""


def _cards(frame: pd.DataFrame, k6_summary: dict[str, Any], queue_size: int) -> str:
    metrics = k6_summary.get("metrics", {})
    duration = metrics.get("http_req_duration", {})
    failed = metrics.get("http_req_failed", {})
    requests = metrics.get("http_reqs", {})
    refused = _scalar(frame["rejected"].sum())
    peak_depth = _scalar(frame["queue_depth"].max())
    mean_wait = _scalar(frame["mean_queue_wait_ms"].mean())
    entries = [
        ("requests", f"{_scalar(requests.get('count', 0)):,.0f}"),
        ("refused", f"{refused:,.0f}"),
        ("refused share", f"{_scalar(failed.get('value', 0)) * 100:.3f}%"),
        ("end-to-end p95", f"{_scalar(duration.get('p(95)', float('nan'))):.1f} ms"),
        ("peak queue fill", f"{peak_depth / queue_size * 100:.0f}%"),
        ("mean queue wait", f"{mean_wait:.1f} ms"),
    ]
    cards = "".join(
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div></div>'
        for label, value in entries
    )
    return f'<div class="cards">{cards}</div>'


@dataclass
class WriteConcurrencyReport:
    """Renders the run into one self-contained page."""

    task_id: str
    title: str
    data_dir: Path
    output_dir: Path
    queue_size: int = 100
    phases: tuple[ReportPhase, ...] = ()
    subtitle: str = ""

    def run(self) -> Path:
        frames = [
            _series_frame(path)
            for path in sorted(self.data_dir.glob("concurrency-series-*.json"))
        ]
        if not frames:
            single = self.data_dir / "concurrency-series.json"
            if single.exists():
                frames = [_series_frame(single)]
        if not frames:
            raise FileNotFoundError(
                f"no concurrency series under {self.data_dir}: nothing to report on"
            )
        frame = pd.concat(frames, ignore_index=True)

        k6_path = self.data_dir / "k6-summary.json"
        k6_summary = (
            json.loads(k6_path.read_text(encoding="utf-8")) if k6_path.exists() else {}
        )
        prom_path = self.data_dir / "metrics" / _PROMETHEUS_SNAPSHOT
        queries: dict[str, Any] = {}
        if prom_path.exists():
            try:
                queries = json.loads(prom_path.read_text(encoding="utf-8")).get("queries", {})
            except json.JSONDecodeError:
                queries = {}

        phases = self.phases or (
            ReportPhase("whole run", 0.0, _scalar(frame["elapsed_seconds"].max()) + 1),
        )
        resources = _resource_frame(self.data_dir / "resource-series.json")
        figures = [
            _limit_and_queue(frame, phases, self.queue_size),
            _wait_against_service(frame, phases),
            _throughput_and_rejections(frame, phases),
            _depth_distribution(frame, self.queue_size),
        ]
        if not resources.empty:
            figures.append(_footprint(resources, phases))
        # Plotly's script is written once, into the first figure only; repeating
        # it per chart would multiply several megabytes by the figure count.
        charts = "\n".join(
            pio.to_html(
                figure,
                full_html=False,
                # The stub types this as bool, but "inline" is what embeds the
                # library rather than linking a CDN — the whole point of a report
                # that still works offline.
                include_plotlyjs=cast(bool, "inline" if index == 0 else False),
                config={"displaylogo": False, "responsive": True},
            )
            for index, figure in enumerate(figures)
        )

        html = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{self.title}</title><style>{_STYLE}</style></head><body>"
            f"<h1>{self.title}</h1>"
            f"<p class='lede'>{self.subtitle}</p>"
            + _cards(frame, k6_summary, self.queue_size)
            + "<p class='note'>A concurrency limit does not remove work, it moves it into the "
            "queue. Service time is what the controller steers; queue wait is what the limit "
            "charges the caller, and end-to-end latency is their sum.</p>"
            "<h2>Over time</h2>" + charts
            + "<h2>By phase</h2>"
            + _table_html(_phase_table(frame, phases), "Per function, per phase")
            + (
                "<h2>Footprint</h2>"
                "<p class='note'>Resident set, not heap. A JVM measured here held 22 MB of heap "
                "inside a 190 MiB resident set: the rest is class metadata, JIT-compiled code and "
                "thread stacks, none of which a heap gauge shows and all of which a node has to "
                "find room for.</p>"
                + _table_html(_footprint_table(resources), "Per container, over the run")
                if not resources.empty
                else ""
            )
            + "<h2>Tail latency</h2>"
            + _table_html(_tail_table(queries), "Percentiles, from the Prometheus histograms")
            + "<h2>Counters</h2>"
            + _table_html(_counter_table(queries), "Increase over the run")
            + "<h2>Load generator</h2>"
            + _table_html(_k6_table(k6_summary), "k6 summary")
            + "</body></html>"
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / "concurrency-report.html"
        destination.write_text(html, encoding="utf-8")
        return destination
