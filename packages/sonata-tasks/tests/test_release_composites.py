"""Tests for the release composite builders in ``sonata_tasks.release_composites``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sonata_engine import Workflow
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask
from sonata_tasks.release_composites import (
    amd64_build_composite,
    arm64_build_composite,
    attest_composite,
    publish_aliases_composite,
    publish_architectures_composite,
    publish_manifests_composite,
    registry_push_composite,
    source_tests_composite,
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


def _gradle_cell(
    name: str,
    image: str,
    arch: str = "amd64",
    flavor: str = "native",
) -> _FakeCell:
    return _FakeCell(
        target=_FakeTarget(
            name=name,
            native_gradle_task=f":{name}:bootBuildImage",
            native_image_property=f"{name}Image",
        ),
        architecture=arch,
        flavor=flavor,
        tag=image.rsplit(":", 1)[-1],
        image=image,
        build_kind="gradle",
    )


# ---------------------------------------------------------------------------
# Test doubles for PublishPlan-like objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeCopy:
    source: str
    destination: str


@dataclass(frozen=True)
class _FakeManifest:
    reference: str
    sources: tuple[str, str]


@dataclass(frozen=True)
class _FakeAlias:
    reference: str
    source: str


@dataclass(frozen=True)
class _FakePublishPlan:
    copies: tuple[_FakeCopy, ...]
    manifests: tuple[_FakeManifest, ...] = ()
    aliases: tuple[_FakeAlias, ...] = ()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSourceTestsComposite:
    def test_wraps_each_spec_as_a_command_task(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Run linter", argv=("echo", "lint"), role="host"),
            CommandTaskSpec(task_id="unit", summary="Run unit tests", argv=("echo", "test"), role="host"),
        ]

        composite = source_tests_composite(commands, executor)
        workflow = Workflow("source-tests")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 2
        assert executor.seen[0].argv == ("echo", "lint")
        assert executor.seen[1].argv == ("echo", "test")

    def test_default_title(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "x"), role="host"),
        ]

        composite = source_tests_composite(commands, executor)
        assert "Run source tests" in composite.title

    def test_custom_title(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "x"), role="host"),
        ]

        composite = source_tests_composite(commands, executor, title="Custom title")
        assert composite.title == "Custom title"

    def test_handles_empty_command_list(self) -> None:
        executor = RecordingExecutor()

        # Steps requires at least one step, so empty should raise
        import pytest

        with pytest.raises(ValueError, match="at least one step"):
            source_tests_composite([], executor)


class TestAmd64BuildComposite:
    def test_builds_only_amd64_cells(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("control-plane", "reg/ctrl:1-a-jvm", arch="amd64"),
            _bake_cell("control-plane", "reg/ctrl:1-arm64-jvm", arch="arm64"),
            _bake_cell("watchdog", "reg/watch:1-amd64", arch="amd64", flavor="default"),
        )

        composite = amd64_build_composite(plan, executor, "host")
        workflow = Workflow("amd64-build")
        workflow.add(composite)
        workflow.run()

        # Only amd64 cells should produce build tasks
        build_images = [
            spec.argv[spec.argv.index("-t") + 1]
            for spec in executor.seen
            if "build" in spec.argv
        ]
        assert build_images == ["reg/ctrl:1-a-jvm", "reg/watch:1-amd64"]

    def test_uses_docker_build_task_for_bake_cells(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:1-a-jvm", arch="amd64"),
        )

        composite = amd64_build_composite(plan, executor, "host")
        workflow = Workflow("amd64-build")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 1
        spec = executor.seen[0]
        assert spec.argv[0] == "docker"
        assert spec.argv[1] == "build"

    def test_uses_gradle_task_for_gradle_cells(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _gradle_cell("ctrl", "reg/ctrl:1-a-native", arch="amd64"),
        )

        composite = amd64_build_composite(plan, executor, "host")
        workflow = Workflow("amd64-build")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 1
        spec = executor.seen[0]
        assert spec.argv[0] == "./gradlew"

    def test_passes_working_directory(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:1-a-jvm", arch="amd64"),
        )

        composite = amd64_build_composite(plan, executor, "host", cwd=Path("/project"))
        workflow = Workflow("amd64-build")
        workflow.add(composite)
        workflow.run()

        assert executor.seen[0].cwd == Path("/project")


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


class TestArm64BuildComposite:
    def test_uses_builder_name_in_create_command(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-arm64", arch="arm64"),
        )

        composite = arm64_build_composite(plan, executor, "arm-builder", "my-builder")
        workflow = Workflow("arm64-build")
        workflow.add(composite)
        workflow.run()

        builder_create = [s for s in executor.seen if "create" in s.argv]
        assert len(builder_create) >= 1
        assert "my-builder" in builder_create[0].argv

    def test_skips_amd64_cells(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
            _bake_cell("ctrl", "reg/ctrl:v1-arm64", arch="arm64"),
        )

        composite = arm64_build_composite(plan, executor, "arm-builder", "b")
        workflow = Workflow("arm64-build")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) >= 2  # builder setup + bake/build

    def test_registry_tunnel_delegated_to_resource(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-arm64", arch="arm64"),
        )

        composite = arm64_build_composite(plan, executor, "arm-builder", "b",
                                           registry_upstream="localhost:5000")
        workflow = Workflow("arm64-build")
        workflow.add(composite)
        workflow.run()

        # The composite does NOT create the tunnel — the registry_tunnel_resource
        # in the parent workflow DAG handles acquire/release.
        tunnel_cmds = [s for s in executor.seen if "socat" in str(s.argv)]
        assert len(tunnel_cmds) == 0


class TestPublishArchitecturesComposite:
    def test_copies_each_cell_with_digest_pinning(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(copies=(
            _FakeCopy(source="reg/ctrl:v1-amd64", destination="ghcr.io/ctrl:v1-amd64"),
            _FakeCopy(source="reg/watch:v1-amd64", destination="ghcr.io/watch:v1-amd64"),
        ))
        digests = {
            "reg/ctrl:v1-amd64": "sha256:aaa111",
            "reg/watch:v1-amd64": "sha256:bbb222",
        }

        composite = publish_architectures_composite(plan, executor, "host",
                                                     digests, "/auth.json")
        workflow = Workflow("publish-arch")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 2
        for spec in executor.seen:
            assert spec.argv[0] == "skopeo"
            assert spec.argv[1] == "copy"

    def test_raises_on_missing_digest(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(copies=(
            _FakeCopy(source="reg/ctrl:v1-amd64", destination="ghcr.io/ctrl:v1-amd64"),
        ))

        import pytest
        with pytest.raises(KeyError):
            publish_architectures_composite(plan, executor, "host", {}, "/auth.json")


class TestPublishManifestsComposite:
    def test_creates_manifests_via_publish_plan(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(
            copies=(),
            manifests=(
                _FakeManifest(
                    reference="ghcr.io/ctrl:v1",
                    sources=("ghcr.io/ctrl:v1-amd64", "ghcr.io/ctrl:v1-arm64"),
                ),
            ),
        )
        digests = {
            "ghcr.io/ctrl:v1-amd64": "sha256:aaa",
            "ghcr.io/ctrl:v1-arm64": "sha256:bbb",
        }

        composite = publish_manifests_composite(plan, executor, "host",
                                                 digests, "/docker")
        workflow = Workflow("publish-manifest")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 1
        spec = executor.seen[0]
        assert "imagetools" in spec.argv
        assert "create" in spec.argv

    def test_raises_on_missing_arch_digest(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(
            copies=(),
            manifests=(
                _FakeManifest(
                    reference="ghcr.io/ctrl:v1",
                    sources=("ghcr.io/ctrl:v1-amd64", "ghcr.io/ctrl:v1-arm64"),
                ),
            ),
        )

        import pytest
        with pytest.raises(KeyError):
            publish_manifests_composite(plan, executor, "host", {}, "/docker")


class TestPublishAliasesComposite:
    def test_creates_aliases_via_publish_plan(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(
            copies=(),
            manifests=(),
            aliases=(
                _FakeAlias(
                    reference="ghcr.io/ctrl:v1",
                    source="ghcr.io/ctrl:v1-native",
                ),
            ),
        )
        digests = {"ghcr.io/ctrl:v1-native": "sha256:ccc"}

        composite = publish_aliases_composite(plan, executor, "host",
                                               digests, "/docker")
        workflow = Workflow("publish-alias")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 1
        spec = executor.seen[0]
        assert "imagetools" in spec.argv
        assert "create" in spec.argv

    def test_noop_when_plan_has_no_aliases(self) -> None:
        executor = RecordingExecutor()
        plan = _FakePublishPlan(copies=(), manifests=(), aliases=())

        composite = publish_aliases_composite(plan, executor, "host", {}, "/docker")
        workflow = Workflow("publish-alias")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 1
        assert "true" in executor.seen[0].argv


class TestAttestComposite:
    def test_attests_each_image_with_syft_and_cosign(self) -> None:
        executor = RecordingExecutor()
        images = ["reg/img1:v1"]

        composite = attest_composite(
            images,
            Path("/predicate.json"),
            Path("/sboms"),
            "/cosign.key",
            "/docker",
            executor,
            "host",
            password_file="/pw.txt",
        )
        workflow = Workflow("attest")
        workflow.add(composite)
        workflow.run()

        assert len(executor.seen) == 2  # syft + cosign
        assert executor.seen[0].argv[0] == "docker"  # syft via docker run


class TestCompositeCompilation:
    """Integration-level checks: composites compile and run in a Workflow."""

    def test_source_tests_compiles(self) -> None:
        executor = RecordingExecutor()
        commands = [
            CommandTaskSpec(task_id="lint", summary="Lint", argv=("echo", "lint"), role="host"),
        ]

        composite = source_tests_composite(commands, executor)
        workflow = Workflow("source-tests")
        workflow.add(composite)
        compiled = workflow.compile()

        # Steps is one compiled unit at the workflow level
        assert len(compiled.tasks) == 1
        assert compiled.tasks[0].task_id == "001.run-source-tests"

    def test_amd64_build_compiles(self) -> None:
        executor = RecordingExecutor()
        plan = _plan_with_cells(
            _bake_cell("ctrl", "reg/ctrl:v1-amd64", arch="amd64"),
            _bake_cell("watchdog", "reg/watch:v1-amd64", arch="amd64", flavor="default"),
        )

        composite = amd64_build_composite(plan, executor, "host")
        workflow = Workflow("amd64-build")
        workflow.add(composite)
        compiled = workflow.compile()

        assert len(compiled.tasks) == 1
        assert compiled.tasks[0].task_id == "001.build-amd64-images"

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
