import json
from pathlib import Path

import pytest
from sonata_engine import Evidence, JournalConfig, TaskInputs, TaskOutcome, Workflow
from workflow_tasks.tasks.models import TaskResult

from nanolab.release.evidence import file_digest_verifier, receipt_artifacts
from nanolab.release import tasks as release_tasks
from nanolab.release.model import ReleaseIdentity, digest_path
from nanolab.release.tasks import (
    amd64_build_task,
    arm64_build_task,
    arm64_smoke_task,
    registry_push_task,
    registry_artifacts_from_receipt,
    run_image_steps,
    run_source_steps,
    source_test_task,
    publish_architectures_task,
    publish_manifests_task,
    publish_aliases_task,
    attest_task,
    finalize_task,
    exact_receipt_artifacts,
    verified_file_receipt,
    require_release_barriers,
    require_attestation_predicate,
)


def _identity(**changes: str) -> ReleaseIdentity:
    values = {
        "source_commit": "a" * 40,
        "prepared_version": "1.2.3",
        "release_config_digest": "sha256:" + "b" * 64,
        "environment_digest": "sha256:" + "c" * 64,
    }
    values.update(changes)
    return ReleaseIdentity(**values)


def test_reuse_key_is_secret_free_and_covers_identity_and_phase_inputs(tmp_path: Path) -> None:
    secret = "fixture-ghcr-token-must-not-leak"

    def work_with_secret_closed_over(_inputs: TaskInputs):
        _ = secret
        return ()

    base = source_test_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"scenario": "sha256:" + "d" * 64},
        work=work_with_secret_closed_over,
    )
    changed_identity = source_test_task(
        identity=_identity(source_commit="e" * 40),
        run_dir=tmp_path,
        phase_inputs={"scenario": "sha256:" + "d" * 64},
        work=lambda _inputs: (),
    )
    changed_input = source_test_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"scenario": "sha256:" + "f" * 64},
        work=lambda _inputs: (),
    )
    changed_receipt = source_test_task(
        identity=_identity(),
        run_dir=tmp_path / "other",
        phase_inputs={"scenario": "sha256:" + "d" * 64},
        work=lambda _inputs: (),
    )

    assert (
        len(
            {
                base.reuse_key,
                changed_identity.reuse_key,
                changed_input.reuse_key,
                changed_receipt.reuse_key,
            }
        )
        == 4
    )
    assert secret not in base.reuse_key
    base.run(TaskInputs.empty())
    assert secret not in base.receipt.read_text(encoding="utf-8")


def test_phase_tasks_write_compact_receipts_and_registry_covers_matrix(tmp_path: Path) -> None:
    digest = "sha256:" + "d" * 64
    source = source_test_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"commands": "v1"},
        work=lambda _inputs: (Evidence("file-digest", __file__, digest_path(Path(__file__))),),
    )
    source_outcome = source.run(TaskInputs.empty())
    build = amd64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image-a:v1", "image-b:v1"]},
        prerequisites=(source.receipt,),
        work=lambda _inputs: (
            Evidence("local-image-digest", "docker-daemon:image-a:v1", digest),
            Evidence("local-image-digest", "docker-daemon:image-b:v1", digest),
        ),
    )
    build_outcome = build.run(TaskInputs.empty())
    push = registry_push_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image-a:v1", "image-b:v1"]},
        expected_images=("image-a:v1", "image-b:v1"),
        prerequisites=(source.receipt, build.receipt),
        work=lambda _inputs: (
            Evidence("local-registry-digest", "docker://image-a:v1", digest),
            Evidence("local-registry-digest", "docker://image-b:v1", digest),
        ),
    )
    push_outcome = push.run(TaskInputs.empty())

    assert source_outcome.value is build_outcome.value is push_outcome.value is None
    assert json.loads(push.receipt.read_text(encoding="utf-8"))["phase"] == "local-registry-push"
    assert {
        item.reference for item in push_outcome.evidence if item.kind == "local-registry-digest"
    } == {
        "docker://image-a:v1",
        "docker://image-b:v1",
    }


