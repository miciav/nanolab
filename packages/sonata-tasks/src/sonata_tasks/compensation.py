from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs


def best_effort(error: BaseException, cleanup: Callable[[], object], *, what: str) -> None:
    """Clean up after a failed acquire without losing why it failed.

    Sonata never releases an acquire that did not pass — it cannot know whether
    the resource exists — so anything created before the failure leaks unless the
    acquire cleans up after itself. That cleanup can fail too, and when it does
    the interesting error is still the first one: the reason the acquire failed,
    not the reason the tidying up did. So the second becomes a note on the first
    rather than replacing it.

    Separate from `compensated_resource` because one resource cannot use that
    policy and still needs this half: a spawned process can only be stopped by
    the acquire that holds it, since nothing outside knows what was started.
    """
    try:
        _ = cleanup()
    except (OSError, RuntimeError) as cleanup_error:
        error.add_note(f"Best-effort {what} after a failed acquire failed: {cleanup_error}")


def compensated_resource[T](
    *,
    title: str,
    acquire: Callable[[TaskInputs], T],
    compensate: Callable[[TaskInputs], object],
    release: Callable[[TaskInputs, T], object] | None = None,
    requires: tuple[Resource[Any], ...] = (),
    always_release: bool = False,
    revive: Callable[[Any], T] | None = None,
) -> Resource[T]:
    """A resource whose acquire cleans up after itself when it fails.

    `acquire` produces the value rather than being handed one. That is what lets
    a VM's info — or a control plane's endpoint — be the resource's value: it
    does not exist until the acquire has run. An earlier version took a static
    `value`, and `vm_resource`, whose value comes from its acquire, could not use
    this policy at all and carried its own copy of it.

    `release` defaults to `compensate`, because for most resources undoing a
    finished acquire and undoing a failed one are the same command. `vm_resource`
    is the exception that needs both: its release destroys the VM the acquire
    returned, while its compensation destroys the one it was trying to create.
    """
    # The engine's release returns None; callers pass things that return a result
    # they do not care about, such as a task's `.run`. This absorbs the mismatch.
    def undo(inputs: TaskInputs, value: T) -> None:
        _ = compensate(inputs) if release is None else release(inputs, value)

    def acquire_with_compensation(inputs: TaskInputs) -> T:
        try:
            return acquire(inputs)
        except BaseException as error:
            best_effort(error, lambda: compensate(inputs), what=f"compensation for {title}")
            raise

    return Resource(
        title=title,
        acquire=acquire_with_compensation,
        release=undo,
        requires=requires,
        always_release=always_release,
        revive=revive,
    )
