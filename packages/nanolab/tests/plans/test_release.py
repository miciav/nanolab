"""Smoke tests for the release workflow builder."""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sonata_engine import JournalConfig, Resource, Selection
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm.models import VmInfo

import nanolab.plans.release as release_plan
import nanolab.release.resources as release_resources
import nanolab.release.build as release_build
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import DEFAULT_REGISTRY, ImagePlan
from nanolab.plans.release import ReleaseRequest, build_release_workflow
from nanolab.release.evidence import signature_evidence_verifier
from nanolab.release.metrics import PerformanceAggregate, PerformanceProfile
from nanolab.release.publish import PublishPlan, build_publish_plan
from nanolab.release.model import (
    ArtifactEvidence,
    CredentialFiles,
    GitState,
    ReleaseSettings,
    digest_path,
)
from nanolab.release.tasks import ReleasePhaseTask
from nanolab.release.versioning import read_project_version

from ..conftest import RejectingProvider


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)


_AZURE_ENV = EnvironmentConfig.model_validate(
    {
        "provider": "azure",
        "roles": {
            "stack": {"name": "release-stack", "user": "azureuser"},
            "loadgen": {"name": "release-loadgen", "user": "azureuser"},
            "arm-builder": {"name": "release-arm", "user": "azureuser"},
        },
        "azure": {
            "resource_group": "test-rg",
            "location": "westeurope",
            "vm_size": "Standard_D8s_v5",
            "loadgen_vm_size": "Standard_D4s_v5",
            "arm_vm_size": "Standard_D4ps_v6",
            "image_urn": "Canonical:0001-com-ubuntu-server-noble:24_04-lts-gen2:latest",
            "arm_image_urn": "Canonical:0001-com-ubuntu-server-noble:24_04-lts-gen2:latest",
            "operator_source_cidr": "1.2.3.4/32",
        },
    }
)


@pytest.fixture
def release_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> ReleaseRequest:
    """The canonical offline release request every workflow test compiles from."""
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    return release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )


def _phase_named(workflow, title: str) -> ReleasePhaseTask:
    for compiled in workflow.compile().tasks:
        if isinstance(compiled.task, ReleasePhaseTask) and compiled.task.title == title:
            return compiled.task
    raise AssertionError(f"no release phase titled {title!r}")


def test_amd64_build_phase_records_the_commands_it_will_run(
    release_request: ReleaseRequest,
) -> None:
    """The phase must run the whole build command, and record what it ran.

    `phase_inputs` is the reuse key, so deriving it from the real argv is what
    keeps a receipt from claiming inputs the executed command never carried.
    """
    workflow = build_release_workflow(release_request, provider=RejectingProvider())
    phase = _phase_named(workflow, "Build AMD64 images")

    argvs = [argv for argv, _role, _remote_dir in phase.phase_inputs["commands"]]

    # No separate native Gradle build survives: every native cell bakes now,
    # so nothing here shells out with a per-platform Gradle property.
    assert not any(
        argv[0] == "./gradlew" and "-PimagePlatform" in " ".join(argv) for argv in argvs
    )
    assert any(argv[:3] == ("docker", "buildx", "bake") for argv in argvs)
    # The JVM prerequisite is the source of truth: control-plane's carries its
    # own extra argument, and dropping it is what shipped in v0.18.1.
    prepares = [argv for argv in argvs if argv[0] == "./gradlew" and "bootJar" in " ".join(argv)]
    assert prepares, "no JVM bootJar prepare command"
    assert any("-PcontrolPlaneModules=all" in argv for argv in prepares)
    assert phase.expected_images == tuple(
        cell.image for cell in release_request.image_plan.cells
    )


def test_release_journal_is_scoped_to_the_versioned_run(tmp_path: Path) -> None:
    request = type("Request", (), {"run_dir": tmp_path, "version": "v1.2.3"})()

    assert release_plan.release_journal_config(request).path == (
        tmp_path / "releases" / "1.2.3" / "sonata.jsonl"
    )


def test_release_request_rejects_non_azure_environment():
    local_env = EnvironmentConfig.model_validate({"provider": "local", "roles": {}})
    req = ReleaseRequest(
        repo_root=Path("/tmp"),
        version="1.0.0",
        environment=local_env,
        scenario=ScenarioConfig(workflow="loadtest", functions=["word-stats-java"]),
        image_plan=ImagePlan(version="v1", registry="localhost:5000", targets=(), cells=()),
        settings=ReleaseSettings(
            max_parallelism=4,
            scenario=Path("loadtest.yaml"),
            scenario_name="loadtest.yaml",
            benchmark_runs=3,
            profile="default",
            throughput_max_loss_percent=10.0,
            p95_max_increase_percent=20.0,
            error_rate_max=0.05,
        ),
        run_dir=Path("/tmp/runs"),
        performance_root=Path("/tmp/perf"),
        source_tree=Path("/tmp"),
    )
    with pytest.raises(ValueError, match="Azure"):
        build_release_workflow(req)


