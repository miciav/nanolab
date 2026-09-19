"""Bound remote reads of run-owned containerd and systemd soak processes."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.models import Target


class RootlessCollectionTransport:
    """Ask the stack machine to inspect actual task PIDs and cgroup-v2 files."""

    def __init__(self, run: RootlessRun, executor: CommandTaskExecutor):
        """Bind the owned run and stack executor to the installed helper."""
        self.run = run
        self.executor = executor
        self.helper = run.script.with_name("soak_collect.py")

    def _read(
        self, action: str, role: str, *args: str, timeout_s: float
    ) -> dict[str, Any]:
        result = self.executor.run(
            CommandTaskSpec(
                task_id="",
                summary=f"Observe owned containerd soak {role}",
                argv=(
                    "python3",
                    str(self.helper),
                    action,
                    self.run.run_id,
                    role,
                    str(self.run.repo_root),
                    str(self.run.script),
                    *args,
                ),
                role="stack",
                options=CommandOptions(timeout_seconds=timeout_s),
            )
        )
        if result.status != "passed" or result.return_code != 0:
            raise OSError(
                f"containerd observation failed for {role}: {result.stderr[:512]}"
            )
        if len(result.stdout) > 4 * 1024 * 1024:
            raise ValueError("containerd observation exceeds output bound")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("containerd observation must be an object")
        return data

    def inspect(
        self, role: str, timeout_s: float = 10
    ) -> tuple[Target, dict[str, Any]]:
        """Return the exact live target and artifact metadata for one role."""
        data = self._read("inspect", role, timeout_s=timeout_s)
        target = Target(**{key: data[key] for key in Target.__dataclass_fields__})
        return target, data

    def collect(
        self, target: Target, endpoint: str | None, timeout_s: float
    ) -> dict[str, Any]:
        """Collect bounded procfs, cgroup and in-namespace exposition data."""
        # Exposition is collected in the target's own network namespace by the
        # remote helper. No function port is published to the host.
        _ = endpoint
        if timeout_s <= 0:
            raise ValueError("collection timeout must be positive")
        expected = {**asdict(target)}
        observed = self._read("inspect", target.role, timeout_s=timeout_s)
        expected.update(
            {
                key: observed[key]
                for key in ("artifact_kind", "artifact_path", "platform")
                if key in observed
            }
        )
        return self._read(
            "sample",
            target.role,
            json.dumps(expected, separators=(",", ":")),
            str(timeout_s),
            timeout_s=timeout_s + 2,
        )
