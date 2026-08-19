from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sonata_tasks.loadtest.models import PrometheusQuery, TimeWindow
from sonata_tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher

if TYPE_CHECKING:
    from sonata_tasks.loadtest.autoscaling import AutoscalingResult

_PROMETHEUS_SNAPSHOT = "prometheus-snapshot.json"


@dataclass
class FetchVmResults:
    task_id: str
    title: str
    fetcher: RemoteFileFetcher
    remote_source: str
    local_dest: Path

    def run(self) -> Path:
        # Absolute on purpose: the fetchers shell out to scp/multipass with their
        # own working directory, so a relative destination lands wherever that
        # happens to point — for the VM providers, inside the nanoFaaS checkout.
        # The transfer then "succeeds", the run dir stays empty, and the report
        # step is the first thing to notice. mkdir here resolves against this
        # process, the transfer against another: they have to be the same path.
        destination = self.local_dest.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        self.fetcher.fetch_from(self.remote_source, destination)
        return destination


@dataclass
class CapturePrometheusSnapshot:
    task_id: str
    title: str
    client: PrometheusClient
    queries: tuple[PrometheusQuery, ...]
    window: TimeWindow | Callable[[], TimeWindow]
    output_dir: Path
    # How far before k6 started to reach back. The trailing margin has a clear
    # job — catch the last scrape after the load stops — but the leading one
    # only picks up samples from before the load began, and on a run that
    # redeploys the control plane those belong to the PREVIOUS build. Measured:
    # a nine-cell comparison reported identical `jvm_gc_pause_seconds` for a JVM
    # build and a native one, which publishes that metric not at all; the series
    # came from the pod the previous cell had just replaced. Set it to 0 where
    # every run starts from a fresh process.
    lead_seconds: float | None = None

    def _resolve_window(self) -> TimeWindow:
        if callable(self.window):
            return self.window()
        return self.window

    # Skew below this (seconds) is treated as no drift; margin widens the shifted
    # window to absorb the Prometheus scrape interval.
    _CLOCK_SKEW_THRESHOLD_S = 5.0
    _WINDOW_MARGIN_S = 30.0

    def _align_window(self, window: TimeWindow) -> TimeWindow:
        """Shift the host-clock window into Prometheus's clock domain.

        The window start/end come from the k6 run on the host clock, but Prometheus
        timestamps samples with the metrics-source VM clock. When those clocks drift
        (e.g. the host slept mid-run), a host-clock window misses the VM-clock
        samples. Anchor the window to Prometheus's own clock so the snapshot is
        robust to that skew. Clients without ``server_time`` (e.g. test fakes) are
        left unshifted.
        """
        lead = timedelta(
            seconds=self._WINDOW_MARGIN_S if self.lead_seconds is None else self.lead_seconds
        )
        trail = timedelta(seconds=self._WINDOW_MARGIN_S)
        expanded = TimeWindow(start=window.start - lead, end=window.end + trail)
        server_time = getattr(self.client, "server_time", None)
        if server_time is None:
            return expanded
        try:
            offset = float(server_time()) - datetime.now(timezone.utc).timestamp()
        except (RuntimeError, OSError, ValueError, TypeError):
            return expanded
        if abs(offset) < self._CLOCK_SKEW_THRESHOLD_S:
            return expanded
        shift = timedelta(seconds=offset)
        return TimeWindow(start=expanded.start + shift, end=expanded.end + shift)

    def run(self) -> Path:
        source_window = self._resolve_window()
        wait_seconds = (
            source_window.end
            + timedelta(seconds=self._WINDOW_MARGIN_S)
            - datetime.now(timezone.utc)
        ).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, self._WINDOW_MARGIN_S))
        window = self._align_window(source_window)
        metrics_dir = self.output_dir / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)

        result: dict[str, dict] = {}
        for q in self.queries:
            entry: dict[str, object] = {"query": q.expr, "required": q.required, "points": []}
            try:
                points = self.client.query_range(q.expr, window)
            except RuntimeError as exc:
                if q.required:
                    raise RuntimeError(f"required query '{q.name}' failed: {exc}") from exc
                entry["error"] = str(exc)
                result[q.name] = entry
                continue
            if q.required and not points:
                raise RuntimeError(f"required query '{q.name}' returned no data")
            entry["points"] = points
            result[q.name] = entry

        snapshot = {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "queries": result,
        }
        dest = metrics_dir / _PROMETHEUS_SNAPSHOT
        dest.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        return dest