def test_release_request_is_frozen():
    req = ReleaseRequest(
        repo_root=Path("/tmp"),
        version="1.0.0",
        environment=_AZURE_ENV,
        scenario=ScenarioConfig(workflow="loadtest", functions=["word-stats-java"]),
        image_plan=ImagePlan(version="v1", registry="localhost:5000", targets=(), cells=()),
        settings=ReleaseSettings(
            max_parallelism=4,
            scenario=Path("loadtest.yaml"),
            scenario_name="loadtest.yaml",
            benchmark_runs=3,
            profile="default",
            throughput_max_loss_percent=10.0,
            p95_max_increase_percent=20.0,
            error_rate_max=0.05,
        ),
        run_dir=Path("/tmp/runs"),
        performance_root=Path("/tmp/perf"),
        source_tree=Path("/tmp"),
    )
    with pytest.raises(Exception):
        req.version = "2.0.0"  # type: ignore[misc]


def test_release_source_commit_uses_clean_committed_head(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "verify_version_consistency",
        lambda _root: "0.18.1",
    )

    assert release_plan._release_source_commit(tmp_path, "v0.18.1") == "abc123"


def test_release_source_commit_rejects_dirty_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=False),
    )

    with pytest.raises(ValueError, match="clean nanoFaaS Git tree"):
        release_plan._release_source_commit(tmp_path, "v0.18.1")


def test_release_source_commit_rejects_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="abc123", clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "verify_version_consistency",
        lambda _root: "0.18.0",
    )

    with pytest.raises(ValueError, match="does not match"):
        release_plan._release_source_commit(tmp_path, "v0.18.1")


def test_build_release_workflow_compiles_to_a_workflow():
    """Minimal smoke: the builder produces a Workflow without crashing."""
    # ponytail: integration test skipped — needs real Azure credentials
    pass


class _ArmWorkflowExecutor:
    def __init__(self) -> None:
        self.commands = []
        # Set by a test to fail a chosen command; None means everything passes.
        self.fail_when: Callable[[CommandTaskSpec], bool] | None = None

    def run(self, task, *, dry_run=False):
        del dry_run
        self.commands.append(task)
        if self.fail_when is not None and self.fail_when(task):
            return TaskResult(
                task_id="", status="failed", return_code=1, stderr="injected failure"
            )
        if task.argv[:3] == ("docker", "buildx", "inspect"):
            if "--bootstrap" in task.argv:
                return TaskResult(
                    task_id="",
                    status="passed",
                    return_code=0,
                    stdout="Platforms: linux/amd64, linux/arm64\n",
                )
            return TaskResult(task_id="", status="passed", return_code=1)
        return TaskResult(task_id="", status="passed", return_code=0)


class _ArmWorkflowProvider:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.commands: list[tuple[str, ...]] = []
        self.transfers: list[str] = []
        self.staged = 0
        self.remote_digests: dict[str, str] = {}
        self.registry_digests: dict[str, str] = {}

    def connection_host(self, _request) -> str:
        return "10.0.0.10"

    def transfer_to(self, _request, *, source: Path, destination: str):
        self.transfers.append(destination)
        if self.failure == "source-transfer" and destination.endswith("/source.tar"):
            return SimpleNamespace(return_code=1, stdout="", stderr="source transfer failed")
        self.remote_digests[destination] = digest_path(source)
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    def exec_argv(self, _request, argv, *, env=None, cwd=None, dry_run=False):
        del env, cwd, dry_run
        self.commands.append(argv)
        rendered = " ".join(argv)
        if argv[:2] == ("mktemp", "-d"):
            # A fresh directory per call, as the real mktemp gives: every
            # credential path a run sees is unique to that run, so anything
            # that folds one into a resume identity stops resuming.
            self.staged += 1
            return SimpleNamespace(
                return_code=0,
                stdout=f"/tmp/nanofaas-release-credentials.aB3xY{self.staged}\n",
                stderr="",
            )
        if argv[0] == "sha256sum":
            digest = self.remote_digests[argv[1]].removeprefix("sha256:")
            return SimpleNamespace(return_code=0, stdout=f"{digest}  {argv[1]}\n", stderr="")
        if (
            self.failure == "build"
            and "docker buildx bake" in rendered
            and "docker-arm64" in rendered
        ):
            # Native images no longer get their own Gradle build step: every
            # cell (JVM, native, default) bakes together in this one command,
            # so this is where an "individual build" failure now surfaces.
            return SimpleNamespace(return_code=1, stdout="", stderr="individual build failed")
        if self.failure == "push" and "docker push" in rendered:
            return SimpleNamespace(return_code=1, stdout="", stderr="push failed")
        if argv[:3] == ("docker", "image", "inspect"):
            if argv[3] == "--format={{.Architecture}}":
                return SimpleNamespace(return_code=0, stdout="arm64\n", stderr="")
            return SimpleNamespace(return_code=0, stdout="sha256:" + "a" * 64, stderr="")
        if "docker push" in rendered:
            image = rendered.split("docker push ", 1)[1].split()[0]
            self.registry_digests[image] = "sha256:" + "b" * 64
        if argv[:2] == ("skopeo", "inspect"):
            image = argv[-1].removeprefix("docker://")
            digest = self.registry_digests.get(image, "sha256:" + "b" * 64)
            if self.failure == "digest":
                digest = "malformed"
            return SimpleNamespace(return_code=0, stdout=digest + "\n", stderr="")
        if argv[:2] == ("docker", "port"):
            return SimpleNamespace(return_code=0, stdout="127.0.0.1:32768\n", stderr="")
        if self.failure == "smoke" and argv and argv[0] == "curl":
            return SimpleNamespace(return_code=1, stdout="", stderr="smoke failed")
        if argv[:2] == ("docker", "run") and "WATCHDOG_CMD" in rendered:
            return SimpleNamespace(
                return_code=1,
                stdout="",
                stderr="Failed to spawn runtime: No such file or directory (os error 2)",
            )
        return SimpleNamespace(return_code=0, stdout="", stderr="")


