from __future__ import annotations

from pathlib import Path

from nanolab.tasks.vm.models import VmRequest
from nanolab.tasks.vm.ports import VmCommandProvider


class VmFileFetcher:
    """Implements RemoteFileFetcher using any provider's transfer_from()."""

    def __init__(self, vm: VmCommandProvider, request: VmRequest) -> None:
        self._vm = vm
        self._request = request

    def fetch_from(self, remote: str, local: Path) -> None:
        result = self._vm.transfer_from(self._request, source=remote, destination=local)
        return_code = getattr(result, "return_code", 0)
        if return_code != 0:
            stderr = getattr(result, "stderr", "") or ""
            stdout = getattr(result, "stdout", "") or ""
            raise RuntimeError(stderr or stdout or f"transfer failed (exit {return_code})")
