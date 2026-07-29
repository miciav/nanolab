from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from sonata_engine import Task, TaskInputs, TaskOutcome


@dataclass
class FileTransferTask(Task[None]):
    """Transfer a local file to a remote path via provider.transfer_to()."""

    source: Path
    destination: str
    provider: Any
    request: Any
    title: str = field(default="")

    def __post_init__(self) -> None:
        if not self.title:
            self.title = f"Transfer {self.source.name}"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        result = self.provider.transfer_to(
            self.request, source=self.source, destination=self.destination
        )
        rc = int(getattr(result, "return_code", 0))
        if rc != 0:
            detail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
            raise RuntimeError(
                f"{self.title} failed (exit {rc})" + (f": {detail}" if detail else "")
            )
        return TaskOutcome(value=None)
