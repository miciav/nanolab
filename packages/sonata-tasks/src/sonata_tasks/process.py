from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sonata_engine import Resource


def managed_process_resource(
    *,
    title: str,
    argv: tuple[str, ...],
    ready: Callable[[], bool],
    cwd: Path | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
    readiness_attempts: int = 90,
    readiness_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Resource:
    """A long-running local process as a Sonata resource.

    The acquire hands back a process that is already answering `ready()`, so
    consumers never have to poll for it themselves. The release always stops it,
    and the compiler runs that release even when a consumer fails.

    This is the Sonata twin of `workflow_tasks.components.container.
    managed_process_resource`, not a reuse of it: that one returns the legacy
    engine's `ResourceTask`, which `sonata_tasks` is forbidden to import.
    """
    process: Any | None = None

    def stop() -> None:
        nonlocal process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def acquire() -> None:
        nonlocal process
        if ready():
            # Something is already answering before we've spawned anything: an
            # orphan from a previous run, a concurrent invocation, or someone's
            # own instance. Starting our process on top of it would make this
            # acquire pass against the WRONG process (the original bug's shape),
            # so refuse instead of racing it.
            raise RuntimeError(
                f"{title}: refusing to start — something is already answering "
                "the readiness check"
            )
        current = spawn(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if current is None:  # pragma: no cover - invalid injected spawn contract
            raise RuntimeError(f"{title} failed to start")
        process = current
        try:
            for attempt in range(readiness_attempts):
                exit_code = current.poll()
                if exit_code is not None:
                    raise RuntimeError(
                        f"{title} exited with code {exit_code} before becoming ready"
                    )
                if ready():
                    return
                if attempt < readiness_attempts - 1:
                    sleep(readiness_interval)
            raise RuntimeError(f"{title} never became ready")
        except BaseException:
            # The engine never releases an acquire that did not complete, and
            # the wait can end in more ways than "gave up": ready() can raise,
            # or a KeyboardInterrupt can land during the up-to-90s wait. Any of
            # those must still stop the process we just spawned, or it leaks.
            stop()
            raise

    # infrastructure stays False on purpose: `keep_infrastructure` skips the release
    # of infrastructure resources, and a spawned child process that outlives the run
    # is a leak, not something a user asked to keep. A process always gets stopped.
    return Resource(title=title, acquire=acquire, release=stop)
