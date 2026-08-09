from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.loadtest.models import K6Config, K6RunResult
from sonata_tasks.tasks.models import CommandTaskSpec


def k6_argv(config: K6Config, target_url: str) -> tuple[str, ...]:
    argv: list[str] = [
        "k6",
        "run",
        "--summary-export",
        str(config.summary_output_path),
        "--summary-trend-stats",
        "avg,min,med,max,p(50),p(90),p(95),p(99)",
    ]
    if config.vus is not None:
        argv.extend(("--vus", str(config.vus)))
    if config.duration is not None:
        argv.extend(("--duration", config.duration))
    if config.vus is None and config.duration is None:
        for stage in config.stages:
            argv.extend(("--stage", f"{stage.duration}:{stage.target}"))
    for key, value in ({**config.env, "NANOFAAS_URL": target_url}).items():
        argv.extend(("-e", f"{key}={value}"))
    if config.payload_path is not None:
        argv.extend(("-e", f"NANOFAAS_PAYLOAD={config.payload_path}"))
    argv.append(str(config.script_path))
    return tuple(argv)


class K6Task(Task[K6RunResult]):
    """Run k6 through a Sonata role and retain threshold failures for reporting."""

    def __init__(
        self,
        config: K6Config,
        *,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        remote_dir: str | None = None,
        title: str = "Run k6",
        cwd: Path | None = None,
        require_pass: bool = False,
    ) -> None:
        self.title = title
        self._config = config
        self._executor = executor
        self._role = role
        self._remote_dir = remote_dir
        self._cwd = cwd
        self._require_pass = require_pass

    def run(self, inputs: TaskInputs) -> TaskOutcome[K6RunResult]:
        target_url = (
            self._config.target_url
            if isinstance(self._config.target_url, str)
            else inputs.resource(self._config.target_url)
        )
        started_at = datetime.now(timezone.utc)
        result = self._executor.run(
            CommandTaskSpec(
                task_id="",
                summary=self.title,
                argv=k6_argv(self._config, target_url),
                role=self._role,
                cwd=self._cwd,
                remote_dir=self._remote_dir,
                expected_exit_codes=frozenset({0, 99}),
            )
        )
        ended_at = datetime.now(timezone.utc)
        if result.return_code not in (0, 99):
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise RuntimeError(f"{self.title} failed (exit {result.return_code}): {detail}")
        if self._require_pass and result.return_code == 99:
            raise RuntimeError(f"{self.title} failed: k6 thresholds failed")
        return TaskOutcome(
            value=K6RunResult(
                summary_path=self._config.summary_output_path,
                started_at=started_at,
                ended_at=ended_at,
                passed=result.return_code == 0,
            )
        )
