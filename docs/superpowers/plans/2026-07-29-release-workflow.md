# Release Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the procedural `nanolab release` with a Sonata `Workflow` compiled by `build_release_workflow()`.

**Architecture:** 9 new Task/Resource classes in `sonata_tasks/`, composite builders for each image-list phase, and a `build_release_workflow()` that wires 12 nodes. `ReleaseJournal`, metrics functions, publish/attest logic are reused unchanged.

**Tech Stack:** Python 3.12+, Sonata Engine, workflow_tasks executors, Azure VM provider

## Global Constraints

- Azure only
- New Tasks follow `CommandTask` pattern (title, executor, role)
- Provider-based Tasks take `provider` and `request` directly
- Resources follow `docker_registry_resource` pattern
- Tests use `RecordingExecutor` pattern from `test_compose.py`
- Composite builders return `Steps`
- No changes to `ReleaseJournal`, `ArtifactEvidence`, or `release/` modules

---

### Task 1: FileTransferTask

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/transfer.py`
- Create: `packages/sonata-tasks/tests/test_transfer.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`

**Interfaces:**
- Produces: `FileTransferTask(source, destination, provider, request, title)` — `Task[None]`

- [ ] **Step 1: Write the test**

```python
"""Tests for FileTransferTask."""

from dataclasses import dataclass
from pathlib import Path

from sonata_engine import TaskInputs

from sonata_tasks.transfer import FileTransferTask


class _FakeTransferProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def transfer_to(self, request, *, source, destination):
        self.calls.append((source, destination))
        return type("Result", (), {"return_code": 0, "stdout": "", "stderr": ""})()


