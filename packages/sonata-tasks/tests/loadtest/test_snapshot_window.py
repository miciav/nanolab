"""The window must not reach into the run before it.

A comparison redeploys the control plane for every cell, so a window that
starts before k6 did contains samples from the build the previous cell was
measuring. That happened: two cells reported identical `jvm_gc_pause_seconds`
for a JVM build and a native one, and a native image publishes that metric not
at all — the series belonged to the pod just replaced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonata_tasks.loadtest.models import TimeWindow
from sonata_tasks.loadtest.tasks import CapturePrometheusSnapshot

START = datetime(2026, 8, 19, 10, 0, 0, tzinfo=timezone.utc)
WINDOW = TimeWindow(start=START, end=START + timedelta(seconds=450))


def _snapshot(lead: float | None) -> CapturePrometheusSnapshot:
    return CapturePrometheusSnapshot(
        task_id="",
        title="snapshot",
        client=object(),  # type: ignore[arg-type]
        queries=(),
        window=WINDOW,
        output_dir=Path("/tmp"),
        lead_seconds=lead,
    )


def test_by_default_the_window_is_widened_both_ways() -> None:
    """Unchanged for every run that does not redeploy between measurements."""
    aligned = _snapshot(None)._align_window(WINDOW)

    assert aligned.start == WINDOW.start - timedelta(seconds=30)
    assert aligned.end == WINDOW.end + timedelta(seconds=30)


def test_a_zero_lead_starts_exactly_when_the_load_did() -> None:
    aligned = _snapshot(0.0)._align_window(WINDOW)

    assert aligned.start == WINDOW.start


def test_the_trailing_margin_survives_a_zero_lead() -> None:
    """It has its own job: catching the last scrape after the load stops."""
    aligned = _snapshot(0.0)._align_window(WINDOW)

    assert aligned.end == WINDOW.end + timedelta(seconds=30)