@pytest.mark.parametrize(
    ("factory", "phase"),
    [
        (release_tasks.aggregate_benchmarks_task, "aggregate"),
        (release_tasks.source_test_task, "source-tests"),
        (release_tasks.arm64_smoke_task, "arm64-smoke"),
    ],
)
def test_receipt_never_overwrites_the_artifact_its_own_phase_produced(
    factory, phase: str, tmp_path: Path
) -> None:
    """These three phases write `<phase>.json` into run_dir as their artifact.

    A receipt stored under the same name replaces the artifact after its digest
    is taken, so every later reader sees the receipt and the digest no longer
    matches what the receipt claims.
    """
    task = factory(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={},
        work=lambda _inputs: (_write_artifact(task.run_dir / f"{phase}.json"),),
    )

    task.run(TaskInputs.empty())

    artifact = task.run_dir / f"{phase}.json"
    assert json.loads(artifact.read_text(encoding="utf-8")) == {"produced": True}
    recorded = receipt_artifacts(task.receipt, phase, "file-digest")
    assert [(item.reference, item.digest) for item in recorded] == [
        (str(artifact), digest_path(artifact))
    ]


def _write_artifact(path: Path) -> Evidence:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"produced": True}), encoding="utf-8")
    return Evidence("file-digest", str(path), digest_path(path))


def test_journal_resume_reruns_only_unsafe_suffix(tmp_path: Path) -> None:
    identity = _identity()
    calls = {"source-tests": 0, "amd64-build": 0, "local-registry-push": 0}
    digest = "sha256:" + "d" * 64

    def work(phase: str, evidence: Evidence):
        def run(_inputs: TaskInputs):
            calls[phase] += 1
            return (evidence,)

        return run

    source_artifact = tmp_path / "source-output"
    source_artifact.write_text("source", encoding="utf-8")
    build_artifact = tmp_path / "build-output"
    build_artifact.write_text("build", encoding="utf-8")
    workflow = Workflow("release-resume")
    source = source_test_task(
        identity=identity,
        run_dir=tmp_path,
        phase_inputs={"source": identity.source_commit},
        work=work(
            "source-tests",
            Evidence("file-digest", str(source_artifact), digest_path(source_artifact)),
        ),
    )
    build = amd64_build_task(
        identity=identity,
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image:v1"]},
        prerequisites=(source.receipt,),
        work=work(
            "amd64-build", Evidence("file-digest", str(build_artifact), digest_path(build_artifact))
        ),
    )
    push = registry_push_task(
        identity=identity,
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image:v1"]},
        expected_images=("image:v1",),
        prerequisites=(source.receipt, build.receipt),
        work=work(
            "local-registry-push",
            Evidence("local-registry-digest", "docker://image:v1", digest),
        ),
    )
    workflow.add(source)
    workflow.add(build)
    workflow.add(push)
    journal = JournalConfig(tmp_path / "journal.jsonl")
    verifiers = {
        "file-digest": file_digest_verifier,
        "local-registry-digest": lambda evidence: evidence.digest == digest,
    }

    workflow.run(journal=journal, verifiers=verifiers)
    workflow.run(journal=journal, verifiers=verifiers, resume=True)
    assert calls == {"source-tests": 1, "amd64-build": 1, "local-registry-push": 1}

    build.receipt.write_text("changed", encoding="utf-8")
    workflow.run(journal=journal, verifiers=verifiers, resume=True)
    assert calls == {"source-tests": 1, "amd64-build": 2, "local-registry-push": 2}


def test_registry_push_rejects_incomplete_dynamic_matrix(tmp_path: Path) -> None:
    task = registry_push_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image-a:v1", "image-b:v1"]},
        expected_images=("image-a:v1", "image-b:v1"),
        work=lambda _inputs: (
            Evidence("local-registry-digest", "docker://image-a:v1", "sha256:" + "d" * 64),
        ),
    )

    with pytest.raises(RuntimeError, match="does not cover the image matrix"):
        task.run(TaskInputs.empty())


@pytest.mark.parametrize(
    ("reference", "digest"),
    [
        ("image-a:v1", "sha256:" + "d" * 64),
        ("docker://image-a:v1", None),
        ("docker://image-a:v1", "sha256:" + "z" * 64),
    ],
)
def test_registry_push_rejects_malformed_matrix_evidence(
    tmp_path: Path, reference: str, digest: str | None
) -> None:
    task = registry_push_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image-a:v1"]},
        expected_images=("image-a:v1",),
        work=lambda _inputs: (Evidence("local-registry-digest", reference, digest),),
    )

    with pytest.raises(RuntimeError, match="does not cover the image matrix"):
        task.run(TaskInputs.empty())


