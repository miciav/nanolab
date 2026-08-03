"""Tests for the release composite builders in ``sonata_tasks.release_composites``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sonata_engine import Evidence, Steps, Workflow
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks import release_composites
from sonata_tasks.release_composites import (
    _AttestImageTask,
    attest_composite,
    command_specs_composite,
    registry_push_composite,
)


# ---------------------------------------------------------------------------
# Test double: a RecordingExecutor that records every command without
# running it.  Mirrors the pattern in test_compose.py.
# ---------------------------------------------------------------------------


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


def _attest(
    *,
    images: Sequence[str] = ("repo/a@sha256:aa",),
    predicate_remote: str = "/work/predicate.json",
    sbom_dir_remote: str = "/work/sboms",
    public_key_remote: str = "/work/cosign.pub",
    cosign_key: str = "/secrets/cosign-key",
    password_file: str = "/secrets/cosign-password",
    docker_config: str = "/secrets/docker",
    executor: RecordingExecutor | None = None,
    signed: list[Evidence] | None = None,
) -> Steps:
    """An attest composite whose inputs a test can vary one at a time."""
    return attest_composite(
        images,
        predicate_remote=predicate_remote,
        sbom_dir_remote=sbom_dir_remote,
        public_key_remote=public_key_remote,
        cosign_key=cosign_key,
        password_file=password_file,
        docker_config=docker_config,
        executor=executor or RecordingExecutor(),
        role="stack",
        signed=signed,
    )


def _attest_group(composite: Steps) -> _AttestImageTask:
    """The first per-image group of an attest composite."""
    group = composite._steps[0]
    assert isinstance(group, _AttestImageTask)
    return group


def _run_composite(composite: Steps, executor: RecordingExecutor) -> None:
    """Compile and run a composite, so ``executor.seen`` fills with real argv."""
    workflow = Workflow("test-composite")
    workflow.add(composite)
    workflow.run()


# ---------------------------------------------------------------------------
# Test doubles for ImagePlan / ImageCell
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeTarget:
    name: str
    dockerfile: Path = Path("Dockerfile")
    context: Path = Path(".")
    native_gradle_task: str | None = None
    native_image_property: str | None = None


@dataclass(frozen=True)
class _FakeCell:
    target: _FakeTarget
    architecture: str
    flavor: str
    tag: str
    image: str
    build_kind: str  # "bake" or "gradle"


@dataclass(frozen=True)
class _FakePlan:
    cells: tuple[_FakeCell, ...]


def _plan_with_cells(*cells: _FakeCell) -> _FakePlan:
    return _FakePlan(cells=cells)


def _bake_cell(
    name: str,
    image: str,
    arch: str = "amd64",
    flavor: str = "jvm",
) -> _FakeCell:
    return _FakeCell(
        target=_FakeTarget(name=name, dockerfile=Path(f"{name}/Dockerfile"), context=Path(name)),
        architecture=arch,
        flavor=flavor,
        tag=image.rsplit(":", 1)[-1],
        image=image,
        build_kind="bake",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSourceTestsComposite:
    def test_wraps_each_spec_as_a_command_task(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(
                task_id="lint", summary="Run linter", argv=("echo", "lint"), role="host"
            ),
            CommandTaskSpec(
                task_id="unit", summary="Run unit tests", argv=("echo", "test"), role="host"
            ),
        ]

        composite = command_specs_composite(commands, executor, title="Run source tests")
        workflow = Workflow("source-tests")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 2
        assert executor.seen[0].argv == ("echo", "lint")
        assert executor.seen[1].argv == ("echo", "test")

    def test_preserves_command_execution_fields(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(
                task_id="tests",
                summary="Run tests",
                argv=("pytest",),
                role="stack",
                env={"CI": "true"},
                cwd=Path("/local/fallback"),
                remote_dir="/remote/source",
                expected_exit_codes=frozenset({0, 5}),
                timeout_seconds=600,
            ),
        ]

        composite = command_specs_composite(commands, executor, title="Run source tests")
        workflow = Workflow("source-tests-fields")
        workflow.add(composite)
        workflow.run()

        seen = executor.seen[0]
        assert dict(seen.env) == {"CI": "true"}
        assert seen.cwd == Path("/local/fallback")
        assert seen.remote_dir == "/remote/source"
        assert seen.expected_exit_codes == frozenset({0, 5})
        assert seen.timeout_seconds == 600

    def test_default_title(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "x"), role="host"),
        ]

        composite = command_specs_composite(commands, executor, title="Run source tests")
        assert "Run source tests" in composite.title

    def test_custom_title(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "x"), role="host"),
        ]

        composite = command_specs_composite(commands, executor, title="Custom title")
        assert composite.title == "Custom title"

    def test_handles_empty_command_list(self) -> None:
        executor = RecordingExecutor()

        # Steps requires at least one step, so empty should raise
        import pytest

        with pytest.raises(ValueError, match="at least one step"):
            command_specs_composite([], executor, title="Run source tests")


class TestRegistryPushComposite:
    def test_pushes_and_inspects_every_cell(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
            _bake_cell("watchdog", "reg/watch:v1-amd64", arch="amd64", flavor="default"),
        )

        composite = registry_push_composite(plan, executor, "host", authfile="/auth/config.json")
        workflow = Workflow("registry-push")
        workflow.add(composite)
        workflow.run()

        # Two cells -> 4 command executions: push + inspect per cell
        assert len(executor.seen) == 4

        # Order: push ctrl, inspect ctrl, push watch, inspect watch
        assert executor.seen[0].argv[:3] == ("docker", "push", "reg/ctrl:v1-amd64")
        assert executor.seen[1].argv[0] == "skopeo"
        assert executor.seen[1].argv[1] == "inspect"
        assert executor.seen[2].argv[:3] == ("docker", "push", "reg/watch:v1-amd64")
        assert executor.seen[3].argv[1] == "inspect"

    def test_skopeo_inspect_uses_authfile(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
        )

        composite = registry_push_composite(plan, executor, "host", authfile="/custom/auth.json")
        workflow = Workflow("registry-push")
        workflow.add(composite)
        workflow.run()

        inspect_specs = [s for s in executor.seen if s.argv[0] == "skopeo"]
        assert len(inspect_specs) == 1
        auth_index = inspect_specs[0].argv.index("--authfile")
        assert inspect_specs[0].argv[auth_index + 1] == "/custom/auth.json"

    def test_default_authfile_is_empty_string(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
        )

        composite = registry_push_composite(plan, executor, "host")
        assert composite is not None


class TestAttestComposite:
    def test_attest_composite_runs_six_operations_per_image(self) -> None:
        executor = RecordingExecutor()
        images = ("repo/a@sha256:aa", "repo/b@sha256:bb")

        composite = attest_composite(
            images,
            predicate_remote="/work/predicate.json",
            sbom_dir_remote="/work/sboms",
            public_key_remote="/work/cosign.pub",
            cosign_key="/secrets/cosign-key",
            password_file="/secrets/cosign-password",
            docker_config="/home/azureuser/.docker",
            executor=executor,
            role="stack",
        )

        # One Steps per image, so a resume picks up from the unsigned image.
        # `Steps` keeps its inner tasks on the private `_steps` attribute --
        # there is no public accessor, so introspection has to reach past it.
        assert len(composite._steps) == 2
        for image, per_image in zip(images, composite._steps, strict=True):
            titles = [step.title for step in per_image._steps]
            # Exact ordered titles, not just a count: a dropped, renamed, or
            # reordered operation (e.g. losing the standalone `verify` step
            # that checks `sign`'s signature, distinct from what
            # `verify-attestation` checks for `attest`) fails this comparison
            # even though the total step count could otherwise stay right by
            # coincidence.
            assert titles == [
                f"Syft SBOM {image}",
                f"cosign sign {image}",
                f"cosign attest {image}",
                f"cosign attach sbom {image}",
                f"cosign verify {image}",
                f"cosign verify-attestation {image}",
            ]

    def test_attest_composite_pins_every_operation_to_the_same_digest(self) -> None:
        executor = RecordingExecutor()
        composite = attest_composite(
            ("repo/a@sha256:aa",),
            predicate_remote="/work/predicate.json",
            sbom_dir_remote="/work/sboms",
            public_key_remote="/work/cosign.pub",
            cosign_key="/secrets/cosign-key",
            password_file="/secrets/cosign-password",
            docker_config="/home/azureuser/.docker",
            executor=executor,
            role="stack",
        )
        _run_composite(composite, executor)

        assert executor.seen, "composite ran nothing"
        for spec in executor.seen:
            assert "repo/a@sha256:aa" in spec.argv, f"unpinned reference in {spec.argv}"

    def test_attest_composite_handles_an_empty_image_set(self) -> None:
        executor = RecordingExecutor()
        composite = attest_composite(
            (),
            predicate_remote="/work/predicate.json",
            sbom_dir_remote="/work/sboms",
            public_key_remote="/work/cosign.pub",
            cosign_key="/secrets/cosign-key",
            password_file="/secrets/cosign-password",
            docker_config="/home/azureuser/.docker",
            executor=executor,
            role="stack",
        )

        assert len(composite._steps) == 1

    def test_attest_composite_rejects_an_unpinned_reference(self) -> None:
        with pytest.raises(ValueError, match="digest-pinned"):
            _attest(images=("repo/a:v1",))

    def test_attest_group_journal_identity_follows_the_reuse_key(self) -> None:
        """The step's journal identity must be a function of the reuse key.

        The engine consults a `reuse_key` only through
        `CompiledWorkflow.fingerprint`, and this composite is built inside a
        release phase at run time, so it never gets compiled. Carrying the key
        in the title -- which the journal slugifies into the step id -- is the
        only thing that makes the key load-bearing. A refactor that changed
        titles by some other means would disarm the mechanism silently, so
        assert the coupling itself and not just that titles differ.
        """
        group = _attest_group(_attest())
        assert group.reusable, "a group that opts out of reuse can never be skipped"
        assert group.reuse_key[-8:] in group.title

    def test_attest_group_reuse_key_covers_what_it_signs(self) -> None:
        """Anything that changes the signature changes the key."""
        base = _attest_group(_attest())

        for other in (
            _attest_group(_attest(images=("repo/b@sha256:bb",))),
            _attest_group(_attest(predicate_remote="/work/other.json")),
            _attest_group(_attest(sbom_dir_remote="/work/other-sboms")),
            _attest_group(_attest(public_key_remote="/work/other.pub")),
        ):
            assert other.reuse_key != base.reuse_key, other.title
            assert other.title != base.title, other.reuse_key

    def test_attest_group_reuse_key_ignores_per_run_credential_paths(self) -> None:
        """The staged credential paths must NOT reach the key.

        `cosign_key`, `password_file` and `docker_config` are all staged into a
        fresh `mktemp -d` on every process (`nanolab.release.secrets`), and the
        directory is `rm -rf`'d on release. Folding one into the key gives every
        run a journal step id it has never recorded, so `decide_task` answers
        "run" and the group re-signs -- the skip never fires in production even
        though every test of it passes.
        """
        base = _attest_group(_attest())

        for other in (
            _attest_group(_attest(cosign_key="/tmp/nanofaas-release-credentials.b/cosign-key")),
            _attest_group(_attest(password_file="/tmp/nanofaas-release-credentials.b/pw")),
            _attest_group(_attest(docker_config="/tmp/nanofaas-release-credentials.b/docker")),
        ):
            assert other.reuse_key == base.reuse_key, other.title
            assert other.title == base.title, other.reuse_key

    def test_attest_group_reuse_key_covers_the_tool_pins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bumping cosign or syft must not resume onto the old tool's work."""
        base = _attest_group(_attest())

        for pin in ("COSIGN_IMAGE", "SYFT_IMAGE"):
            monkeypatch.setattr(release_composites, pin, f"example/{pin}@sha256:ff")
            assert _attest_group(_attest()).reuse_key != base.reuse_key, pin
            monkeypatch.undo()

    def test_attest_group_emits_the_signature_it_produced(self) -> None:
        """`signed` collects one entry per group that ran, not per image asked."""
        executor = RecordingExecutor()
        images = ("repo/a@sha256:aa", "repo/b@sha256:bb")
        signed: list[Evidence] = []

        _run_composite(_attest(images=images, executor=executor, signed=signed), executor)

        assert [(e.kind, e.reference, e.digest) for e in signed] == [
            ("cosign-attestation", image, image.split("@", 1)[1]) for image in images
        ]