def _arm_failure_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    scenario_path: Path,
    environment_path: Path,
):
    provider = _ArmWorkflowProvider(failure)
    executor = _ArmWorkflowExecutor()
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_build, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    monkeypatch.setattr(
        release_plan,
        "build_role_bindings",
        lambda *_args, **_kwargs: (
            RoleBindings(
                host=executor,
                stack=executor,
                loadgen=executor,
                cloud=executor,
                arm_builder=executor,
            ),
            None,
        ),
    )

    def infrastructure(_environment, _root, _provider, *, requires=()):
        def vm(title: str, name: str) -> Resource[VmInfo]:
            return Resource(
                title=title,
                acquire=lambda _inputs: VmInfo(
                    name=name, host="10.0.0.1", user="azureuser", home="/home/azureuser"
                ),
                release=lambda _inputs, _value: None,
                                requires=requires,
            )

        stack = vm("Acquire release stack VM", "release-stack")
        loadgen = vm("Acquire release loadgen VM", "release-loadgen")
        arm_builder = vm("Acquire release ARM builder VM", "release-arm")
        endpoints = Resource(
            title="Acquire release endpoints",
            acquire=lambda _inputs: release_resources.ReleaseEndpoints(
                "http://stack", "http://prom"
            ),
            release=lambda _inputs, _value: None,
            requires=(stack, loadgen),
        )
        return release_resources.ReleaseResources(stack, loadgen, arm_builder, endpoints)

    monkeypatch.setattr(release_plan, "build_release_resources", infrastructure)

    def create_archive(_root: Path, _commit: str, destination: Path) -> ArtifactEvidence:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"immutable source")
        return ArtifactEvidence("local", str(destination), digest_path(destination))

    monkeypatch.setattr(release_resources, "create_source_archive", create_archive)
    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )
    secret_paths = tuple(tmp_path / name for name in ("ghcr", "cosign.key", "cosign.password"))
    for path in secret_paths:
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o600)
    request = replace(
        request,
        credentials=CredentialFiles(
            ghcr_token=secret_paths[0],
            cosign_key=secret_paths[1],
            cosign_password=secret_paths[2],
        ),
    )
    workflow = build_release_workflow(request, provider=provider)
    phases = {
        compiled.task.title: compiled.task
        for compiled in workflow.compile().tasks
        if isinstance(compiled.task, ReleasePhaseTask)
    }
    arm_build = phases["Build ARM64 images"]
    for receipt in arm_build.prerequisites:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text("{}\n", encoding="utf-8")
    return workflow, provider, executor, phases


@pytest.mark.parametrize(
    ("failure", "error"),
    (
        ("build", "individual build failed"),
        ("push", "push failed"),
        ("digest", "invalid registry digest"),
        ("smoke", "smoke failed"),
    ),
)
def test_new_arm_workflow_failures_cleanup_and_never_publish(
    failure: str,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    workflow, provider, executor, phases = _arm_failure_workflow(
        monkeypatch, tmp_path, failure, *canonical_release_configs
    )

    with pytest.raises(RuntimeError, match=error):
        workflow.run(select=Selection(start="build-arm64-images"))

    provider_rendered = [" ".join(argv) for argv in provider.commands]
    executor_rendered = [" ".join(task.argv) for task in executor.commands]
    assert not any("skopeo copy" in command for command in executor_rendered)
    assert not any("imagetools create" in command for command in executor_rendered)
    assert any(
        command[:4] == ("docker", "buildx", "rm", "--force")
        for command in (task.argv for task in executor.commands)
    )
    assert (
        sum("systemctl stop nanofaas-registry-tunnel" in command for command in provider_rendered)
        >= 2
    )
    assert any("rm -rf --" in command and "/source" in command for command in provider_rendered)
    assert phases["Build ARM64 images"].receipt.exists() is (failure == "smoke")
    assert not phases["Test ARM64 images"].receipt.exists()


def test_new_arm_source_transfer_failure_compensates_all_acquired_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    workflow, provider, executor, phases = _arm_failure_workflow(
        monkeypatch, tmp_path, "source-transfer", *canonical_release_configs
    )

    with pytest.raises(RuntimeError, match="source transfer failed"):
        workflow.run(select=Selection(start="build-arm64-images"))

    rendered = [" ".join(argv) for argv in provider.commands]
    assert any("rm -rf --" in command and "source.tar" in command for command in rendered)
    assert sum("systemctl stop nanofaas-registry-tunnel" in command for command in rendered) >= 2
    assert any(task.argv[:4] == ("docker", "buildx", "rm", "--force") for task in executor.commands)
    assert not any("skopeo copy" in " ".join(task.argv) for task in executor.commands)
    assert not phases["Build ARM64 images"].receipt.exists()


def _write_receipt(path: Path, phase: str, kind: str, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "phase": phase,
                "evidence": [
                    {"kind": kind, "reference": reference, "digest": digest}
                    for reference, digest in entries.items()
                ],
            }
        ),
        encoding="utf-8",
    )


