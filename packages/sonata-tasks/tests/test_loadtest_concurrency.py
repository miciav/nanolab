from __future__ import annotations

import json

import pytest

from sonata_tasks.loadtest.concurrency import (
    ConcurrencyReading,
    ConcurrencySample,
    ConcurrencySummary,
    ConcurrencyWatcher,
    find_dip,
    governed_samples,
    verify_concurrency_cycle,
)


def series(*values: int) -> tuple[ConcurrencySample, ...]:
    return tuple(
        ConcurrencySample(elapsed_seconds=float(index), effective=value)
        for index, value in enumerate(values)
    )


def test_finds_the_excursion_of_a_full_cycle() -> None:
    dip = find_dip(series(2, 4, 6, 6, 3, 1, 1, 4, 6))

    assert dip is not None
    assert (dip.peak_before, dip.trough, dip.recovery_after) == (6, 1, 6)


def test_ignores_a_fall_that_never_recovers() -> None:
    # The governor throttled and stayed throttled: this is the failure a
    # low-to-high-only run cannot tell apart from success.
    assert find_dip(series(2, 4, 6, 6, 3, 1, 1, 1)) is None


def test_ignores_a_rise_that_was_never_preceded_by_a_fall() -> None:
    assert find_dip(series(1, 1, 2, 4, 6)) is None


def test_ignores_a_constant_series() -> None:
    assert find_dip(series(4, 4, 4, 4)) is None


def test_needs_at_least_three_readings() -> None:
    assert find_dip(series(6, 1)) is None


def test_measures_recovery_against_the_best_later_reading() -> None:
    # Two troughs, 1 and 2, both recovering to 5. The deeper one wins, and the
    # recovery is the highest the limit reached afterwards, not the next sample.
    dip = find_dip(series(6, 1, 2, 5, 2, 5))

    assert dip is not None
    assert (dip.peak_before, dip.trough, dip.recovery_after) == (6, 1, 5)


def test_prefers_the_cycle_with_the_stronger_weak_side() -> None:
    # The second excursion falls further (5 against 3) but limps back by 1,
    # while the first is 3 in both directions. Ranking on the fall alone would
    # pick the deeper one, which is mostly a governor that did not recover.
    dip = find_dip(series(5, 2, 5, 0, 1))

    assert dip is not None
    assert (dip.peak_before, dip.trough, dip.recovery_after) == (5, 2, 5)


def test_verify_rejects_a_run_with_no_readings() -> None:
    with pytest.raises(RuntimeError, match="metrics profile"):
        verify_concurrency_cycle(ConcurrencySummary(function_name="word-stats-java"))


def test_verify_rejects_a_limit_that_never_moved() -> None:
    samples = series(4, 4, 4)
    summary = ConcurrencySummary(
        function_name="word-stats-java", samples=samples, dip=find_dip(samples)
    )

    with pytest.raises(RuntimeError, match="never fell and recovered"):
        verify_concurrency_cycle(summary)


def test_verify_accepts_a_full_cycle() -> None:
    samples = series(6, 6, 2, 2, 6)
    summary = ConcurrencySummary(
        function_name="word-stats-java", samples=samples, dip=find_dip(samples)
    )

    verify_concurrency_cycle(summary)


class _StubProbe:
    """Yields readings in order; an exhausted stub stands in for a failing scrape."""

    def __init__(self, *readings: ConcurrencyReading | int) -> None:
        self._readings = [
            reading
            if isinstance(reading, ConcurrencyReading)
            else ConcurrencyReading(
                effective=reading, in_flight=0, latency_count=0.0, latency_total_ms=0.0
            )
            for reading in readings
        ]
        self.calls = 0

    def read(self) -> ConcurrencyReading:
        self.calls += 1
        if not self._readings:
            raise RuntimeError("scrape unavailable")
        return self._readings.pop(0)


def test_watcher_opens_the_series_before_the_background_thread_runs() -> None:
    probe = _StubProbe(6, 2)
    watcher = ConcurrencyWatcher(probe, poll_interval_seconds=60)

    watcher.start()
    try:
        # start() takes the opening reading itself, so the peak the load arrived
        # at is in the series even if the run ends before the first poll.
        assert [sample.effective for sample in watcher.samples] == [6]
    finally:
        watcher.stop()


def test_watcher_records_probe_failures_without_dying() -> None:
    watcher = ConcurrencyWatcher(_StubProbe(), poll_interval_seconds=60)

    watcher.start()
    try:
        assert watcher.samples == ()
        assert watcher.errors == ["scrape unavailable"]
    finally:
        watcher.stop()


def test_watcher_refuses_a_second_start() -> None:
    watcher = ConcurrencyWatcher(_StubProbe(1), poll_interval_seconds=60)
    watcher.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            watcher.start()
    finally:
        watcher.stop()


def test_trajectory_survives_the_failure_that_discards_the_summary() -> None:
    samples = series(2, 4, 4, 4, 8, 8)
    summary = ConcurrencySummary(function_name="fn", samples=samples, dip=find_dip(samples))

    assert summary.trajectory() == "2x1 4x3 8x2"

    with pytest.raises(RuntimeError, match=r"\[2x1 4x3 8x2\]"):
        verify_concurrency_cycle(summary)


