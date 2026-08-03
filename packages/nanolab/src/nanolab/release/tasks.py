"""Coarse release phases that expose only durable Sonata evidence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

from sonata_engine import Evidence, ReusableTask, Task, TaskInputs, TaskOutcome
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.tasks.models import CommandTaskSpec

from nanolab.release.evidence import is_sha256_digest, receipt_artifacts
from nanolab.release.state import ReleaseIdentity, digest_path
from nanolab.release.state import ArtifactEvidence


PhaseWork = Callable[[TaskInputs], Iterable[Evidence]]

# The two ways a phase proves it holds an image, and the reference scheme each
# one must carry: the registry (skopeo) and the local daemon (docker inspect).
MATRIX_DIGEST_SCHEMES = {
    "local-registry-digest": "docker://",
    "local-image-digest": "docker-daemon:",
}


def versioned_release_run_dir(run_dir: Path, version: str) -> Path:
    """Normalize a generic run root to one directory per release version."""
    root = Path(run_dir)
    names = {version, version.removeprefix("v"), f"v{version.removeprefix('v')}"}
    return (
        root
        if root.name in names and root.parent.name == "releases"
        else root / "releases" / version
    )


@dataclass(frozen=True, slots=True)
class ReleasePhaseTask(ReusableTask):
    """One reusable release boundary; domain work stays in the supplied callable."""

    phase: str
    identity: ReleaseIdentity
    run_dir: Path
    phase_inputs: Mapping[str, Any]
    work: PhaseWork = field(repr=False, compare=False)
    prerequisites: tuple[Path, ...] = ()
    expected_images: tuple[str, ...] = ()
    title: str = ""
    idempotent: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_dir",
            versioned_release_run_dir(self.run_dir, self.identity.prepared_version),
        )
        if not self.title:
            object.__setattr__(self, "title", self.phase)

    @property
    def receipt(self) -> Path:
        # Own subdirectory, not run_dir itself: several phases write their
        # artifact to run_dir/<phase>.json, and a receipt sharing that name
        # replaces the artifact right after its digest is recorded.
        return self.run_dir / "receipts" / f"{self.phase}.json"

    @property
    def reuse_key(self) -> str:
        payload = {
            "schema": 1,
            "phase": self.phase,
            "release": self.identity.as_entry(),
            "inputs": self.phase_inputs,
            "prerequisites": tuple(map(str, self.prerequisites)),
            "expectedImages": self.expected_images,
            "receipt": str(self.receipt),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return f"release:{self.phase}:sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        prerequisite_evidence = tuple(
            Evidence("file-digest", str(path), digest_path(path)) for path in self.prerequisites
        )
        produced = tuple(self.work(inputs))
        if self.expected_images:
            # Both digest kinds cover the same matrix: the build phase inspects
            # the daemon (docker-daemon:), the push phase inspects the registry
            # (docker://). Compare the image, not the scheme it was read through
            # -- but a reference missing its own scheme stays uncounted, so
            # malformed evidence still fails the matrix.
            matrix = tuple(entry for entry in produced if entry.kind in MATRIX_DIGEST_SCHEMES)
            references = {
                entry.reference.removeprefix(MATRIX_DIGEST_SCHEMES[entry.kind])
                for entry in matrix
                if entry.reference.startswith(MATRIX_DIGEST_SCHEMES[entry.kind])
            }
            if (
                len(matrix) != len(self.expected_images)
                or references != set(self.expected_images)
                or any(not is_sha256_digest(entry.digest) for entry in matrix)
            ):
                raise RuntimeError(f"{self.phase} evidence does not cover the image matrix")

        _write_receipt(self.receipt, self.phase, produced)
        receipt = Evidence("file-digest", str(self.receipt), digest_path(self.receipt))
        return TaskOutcome(evidence=prerequisite_evidence + produced + (receipt,))


def source_test_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="source-tests", title="Run source tests", **kwargs)


def amd64_build_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="amd64-build", title="Build AMD64 images", **kwargs)


def registry_push_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(
        phase="local-registry-push", title="Push AMD64 images to local registry", **kwargs
    )


def benchmark_task(index: int, **kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(
        phase=f"benchmark-{index}", title=f"Run release benchmark {index}", **kwargs
    )


def aggregate_benchmarks_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="aggregate", title="Aggregate benchmarks", **kwargs)


def regression_gate_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="regression-gate", title="Evaluate regression gate", **kwargs)


def arm64_build_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="arm64-build", title="Build ARM64 images", **kwargs)


def arm64_smoke_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="arm64-smoke", title="Test ARM64 images", **kwargs)


def publish_architectures_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(
        phase="publish-architectures", title="Publish architecture images", **kwargs
    )


def publish_manifests_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="publish-manifests", title="Publish image manifests", **kwargs)


def publish_aliases_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="publish-aliases", title="Publish image aliases", **kwargs)


def attest_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="attest", title="Attest published images", **kwargs)


def finalize_task(**kwargs: Any) -> ReleasePhaseTask:
    return ReleasePhaseTask(phase="finalize", title="Finalize release documentation", **kwargs)


def registry_evidence(artifacts: Iterable[ArtifactEvidence]) -> tuple[Evidence, ...]:
    return tuple(
        Evidence("local-registry-digest", artifact.reference, artifact.digest)
        for artifact in artifacts
    )


def registry_artifacts_from_receipt(
    receipt: Path, images: tuple[str, ...]
) -> tuple[ArtifactEvidence, ...]:
    evidence = receipt_artifacts(receipt, "arm64-build", "local-registry-digest")
    expected = {f"docker://{image}" for image in images}
    if (
        len(evidence) != len(expected)
        or {item.reference for item in evidence} != expected
        or any(not is_sha256_digest(item.digest) for item in evidence)
    ):
        raise RuntimeError("ARM64 build receipt does not cover the image matrix")
    return evidence


def exact_receipt_artifacts(
    receipt: Path,
    phase: str,
    kind: str,
    expected_references: Iterable[str],
) -> tuple[ArtifactEvidence, ...]:
    """Require one canonical receipt to cover exactly the expected references."""
    expected = tuple(expected_references)
    evidence = receipt_artifacts(receipt, phase, kind)
    references = tuple(item.reference for item in evidence)
    if (
        len(expected) != len(set(expected))
        or len(references) != len(expected)
        or len(references) != len(set(references))
        or set(references) != set(expected)
        or any(not is_sha256_digest(item.digest) for item in evidence)
    ):
        raise RuntimeError(f"{phase} receipt does not have exact artifact coverage")
    return evidence


def verified_file_receipt(receipt: Path, phase: str, expected: Path) -> ArtifactEvidence:
    """Require one exact file artifact and verify its current digest."""
    artifact = exact_receipt_artifacts(receipt, phase, "file-digest", (str(expected),))[0]
    if digest_path(expected) != artifact.digest:
        raise RuntimeError(f"{phase} evidence changed")
    return artifact


def verified_json_receipt(receipt: Path, phase: str, expected: Path) -> Mapping[str, Any]:
    verified_file_receipt(receipt, phase, expected)
    try:
        payload = json.loads(expected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{phase} evidence is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{phase} evidence is not a JSON object")
    return payload


def require_release_barriers(
    *,
    gate_receipt: Path,
    gate_file: Path,
    smoke_receipt: Path,
    smoke_file: Path,
    arm_build_receipt: Path,
    arm_images: tuple[str, ...],
) -> tuple[ArtifactEvidence, ...]:
    decision = verified_json_receipt(gate_receipt, "regression-gate", gate_file)
    if decision.get("passed") is not True:
        raise RuntimeError("publication requires a passing regression gate")
    arm_evidence = exact_receipt_artifacts(
        arm_build_receipt,
        "arm64-build",
        "local-registry-digest",
        tuple(f"docker://{image}" for image in arm_images),
    )
    smoke = verified_json_receipt(smoke_receipt, "arm64-smoke", smoke_file)
    expected_images = {
        item.reference.removeprefix("docker://"): item.digest for item in arm_evidence
    }
    if smoke.get("architecture") != "linux/arm64" or smoke.get("images") != expected_images:
        raise RuntimeError("ARM smoke evidence does not match the ARM build")
    return arm_evidence


def require_attestation_predicate(
    receipt: Path,
    predicate: Path,
    expected: Mapping[str, Any],
) -> None:
    if verified_json_receipt(receipt, "attest", predicate) != expected:
        raise RuntimeError("attestation predicate does not match the release evidence")


def run_source_steps(steps: Task[Any], inputs: TaskInputs) -> tuple[Evidence, ...]:
    _run_steps(steps, inputs)
    return ()


def run_image_steps(
    steps: Task[Any],
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    images: tuple[str, ...],
    *,
    registry: bool,
) -> tuple[Evidence, ...]:
    """Run build/push steps and capture the complete current image matrix."""
    _run_steps(steps, inputs)
    evidence: list[Evidence] = []
    for image in images:
        argv = (
            (
                "skopeo",
                "inspect",
                "--tls-verify=false",
                "--format={{.Digest}}",
                f"docker://{image}",
            )
            if registry
            else ("docker", "image", "inspect", "--format={{.Id}}", image)
        )
        result = executor.run(
            CommandTaskSpec(
                task_id="",
                summary=f"Verify {image}",
                argv=argv,
                role="stack",
            )
        )
        digest = result.stdout.strip() if isinstance(result.stdout, str) else None
        if result.status != "passed" or not is_sha256_digest(digest):
            raise RuntimeError(f"invalid image digest for {image}")
        evidence.append(
            Evidence(
                "local-registry-digest" if registry else "local-image-digest",
                f"docker://{image}" if registry else f"docker-daemon:{image}",
                digest,
            )
        )
    return tuple(evidence)


def _run_steps(steps: Task[Any], inputs: TaskInputs) -> None:
    outcome = steps.run(inputs)
    if not isinstance(outcome, TaskOutcome):
        raise RuntimeError(f"{steps.title} returned an invalid outcome")


def _write_receipt(path: Path, phase: str, evidence: tuple[Evidence, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase,
        "execution": uuid.uuid4().hex,
        "evidence": [
            {"kind": item.kind, "reference": item.reference, "digest": item.digest}
            for item in evidence
        ],
    }
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