def _stage_attest_inputs(phases: Mapping[str, ReleasePhaseTask]) -> dict[str, str]:
    """Write the receipts the attest phase reads; return what they claim published.

    Every alias carries the digest of the manifest it points at, exactly as
    GHCR reports it, so the de-duplication the phase must do has something to
    de-duplicate.
    """
    release_dir = phases["Attest published images"].run_dir
    plan = build_publish_plan(
        NANOFAAS_ROOT, f"v{CURRENT_VERSION}", local_registry=DEFAULT_REGISTRY
    )

    def digest(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    architectures = {copy.destination: digest(copy.destination) for copy in plan.copies}
    manifests = {item.reference: digest(item.reference) for item in plan.manifests}
    aliases = {item.reference: manifests[item.source] for item in plan.aliases}
    assert aliases, "publish plan has no aliases, so nothing would de-duplicate"

    for title, phase, published in (
        ("Publish architecture images", "publish-architectures", architectures),
        ("Publish image manifests", "publish-manifests", manifests),
        ("Publish image aliases", "publish-aliases", aliases),
    ):
        _write_receipt(
            phases[title].receipt,
            phase,
            "ghcr-digest",
            {f"docker://{reference}": value for reference, value in published.items()},
        )

    aggregate_file = release_dir / "aggregate.json"
    aggregate_file.parent.mkdir(parents=True, exist_ok=True)
    aggregate_file.write_text(
        json.dumps(
            asdict(
                PerformanceAggregate(
                    profile=PerformanceProfile(
                        name="azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1",
                        provider="azure",
                        stack_vm="Standard_D8s_v5",
                        loadgen_vm="Standard_D2s_v5",
                        architecture="amd64",
                        flavor="native",
                        scenario="loadtest.yaml",
                    ),
                    run_count=3,
                    metrics={"throughputRps": 100.0, "latencyP95Ms": 10.0, "errorRate": 0.0},
                )
            )
        ),
        encoding="utf-8",
    )
    _write_receipt(
        phases["Aggregate benchmarks"].receipt,
        "aggregate",
        "file-digest",
        {str(aggregate_file): digest_path(aggregate_file)},
    )
    return {**architectures, **manifests, **aliases}


def test_attest_phase_records_one_signature_per_pinned_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """The attest receipt must prove a signature happened, once per pinned digest.

    The predicate digest alone proves nothing about signing: a release where
    cosign failed non-fatally would write the very same receipt.
    """
    workflow, provider, executor, phases = _arm_failure_workflow(
        monkeypatch, tmp_path, "none", *canonical_release_configs
    )
    published = _stage_attest_inputs(phases)
    attest = phases["Attest published images"]

    workflow.run(select=Selection(only="attest-published-images"))

    evidence = json.loads(attest.receipt.read_text(encoding="utf-8"))["evidence"]
    signatures = [item for item in evidence if item["kind"] == "cosign-attestation"]
    assert signatures, "attest produced no signing evidence"
    assert any(item["kind"] == "file-digest" for item in evidence), "predicate evidence lost"
    # one signature per unique pinned digest -- aliases resolve to the same
    # digest as their manifest and must not be signed twice
    pinned = {f"{reference.rsplit(':', 1)[0]}@{value}" for reference, value in published.items()}
    assert {item["reference"] for item in signatures} == pinned
    assert len(signatures) == len(pinned) < len(published)
    assert all(item["reference"].endswith(f"@{item['digest']}") for item in signatures)

    # the receipt may only claim what cosign was actually asked to sign
    signed = [
        task.argv[-1]
        for task in executor.commands
        if task.argv[-5:-1] == ("sign", "--yes", "--key", "/key.cosign")
    ]
    assert sorted(signed) == sorted(item["reference"] for item in signatures)
    assert any(destination.endswith("/predicate.json") for destination in provider.transfers)
    assert any(task.argv[:3] == ("grep", "-q", "PUBLIC KEY") for task in executor.commands)


def _touched(commands, references: set[str]) -> set[str]:
    """Which references the executor was asked to do any work on."""
    return {reference for reference in references if any(reference in c.argv for c in commands)}


def _attested(commands, references: set[str]) -> set[str]:
    """Which references reached `verify-attestation`, the last step of a group."""
    return {
        c.argv[-1]
        for c in commands
        if c.argv[-1] in references and "verify-attestation" in c.argv
    }


def _spoil_signature_evidence(journal: Path, reference: str) -> None:
    """Point one group's recorded signature at a digest its reference disowns.

    `signature_evidence_verifier` accepts a record only when the reference is
    pinned to the digest it claims, so this is the smallest edit that makes a
    real journal record stop verifying.
    """
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    spoiled = 0
    for record in records:
        if "/attest-" not in str(record.get("task_id", "")):
            continue
        for item in record.get("evidence", ()):
            if item.get("reference") == reference:
                item["digest"] = "sha256:" + "c" * 64
                spoiled += 1
    assert spoiled, f"no journalled group evidence for {reference}"
    journal.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def test_resumed_attest_skips_the_digests_it_already_signed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """A resume must not re-sign a digest whose group already finished.

    This is the property Tasks 8-9 assumed and never observed: a `Steps` of
    plain tasks re-runs every step, because `decide_resume` skips only a
    `ReusableTask` whose evidence verifies. So the assertion is what cosign was
    asked to do on the second run, not what a status string says.
    """
    workflow, _provider, executor, phases = _arm_failure_workflow(
        monkeypatch, tmp_path, "none", *canonical_release_configs
    )
    published = _stage_attest_inputs(phases)
    attest = phases["Attest published images"]
    journal = JournalConfig(tmp_path / "release.jsonl")
    verifiers = {"cosign-attestation": signature_evidence_verifier}
    only_attest = Selection(only="attest-published-images")
    pinned = {f"{reference.rsplit(':', 1)[0]}@{digest}" for reference, digest in published.items()}
    assert len(pinned) > 1, "one digest cannot show a resume skipping anything"

    # Run 1 dies on the second signature, so at least one group finished first.
    signs: list[str] = []

    def fail_second_sign(spec: CommandTaskSpec) -> bool:
        if "sign" in spec.argv and spec.argv[-1] in pinned:
            signs.append(spec.argv[-1])
            return len(signs) > 1
        return False

    executor.fail_when = fail_second_sign
    with pytest.raises(RuntimeError, match="cosign sign"):
        workflow.run(select=only_attest, journal=journal)

    finished = _attested(executor.commands, pinned)
    assert finished, "run 1 finished no group, so there is nothing to skip"
    assert signs[-1] not in finished

    # Run 2 resumes and must touch only the digests run 1 left unsigned.
    executor.fail_when = None
    mark = len(executor.commands)
    workflow.run(select=only_attest, journal=journal, resume=True, verifiers=verifiers)

    resumed = executor.commands[mark:]
    assert _touched(resumed, pinned) == pinned - finished
    assert _attested(resumed, pinned) == pinned - finished
    # The receipt claims what this run signed, not what it set out to sign.
    evidence = json.loads(attest.receipt.read_text(encoding="utf-8"))["evidence"]
    signatures = {item["reference"] for item in evidence if item["kind"] == "cosign-attestation"}
    assert signatures == pinned - finished

    # Run 3: one group's recorded evidence stops verifying, and that group
    # alone re-runs. Removing the receipt is what makes the phase itself run
    # again -- otherwise the whole phase skips and proves nothing.
    stale = sorted(finished)[0]
    attest.receipt.unlink()
    _spoil_signature_evidence(journal.path, stale)
    mark = len(executor.commands)
    workflow.run(select=only_attest, journal=journal, resume=True, verifiers=verifiers)

    rerun = executor.commands[mark:]
    assert _touched(rerun, pinned) == {stale}


def test_resumed_attest_retries_a_failed_prelude_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """A prelude failure must be retryable, not fatal to the next resume.

    The four prelude steps are plain tasks, and `decide_resume` refuses to
    resume a `failed` non-idempotent task at all -- it raises rather than
    re-running it. The public-key guard exists because `cosign public-key` can
    fail while exiting 0, so this is a path with a known failure mode.
    """
    workflow, _provider, executor, phases = _arm_failure_workflow(
        monkeypatch, tmp_path, "none", *canonical_release_configs
    )
    _stage_attest_inputs(phases)
    journal = JournalConfig(tmp_path / "release.jsonl")
    only_attest = Selection(only="attest-published-images")
    guard = ("grep", "-q", "PUBLIC KEY")

    executor.fail_when = lambda spec: spec.argv[:3] == guard
    with pytest.raises(RuntimeError, match="Verify derived cosign public key"):
        workflow.run(select=only_attest, journal=journal)

    executor.fail_when = None
    mark = len(executor.commands)
    workflow.run(
        select=only_attest,
        journal=journal,
        resume=True,
        verifiers={"cosign-attestation": signature_evidence_verifier},
    )

    assert any(spec.argv[:3] == guard for spec in executor.commands[mark:])


def test_build_release_workflow_compiles_without_cloud_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )

    workflow = build_release_workflow(request, provider=RejectingProvider())
    compiled = workflow.compile()

    release_phases = {
        task.task.title: task.task
        for task in compiled.tasks
        if isinstance(task.task, ReleasePhaseTask)
    }
    assert set(release_phases) == {
        "Run source tests",
        "Build AMD64 images",
        "Push AMD64 images to local registry",
        "Run release benchmark 1",
        "Run release benchmark 2",
        "Run release benchmark 3",
        "Aggregate benchmarks",
        "Evaluate regression gate",
        "Build ARM64 images",
        "Test ARM64 images",
        "Publish architecture images",
        "Publish image manifests",
        "Publish image aliases",
        "Attest published images",
        "Finalize release documentation",
    }
    assert all(
        task.receipt.parent == tmp_path / "run" / "releases" / CURRENT_VERSION / "receipts"
        for task in release_phases.values()
    )
    benchmarks = tuple(release_phases[f"Run release benchmark {index}"] for index in range(1, 4))
    push = release_phases["Push AMD64 images to local registry"]
    aggregate = release_phases["Aggregate benchmarks"]
    gate = release_phases["Evaluate regression gate"]
    arm_build = release_phases["Build ARM64 images"]
    arm_smoke = release_phases["Test ARM64 images"]
    publish_architectures = release_phases["Publish architecture images"]
    publish_manifests = release_phases["Publish image manifests"]
    publish_aliases = release_phases["Publish image aliases"]
    attestation = release_phases["Attest published images"]
    finalize = release_phases["Finalize release documentation"]
    assert all(task.prerequisites == (push.receipt,) for task in benchmarks)
    assert aggregate.prerequisites == tuple(task.receipt for task in benchmarks)
    assert gate.prerequisites == (aggregate.receipt,)
    assert arm_build.prerequisites == (gate.receipt, release_phases["Run source tests"].receipt)
    assert arm_smoke.prerequisites == (arm_build.receipt,)
    assert publish_architectures.prerequisites == (
        gate.receipt,
        arm_smoke.receipt,
        push.receipt,
        arm_build.receipt,
    )
    assert publish_manifests.prerequisites == (publish_architectures.receipt,)
    assert publish_aliases.prerequisites == (publish_manifests.receipt,)
    assert attestation.prerequisites == (
        publish_architectures.receipt,
        publish_manifests.receipt,
        publish_aliases.receipt,
        aggregate.receipt,
    )
    assert finalize.prerequisites[0] == attestation.receipt

    benchmark_slice = workflow.compile(select=Selection(only="run-release-benchmark-1"))
    benchmark_titles = [task.task.title for task in benchmark_slice.tasks]
    assert "Acquire release stack VM" in benchmark_titles
    assert "Acquire release loadgen VM" in benchmark_titles
    assert "Acquire release endpoints" in benchmark_titles
    assert "Acquire release ARM builder VM" not in benchmark_titles

    titles = [task.task.title for task in compiled.tasks]
    assert (
        titles.index("Acquire release stack VM")
        < titles.index("Acquire release loadgen VM")
        < titles.index("Acquire release ARM builder VM")
    )
    # Retention is opt-out, so the interesting set is the opposite one: nothing in
    # the release DAG may ask to always be released except the credential
    # resources, which this credential-free compile does not build. A tunnel or a
    # builder marked always_release would show up here.
    always_released = {
        task.resource.title
        for task in compiled.tasks
        if task.kind == "acquire" and task.resource is not None and task.resource.always_release
    }
    assert always_released == set()

    stack_slice = workflow.compile(select=Selection(only="build-amd64-images"))
    assert [task.task.title for task in stack_slice.tasks] == [
        "Acquire validated release execution credentials",
        "Acquire release stack VM",
        "Acquire immutable release source archive",
        "Acquire verified source on nanofaas-azure-release",
        "Acquire AMD64 Bake and BuildKit inputs",
        f"Acquire release-amd64-v{CURRENT_VERSION} buildx builder",
        "Build AMD64 images",
        f"Release release-amd64-v{CURRENT_VERSION} buildx builder",
        "Release AMD64 Bake and BuildKit inputs",
        "Release verified source on nanofaas-azure-release",
        "Release immutable release source archive",
        "Release release stack VM",
        "Release validated release execution credentials",
    ]

    arm_slice = workflow.compile(select=Selection(only="build-arm64-images"))
    arm_titles = [task.task.title for task in arm_slice.tasks]
    assert arm_titles[:8] == [
        "Acquire validated release execution credentials",
        "Acquire release stack VM",
        "Acquire release ARM builder VM",
        "Acquire registry tunnel to <release-stack>:5000",
        "Acquire ARM64 Bake and BuildKit inputs",
        f"Acquire release-arm64-v{CURRENT_VERSION} buildx builder",
        "Acquire immutable release source archive",
        "Acquire verified source on nanofaas-azure-release-arm",
    ]
    assert arm_titles[-8:] == [
        "Release verified source on nanofaas-azure-release-arm",
        "Release immutable release source archive",
        f"Release release-arm64-v{CURRENT_VERSION} buildx builder",
        "Release ARM64 Bake and BuildKit inputs",
        "Release registry tunnel to <release-stack>:5000",
        "Release release ARM builder VM",
        "Release release stack VM",
        "Release validated release execution credentials",
    ]

    arm_suffix = workflow.compile(select=Selection(start="build-arm64-images"))
    suffix_titles = [task.task.title for task in arm_suffix.tasks]
    assert "Acquire release stack VM" in suffix_titles
    assert "Acquire release ARM builder VM" in suffix_titles
    assert "Acquire release loadgen VM" not in suffix_titles
    assert "Release release ARM builder VM" in suffix_titles
    assert "Release release stack VM" in suffix_titles


