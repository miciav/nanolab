from __future__ import annotations

from typing import Any

from sonata_engine import Resource, TaskInputs

from sonata_tasks.command import CommandTask


def function_resource(
    *,
    name: str,
    register: CommandTask,
    delete: CommandTask,
    readiness: tuple[CommandTask, ...] = (),
    requires: tuple[Resource[Any], ...] = (),
    infrastructure: bool = True,
) -> Resource[None]:
    """A registered nanoFaaS function as an acquire/release pair.

    The commands are injected because every workflow registers differently — the
    CLI applies a manifest through its own binary, others POST with curl — while
    the lifecycle around them is always the same. That lifecycle is what this
    function owns:

    * the compiler splices the register before the first task that needs the
      function and the delete after the last one, and runs the delete even when a
      consumer fails, which is why no workflow needs a separate cleanup list;
    * `readiness` runs after a successful register, for backends where the
      function answers only once its container or pod is up;
    * a register that fails *after* creating the function would leak it, because
      the engine never releases an acquire that did not pass — so the
      compensation happens here, best-effort, and never masks the original error.

    `requires` is a real compiled edge (e.g. the Helm release whose control plane
    the function is registered against), not merely something every consumer also
    happens to list: a slice that keeps this function keeps its dependencies too.
    """

    def acquire(_inputs: TaskInputs) -> None:
        try:
            _ = register.run(TaskInputs.empty())
            for ready_task in readiness:
                _ = ready_task.run(TaskInputs.empty())
        except BaseException as error:
            try:
                _ = delete.run(TaskInputs.empty())
            except BaseException as cleanup_error:
                error.add_note(
                    f"Best-effort delete after a failed register failed: {cleanup_error}"
                )
            raise

    def release(_inputs: TaskInputs, _value: None) -> None:
        _ = delete.run(TaskInputs.empty())

    return Resource(
        title=f"Acquire {name}",
        acquire=acquire,
        release=release,
        requires=requires,
        infrastructure=infrastructure,
    )