def test_expected_images_accepts_daemon_local_digests(tmp_path: Path) -> None:
    digest = "sha256:" + "c" * 64
    image = "localhost:5000/nanofaas/server:v1-amd64"
    task = amd64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": [image]},
        expected_images=(image,),
        work=lambda _inputs: (Evidence("local-image-digest", f"docker-daemon:{image}", digest),),
    )

    outcome = task.run(TaskInputs.empty())

    assert any(item.kind == "local-image-digest" for item in outcome.evidence)


def test_expected_images_still_rejects_an_incomplete_matrix(tmp_path: Path) -> None:
    task = amd64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["a:v1", "b:v1"]},
        expected_images=("a:v1", "b:v1"),
        work=lambda _inputs: (
            Evidence("local-image-digest", "docker-daemon:a:v1", "sha256:" + "d" * 64),
        ),
    )

    with pytest.raises(RuntimeError, match="amd64-build evidence does not cover the image matrix"):
        task.run(TaskInputs.empty())


def test_expected_images_rejects_a_daemon_digest_without_its_scheme(tmp_path: Path) -> None:
    task = amd64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["a:v1"]},
        expected_images=("a:v1",),
        work=lambda _inputs: (Evidence("local-image-digest", "a:v1", "sha256:" + "d" * 64),),
    )

    with pytest.raises(RuntimeError, match="does not cover the image matrix"):
        task.run(TaskInputs.empty())


def test_registry_push_rejects_duplicate_matrix_evidence(tmp_path: Path) -> None:
    item = Evidence("local-registry-digest", "docker://image-a:v1", "sha256:" + "d" * 64)
    task = registry_push_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"matrix": ["image-a:v1"]},
        expected_images=("image-a:v1",),
        work=lambda _inputs: (item, item),
    )

    with pytest.raises(RuntimeError, match="does not cover the image matrix"):
        task.run(TaskInputs.empty())


def test_benchmark_gate_tasks_form_a_digest_prerequisite_chain(tmp_path: Path) -> None:
    common = {"identity": _identity(), "run_dir": tmp_path, "phase_inputs": {"v": 1}}
    push = tmp_path / "local-registry-push.json"
    runs = tuple(
        release_tasks.benchmark_task(
            index=index,
            prerequisites=(push,),
            work=lambda _inputs: (),
            **common,
        )
        for index in range(1, 4)
    )
    aggregate = release_tasks.aggregate_benchmarks_task(
        prerequisites=tuple(task.receipt for task in runs),
        work=lambda _inputs: (),
        **common,
    )
    gate = release_tasks.regression_gate_task(
        prerequisites=(aggregate.receipt,),
        work=lambda _inputs: (),
        **common,
    )

    assert [task.phase for task in runs] == ["benchmark-1", "benchmark-2", "benchmark-3"]
    assert aggregate.phase == "aggregate"
    assert gate.phase == "regression-gate"
    assert aggregate.prerequisites == tuple(task.receipt for task in runs)
    assert gate.prerequisites == (aggregate.receipt,)
    assert len({task.reuse_key for task in (*runs, aggregate, gate)}) == 5


def test_arm_tasks_form_gate_build_smoke_digest_chain(tmp_path: Path) -> None:
    digest = "sha256:" + "d" * 64
    gate = tmp_path / "regression-gate.json"
    build = arm64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"images": ["image:v1-arm64"]},
        prerequisites=(gate,),
        expected_images=("image:v1-arm64",),
        work=lambda _inputs: (
            Evidence("local-registry-digest", "docker://image:v1-arm64", digest),
        ),
    )
    gate.write_text("passed", encoding="utf-8")
    build.run(TaskInputs.empty())
    smoke = arm64_smoke_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={"images": ["image:v1-arm64"]},
        prerequisites=(build.receipt,),
        expected_images=("image:v1-arm64",),
        work=lambda _inputs: (
            Evidence("local-registry-digest", "docker://image:v1-arm64", digest),
        ),
    )

    smoke.run(TaskInputs.empty())

    assert build.phase == "arm64-build"
    assert smoke.phase == "arm64-smoke"
    assert build.prerequisites == (gate,)
    assert smoke.prerequisites == (build.receipt,)


