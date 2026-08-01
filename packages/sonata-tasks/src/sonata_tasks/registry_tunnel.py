from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import best_effort


_UNIT = "nanofaas-registry-tunnel"
_STOP = ("sudo", "systemctl", "stop", _UNIT)
_RESET = ("sudo", "systemctl", "reset-failed", _UNIT)


def registry_tunnel_resource(
    *,
    registry_upstream: str | Callable[[], str],
    provider: Any,
    request: Any,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[None]:
    """A socat tunnel on the ARM builder VM for accessing the stack registry.

    acquire:
        sudo systemd-run --unit nanofaas-registry-tunnel \
            socat TCP-LISTEN:5000,fork,reuseaddr TCP:{upstream}:5000

    release:
        sudo systemctl stop nanofaas-registry-tunnel
    """

    def run(argv: tuple[str, ...], *, action: str, absent_ok: bool = False) -> None:
        result = provider.exec_argv(
            request,
            argv,
        )
        rc = getattr(result, "return_code", 0)
        if rc != 0:
            detail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
            if absent_ok and any(
                marker in str(detail).lower()
                for marker in ("not loaded", "not found", "does not exist")
            ):
                return
            raise RuntimeError(
                f"registry tunnel {action} failed (exit {rc})" + (f": {detail}" if detail else "")
            )

    def cleanup() -> None:
        for argv in (_STOP, _RESET):
            try:
                _ = provider.exec_argv(request, argv)
            except BaseException:
                pass

    def acquire(_inputs: TaskInputs) -> None:
        try:
            run(_STOP, action="acquire", absent_ok=True)
            run(_RESET, action="acquire", absent_ok=True)
            upstream = registry_upstream() if callable(registry_upstream) else registry_upstream
            run(
                (
                    "sudo",
                    "systemd-run",
                    "--unit",
                    _UNIT,
                    "socat",
                    "TCP-LISTEN:5000,fork,reuseaddr",
                    f"TCP:{upstream}:5000",
                ),
                action="acquire",
            )
        except BaseException as error:
            best_effort(error, cleanup, what="registry tunnel failed acquire")
            raise

    def release(_inputs: TaskInputs, _state: None) -> None:
        try:
            run(_STOP, action="release", absent_ok=True)
        finally:
            try:
                _ = provider.exec_argv(request, _RESET)
            except BaseException:
                pass

    return Resource(
        title=(
            "Acquire registry tunnel to <release-stack>:5000"
            if callable(registry_upstream)
            else f"Acquire registry tunnel to {registry_upstream}:5000"
        ),
        acquire=acquire,
        release=release,
        requires=requires,
    )
