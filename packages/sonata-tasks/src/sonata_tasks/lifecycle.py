from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from sonata_engine import Resource, TaskInputs

from sonata_tasks.command import CommandTask

T = TypeVar("T")


def compensated_resource(
    *,
    title: str,
    acquire: Sequence[CommandTask],
    compensate: CommandTask,
    value: T,
    requires: tuple[Resource[Any], ...] = (),
    infrastructure: bool = True,
) -> Resource[T]:
    """An acquire/release pair whose acquire cleans up after itself when it fails.

    Sonata never releases an acquire that did not pass — it cannot know whether
    the resource exists. So a step that created something before failing, or a
    readiness wait that timed out after a real creation, would leak unless the
    acquire compensates itself. That compensation runs `compensate`, the very
    task used for the release, and reports its own failure as a note rather than
    replacing the error that caused it.

    `acquire` runs in order and is a sequence because creating a thing and
    waiting for it to answer are separate commands that share one failure path.
    """

    def acquire_all(inputs: TaskInputs) -> T:
        try:
            for task in acquire:
                _ = task.run(inputs)
        except BaseException as error:
            try:
                _ = compensate.run(inputs)
            except BaseException as cleanup_error:
                error.add_note(
                    f"Best-effort {compensate.title} after a failed acquire failed: "
                    f"{cleanup_error}"
                )
            raise
        return value

    def release(inputs: TaskInputs, _acquired: T) -> None:
        _ = compensate.run(inputs)

    return Resource(
        title=title,
        acquire=acquire_all,
        release=release,
        requires=requires,
        infrastructure=infrastructure,
    )