def _render_k6_html(k6_summary: dict, prom_snapshot: dict | None) -> str:
    metrics = k6_summary.get("metrics", {})
    rows: list[str] = []
    for name, entry in metrics.items():
        if not isinstance(entry, dict):
            continue
        values = entry.get("values")
        if not isinstance(values, dict):
            values = entry
        formatted = " | ".join(
            f"{k}: {v:.3g}" if isinstance(v, float) else f"{k}: {v}"
            for k, v in values.items()
        )
        rows.append(f"<tr><td>{name}</td><td>{entry.get('type', '')}</td><td>{formatted}</td></tr>")

    prom_section = ""
    if prom_snapshot:
        queries = prom_snapshot.get("queries", {})
        prom_rows = [
            f"<tr><td>{metric}</td><td>{len(data.get('points', []))} points</td></tr>"
            for metric, data in queries.items()
            if isinstance(data, dict)
        ]
        if prom_rows:
            prom_section = (
                "<h2>Prometheus Metrics</h2>"
                "<table><tr><th>Metric</th><th>Data</th></tr>"
                + "".join(prom_rows)
                + "</table>"
            )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>k6 Loadtest Report</title>\n"
        "<style>\n"
        "  body { font-family: sans-serif; max-width: 1000px; margin: 0 auto; padding: 24px; }\n"
        "  h1, h2 { border-bottom: 1px solid #eee; padding-bottom: 8px; }\n"
        "  table { border-collapse: collapse; width: 100%; margin: 16px 0; }\n"
        "  th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; }\n"
        "  th { background: #f5f5f5; font-weight: 600; }\n"
        "  tr:hover { background: #fafafa; }\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>k6 Loadtest Report</h1>\n"
        "<h2>k6 Metrics</h2>\n"
        "<table>\n"
        "<tr><th>Metric</th><th>Type</th><th>Values</th></tr>\n"
        + "".join(rows)
        + "\n</table>\n"
        + prom_section
        + "\n</body>\n</html>"
    )


@dataclass
class WriteK6Report:
    task_id: str
    title: str
    data_dir: Path
    output_dir: Path

    def run(self) -> Path:
        k6_summary_path = self.data_dir / "k6-summary.json"
        k6_summary = json.loads(k6_summary_path.read_text(encoding="utf-8"))

        prom_path = self.data_dir / "metrics" / _PROMETHEUS_SNAPSHOT
        prom_snapshot: dict | None = None
        if prom_path.exists():
            try:
                prom_snapshot = json.loads(prom_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        self.output_dir.mkdir(parents=True, exist_ok=True)
        html = _render_k6_html(k6_summary, prom_snapshot)
        dest = self.output_dir / "report.html"
        dest.write_text(html, encoding="utf-8")
        return dest


def _point_stats(points: list[dict]) -> dict[str, float | int]:
    values = [float(point["value"]) for point in points if "value" in point]
    if not values:
        return {"points": 0}
    return {
        "points": len(values),
        "first": values[0],
        "last": values[-1],
        "delta": values[-1] - values[0],
        "min": min(values),
        "max": max(values),
    }


@dataclass
class WriteLoadtestSummary:
    task_id: str
    title: str
    data_dir: Path
    output_dir: Path
    autoscaling: "AutoscalingResult | None" = None

    def run(self) -> Path:
        k6 = json.loads((self.data_dir / "k6-summary.json").read_text(encoding="utf-8"))
        prometheus_path = self.data_dir / "metrics" / _PROMETHEUS_SNAPSHOT
        prometheus = (
            json.loads(prometheus_path.read_text(encoding="utf-8"))
            if prometheus_path.exists()
            else {"queries": {}}
        )
        selected = ("http_reqs", "http_req_failed", "http_req_duration", "checks")
        summary = {
            "schema_version": 1,
            "k6": {name: k6.get("metrics", {}).get(name, {}) for name in selected},
            "prometheus": {
                name: _point_stats(entry.get("points", []))
                for name, entry in prometheus.get("queries", {}).items()
            },
            "autoscaling": asdict(self.autoscaling.result) if self.autoscaling else None,
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / "summary.json"
        destination.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return destination
