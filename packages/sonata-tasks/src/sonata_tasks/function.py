from __future__ import annotations

from typing import Any

from sonata_engine import Resource, TaskInputs

from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource


def function_resource(
    *,
    name: str,
    register: CommandTask,
    delete: CommandTask,
    readiness: tuple[CommandTask, ...] = (),
    requires: tuple[Resource[Any], ...] = (),
    always_release: bool = True,
) -> Resource[None]:
    """A registered nanoFaaS function as an acquire/release pair.

    The commands are injected because every workflow registers differently — the
    CLI applies a manifest through its own binary, others POST with curl — while
    the lifecycle around them is `compensated_resource`, shared with Helm.

    `always_release` is True for the same reason `managed_process_resource` sets
    it: a function still registered after the run is not something `--keep` was
    asked for — it is what makes the next run fail. It did, twice: a `--keep`
    run left `word-stats-java` registered, and the following one died on
    `register ... 409`. `--keep` means keep the VM and the platform on it, both
    expensive to rebuild; re-registering a function costs a second.

    `readiness` runs after a successful register, for backends where the function
    answers only once its container or pod is up; it shares the register's failure
    path, so a timeout still deletes what the register created.

    `requires` is a real compiled edge (e.g. the Helm release whose control plane
    the function is registered against), not merely something every consumer also
    happens to list: a slice that keeps this function keeps its dependencies too.
    """

    def acquire(inputs: TaskInputs) -> None:
        _ = register.run(inputs)
        for check in readiness:
            _ = check.run(inputs)

    return compensated_resource(
        title=f"Acquire {name}",
        acquire=acquire,
        compensate=delete.run,
        requires=requires,
        always_release=always_release,
    )