class TestCommandSpecsComposite:
    def test_command_specs_composite_titles_each_step_from_the_spec_summary(self) -> None:
        executor = RecordingExecutor()
        commands = (
            CommandTaskSpec(task_id="a", summary="First", argv=("echo", "one"), role="stack"),
            CommandTaskSpec(task_id="b", summary="Second", argv=("echo", "two"), role="stack"),
        )

        composite = command_specs_composite(commands, executor=executor, title="Build AMD64 images")

        workflow = Workflow("test-command-specs")
        workflow.add(composite)
        workflow.run()

        assert composite.title == "Build AMD64 images"
        assert len(executor.seen) == 2
        assert executor.seen[0].summary == "First"
        assert executor.seen[1].summary == "Second"


class TestCompositeCompilation:
    """Integration-level checks: composites compile and run in a Workflow."""

    def test_source_tests_compiles(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "lint"), role="host"),
        ]

        composite = command_specs_composite(commands, executor, title="Run source tests")
        workflow = Workflow("source-tests")
        workflow.add(composite)
        compiled = workflow.compile()

        # Steps is one compiled unit at the workflow level
        assert len(compiled.tasks) == 1
        assert compiled.tasks[0].task_id == "001.run-source-tests"

    def test_registry_push_compiles(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
        )

        composite = registry_push_composite(plan, executor, "host")
        workflow = Workflow("registry-push")
        workflow.add(composite)
        compiled = workflow.compile()

        # Steps is one compiled unit at the workflow level
        assert len(compiled.tasks) == 1
        assert compiled.tasks[0].task_id == "001.push-images-to-registry"
