from __future__ import annotations

import pytest

from sonata_tasks.loadtest.concurrency import (
    ConcurrencySample,
    ConcurrencySummary,
    ConcurrencyWatcher,
    find_dip,
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
    def __init__(self, *values: int) -> None:
        self._values = list(values)
        self.calls = 0

    def effective_concurrency(self) -> int:
        self.calls += 1
        if not self._values:
            raise RuntimeError("scrape unavailable")
        return self._values.pop(0)


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