def test_file_transfer_invokes_provider_and_returns_none():
    provider = _FakeTransferProvider()
    request = object()
    task = FileTransferTask(
        source=Path("/tmp/bake.json"),
        destination="/home/user/bake.json",
        provider=provider,
        request=request,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is None
    assert provider.calls == [(Path("/tmp/bake.json"), "/home/user/bake.json")]


def test_file_transfer_title_defaults_to_source_filename():
    task = FileTransferTask(
        source=Path("/tmp/buildkitd.toml"),
        destination="/remote/buildkitd.toml",
        provider=object(),
        request=object(),
    )
    assert task.title == "Transfer buildkitd.toml"


def test_file_transfer_raises_on_nonzero_exit():
    import pytest

    class _FailingProvider:
        def transfer_to(self, request, *, source, destination):
            return type("Result", (), {"return_code": 1, "stdout": "", "stderr": "disk full"})()

    task = FileTransferTask(
        source=Path("/tmp/bake.json"), destination="/remote/bake.json",
        provider=_FailingProvider(), request=object(),
    )
    with pytest.raises(RuntimeError, match="Transfer bake.json failed"):
        task.run(TaskInputs.empty())
```

- [ ] **Step 2: Verify failure** — `uv run pytest packages/sonata-tasks/tests/test_transfer.py -v`

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from sonata_engine import Task, TaskInputs, TaskOutcome


@dataclass
class FileTransferTask(Task[None]):
    """Transfer a local file to a remote path via provider.transfer_to()."""

    source: Path
    destination: str
    provider: Any
    request: Any
    title: str = field(default="")

    def __post_init__(self) -> None:
        if not self.title:
            self.title = f"Transfer {self.source.name}"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        result = self.provider.transfer_to(
            self.request, source=self.source, destination=self.destination
        )
        rc = int(getattr(result, "return_code", 0))
        if rc != 0:
            detail = getattr(result, "stderr", None) or getattr(result, "stdout", None) or ""
            raise RuntimeError(
                f"{self.title} failed (exit {rc})" + (f": {detail}" if detail else "")
            )
        return TaskOutcome(value=None)
```

- [ ] **Step 4: Verify pass** — `uv run pytest packages/sonata-tasks/tests/test_transfer.py -v`

- [ ] **Step 5: Export and commit**

In `__init__.py`:
```python
from sonata_tasks.transfer import FileTransferTask
# and to __all__: "FileTransferTask"
```

```bash
git add packages/sonata-tasks/ && git commit -m "feat: add FileTransferTask for remote file staging"
```

---

### Task 2: BuildxBuilder Resource

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/buildx.py`
- Create: `packages/sonata-tasks/tests/test_buildx.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`

**Interfaces:**
- Produces: `buildx_builder_resource(*, name, executor, role)` — `Resource[str]`, state = "existing" | builder name

- [ ] **Step 1: Test**

```python
"""Tests for buildx builder resource."""

from dataclasses import dataclass, field

from sonata_engine import TaskInputs
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.buildx import buildx_builder_resource


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[1] == "inspect":
            return TaskResult(
                task_id="", status="passed", return_code=0,
                stdout="Name: builder\nPlatforms: linux/amd64*, linux/arm64*\n",
            )
        return TaskResult(task_id="", status="passed", return_code=0)


def test_buildx_builder_creates_bootstraps_and_removes():
    executor = RecordingExecutor()
    resource = buildx_builder_resource(
        name="release-builder", executor=executor, role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "release-builder"
    resource.release(TaskInputs.empty(), state)

    argv_seqs = [tuple(s.argv) for s in executor.seen]
    assert argv_seqs == [
        ("docker", "buildx", "inspect", "release-builder"),
        ("docker", "buildx", "create", "--name", "release-builder",
         "--driver", "docker-container", "--use"),
        ("docker", "buildx", "inspect", "--bootstrap", "release-builder"),
        ("docker", "buildx", "rm", "--force", "release-builder"),
    ]


def test_buildx_builder_preexisting_is_not_removed():
    executor = RecordingExecutor()
    resource = buildx_builder_resource(
        name="reuse-me", executor=executor, role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "existing"
    resource.release(TaskInputs.empty(), state)
    assert len(executor.seen) == 1  # only inspect, no create or rm
```

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Implement** — Resource with `docker buildx inspect` → if exists return `"existing"`, else `create` → `bootstrap`, release: `rm --force` only if not existing.

- [ ] **Step 4: Verify pass**

- [ ] **Step 5: Export and commit**

---

### Task 3: RegistryTunnel Resource

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/registry_tunnel.py`
- Create: `packages/sonata-tasks/tests/test_registry_tunnel.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`

**Interfaces:**
- Produces: `registry_tunnel_resource(*, registry_upstream, provider, request)` — `Resource[None]`

- [ ] **Step 1: Test** — verify socat command references upstream host, cleanup stops the tunnel

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Implement** — acquire: `sudo systemd-run --unit nanofaas-registry-tunnel socat TCP-LISTEN:5000,fork,reuseaddr TCP:{upstream}:5000`; release: `sudo systemctl stop nanofaas-registry-tunnel`

- [ ] **Step 4: Verify pass**

- [ ] **Step 5: Export and commit**

---

### Task 4: SkopeoCopyTask + SkopeoInspectTask

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/skopeo.py`
- Create: `packages/sonata-tasks/tests/test_skopeo.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`

**Interfaces:**
- Produces: `SkopeoCopyTask(*, source, destination, authfile, src_tls_verify, executor, role)` — `CommandTask`
- Produces: `SkopeoInspectTask(*, reference, authfile, tls_verify, executor, role)` — `CommandTask`

- [ ] **Step 1: Test** — verify argv construction for copy (--preserve-digests, --src-tls-verify=false, --dest-authfile, docker:// prefixes) and inspect (--format={{.Digest}}, docker:// prefix)

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Implement** — both extend `CommandTask`, self.argv built in `__init__`

- [ ] **Step 4: Verify pass**

- [ ] **Step 5: Export and commit**

---

### Task 5: ImagetoolsCreateTask + ImagetoolsInspectTask

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/imagetools.py`
- Create: `packages/sonata-tasks/tests/test_imagetools.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`

**Interfaces:**
- Produces: `ImagetoolsCreateTask(*, tag, sources, docker_config, executor, role)` — `CommandTask`
- Produces: `ImagetoolsInspectTask(*, reference, docker_config, executor, role)` — `CommandTask`

- [ ] **Step 1: Test** — verify `docker buildx imagetools create --tag X src1 src2` with `DOCKER_CONFIG` env

- [ ] **Step 2-5:** Standard TDD cycle

---

### Task 6: SyftTask

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/syft.py`
- Create: `packages/sonata-tasks/tests/test_syft.py`

**Interfaces:**
- Produces: `SyftTask(*, image, output_file, docker_config, executor, role)` — `CommandTask`

Uses pinned `anchore/syft@sha256:f94e5d9f...` image from `attest.py`.

- [ ] **Step 1-5:** Standard TDD cycle

---

### Task 7: CosignTask

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/cosign.py`
- Create: `packages/sonata-tasks/tests/test_cosign.py`

**Interfaces:**
- Produces: `CosignTask(*, operation, image, key_file, password_file, docker_config, executor, role, predicate_file?, sbom_file?, public_key_file?)` — `CommandTask`

Password read from file inside `sh -c` wrapper, never in argv. Supports: sign, attest, attach sbom, verify, verify-attestation.

Uses pinned `gcr.io/projectsigstore/cosign@sha256:f1946d0f...` image.

- [ ] **Step 1-5:** Standard TDD cycle

---

### Task 8: SourceArchive Resource

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/archive.py`
- Create: `packages/sonata-tasks/tests/test_archive.py`

**Interfaces:**
- Produces: `source_archive_resource(*, repo_root, commit, remote_source_dir, remote_archive, provider, request)` — `Resource[str]`

Acquire: `git archive` locally → `transfer_to` remote → `sha256sum` verify → `tar -xf`. Release: `rm -rf` source dir.

- [ ] **Step 1-5:** Standard TDD cycle

---

### Task 9: Release Metrics Tasks

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/release_metrics.py`
- Create: `packages/sonata-tasks/tests/test_release_metrics.py`

**Interfaces:**
- Produces: `AggregateBenchmarks(run_dir, benchmark_runs, profile)` — `Task[PerformanceAggregate]`
- Produces: `EvaluateRegressionGate(aggregate, baseline, policy, k6_passed, autoscaling_passed)` — `Task[RegressionDecision]`

Pure Python wrappers around existing `aggregate_runs()` and `evaluate_regression()`.

- [ ] **Step 1-5:** Standard TDD cycle

---

### Task 10: Composite Builders

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/release_composites.py`
- Create: `packages/sonata-tasks/tests/test_release_composites.py`

**Interfaces:**
- `source_tests_composite(commands, executor)` → `Steps`
- `amd64_build_composite(plan, executor, role)` → `Steps`
- `registry_push_composite(plan, executor, role)` → `Steps`
- `arm64_build_composite(plan, executor, role, builder_name, ...)` → `Steps`
- `arm64_smoke_composite(plan, provider, request)` → `Steps`
- `publish_architectures_composite(plan, executor, role, source_digests, authfile)` → `Steps`
- `publish_manifests_composite(plan, executor, role, architecture_digests, docker_config)` → `Steps`
- `publish_aliases_composite(plan, executor, role, manifest_digests, docker_config)` → `Steps`
- `attest_composite(images, predicate_remote, sbom_dir_remote, cosign, docker_config, executor, role)` → `Steps`

Each returns a `Steps` with one child per image cell, e.g. `registry_push_composite` produces `Steps(*(Steps(DockerPushTask(...), SkopeoInspectTask(...)) for cell in plan.cells))`.

- [ ] **Step 1-5:** Write test for `registry_push_composite`, implement all 9, export, commit

---

### Task 11: ReleaseRequest and build_release_workflow()

**Files:**
- Create: `packages/nanolab/src/nanolab/plans/release.py`
- Create: `packages/nanolab/tests/plans/test_release.py`

**Interfaces:**
- Produces: `ReleaseRequest` dataclass
- Produces: `build_release_workflow(request)` → `Workflow`

**Implementation sketch** (the full builder, ~150 lines):

```python
@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    repo_root: Path
    version: str
    environment: EnvironmentConfig
    scenario: ScenarioConfig
    image_plan: ImagePlan
    settings: ReleaseSettings
    run_dir: Path
    performance_root: Path
    credentials: object | None = None


def build_release_workflow(request: ReleaseRequest) -> Workflow:
    env = request.environment
    if env.provider != "azure":
        raise ValueError("release workflow requires an Azure environment")

    from nanolab.cli.vm_provider import vm_provider_for_environment, vm_request_for_role
    from nanolab.cli.execution import build_role_bindings

    provider = vm_provider_for_environment(env, request.repo_root)
    stack_req = vm_request_for_role(env, "stack", loadtest=True)
    loadgen_req = vm_request_for_role(env, "loadgen", loadtest=True)
    arm_req = vm_request_for_role(env, "arm-builder")
    bindings, fetcher = build_role_bindings(env, vm_provider=provider, repo_root=request.repo_root)
    executor = RoleBoundCommandTaskExecutor(bindings)
    stack_host = provider.connection_host(stack_req)

    remote_root = f"/home/azureuser/nanofaas-release/{request.version}"
    source_dir = f"{remote_root}/source"
    control_plane_url = f"http://{stack_host}:30080"
    prometheus_url = f"http://{stack_host}:30090"

    wf = Workflow(workflow_id=f"release-{request.version}")

    # 01 — Source Tests
    archive = source_archive_resource(
        repo_root=request.repo_root, commit=git_state(request.repo_root).commit,
        remote_source_dir=source_dir, remote_archive=f"{remote_root}/source.tar",
        provider=provider, request=stack_req,
    )
    source_tests = source_tests_composite(
        source_test_commands(Path(source_dir)), executor=executor,
    )

    # 02 — AMD64 Build
    amd64_builder = buildx_builder_resource(
        name=f"release-amd64-{request.version}", executor=executor, role="stack",
    )
    amd64_build = amd64_build_composite(
        request.image_plan, executor=executor, role="stack",
    )

    # 03 — Registry Push
    registry_push = registry_push_composite(
        request.image_plan, executor=executor, role="stack",
    )

    # 04-06 — Benchmarks
    from nanolab.plans.loadtest import build_loadtest_plan
    from workflow_tasks.loadtest.adapters import HttpPrometheusClient
    benchmarks = []
    for i in range(1, request.settings.benchmark_runs + 1):
        benchmarks.append(
            build_loadtest_plan(
                request.scenario, env, bindings,
                control_plane_url=control_plane_url,
                prometheus_client=HttpPrometheusClient(prometheus_url),
                run_dir=request.run_dir / f"run-{i}",
                fetcher=fetcher, repo_root=request.repo_root,
            )
        )

    # 07 — Aggregate
    aggregate = AggregateBenchmarks(
        run_dir=request.run_dir, benchmark_runs=request.settings.benchmark_runs,
        profile=PerformanceProfile(
            name=request.settings.profile, provider="azure",
            stack_vm=env.azure.vm_size, loadgen_vm=env.azure.loadgen_vm_size,
            architecture="amd64", flavor="native", scenario=request.settings.scenario_name,
        ),
    )

    # 08 — Regression Gate
    reg_gate = EvaluateRegressionGate(
        aggregate=...,  # fed by upstream TaskOutcome
        baseline=None,  # resolved at runtime
        policy=RegressionPolicy(
            throughput_max_loss_percent=request.settings.throughput_max_loss_percent,
            p95_max_increase_percent=request.settings.p95_max_increase_percent,
            error_rate_max=request.settings.error_rate_max,
        ),
        k6_passed=True, autoscaling_passed=True,
    )

    # 09 — ARM64 Build
    tunnel = registry_tunnel_resource(
        registry_upstream=stack_host, provider=provider, request=arm_req,
    )
    arm64_builder = buildx_builder_resource(
        name=f"release-arm64-{request.version}", executor=executor, role="arm-builder",
    )
    arm_plan = arm.build_arm64_image_plan(
        request.repo_root, request.version, registry=request.image_plan.registry,
    )
    arm64_build = arm64_build_composite(
        arm_plan, executor=executor, role="arm-builder",
        builder_name=f"release-arm64-{request.version}",
        remote_bake_file=f"{remote_root}/docker-bake-arm64.json",
        remote_buildkit_config=f"{remote_root}/buildkitd.toml",
        remote_source_dir=source_dir, registry_upstream=stack_host,
    )

    # 10 — ARM64 Smoke
    arm64_smoke = arm64_smoke_composite(
        arm_plan, provider=provider, request=arm_req,
    )

    # 11 — Publish
    pub_plan = build_publish_plan(
        request.repo_root, request.version,
        local_registry=request.image_plan.registry,
    )
    pub_arch = publish_architectures_composite(
        pub_plan, executor=executor, role="stack",
        source_digests={}, authfile="...",  # resolved at runtime
    )
    pub_manifests = publish_manifests_composite(
        pub_plan, executor=executor, role="stack",
        architecture_digests={}, docker_config="...",
    )
    pub_aliases = publish_aliases_composite(
        pub_plan, executor=executor, role="stack",
        manifest_digests={}, docker_config="...",
    )

    # 12 — Attest + Finalize
    attest = attest_composite(
        images={}, predicate_remote=f"{remote_root}/predicate.json",
        sbom_dir_remote=f"{remote_root}/sboms",
        cosign_key="...", cosign_pw="...", docker_config="...",
        executor=executor, role="stack",
    )

    # Wire the DAG
    wf.add(source_tests, requires=(archive,))
    wf.add(amd64_build, requires=(source_tests, amd64_builder))
    wf.add(registry_push, requires=(amd64_build,))
    # Benchmarks depend on registry push for image availability
    for i, bw in enumerate(benchmarks):
        wf.add(bw, requires=(registry_push,) if i == 0 else (benchmarks[i - 1],))
    wf.add(aggregate, requires=tuple(benchmarks))
    wf.add(reg_gate, requires=(aggregate,))
    wf.add(arm64_build, requires=(reg_gate, tunnel, arm64_builder, archive))
    wf.add(arm64_smoke, requires=(arm64_build,))
    wf.add(pub_arch, requires=(arm64_smoke,))
    wf.add(pub_manifests, requires=(pub_arch,))
    wf.add(pub_aliases, requires=(pub_manifests,))
    wf.add(attest, requires=(pub_aliases,))

    return wf
```

Note: the actual implementation needs to handle the full benchmark workflow embedding (which requires converting `build_loadtest_plan()`'s returned `Workflow` into tasks the parent workflow can depend on). The current `loadtest.py` already returns a `Workflow`, which can be added as a sub-workflow or its tasks can be extracted.

- [ ] **Step 1: Write smoke test** — compile a minimal `ReleaseRequest`, verify `Workflow` has > 0 tasks

- [ ] **Step 2-5:** Implement, test, commit

---

### Task 12: Integration — wire benchmark workflows, handle evidence flow, finalize

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/release.py`
- Modify: `packages/nanolab/tests/plans/test_release.py`

**Remaining work:**

1. **Benchmark embedding**: `build_loadtest_plan()` returns a `Workflow`. The release workflow needs to embed it. Options: (a) add the sub-workflow's tasks directly to the parent, or (b) wrap it in a single `Task` that runs the sub-workflow. Choose (b) — cleanest.

2. **Evidence plumbing**: The current release code uses `ReleaseJournal` to track digest evidence between phases. The workflow needs to either: (a) pass evidence through `TaskOutcome.value` chains, or (b) keep using `ReleaseJournal` as a side-effect. Choose (b) — simpler, journal already has file locking.

3. **Credential staging**: GHCR token and cosign key need to be staged on the stack VM before publish/attest. Use `stage_ghcr_credentials` / `stage_cosign_credentials` as context-managed Resources.

4. **Remote file deployment**: bake.json, buildkitd.toml, predicate.json need transferring. Use `FileTransferTask` (Task 1).

- [ ] **Step 1: Benchmark wrapper Task**

```python
@dataclass
class RunBenchmarkWorkflow(Task[Path]):
    """Run a loadtest workflow and return the summary path."""
    workflow: Workflow
    summary_path: Path

    def run(self, inputs: TaskInputs) -> TaskOutcome[Path]:
        self.workflow.run()
        if not self.summary_path.is_file():
            raise RuntimeError(f"benchmark summary not written: {self.summary_path}")
        return TaskOutcome(value=self.summary_path)
```

- [ ] **Step 2-5:** Implement credential Resources, wire evidence journal, integration test, commit