def test_describe_names_the_shape_for_the_run_log() -> None:
    """A green run is otherwise silent about what the governor did, and nothing
    downstream persists the series."""
    samples = series(6, 6, 2, 2, 6)
    summary = ConcurrencySummary(function_name="fn", samples=samples, dip=find_dip(samples))

    described = summary.describe()

    assert "[6x2 2x2 6x1]" in described
    assert "peak 6 -> trough 2 -> recovery 6" in described


def test_the_trajectory_pairs_each_block_with_its_service_time() -> None:
    """Aggregate extremes could not say whether the function slowed while the
    limit was still high, which is the whole diagnosis."""
    samples = (
        ConcurrencySample(elapsed_seconds=0.0, effective=8, mean_latency_ms=None),
        ConcurrencySample(elapsed_seconds=2.0, effective=8, mean_latency_ms=2.0),
        ConcurrencySample(elapsed_seconds=4.0, effective=8, mean_latency_ms=19.3),
        ConcurrencySample(elapsed_seconds=6.0, effective=4, mean_latency_ms=5.0),
    )
    summary = ConcurrencySummary(function_name="fn", samples=samples)

    assert summary.trajectory() == "8x3@2.0-19.3 4x1@5.0-5.0"


def test_describe_still_names_the_series_when_there_was_no_excursion() -> None:
    samples = series(4, 4, 4)
    summary = ConcurrencySummary(function_name="fn", samples=samples, dip=find_dip(samples))

    assert summary.describe() == "concurrency governor for 'fn': [4x3]"


def test_readings_before_the_governors_first_decision_are_dropped() -> None:
    """The queue opens at the function's configured `concurrency`, a value nobody
    decided and higher than anything the governor picks while still climbing."""
    assert [s.effective for s in governed_samples(series(8, 8, 5, 6, 7, 8))] == [5, 6, 7, 8]


def test_nothing_is_dropped_when_the_limit_never_left_its_opening_value() -> None:
    assert [s.effective for s in governed_samples(series(4, 4, 4))] == [4, 4, 4]


def test_the_verdict_ignores_the_warm_up_that_a_real_run_passed_on() -> None:
    """The observed run was `8x2 5x3 6x4 7x3 8x44 7x5 6x3 5x5 4x5 3x1`.

    It passed on the opening 8 -> 5 -> 8, which is the queue's initial value
    followed by the governor's first write; the genuine back-off from 8 to 3
    never recovered before the run ended. Judged on governed readings only, that
    run has no cycle to report — which is the honest answer.
    """
    observed = series(
        *([8] * 2 + [5] * 3 + [6] * 4 + [7] * 3 + [8] * 44 + [7] * 5 + [6] * 3 + [5] * 5 + [4] * 5 + [3])
    )

    assert find_dip(observed) is not None, "the raw series does contain the warm-up excursion"
    assert find_dip(governed_samples(observed)) is None


def test_the_interval_mean_service_time_travels_with_the_limit() -> None:
    watcher = ConcurrencyWatcher(
        _StubProbe(
            ConcurrencyReading(effective=8, in_flight=6, latency_count=10, latency_total_ms=50),
            ConcurrencyReading(effective=8, in_flight=7, latency_count=20, latency_total_ms=250),
        ),
        poll_interval_seconds=60,
    )
    watcher.start()
    try:
        watcher._sample()  # the interval that followed the opening reading
    finally:
        watcher.stop()

    # The cumulative timer says 15ms; the ten invocations of this interval
    # averaged 20ms, and it is the interval the governor decides from.
    assert [s.mean_latency_ms for s in watcher.samples] == [None, 20.0]


def test_the_series_is_written_before_the_verdict_can_raise(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Seven runs were spent inferring an ordering recorded nowhere; the failing
    run is exactly the one whose readings are worth keeping."""
    from sonata_tasks.loadtest.concurrency import write_series

    samples = (
        ConcurrencySample(elapsed_seconds=0.0, effective=8, in_flight=1, mean_latency_ms=None),
        ConcurrencySample(elapsed_seconds=2.0, effective=8, in_flight=7, mean_latency_ms=19.3),
    )
    summary = ConcurrencySummary(function_name="fn", samples=samples, dip=find_dip(samples))
    path = tmp_path / "nested" / "concurrency-series.json"

    write_series(summary, path)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["function"] == "fn"
    assert written["samples"][1] == {
        "elapsed_seconds": 2.0,
        "effective_concurrency": 8,
        "in_flight": 7,
        "mean_latency_ms": 19.3,
    }


def test_the_scrape_records_what_the_queue_was_actually_running() -> None:
    """A limit of 8 with one request in flight is not the governor holding
    firm under load; it is load that never became concurrent."""
    reading = ConcurrencyReading(
        effective=8, in_flight=1, latency_count=10, latency_total_ms=25
    )

    assert reading.in_flight == 1
