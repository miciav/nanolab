from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import best_effort


_UNIT = "nanofaas-registry-tunnel"
_STOP = ("sudo", "systemctl", "stop", _UNIT)
_RESET = ("sudo", "systemctl", "reset-failed", _UNIT)


def _run(
    provider: Any,
    request: Any,
    argv: tuple[str, ...],
    *,
    action: str,
    absent_ok: bool = False,
) -> None:
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


def _exec_quietly(provider: Any, request: Any, argv: tuple[str, ...]) -> None:
    try:
        _ = provider.exec_argv(request, argv)
    except RuntimeError:
        pass


def _stop_and_reset(provider: Any, request: Any) -> None:
    for argv in (_STOP, _RESET):
        _exec_quietly(provider, request, argv)


def _acquire_tunnel(
    provider: Any,
    request: Any,
    registry_upstream: str | Callable[[], str],
) -> None:
    try:
        _run(provider, request, _STOP, action="acquire", absent_ok=True)
        _run(provider, request, _RESET, action="acquire", absent_ok=True)
        upstream = registry_upstream() if callable(registry_upstream) else registry_upstream
        _run(
            provider,
            request,
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
        best_effort(
            error,
            lambda: _stop_and_reset(provider, request),
            what="registry tunnel failed acquire",
        )
        raise


def _release_tunnel(provider: Any, request: Any) -> None:
    try:
        _run(provider, request, _STOP, action="release", absent_ok=True)
    finally:
        _exec_quietly(provider, request, _RESET)


def _registry_title(registry_upstream: str | Callable[[], str]) -> str:
    if callable(registry_upstream):
        return "Acquire registry tunnel to <release-stack>:5000"
    return f"Acquire registry tunnel to {registry_upstream}:5000"


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

    def acquire(_inputs: TaskInputs) -> None:
        _acquire_tunnel(provider, request, registry_upstream)

    def release(_inputs: TaskInputs, _state: None) -> None:
        _release_tunnel(provider, request)

    return Resource(
        title=_registry_title(registry_upstream),
        acquire=acquire,
        release=release,
        requires=requires,
        always_release=True,
    )