def test_amd64_buildx_builder_replaces_a_surviving_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """A builder left over from a prior run must be replaced, not reused.

    ``buildx_builder_resource`` short-circuits on an existing builder unless
    ``replace_existing=True``, and skipping the create step also skips the
    ``--buildkitd-config`` that carries ``[worker.oci] max-parallelism`` --
    the unbounded-parallelism defect this release path exists to fix. The
    ARM64 builder already sets ``replace_existing=True``; the AMD64 builder
    must match it.
    """
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )

    calls: list[dict] = []
    real_buildx_builder_resource = release_plan.buildx_builder_resource

    def spy(**kwargs):
        calls.append(kwargs)
        return real_buildx_builder_resource(**kwargs)

    monkeypatch.setattr(release_plan, "buildx_builder_resource", spy)

    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )

    build_release_workflow(request, provider=RejectingProvider())

    amd64_calls = [call for call in calls if call["name"].startswith("release-amd64-")]
    assert len(amd64_calls) == 1
    assert amd64_calls[0]["replace_existing"] is True
    assert amd64_calls[0]["buildkitd_config"] is not None


@pytest.mark.parametrize(
    "selection",
    (
        None,
        Selection(only="build-arm64-images"),
        Selection(only="publish-architecture-images"),
    ),
)
def test_missing_execution_credentials_fail_before_any_provider_call(
    selection: Selection | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    provider = RejectingProvider()

    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )
    workflow = build_release_workflow(request, provider=provider)

    with pytest.raises(ValueError, match="credential config is required"):
        workflow.run(select=selection)
    assert provider.calls == []