def test_arm_work_never_starts_without_its_prerequisite(tmp_path: Path) -> None:
    called = False

    def work(_inputs: TaskInputs):
        nonlocal called
        called = True
        return ()

    task = arm64_build_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={},
        prerequisites=(tmp_path / "missing-gate.json",),
        work=work,
    )

    with pytest.raises(FileNotFoundError):
        task.run(TaskInputs.empty())

    assert called is False


def test_terminal_tasks_keep_finalize_as_the_only_documentation_retry(tmp_path: Path) -> None:
    common = {"identity": _identity(), "run_dir": tmp_path, "phase_inputs": {"v": 1}}
    gate = tmp_path / "regression-gate.json"
    smoke = tmp_path / "arm64-smoke.json"
    registry = tmp_path / "local-registry-push.json"
    arm = tmp_path / "arm64-build.json"
    aggregate = tmp_path / "aggregate.json"
    architectures = publish_architectures_task(
        prerequisites=(gate, smoke, registry, arm), work=lambda _inputs: (), **common
    )
    manifests = publish_manifests_task(
        prerequisites=(architectures.receipt,), work=lambda _inputs: (), **common
    )
    aliases = publish_aliases_task(
        prerequisites=(manifests.receipt,), work=lambda _inputs: (), **common
    )
    attestation = attest_task(
        prerequisites=(aliases.receipt, aggregate), work=lambda _inputs: (), **common
    )
    finalize = finalize_task(
        prerequisites=(attestation.receipt,), work=lambda _inputs: (), **common
    )

    assert [task.phase for task in (architectures, manifests, aliases, attestation, finalize)] == [
        "publish-architectures",
        "publish-manifests",
        "publish-aliases",
        "attest",
        "finalize",
    ]
    assert finalize.prerequisites == (attestation.receipt,)


def test_documentation_failure_resumes_finalize_without_reattesting(tmp_path: Path) -> None:
    calls = {"attest": 0, "finalize": 0}
    signed = tmp_path / "signed"
    documentation = tmp_path / "history.md"

    def sign(_inputs: TaskInputs):
        calls["attest"] += 1
        signed.write_text("verified", encoding="utf-8")
        return (Evidence("file-digest", str(signed), digest_path(signed)),)

    def document(_inputs: TaskInputs):
        calls["finalize"] += 1
        if calls["finalize"] == 1:
            raise RuntimeError("documentation failed")
        documentation.write_text("published", encoding="utf-8")
        return (Evidence("file-digest", str(documentation), digest_path(documentation)),)

    workflow = Workflow("release-finalize-resume")
    attestation = attest_task(
        identity=_identity(), run_dir=tmp_path, phase_inputs={}, work=sign
    )
    finalize = finalize_task(
        identity=_identity(),
        run_dir=tmp_path,
        phase_inputs={},
        prerequisites=(attestation.receipt,),
        work=document,
    )
    workflow.add(attestation)
    workflow.add(finalize)
    journal = JournalConfig(tmp_path / "sonata.jsonl")

    with pytest.raises(RuntimeError, match="documentation failed"):
        workflow.run(journal=journal, verifiers={"file-digest": file_digest_verifier})
    workflow.run(
        journal=journal,
        resume=True,
        verifiers={"file-digest": file_digest_verifier},
    )

    assert calls == {"attest": 1, "finalize": 2}


