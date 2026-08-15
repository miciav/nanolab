"""Observing the nanoFaaS per-function concurrency governor while load runs.

The governor is not the autoscaler and does not move replicas. It decides how
many invocations one replica of a function may run at once, and in
`ADAPTIVE_PER_POD` mode it decides that from the function's own service time:
the per-replica target grows while latency sits at its best and shrinks once
latency degrades. So the reading to watch is `function_effective_concurrency`,
and the direction is the opposite of replica scaling — heavier load drives the
limit *down*, which is the behaviour the governor exists to produce.

That reading is a gauge on the control plane's management port, and it is only
exported under `nanofaas.metrics.profile=advanced`; under the default `basic`
profile a MeterFilter denies it and the scrape simply has no such line.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from sonata_tasks.metrics import metric_sum

_EFFECTIVE_CONCURRENCY = "function_effective_concurrency"
# Micrometer's own suffixes for the timer the governor reads.
_LATENCY_COUNT = "function_latency_ms_seconds_count"
_LATENCY_SUM = "function_latency_ms_seconds_sum"
# What the queue was actually running. Without it the series cannot tell a
# function that refused to slow down from one the load never made concurrent.
_IN_FLIGHT = "function_inFlight"


@dataclass(frozen=True, slots=True)
class ConcurrencyReading:
    """One scrape: the limit, and the cumulative timer the governor decides from."""

    effective: int
    in_flight: int
    latency_count: float
    latency_total_ms: float


class EffectiveConcurrencyReader(Protocol):
    def read(self) -> ConcurrencyReading: ...


@dataclass(frozen=True, slots=True)
class ConcurrencySample:
    """One reading of a function's effective concurrency while load was running.

    The service time of the same interval travels with it. Without it a series
    shows the governor moving but never why, and "why" is the whole question
    when the thresholds are still being chosen: a limit that falls late is a
    different problem depending on whether latency rose late or the controller
    reacted late.
    """

    elapsed_seconds: float
    effective: int
    in_flight: int = 0
    mean_latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ConcurrencyDip:
    """A down-then-up excursion of the limit: the governor backing off and recovering."""

    peak_before: int
    trough: int
    recovery_after: int

    @property
    def descent(self) -> int:
        return self.peak_before - self.trough

    @property
    def ascent(self) -> int:
        return self.recovery_after - self.trough


def governed_samples(
    samples: Sequence[ConcurrencySample],
) -> tuple[ConcurrencySample, ...]:
    """Drop the readings taken before the governor's first decision.

    The queue is created at the function's configured `concurrency`, so a series
    opens on a value nobody decided, and one higher than anything the governor
    picks while it is still climbing. Leaving it in hands the search a free
    "peak before", which lets the warm-up masquerade as a reaction to load.

    That is not hypothetical. A real run produced
    `8x2 5x3 6x4 7x3 8x44 7x5 6x3 5x5 4x5 3x1`: the check passed on the opening
    8 -> 5 -> 8, which is just the queue's initial value and the governor's first
    write, while the genuine back-off from 8 down to 3 went unrecognised because
    it never recovered before the run ended.
    """
    if not samples:
        return ()
    opening = samples[0].effective
    first_decision = next(
        (index for index, sample in enumerate(samples) if sample.effective != opening),
        None,
    )
    return tuple(samples) if first_decision is None else tuple(samples[first_decision:])


def find_dip(samples: Sequence[ConcurrencySample]) -> ConcurrencyDip | None:
    """The clearest "went down, then came back up" excursion in the series, if any.

    Phase windows are deliberately not taken as input. Tying the check to the k6
    stage boundaries would assert that the governor reacted *on schedule*, which
    it cannot: it moves on its own poll interval and honours upscale and
    downscale cooldowns, so the reaction always trails the load change by an
    amount the scenario does not control. What the run must show is the shape —
    the limit fell while the function was slow and rose again once it was not.

    Both sides are required. A fall alone is also what a governor stuck at
    `minTargetInFlightPerPod` looks like, which is the failure mode a
    low-to-high-only run cannot distinguish: once the latency baseline is pinned
    too low, the target never climbs back and the function stays throttled for
    the rest of its life.

    Where several excursions qualify, the deepest one wins, measured by its
    weaker side so that a large fall with a token recovery cannot outrank a
    genuine cycle.
    """
    values = [sample.effective for sample in samples]
    if len(values) < 3:
        return None

    best: ConcurrencyDip | None = None
    peak_before = values[0]
    for index in range(1, len(values) - 1):
        peak_before = max(peak_before, values[index - 1])
        trough = values[index]
        recovery_after = max(values[index + 1 :])
        if trough >= peak_before or trough >= recovery_after:
            continue
        candidate = ConcurrencyDip(
            peak_before=peak_before, trough=trough, recovery_after=recovery_after
        )
        if best is None or min(candidate.descent, candidate.ascent) > min(
            best.descent, best.ascent
        ):
            best = candidate
    return best


@dataclass(frozen=True)
class ConcurrencySummary:
    """What the governor did to one function over a run."""

    function_name: str
    # The trajectory, not just its extremes: a governor that oscillates between
    # min and max produces the same min/max pair as one that made a single
    # deliberate excursion, and only the series tells them apart.
    samples: tuple[ConcurrencySample, ...] = ()
    dip: ConcurrencyDip | None = None
    errors: tuple[str, ...] = ()

    @property
    def min_observed(self) -> int:
        return min((sample.effective for sample in self.samples), default=0)

    @property
    def max_observed(self) -> int:
        return max((sample.effective for sample in self.samples), default=0)

    def describe(self) -> str:
        """One line naming what the governor did, for the run log.

        Nothing downstream persists the series — the k6 report knows nothing of
        concurrency — so on a passing run this is the only place the shape
        survives, and the shape is what threshold tuning needs.
        """
        line = f"concurrency governor for {self.function_name!r}: [{self.trajectory()}]"
        if self.dip is None:
            return line
        return (
            f"{line} peak {self.dip.peak_before} -> trough {self.dip.trough} "
            f"-> recovery {self.dip.recovery_after}"
        )

    def trajectory(self) -> str:
        """The series, run-length encoded with each block's service time.

        For example `8x42@2.0-19.3` — the limit sat at 8 for 42 readings while
        the mean service time of those intervals ranged from 2.0ms to 19.3ms.
        Aggregate extremes over the whole run were not enough to explain a run:
        they said the function had slowed tenfold without saying whether that
        happened while the limit was still high, which is the difference between
        a controller that ignored the signal and one that never received it.
        """
        blocks: list[tuple[int, int, list[float]]] = []
        for sample in self.samples:
            if blocks and blocks[-1][0] == sample.effective:
                value, count, latencies = blocks[-1]
                blocks[-1] = (value, count + 1, latencies)
            else:
                blocks.append((sample.effective, 1, []))
            if sample.mean_latency_ms is not None:
                blocks[-1][2].append(sample.mean_latency_ms)

        parts: list[str] = []
        for value, count, latencies in blocks:
            part = f"{value}x{count}"
            if latencies:
                part += f"@{min(latencies)}-{max(latencies)}"
            parts.append(part)
        return " ".join(parts)


def write_series(summary: ConcurrencySummary, path: Path) -> None:
    """Persist every reading, before anything can raise on the verdict.

    The compressed line in the run log was not enough to explain a run: it says
    the limit sat at 8 for 146 readings while service time ranged from 2.6ms to
    10.8ms, without saying which reading was which. Seven runs were spent
    inferring an ordering that was recorded nowhere, so the series is written
    out in full and written FIRST — a failing verdict is exactly when it matters.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "function": summary.function_name,
        "dip": None
        if summary.dip is None
        else {
            "peak_before": summary.dip.peak_before,
            "trough": summary.dip.trough,
            "recovery_after": summary.dip.recovery_after,
        },
        "errors": list(summary.errors),
        "samples": [
            {
                "elapsed_seconds": sample.elapsed_seconds,
                "effective_concurrency": sample.effective,
                "in_flight": sample.in_flight,
                "mean_latency_ms": sample.mean_latency_ms,
            }
            for sample in summary.samples
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def verify_concurrency_cycle(summary: ConcurrencySummary) -> None:
    """Raise unless the governor lowered the limit under load and raised it after.

    A constant series is the common failure and has several causes worth telling
    apart in the message: the module absent from the build, the function left in
    `FIXED` mode, or the load never slowing the function enough to register as
    degradation.
    """
    if not summary.samples:
        raise RuntimeError(
            f"no {_EFFECTIVE_CONCURRENCY} readings for {summary.function_name!r}; "
            "the concurrency-control module may be missing from the build, or the "
            "control plane may be running the basic metrics profile, which denies "
            "this gauge"
        )
    if summary.dip is None:
        raise RuntimeError(
            f"{summary.function_name!r} effective concurrency never fell and "
            f"recovered: {summary.min_observed}..{summary.max_observed} over "
            f"{len(summary.samples)} readings [{summary.trajectory()}]. Either the "
            "load never degraded the function's service time, or the function is "
            "not in ADAPTIVE_PER_POD mode."
        )


@dataclass(frozen=True)
class ScrapeConcurrencyProbe:
    """Reads the governor's gauge and its input timer from one metrics scrape.

    Both come from the same response on purpose: read separately they would
    describe two different moments, and the whole point of carrying the service
    time is to say what the limit was reacting to.
    """

    management_url: str
    function_name: str
    timeout_seconds: float = 4.0

    def read(self) -> ConcurrencyReading:
        url = f"{self.management_url.rstrip('/')}/actuator/prometheus"
        try:
            response = httpx.get(url, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"metrics scrape failed for {url}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"metrics scrape failed for {url} (HTTP {response.status_code})")

        labels = {"function": self.function_name}
        matches, effective = metric_sum(response.text, _EFFECTIVE_CONCURRENCY, labels)
        _, in_flight = metric_sum(response.text, _IN_FLIGHT, labels)
        if not matches:
            raise RuntimeError(
                f"{url}: no {_EFFECTIVE_CONCURRENCY} sample for "
                f"function={self.function_name!r}"
            )
        # The timer is absent until the function has served something, which is
        # normal at the start of a run and must not read as a scrape failure.
        _, count = metric_sum(response.text, _LATENCY_COUNT, labels)
        _, total_seconds = metric_sum(response.text, _LATENCY_SUM, labels)
        return ConcurrencyReading(
            effective=int(effective),
            in_flight=int(in_flight),
            latency_count=count,
            latency_total_ms=total_seconds * 1000.0,
        )


class ConcurrencyWatcher:
    """Samples the effective concurrency on a background thread while load runs.

    Has to be observed DURING the run: the governor raises the limit again once
    the function speeds back up, so a reading taken afterwards shows the
    recovered value and nothing of the excursion that is the point of the test.
    """

    def __init__(
        self,
        probe: EffectiveConcurrencyReader,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._probe = probe
        self._poll_interval = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[ConcurrencySample] = []
        self._previous: ConcurrencyReading | None = None
        self._started_at: float | None = None
        self.errors: list[str] = []

    @property
    def samples(self) -> tuple[ConcurrencySample, ...]:
        return tuple(self._samples)

    def summary(self, function_name: str) -> ConcurrencySummary:
        samples = self.samples
        return ConcurrencySummary(
            function_name=function_name,
            # The full series is kept for the log: the warm-up is worth seeing.
            samples=samples,
            # The verdict is not allowed to use it. `governed_samples` is applied
            # here, at the one boundary where raw readings become governor
            # decisions, rather than inside `find_dip`, which stays a plain
            # search for a shape in whatever it is handed.
            dip=find_dip(governed_samples(samples)),
            errors=tuple(self.errors),
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ConcurrencyWatcher already started")
        self._stop.clear()
        self._started_at = time.monotonic()
        # Opened here rather than on the thread's first turn so the series starts
        # from the limit the load arrived at, which is the peak the later trough
        # is measured against.
        self._sample()
        self._thread = threading.Thread(
            target=self._loop, name="concurrency-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            self._sample()

    def _sample(self) -> None:
        try:
            reading = self._probe.read()
        except RuntimeError as exc:
            # A transient scrape failure must not kill the watcher mid-load: the
            # gap it leaves is one missing reading, while a dead watcher loses
            # the rest of the run.
            self.errors.append(str(exc))
            return
        self._samples.append(
            ConcurrencySample(
                elapsed_seconds=round(
                    time.monotonic() - (self._started_at or time.monotonic()), 3
                ),
                effective=reading.effective,
                in_flight=reading.in_flight,
                mean_latency_ms=self._interval_mean_latency(reading),
            )
        )
        self._previous = reading

    def _interval_mean_latency(self, reading: ConcurrencyReading) -> float | None:
        """Mean service time of the interval just ended, or None if nothing finished.

        The cumulative timer answers "since startup", which flattens exactly the
        change the governor is reacting to; the difference between two scrapes is
        what the controller itself works from.
        """
        previous = self._previous
        if previous is None:
            return None
        delta_count = reading.latency_count - previous.latency_count
        delta_total = reading.latency_total_ms - previous.latency_total_ms
        if delta_count <= 0 or delta_total <= 0:
            return None
        return round(delta_total / delta_count, 2)
