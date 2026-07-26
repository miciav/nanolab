from __future__ import annotations

from pathlib import Path

import pytest
from sonata_engine import Resource

from sonata_tasks.process import managed_process_resource


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def _spawner(process: FakeProcess, seen: list[dict[str, object]]):
    def spawn(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        return process

    return spawn


def test_it_builds_a_sonata_resource() -> None:
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(FakeProcess(), [])
    )

    assert isinstance(resource, Resource)
    assert resource.title == "Acquire thing"
    assert resource.release_title == "Release thing"


def test_acquire_spawns_with_argv_and_cwd() -> None:
    seen: list[dict[str, object]] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("java", "-jar", "app.jar"),
        cwd=Path("/repo"),
        ready=lambda: True,
        spawn=_spawner(FakeProcess(), seen),
    )

    resource.acquire()

    assert seen[0]["argv"] == ("java", "-jar", "app.jar")
    assert seen[0]["cwd"] == Path("/repo")


def test_acquire_waits_until_ready() -> None:
    attempts = iter([False, False, True])
    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: next(attempts),
        spawn=_spawner(FakeProcess(), []),
        readiness_interval=0.5,
        sleep=slept.append,
    )

    resource.acquire()

    assert slept == [0.5, 0.5]


def test_acquire_gives_up_and_stops_the_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(process, []),
        readiness_attempts=3,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="never became ready"):
        resource.acquire()

    # A failed acquire is never released by the engine, so it must clean up itself.
    assert process.terminated is True


def test_acquire_fails_immediately_when_the_process_exits() -> None:
    class Exited(FakeProcess):
        def poll(self) -> int | None:
            return 23

    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(Exited(), []),
        sleep=slept.append,
    )

    with pytest.raises(RuntimeError, match="exited with code 23"):
        resource.acquire()

    assert slept == []


def test_release_terminates_a_live_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(process, [])
    )

    resource.acquire()
    resource.release()

    assert (process.terminated, process.killed) == (True, False)


def test_release_kills_a_process_that_ignores_terminate() -> None:
    import subprocess

    class Stubborn(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self._waits = 0

        def terminate(self) -> None:
            self.terminated = True  # stays alive on purpose

        def wait(self, timeout: float | None = None) -> int:
            self._waits += 1
            if self._waits == 1:
                raise subprocess.TimeoutExpired(cmd="run", timeout=timeout or 0)
            return 0

    process = Stubborn()
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(process, [])
    )

    resource.acquire()
    resource.release()

    assert process.killed is True


def test_release_is_a_no_op_when_nothing_was_started() -> None:
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(FakeProcess(), [])
    )

    resource.release()  # must not raise
