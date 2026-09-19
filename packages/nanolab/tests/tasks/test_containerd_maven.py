"""The VM receives only the reviewed snapshot coordinates and a receipt."""

import hashlib
import json
from pathlib import Path

import pytest

from nanolab.config.environment import EnvironmentConfig
from nanolab.tasks.containerd_maven import (
    remote_repository_path,
    stage_snapshot_repository,
)

COORDINATES = (
    ("io/nanofaas", "containerd-java", "0.4.0-SNAPSHOT"),
    ("io/nanofaas", "containerd-java-cni", "0.4.0-SNAPSHOT"),
    ("io/libcni", "libcni-java", "0.1.1-SNAPSHOT"),
)


def _repository(root: Path) -> None:
    for group, artifact, version in COORDINATES:
        folder = root / group / artifact / version
        folder.mkdir(parents=True)
        for suffix in ("jar", "pom"):
            (folder / f"{artifact}-{version}.{suffix}").write_text(
                f"{artifact}-{suffix}"
            )
        (folder / "maven-metadata-local.xml").write_text("metadata")
    (root / "io/nanofaas/containerd-java/0.4.0-SNAPSHOT/secret.txt").write_text(
        "should not transfer"
    )
    (root / "com/private").mkdir(parents=True)
    (root / "com/private/credentials.xml").write_text("secret")


def test_stage_snapshot_repository_filters_and_receipts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "staged"
    _repository(source)
    receipt = stage_snapshot_repository(source, target)
    assert not (target / "com").exists()
    assert not (
        target / "io/nanofaas/containerd-java/0.4.0-SNAPSHOT/secret.txt"
    ).exists()
    files = json.loads((target / "nanolab-receipt.json").read_text())["files"]
    assert len(files) == 9
    assert receipt["files"] == files
    jar = (
        "io/nanofaas/containerd-java/0.4.0-SNAPSHOT/containerd-java-0.4.0-SNAPSHOT.jar"
    )
    assert files[jar] == hashlib.sha256(b"containerd-java-jar").hexdigest()


def test_stage_snapshot_repository_requires_complete_coordinate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _repository(source)
    (
        source / "io/libcni/libcni-java/0.1.1-SNAPSHOT/libcni-java-0.1.1-SNAPSHOT.pom"
    ).unlink()
    with pytest.raises(ValueError, match="missing Maven artifact"):
        stage_snapshot_repository(source, tmp_path / "staged")


def test_remote_repository_path_is_owned_per_environment(tmp_path: Path) -> None:
    env = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "fresh-vm", "user": "ubuntu"}},
            "containerdMavenRepository": str(tmp_path),
        }
    )
    path = remote_repository_path(env)
    assert str(path).startswith("/home/ubuntu/nanolab-containerd-maven-")
    assert remote_repository_path(env) == path
    assert path != remote_repository_path(
        EnvironmentConfig.model_validate(
            {
                "provider": "multipass",
                "roles": {"stack": {"name": "fresh-vm"}},
                "containerdMavenRepository": str(tmp_path),
            }
        )
    )
