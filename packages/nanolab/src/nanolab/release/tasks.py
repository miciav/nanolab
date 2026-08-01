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

from nanolab.release.evidence import is_sha256_digest
from nanolab.release.state import ReleaseIdentity, digest_path


PhaseWork = Callable[[TaskInputs], Iterable[Evidence]]


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
        return self.run_dir / f"{self.phase}.json"

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
        produced = tuple(self.work(inputs))
        if self.expected_images:
            registry_evidence = tuple(
                entry for entry in produced if entry.kind == "local-registry-digest"
            )
            expected = {f"docker://{image}" for image in self.expected_images}
            if (
                len(registry_evidence) != len(expected)
                or {entry.reference for entry in registry_evidence} != expected
                or any(not is_sha256_digest(entry.digest) for entry in registry_evidence)
            ):
                raise RuntimeError("local-registry-push evidence does not cover the image matrix")

        prerequisite_evidence = tuple(
            Evidence("file-digest", str(path), digest_path(path)) for path in self.prerequisites
        )
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
    return ReleasePhaseTask(
        phase="regression-gate", title="Evaluate regression gate", **kwargs
    )


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
