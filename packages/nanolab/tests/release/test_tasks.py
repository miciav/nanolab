import json
from pathlib import Path

import pytest
from sonata_engine import Evidence, JournalConfig, TaskInputs, TaskOutcome, Workflow
from workflow_tasks.tasks.models import TaskResult

from nanolab.release.evidence import file_digest_verifier
from nanolab.release import tasks as release_tasks
from nanolab.release.state import ReleaseIdentity, digest_path
from nanolab.release.tasks import (
    amd64_build_task,
    registry_push_task,
    run_image_steps,
    source_test_task,
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