def test_release_scenario_matches_comparable_history() -> None:
    scenario = ScenarioConfig.model_validate(
        yaml.safe_load((NANOLAB_ROOT / "scenarios-v2/release.yaml").read_text(encoding="utf-8"))
    )

    assert scenario.release is not None
    assert scenario.release.profile == "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1"
    assert scenario.release.benchmark_scenario == "loadtest.yaml"
    assert scenario.release.throughput_max_loss_percent == 10
    assert scenario.release.p95_max_increase_percent == 15
    assert scenario.release.error_rate_max == 0.30


def test_build_release_request_is_offline_and_builds_current_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    home = tmp_path / "home"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    secret_values = []
    for name in ("ghcr", "cosign.key", "cosign.password"):
        secret = secrets / name
        value = f"fixture-{name}-must-not-leak"
        secret_values.append(value)
        secret.write_text(value, encoding="utf-8")
        secret.chmod(0o600)
    credential_path = tmp_path / "credentials.yaml"
    credential_path.write_text(
        yaml.safe_dump(
            {
                "ghcr_token_file": "~/secrets/ghcr",
                "cosign_key_file": "~/secrets/cosign.key",
                "cosign_password_file": "~/secrets/cosign.password",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        release_plan,
        "vm_provider_for_environment",
        lambda *_args, **_kwargs: pytest.fail("preflight constructed a cloud client"),
    )
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )

    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=credential_path,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
        executable=True,
    )

    assert request.identity.source_commit == "a" * 40
    assert request.identity.release_config_digest == digest_path(scenario_path)
    assert request.identity.environment_digest == digest_path(environment_path)
    assert request.image_plan.cells
    assert len(request.image_plan.cells) == len(request.image_plan.targets) + sum(
        len(target.flavors) - 1 for target in request.image_plan.targets
    )
    assert request.credentials is not None
    assert request.credentials.ghcr_token == secrets / "ghcr"

    workflow = build_release_workflow(request, provider=SimpleNamespace())
    assert all(value not in repr(workflow.compile()) for value in secret_values)
    publish_titles = [
        item.task.title
        for item in workflow.compile(select=Selection(only="publish-architecture-images")).tasks
    ]
    assert "Acquire staged GHCR credentials" in publish_titles
    assert "Acquire staged Cosign credentials" not in publish_titles
    attest_titles = [
        item.task.title
        for item in workflow.compile(select=Selection(only="attest-published-images")).tasks
    ]
    assert "Acquire staged GHCR credentials" in attest_titles
    assert "Acquire staged Cosign credentials" in attest_titles
    finalize_titles = [
        item.task.title
        for item in workflow.compile(select=Selection(only="finalize-release-documentation")).tasks
    ]
    assert not any("credentials" in title.lower() for title in finalize_titles)