@pytest.mark.parametrize(
    "references",
    (
        ("docker://a:v1",),
        ("docker://a:v1", "docker://x:v1"),
        ("docker://a:v1", "docker://a:v1"),
    ),
)
def test_exact_receipt_coverage_rejects_missing_extra_and_duplicate(
    tmp_path: Path, references: tuple[str, ...]
) -> None:
    receipt = tmp_path / "publish.json"
    receipt.write_text(
        json.dumps(
            {
                "phase": "publish-architectures",
                "evidence": [
                    {
                        "kind": "ghcr-digest",
                        "reference": reference,
                        "digest": "sha256:" + "d" * 64,
                    }
                    for reference in references
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="exact artifact coverage"):
        exact_receipt_artifacts(
            receipt,
            "publish-architectures",
            "ghcr-digest",
            ("docker://a:v1", "docker://b:v1"),
        )


def test_verified_file_receipt_rejects_tampered_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "decision.json"
    artifact.write_text('{"passed": true}', encoding="utf-8")
    receipt = tmp_path / "gate.json"
    receipt.write_text(
        json.dumps(
            {
                "phase": "regression-gate",
                "evidence": [
                    {
                        "kind": "file-digest",
                        "reference": str(artifact),
                        "digest": digest_path(artifact),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    artifact.write_text('{"passed": false}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed"):
        verified_file_receipt(receipt, "regression-gate", artifact)


def _file_receipt(receipt: Path, phase: str, artifact: Path) -> None:
    receipt.write_text(
        json.dumps(
            {
                "phase": phase,
                "evidence": [
                    {
                        "kind": "file-digest",
                        "reference": str(artifact),
                        "digest": digest_path(artifact),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_release_barriers_reject_failed_gate_and_mismatched_smoke(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    image = "registry/image:v1-arm64"
    arm_receipt = tmp_path / "arm-build.json"
    arm_receipt.write_text(
        json.dumps(
            {
                "phase": "arm64-build",
                "evidence": [
                    {
                        "kind": "local-registry-digest",
                        "reference": f"docker://{image}",
                        "digest": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    gate_file = tmp_path / "regression-decision.json"
    gate_receipt = tmp_path / "gate.json"
    smoke_file = tmp_path / "arm64-smoke.json"
    smoke_receipt = tmp_path / "smoke.json"
    gate_file.write_text('{"passed": false}', encoding="utf-8")
    smoke_file.write_text(
        json.dumps({"architecture": "linux/arm64", "images": {image: digest}}),
        encoding="utf-8",
    )
    _file_receipt(gate_receipt, "regression-gate", gate_file)
    _file_receipt(smoke_receipt, "arm64-smoke", smoke_file)

    with pytest.raises(RuntimeError, match="passing regression"):
        require_release_barriers(
            gate_receipt=gate_receipt,
            gate_file=gate_file,
            smoke_receipt=smoke_receipt,
            smoke_file=smoke_file,
            arm_build_receipt=arm_receipt,
            arm_images=(image,),
        )

    gate_file.write_text('{"passed": true}', encoding="utf-8")
    _file_receipt(gate_receipt, "regression-gate", gate_file)
    smoke_file.write_text(
        json.dumps({"architecture": "linux/amd64", "images": {image: digest}}),
        encoding="utf-8",
    )
    _file_receipt(smoke_receipt, "arm64-smoke", smoke_file)
    with pytest.raises(RuntimeError, match="does not match"):
        require_release_barriers(
            gate_receipt=gate_receipt,
            gate_file=gate_file,
            smoke_receipt=smoke_receipt,
            smoke_file=smoke_file,
            arm_build_receipt=arm_receipt,
            arm_images=(image,),
        )


def test_finalize_reads_a_predicate_from_a_receipt_that_also_records_signatures(
    tmp_path: Path,
) -> None:
    """The attest receipt carries two kinds; finalize must still read its one file.

    Signing evidence lands in the same receipt as the predicate digest, so a
    parser that requires every entry to be the kind the caller asked for kills
    finalize on every signed release.
    """
    predicate = tmp_path / "predicate.json"
    predicate.write_text('{"version": "v1"}', encoding="utf-8")
    receipt = tmp_path / "attest.json"
    receipt.write_text(
        json.dumps(
            {
                "phase": "attest",
                "evidence": [
                    {
                        "kind": "file-digest",
                        "reference": str(predicate),
                        "digest": digest_path(predicate),
                    },
                    {
                        "kind": "cosign-attestation",
                        "reference": "ghcr.io/nanofaas/gateway@sha256:" + "a" * 64,
                        "digest": "sha256:" + "a" * 64,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    require_attestation_predicate(receipt, predicate, {"version": "v1"})


def test_finalize_rejects_semantically_wrong_current_predicate(tmp_path: Path) -> None:
    predicate = tmp_path / "predicate.json"
    predicate.write_text('{"version": "wrong"}', encoding="utf-8")
    receipt = tmp_path / "attest.json"
    _file_receipt(receipt, "attest", predicate)

    with pytest.raises(RuntimeError, match="does not match"):
        require_attestation_predicate(receipt, predicate, {"version": "v1"})


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"phase": "wrong", "evidence": []},
        {"phase": "arm64-build", "evidence": {}},
        {"phase": "arm64-build", "evidence": ["bad"]},
        {
            "phase": "arm64-build",
            "evidence": [
                {
                    "kind": "unexpected",
                    "reference": "docker://image:v1",
                    "digest": "sha256:" + "a" * 64,
                }
            ],
        },
        {
            "phase": "arm64-build",
            "evidence": [
                {"kind": "local-registry-digest", "reference": 1, "digest": "sha256:" + "a" * 64}
            ],
        },
        {
            "phase": "arm64-build",
            "evidence": [
                {"kind": "local-registry-digest", "reference": "docker://image:v1", "digest": None}
            ],
        },
    ),
)
def test_arm_receipt_parser_rejects_malformed_schema(payload, tmp_path: Path) -> None:
    receipt = tmp_path / "arm64-build.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid arm64-build receipt"):
        registry_artifacts_from_receipt(receipt, ("image:v1",))


def test_image_steps_capture_every_current_registry_digest() -> None:
    digest = "sha256:" + "d" * 64

    class Steps:
        title = "Push"

        def run(self, _inputs):
            return TaskOutcome()

    class Executor:
        def __init__(self):
            self.commands = []

        def run(self, task, *, dry_run=False):
            self.commands.append(task.argv)
            return TaskResult(task_id="", status="passed", return_code=0, stdout=digest)

    executor = Executor()
    evidence = run_image_steps(
        Steps(),
        TaskInputs.empty(),
        executor,
        ("image-a:v1", "image-b:v1"),
        registry=True,
    )

    assert {item.reference for item in evidence} == {
        "docker://image-a:v1",
        "docker://image-b:v1",
    }
    assert all(command[0] == "skopeo" for command in executor.commands)


@pytest.mark.parametrize(
    "stdout",
    [None, "sha256:" + "A" * 64, "sha256:" + "z" * 64, "sha256:" + "a" * 63],
)
def test_image_steps_reject_malformed_digest_output(stdout) -> None:
    class Steps:
        title = "Build"

        def run(self, _inputs):
            return TaskOutcome()

    class Executor:
        def run(self, _task, *, dry_run=False):
            return TaskResult(task_id="", status="passed", return_code=0, stdout=stdout)

    with pytest.raises(RuntimeError, match="invalid image digest"):
        run_image_steps(Steps(), TaskInputs.empty(), Executor(), ("image:v1",), registry=False)


class _NoopSteps:
    title = "Steps"

    def run(self, _inputs):
        return TaskOutcome()


class _ScriptedExecutor:
    """Maps an exact argv tuple to the stdout a real command would print."""

    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self._responses = responses

    def run(self, task, *, dry_run=False):
        return TaskResult(
            task_id="", status="passed", return_code=0, stdout=self._responses[task.argv]
        )


def test_run_source_steps_records_the_tested_source_tree(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"tree")

    evidence = run_source_steps(_NoopSteps(), TaskInputs.empty(), source_archive=archive)

    assert len(evidence) == 1
    assert evidence[0].kind == "file-digest"
    assert evidence[0].reference == str(archive)
    assert evidence[0].digest == digest_path(archive)


def test_run_image_steps_rejects_a_foreign_architecture() -> None:
    executor = _ScriptedExecutor(
        {
            ("docker", "image", "inspect", "--format={{.Architecture}}", "img:v1"): "arm64",
            ("docker", "image", "inspect", "--format={{.Id}}", "img:v1"): "sha256:" + "a" * 64,
        }
    )

    with pytest.raises(RuntimeError, match="image architecture mismatch"):
        run_image_steps(
            _NoopSteps(),
            TaskInputs.empty(),
            executor,
            ("img:v1",),
            registry=False,
            architecture="amd64",
        )


def test_run_image_steps_accepts_the_expected_architecture() -> None:
    digest = "sha256:" + "b" * 64
    executor = _ScriptedExecutor(
        {
            ("docker", "image", "inspect", "--format={{.Architecture}}", "img:v1"): "amd64",
            ("docker", "image", "inspect", "--format={{.Id}}", "img:v1"): digest,
        }
    )

    evidence = run_image_steps(
        _NoopSteps(),
        TaskInputs.empty(),
        executor,
        ("img:v1",),
        registry=False,
        architecture="amd64",
    )

    assert [item.digest for item in evidence] == [digest]
