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

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from sonata_tasks.metrics import metric_sum

_EFFECTIVE_CONCURRENCY = "function_effective_concurrency"


class EffectiveConcurrencyReader(Protocol):
    def effective_concurrency(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ConcurrencySample:
    """One reading of a function's effective concurrency while load was running."""

    elapsed_seconds: float
    effective: int


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
            f"{len(summary.samples)} readings. Either the load never degraded the "
            "function's service time, or the function is not in ADAPTIVE_PER_POD "
            "mode."
        )


@dataclass(frozen=True)
class ScrapeConcurrencyProbe:
    """Reads the effective-concurrency gauge off the control plane's metrics scrape."""

    management_url: str
    function_name: str
    timeout_seconds: float = 4.0

    def effective_concurrency(self) -> int:
        url = f"{self.management_url.rstrip('/')}/actuator/prometheus"
        try:
            response = httpx.get(url, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"metrics scrape failed for {url}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"metrics scrape failed for {url} (HTTP {response.status_code})")
        matches, total = metric_sum(
            response.text, _EFFECTIVE_CONCURRENCY, {"function": self.function_name}
        )
        if not matches:
            raise RuntimeError(
                f"{url}: no {_EFFECTIVE_CONCURRENCY} sample for "
                f"function={self.function_name!r}"
            )
        return int(total)


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
        self._started_at: float | None = None
        self.errors: list[str] = []

    @property
    def samples(self) -> tuple[ConcurrencySample, ...]:
        return tuple(self._samples)

    def summary(self, function_name: str) -> ConcurrencySummary:
        samples = self.samples
        return ConcurrencySummary(
            function_name=function_name,
            samples=samples,
            dip=find_dip(samples),
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
            effective = self._probe.effective_concurrency()
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
                effective=effective,
            )
        )
