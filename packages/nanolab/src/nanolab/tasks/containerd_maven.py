"""Stage only the three locally built containerd snapshot coordinates."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from nanolab.config.environment import EnvironmentConfig

_COORDINATES = (
    ("io/nanofaas", "containerd-java", "0.4.0-SNAPSHOT"),
    ("io/nanofaas", "containerd-java-cni", "0.4.0-SNAPSHOT"),
    ("io/libcni", "libcni-java", "0.1.1-SNAPSHOT"),
)
_MAX_STAGED_BYTES = 64 * 1024 * 1024


def remote_repository_path(environment: EnvironmentConfig) -> Path:
    """Give this environment instance an isolated, owned VM repository path."""
    home = Path(environment.target("stack").remote_home)
    return home / f"nanolab-containerd-maven-{environment.containerd_maven_token}"


def repository_for_build(environment: EnvironmentConfig | None) -> Path:
    """Resolve the same repository that provisioning stages for the stack."""
    if environment is None or environment.containerd_maven_repository is None:
        raise ValueError("containerdMavenRepository is required for a containerd build")
    if environment.provider == "multipass":
        return remote_repository_path(environment)
    if environment.provider == "local":
        return environment.containerd_maven_repository
    raise ValueError("containerd build requires a local or Multipass environment")


def stage_snapshot_repository(source: Path, destination: Path) -> dict[str, Any]:
    """Copy pinned coordinates and record exactly what will cross the VM boundary."""
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"containerd Maven repository is absent: {source}")
    if destination.exists():
        raise FileExistsError(destination)
    files: dict[str, str] = {}
    total = 0
    for group, artifact, version in _COORDINATES:
        parent = Path(group) / artifact
        version_dir = parent / version
        required = tuple(
            version_dir / f"{artifact}-{version}.{suffix}" for suffix in ("jar", "pom")
        )
        optional = (
            version_dir / f"{artifact}-{version}.module",
            version_dir / "maven-metadata-local.xml",
            parent / "maven-metadata-local.xml",
        )
        for relative in (*required, *optional):
            input_path = source / relative
            if relative in required and not input_path.is_file():
                raise ValueError(f"missing Maven artifact: {relative}")
            if not input_path.exists():
                continue
            if input_path.is_symlink() or not input_path.is_file():
                raise ValueError(f"unsupported Maven artifact: {relative}")
            size = input_path.stat().st_size
            total += size
            if total > _MAX_STAGED_BYTES:
                raise ValueError("containerd Maven stage exceeds 64 MiB")
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(input_path, output)
            files[relative.as_posix()] = hashlib.sha256(output.read_bytes()).hexdigest()
    receipt: dict[str, Any] = {
        "schema": "nanolab-containerd-maven-v1",
        "source": str(source),
        "total_bytes": total,
        "files": dict(sorted(files.items())),
    }
    (destination / "nanolab-receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return receipt
