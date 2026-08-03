from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanolab.release import attest


PROFILE = "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1"


def _record() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "version": "v0.18.0",
        "sourceCommit": "a" * 40,
        "imageDigests": {"ghcr.io/miciav/nanofaas/control-plane:v0.18.0-amd64-native": "sha256:" + "1" * 64},
        "profile": {"name": PROFILE},
        "runCount": 3,
        "thresholds": {},
        "aggregates": {
            "throughputRps": 100.0,
            "errorRate": 0.0,
            "latencyP95Ms": 12.0,
            "peakReplicas": 4.0,
        },
    }


def _images() -> dict[str, str]:
    return {
        "ghcr.io/miciav/nanofaas/control-plane:v0.18.0-amd64-native": "sha256:" + "1" * 64,
        "ghcr.io/miciav/nanofaas/control-plane:v0.18.0-native": "sha256:" + "2" * 64,
    }


# --- predicate -------------------------------------------------------------


def test_release_predicate_contains_the_required_evidence() -> None:
    predicate = attest.build_release_predicate(
        version="v0.18.0",
        source_commit="a" * 40,
        azure_profile=PROFILE,
        benchmark_record_digest="sha256:" + "3" * 64,
        image_digests=_images(),
    )

    assert predicate["schemaVersion"] == 1
    assert predicate["sourceCommit"] == "a" * 40
    assert predicate["azureProfile"] == PROFILE
    assert predicate["benchmarkRecordDigest"] == "sha256:" + "3" * 64
    assert predicate["imageDigests"] == _images()

    rendered = attest.render_predicate(predicate)
    assert rendered == attest.render_predicate(json.loads(rendered))


def test_predicate_rejects_missing_or_invalid_digests() -> None:
    with pytest.raises(ValueError):
        attest.build_release_predicate(
            version="v0.18.0",
            source_commit="a" * 40,
            azure_profile=PROFILE,
            benchmark_record_digest="not-a-digest",
            image_digests=_images(),
        )
    with pytest.raises(ValueError):
        attest.build_release_predicate(
            version="v0.18.0",
            source_commit="a" * 40,
            azure_profile=PROFILE,
            benchmark_record_digest="sha256:" + "3" * 64,
            image_digests={},
        )


# --- finalization ----------------------------------------------------------


def test_finalize_publishes_the_record_and_its_history(tmp_path: Path) -> None:
    docs = tmp_path / "performance"

    evidence = attest.finalize_release(record=_record(), performance_root=docs)

    release_file = docs / "releases" / "v0.18.0.json"
    history = docs / "history.md"
    assert json.loads(release_file.read_text(encoding="utf-8"))["version"] == "v0.18.0"
    assert "v0.18.0" in history.read_text(encoding="utf-8")
    references = {artifact.reference for artifact in evidence}
    assert references == {str(release_file), str(history)}


def test_history_regeneration_includes_previous_release_records(tmp_path: Path) -> None:
    docs = tmp_path / "performance"
    (docs / "releases").mkdir(parents=True)
    older = dict(_record(), version="v0.17.0")
    (docs / "releases" / "v0.17.0.json").write_text(
        json.dumps(older), encoding="utf-8"
    )

    attest.finalize_release(record=_record(), performance_root=docs)

    history = (docs / "history.md").read_text(encoding="utf-8")
    assert "v0.17.0" in history
    assert "v0.18.0" in history


def test_finalize_retries_after_a_documentation_failure_without_rebuilding(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "performance"
    (docs / "releases").mkdir(parents=True)
    # history.md is a directory: regeneration must fail after the record write
    blocker = docs / "history.md"
    blocker.mkdir()

    with pytest.raises(OSError):
        attest.finalize_release(record=_record(), performance_root=docs)

    blocker.rmdir()
    evidence = attest.finalize_release(record=_record(), performance_root=docs)

    assert (docs / "history.md").is_file()
    assert evidence
