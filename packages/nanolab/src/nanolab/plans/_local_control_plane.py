"""Shared knowledge about the local, container-backed control plane process.

Both the `validate` and `cli` workflows can spawn this exact process when
their scenario's `backend` is `container`: the control-plane jar, on this
machine, on fixed ports, deployed with Docker. The two plan builders
(`nanolab.plans.validate` and `nanolab.plans.cli`) share this definition so
the process remains one fact, not two. A wrong port or path here is exactly
the shape of the original bug that made the `cli` workflow's readiness check
impossible to satisfy.

Both workflows bind the SAME fixed ports (18080/18081), so a `validate`
container run and a `cli` container run can never run concurrently on the
same machine.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

from sonata_tasks.deployment import (
    LOCAL_CONTROL_PLANE_API_PORT,
    LOCAL_CONTROL_PLANE_MANAGEMENT_PORT,
)

API_PORT = LOCAL_CONTROL_PLANE_API_PORT
MANAGEMENT_PORT = LOCAL_CONTROL_PLANE_MANAGEMENT_PORT
ENDPOINT = f"http://127.0.0.1:{API_PORT}"
HEALTH_URL = f"http://127.0.0.1:{MANAGEMENT_PORT}/actuator/health"


def jar_path(repo_root: Path) -> Path:
    return repo_root / "platform/control-plane/build/libs/app.jar"


def argv(repo_root: Path) -> tuple[str, ...]:
    return (
        "java",
        "-jar",
        str(jar_path(repo_root)),
        f"--server.port={API_PORT}",
        f"--management.server.port={MANAGEMENT_PORT}",
        "--sync-queue.enabled=false",
        "--nanofaas.deployment.default-backend=container-local",
        "--nanofaas.container-local.runtime-adapter=docker",
        "--nanofaas.container-local.bind-host=127.0.0.1",
    )


def ready() -> bool:
    """The health probe deliberately targets the management port: the actuator
    never lives on the API port, which is what made the old `cli` preflight
    impossible to satisfy."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1) as response:
            return response.status == 200
    except OSError:
        return False