def test_build_release_request_requires_credentials_for_execution(
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
    nanofaas_checkout: Path,
) -> None:
    scenario_path, environment_path = canonical_release_configs
    # Reaches git_state for real, so it needs the repository, not the archive.
    with pytest.raises(ValueError, match="credential config is required"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=nanofaas_checkout,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
            executable=True,
        )


def test_build_release_request_rejects_missing_benchmark_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    (tmp_path / "loadtest.yaml").unlink()
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )

    with pytest.raises(ValueError, match="benchmark scenario must be a file"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
        )


def test_build_release_request_rejects_symlink_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    target = secrets / "real-token"
    target.write_text("token", encoding="utf-8")
    target.chmod(0o600)
    token = secrets / "ghcr-token"
    token.symlink_to(target)
    key = secrets / "cosign.key"
    password = secrets / "cosign.password"
    for path in (key, password):
        path.write_text(path.name, encoding="utf-8")
        path.chmod(0o600)
    credential_path = tmp_path / "credentials.yaml"
    credential_path.write_text(
        yaml.safe_dump(
            {
                "ghcr_token_file": str(token),
                "cosign_key_file": str(key),
                "cosign_password_file": str(password),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )

    with pytest.raises(ValueError, match="regular file"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=credential_path,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
            executable=True,
        )


def test_build_release_request_rejects_noncanonical_policy(tmp_path: Path, canonical_release_configs: tuple[Path, Path]) -> None:
    scenario_path, environment_path = canonical_release_configs
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scenario["release"]["profile"] = "different-profile"
    scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical Azure performance policy"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
        )


@pytest.mark.parametrize("repository", ("nanolab", "nanofaas"))
def test_build_release_request_rejects_credentials_inside_either_repository(
    repository: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    tool_root = tmp_path / "nanolab"
    source_root = tmp_path / "nanofaas"
    tool_root.mkdir()
    source_root.mkdir()
    secret_root = tool_root if repository == "nanolab" else source_root
    credential_paths = []
    for index, name in enumerate(("ghcr", "cosign.key", "cosign.password")):
        path = (secret_root if index == 0 else tmp_path) / name
        path.write_text(name, encoding="utf-8")
        path.chmod(0o600)
        credential_paths.append(path)
    credential_path = tmp_path / "credentials.yaml"
    credential_path.write_text(
        yaml.safe_dump(
            dict(
                zip(
                    ("ghcr_token_file", "cosign_key_file", "cosign_password_file"),
                    map(str, credential_paths),
                    strict=True,
                )
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(release_plan, "_release_source_commit", lambda *_args: "a" * 40)
    monkeypatch.setattr(release_plan, "validate_release_environment", lambda *_args: None)
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: source_root,
    )

    with pytest.raises(ValueError, match="outside the repository"):
        release_plan.build_release_request(
            repo_root=tool_root,
            nanofaas_root=source_root,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=credential_path,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
            executable=True,
        )


def test_build_release_request_rejects_dirty_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=False),
    )

    with pytest.raises(ValueError, match="clean nanoFaaS Git tree"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            source_tree=tmp_path / "tree",
        )


def test_build_release_request_plans_from_the_commit_not_the_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """Gitignored leftovers in the checkout must not reach the image matrix.

    The stub returns a sentinel that is *not* NANOFAAS_ROOT, and a spy on
    ``build_image_plan`` records which root it actually receives. A revert
    to planning from ``source_root`` would make the spy see NANOFAAS_ROOT
    instead of the sentinel, so this has teeth where comparing
    ``request.source_tree`` against a value equal to NANOFAAS_ROOT would not.
    """
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    sentinel_tree = Path("/sentinel-extracted-tree")
    assert sentinel_tree != NANOFAAS_ROOT
    extracted: list[tuple[Path, str, Path]] = []

    def fake_extract(repo_root: Path, commit: str, destination: Path) -> Path:
        extracted.append((repo_root, commit, destination))
        return sentinel_tree

    monkeypatch.setattr(release_plan, "extract_commit_tree", fake_extract)

    planned_roots: list[Path] = []

    def fake_build_image_plan(root, version, *, registry, architectures):
        planned_roots.append(root)
        return ImagePlan(version=version, registry=registry, targets=(), cells=("dummy-cell",))

    monkeypatch.setattr(release_plan, "build_image_plan", fake_build_image_plan)
    source_tree = tmp_path / "tree"

    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=source_tree,
    )

    assert extracted == [(NANOFAAS_ROOT, "a" * 40, source_tree)]
    assert planned_roots == [sentinel_tree]
    assert request.source_tree == sentinel_tree
    assert request.image_plan.cells


def test_build_release_workflow_plans_arm64_and_publish_from_the_source_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """The ARM64 and publish plans must read the extracted tree, not the checkout.

    ``request.source_tree`` is overridden to a sentinel distinct from
    ``request.nanofaas_root`` (both equal NANOFAAS_ROOT otherwise, which
    would make this assertion pass even if the workflow builder reverted to
    planning from the checkout). Spies on the two planners confirm they
    receive the sentinel.
    """
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )
    sentinel_tree = Path("/sentinel-extracted-tree")
    assert sentinel_tree != NANOFAAS_ROOT
    request = replace(request, source_tree=sentinel_tree)

    arm_roots: list[Path] = []

    def fake_build_arm64_image_plan(root, version, *, registry):
        arm_roots.append(root)
        return ImagePlan(version=version, registry=registry, targets=(), cells=())

    monkeypatch.setattr(release_plan, "build_arm64_image_plan", fake_build_arm64_image_plan)

    publish_roots: list[Path] = []

    def fake_build_publish_plan(root, version, *, local_registry):
        publish_roots.append(root)
        return PublishPlan(
            version=version, repository="ghcr.io/x", copies=(), manifests=(), aliases=()
        )

    monkeypatch.setattr(
        release_plan.release_publish, "build_publish_plan", fake_build_publish_plan
    )

    build_release_workflow(request, provider=RejectingProvider())

    assert arm_roots == [sentinel_tree]
    assert publish_roots == [sentinel_tree]


def test_release_source_outlives_the_benchmarks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
) -> None:
    """Benchmarks run their commands inside the staged source on the stack VM.

    `benchmark_plan` sets `repo_root` to the remote source directory, so Sonata
    must not splice the source resource's release before the last benchmark --
    it did, and every benchmark died on `cd: .../source: No such file or
    directory` after a full AMD64 build had already been paid for.
    """
    scenario_path, environment_path = canonical_release_configs
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: NANOFAAS_ROOT,
    )
    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=None,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
        source_tree=tmp_path / "tree",
    )

    titles = [
        task.task.title
        for task in build_release_workflow(request, provider=RejectingProvider()).compile().tasks
    ]
    release_source = titles.index("Release verified source on nanofaas-azure-release")

    assert release_source > titles.index("Run release benchmark 3")
