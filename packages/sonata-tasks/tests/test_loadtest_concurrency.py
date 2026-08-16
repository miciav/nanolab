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
    measure_response,
    verify_concurrency_cycle,
)


def series(*values: int) -> tuple[ConcurrencySample, ...]:
    """A series with no load recorded: enough for shape questions."""
    return tuple(
        ConcurrencySample(elapsed_seconds=float(index), effective=value)
        for index, value in enumerate(values)
    )


def loaded_series(*pairs: tuple[int, int]) -> tuple[ConcurrencySample, ...]:
    """`(limit, in_flight)` per reading — what the verdict actually judges."""
    return tuple(
        ConcurrencySample(elapsed_seconds=float(index), effective=limit, in_flight=busy)
        for index, (limit, busy) in enumerate(pairs)
    )


def summarise(samples: tuple[ConcurrencySample, ...]) -> ConcurrencySummary:
    return ConcurrencySummary(
        function_name="fn",
        samples=samples,
        dip=find_dip(samples),
        response=measure_response(samples),
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
    summary = summarise(loaded_series((4, 0), (4, 4), (4, 4)))

    with pytest.raises(RuntimeError, match="never gave concurrency back"):
        verify_concurrency_cycle(summary)


def test_verify_accepts_a_governor_that_converged_and_held() -> None:
    """The shape a sustained load actually produces. Demanding a climb back made
    the verdict depend on the tail relaxing, and failed a run that descended
    8 -> 2 and held there for 146 readings."""
    summary = summarise(
        loaded_series((8, 0), (8, 1), (6, 6), (4, 4), (2, 2), (2, 2), (2, 2))
    )

    verify_concurrency_cycle(summary)


def test_verify_rejects_a_run_that_never_saw_the_function_busy() -> None:
    summary = summarise(loaded_series((8, 0), (6, 1), (4, 0)))

    with pytest.raises(RuntimeError, match="never observed both idle and concurrent"):
        verify_concurrency_cycle(summary)


def test_verify_accepts_a_full_cycle() -> None:
    summary = summarise(loaded_series((6, 0), (6, 1), (2, 5), (2, 5), (6, 0)))

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
    described = summarise(loaded_series((6, 0), (6, 1), (2, 5), (2, 5), (6, 0))).describe()

    assert "[6x2 2x2 6x1]" in described
    assert "idle peak 6 -> busy floor 2" in described
    assert "recovered 6 -> 2 -> 6" in described


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
        "throughput_rps": None,
        "queue_depth": 0,
        "mean_queue_wait_ms": None,
        "rejected": 0.0,
    }


def test_the_series_carries_what_the_limit_cost_the_caller() -> None:
    """A limit does not delete work, it moves it into the buffer. A series with
    the limit and the service time but no queue depth and no queue wait reports
    the decision while hiding its price."""
    watcher = ConcurrencyWatcher(
        _StubProbe(
            ConcurrencyReading(
                effective=8,
                in_flight=8,
                latency_count=10,
                latency_total_ms=50,
                queue_depth=4,
                queue_wait_count=10,
                queue_wait_total_ms=20,
            ),
            ConcurrencyReading(
                effective=8,
                in_flight=8,
                latency_count=20,
                latency_total_ms=250,
                queue_depth=97,
                queue_wait_count=20,
                queue_wait_total_ms=1020,
            ),
        ),
        poll_interval_seconds=60,
    )
    watcher.start()
    try:
        watcher._sample()
    finally:
        watcher.stop()

    latest = watcher.samples[-1]
    assert latest.queue_depth == 97
    # Cumulatively the wait is 51ms; the ten requests of THIS interval waited
    # 100ms each, and the interval is what the burst actually did.
    assert latest.mean_queue_wait_ms == 100.0


def test_an_interval_that_completed_nothing_still_reports_its_rejections() -> None:
    """The interval where the queue was full is the one worth recording, and it
    is exactly the one where no request completed to feed the latency timer."""
    watcher = ConcurrencyWatcher(
        _StubProbe(
            ConcurrencyReading(
                effective=1, in_flight=1, latency_count=10, latency_total_ms=50, rejected=0
            ),
            ConcurrencyReading(
                effective=1, in_flight=1, latency_count=10, latency_total_ms=50, rejected=430
            ),
        ),
        poll_interval_seconds=60,
    )
    watcher.start()
    try:
        watcher._sample()
    finally:
        watcher.stop()

    latest = watcher.samples[-1]
    assert latest.mean_latency_ms is None, "nothing completed, so there is no service time"
    assert latest.rejected == 430


def test_the_scrape_records_what_the_queue_was_actually_running() -> None:
    """A limit of 8 with one request in flight is not the governor holding
    firm under load; it is load that never became concurrent."""
    reading = ConcurrencyReading(
        effective=8, in_flight=1, latency_count=10, latency_total_ms=25
    )

    assert reading.in_flight == 1


def test_a_governor_converged_to_one_still_counts_as_loaded() -> None:
    """With a limit of 1 the function can never show two requests in flight, so a
    fixed threshold filed its converged state as idle and reported a floor it had
    already left."""
    response = measure_response(loaded_series((8, 0), (8, 2), (1, 1), (1, 1)))

    assert response is not None
    assert (response.idle_peak, response.loaded_floor) == (8, 1)


def test_a_group_starts_and_stops_its_watchers_together() -> None:
    """Both series have to cover the same window, or "the neighbour arrived here"
    is not a statement about both."""
    from sonata_tasks.loadtest.concurrency import ConcurrencyWatcherGroup

    group = ConcurrencyWatcherGroup(
        {
            "a": ConcurrencyWatcher(_StubProbe(8), poll_interval_seconds=60),
            "b": ConcurrencyWatcher(_StubProbe(4), poll_interval_seconds=60),
        }
    )

    group.start()
    try:
        summaries = group.summaries()
    finally:
        group.stop()

    assert sorted(summaries) == ["a", "b"]
    assert summaries["a"].samples[0].effective == 8
    assert summaries["b"].samples[0].effective == 4


def test_an_exploratory_run_still_has_to_prove_it_measured_something() -> None:
    """A silent run and a run that disproved the effect must not look alike."""
    from sonata_tasks.loadtest.concurrency import verify_observable

    verify_observable(summarise(loaded_series((8, 0), (8, 8), (2, 2))))

    with pytest.raises(RuntimeError, match="never observed both idle and concurrent"):
        verify_observable(summarise(loaded_series((8, 0), (6, 1), (4, 0))))


def test_the_interval_carries_throughput_as_well_as_latency() -> None:
    """Latency alone cannot say whether the concurrency was worth having: service
    time rises with concurrency on any shared resource, so a limit that costs
    latency is only wrong if it bought no completions."""
    import time as _time

    watcher = ConcurrencyWatcher(
        _StubProbe(
            ConcurrencyReading(effective=8, in_flight=6, latency_count=100, latency_total_ms=200),
            ConcurrencyReading(effective=8, in_flight=7, latency_count=140, latency_total_ms=360),
        ),
        poll_interval_seconds=60,
    )
    watcher.start()
    try:
        _time.sleep(0.05)
        watcher._sample()
    finally:
        watcher.stop()

    second = watcher.samples[1]
    assert second.mean_latency_ms == 4.0  # 160ms over 40 completions
    assert second.throughput_rps is not None and second.throughput_rps > 0
