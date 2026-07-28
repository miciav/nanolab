from __future__ import annotations

from pathlib import Path

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


class PrometheusScrapeCheckTask(CommandTask):
    """Scrape an actuator endpoint and assert which samples are there.

    `expect` names samples that must appear, `reject` samples that must not. Both
    are matched as plain substrings of the scrape, which is how the shell version
    did it with `grep -F` — the difference is what happens when one does not hold.

    That version was two `grep -F` calls joined by `&&`: a failure exited 1 and
    said nothing, so a missing metric, one carrying the wrong label, and an
    unexpected failure counter were the same non-zero exit. This names the sample
    that broke the expectation, and which way.
    """

    def __init__(
        self,
        *,
        url: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        expect: tuple[str, ...] = (),
        reject: tuple[str, ...] = (),
        title: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        if not expect and not reject:
            raise ValueError("a scrape check must expect or reject at least one sample")

        def check(result: TaskResult) -> None:
            for sample in expect:
                if sample not in result.stdout:
                    raise RuntimeError(f"{url}: expected sample not scraped: {sample}")
            for sample in reject:
                if sample in result.stdout:
                    raise RuntimeError(f"{url}: sample present and should not be: {sample}")

        super().__init__(
            title=title or f"Check the metrics at {url}",
            argv=("curl", "-fsS", url),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=check,
        )
