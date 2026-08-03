"""The release attestation predicate and the final release records.

Signing itself belongs to the Sonata DAG (`sonata_tasks.cosign`); what stays
here is the predicate the workflow signs and the documentation it publishes
once every signature has verified.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any

from nanolab.release.metrics import render_history, render_release_record
from nanolab.release.model import ArtifactEvidence, digest_path


ATTEST_PHASES = ("attest", "finalize")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def build_release_predicate(
    *,
    version: str,
    source_commit: str,
    azure_profile: str,
    benchmark_record_digest: str,
    image_digests: Mapping[str, str],
) -> dict[str, Any]:
    if not _DIGEST.fullmatch(benchmark_record_digest):
        raise ValueError("benchmark record digest must be a sha256 digest")
    if not image_digests:
        raise ValueError("release predicate requires at least one image digest")
    for reference, digest in image_digests.items():
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"invalid image digest for {reference}")
    return {
        "schemaVersion": 1,
        "version": version,
        "sourceCommit": source_commit,
        "azureProfile": azure_profile,
        "benchmarkRecordDigest": benchmark_record_digest,
        "imageDigests": dict(sorted(image_digests.items())),
    }


def render_predicate(predicate: Mapping[str, Any]) -> str:
    return json.dumps(predicate, indent=2, sort_keys=True) + "\n"


def performance_root(repo_root: Path) -> Path:
    return repo_root / "docs" / "performance"


def finalize_release(
    *,
    record: Mapping[str, Any],
    performance_root: Path,
) -> tuple[ArtifactEvidence, ...]:
    """Atomically publish the performance record and its history table.

    A failure writing either documentation file records no finalize evidence,
    so a resume retries finalization without rebuilding anything.
    """
    version = str(record["version"])
    releases_dir = performance_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    release_file = releases_dir / f"{version}.json"
    _write_atomic(release_file, render_release_record(record))

    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(releases_dir.glob("*.json"))
    ]
    history_file = performance_root / "history.md"
    _write_atomic(history_file, render_history(records))

    return (
        ArtifactEvidence("local", str(release_file), digest_path(release_file)),
        ArtifactEvidence("local", str(history_file), digest_path(history_file)),
    )


def _write_atomic(path: Path, content: str) -> None:
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(content, encoding="utf-8")
    staging.replace(path)
