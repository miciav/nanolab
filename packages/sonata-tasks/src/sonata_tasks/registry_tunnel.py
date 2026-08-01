from __future__ import annotations

from typing import Any

from sonata_engine import Resource, TaskInputs


def registry_tunnel_resource(
    *,
    registry_upstream: str,
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
        result = provider.exec_argv(
            request,
            (
                "sudo",
                "systemd-run",
                "--unit",
                "nanofaas-registry-tunnel",
                "socat",
                "TCP-LISTEN:5000,fork,reuseaddr",
                f"TCP:{registry_upstream}:5000",
            ),
        )
        rc = getattr(result, "return_code", 0)
        if rc != 0:
            detail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
            raise RuntimeError(
                f"registry tunnel acquire failed (exit {rc})"
                + (f": {detail}" if detail else "")
            )

    def release(_inputs: TaskInputs, _state: None) -> None:
        result = provider.exec_argv(
            request,
            ("sudo", "systemctl", "stop", "nanofaas-registry-tunnel"),
        )
        rc = getattr(result, "return_code", 0)
        if rc != 0:
            detail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
            raise RuntimeError(
                f"registry tunnel release failed (exit {rc})"
                + (f": {detail}" if detail else "")
            )

    return Resource(
        title=f"Acquire registry tunnel to {registry_upstream}:5000",
        acquire=acquire,
        release=release,
        requires=requires,
    )
