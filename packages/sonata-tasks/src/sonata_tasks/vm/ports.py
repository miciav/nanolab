from __future__ import annotations

from pathlib import Path
from typing import Protocol

from sonata_tasks.tasks.executors import VmCommandResult
from sonata_tasks.vm.models import VmConfig, VmInfo, VmRequest


class VmLifecycleProtocol(Protocol):
    def ensure_running(self, config: VmConfig) -> VmInfo: ...
    def destroy(self, info: VmInfo) -> None: ...


class VmCommandProvider(Protocol):
    """What the execution layer needs from a VM provider, and nothing else.

    The callers used to take `object` and reach through it behind a
    `type: ignore[attr-defined]`, which is why a keyword rename could only be
    caught by running the code. Naming the surface lets the checker follow it.

    `remote_dir` is a directory inside the VM; local paths belong to the caller's
    filesystem and must already be absolute, since the providers shell out from
    a working directory of their own.
    """

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = ...,
        remote_dir: str | None = ...,
        dry_run: bool = ...,
    ) -> VmCommandResult: ...

    def transfer_from(
        self,
        request: VmRequest,
        *,
        source: str,
        destination: Path,
    ) -> VmCommandResult: ...
