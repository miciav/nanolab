"""Synthetic runtime integration; no Docker/build/live workload execution."""

import json
import os
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import yaml
from sonata_engine import TaskInputs, Workflow
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.execution.ports import CommandTaskExecutor

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.images import BuildReceipt, BuildRecipe
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.preparation import PreparedSoak
from nanolab.tasks.soak.prerequisites import select_relevant_config
from nanolab.tasks.soak.runtime import (
    RunSingleVersionSoak,
    RuntimeDeployment,
    create_local_deployment,
    create_soak_lifecycle,
)
from nanolab.tasks.soak.sources import SourceSnapshot


def fake_deployment(**fields: object) -> RuntimeDeployment:
    """Build a deployment stand-in; these tests never read its unset fields."""
    return cast(RuntimeDeployment, SimpleNamespace(**fields))


def host_bindings() -> RoleBindings:
    """Build a binding whose executor these tests never invoke."""
    return RoleBindings({"host": cast(CommandTaskExecutor, object())})


def inert_scenario():
    """Build a scenario declaring neither diagnostics nor prerequisites."""
    return SimpleNamespace(
        soak=SimpleNamespace(
            diagnostics=SimpleNamespace(operations={}, baseline_operations={}),
            prerequisites=SimpleNamespace(mode="run", required_coverage=[]),
        )
    )


def prepared(
    tmp_path,
    *,
    metrics_profile=None,
    scenario="memory-soak-smoke-container.yaml",
):
    path = Path(__file__).resolve().parents[2] / "scenarios-v2" / scenario
    raw = yaml.safe_load(path.read_text())["soak"]
    if not raw["criteria"]:
        smoke = path.with_name("memory-soak-smoke-container.yaml")
        raw["criteria"] = yaml.safe_load(smoke.read_text())["soak"]["criteria"]
    if metrics_profile is not None:
        raw["metrics_profile"] = metrics_profile
    config = SoakConfig.model_validate(raw)
    root = tmp_path / "evidence"
    writer = ArtifactWriter(root, config.artifact_limit_bytes)
    writer.write_json("config.json", config.model_dump(mode="json"))
    recipes = tuple(
        BuildRecipe(
            role,
            "build",
            "jvm",
            "linux/amd64",
            "registry/" + role + ":run",
            None,
            {},
            role + "-recipe",
            None,
        )
        for role in config.roles
    )
    receipts = tuple(
        BuildReceipt(
            role,
            "registry/" + role + "@sha256:" + "a" * 64,
            "source",
            role + "-recipe",
            role + "-build",
            "linux/amd64",
            (),
            (),
            (),
        )
        for role in config.roles
    )
    return PreparedSoak(
        "soak-synthetic",
        config,
        root,
        writer,
        cast(SourceSnapshot, SimpleNamespace(fingerprint="source")),
        recipes,
        receipts,
        {name: [{"input": {}, "expected": {}}] for name in config.workload.rates},
        1.0,
        2.0,
    )


@pytest.mark.parametrize(
    ("scenario", "expected_profile"),
    [
        ("memory-soak-sync-container.yaml", "advanced"),
        ("memory-soak-sync-candidate-diagnostic-container.yaml", "soak"),
    ],
)
def test_local_compose_uses_only_frozen_images_and_private_network(
    tmp_path, monkeypatch, scenario, expected_profile
):
    value = prepared(tmp_path, scenario=scenario)
    value.config.diagnostics.operations = {role: [] for role in value.config.roles}
    leases = []

    class Lease:
        def __init__(self):
            self.closed = False
            leases.append(self)

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return "127.0.0.1", 20000 + len(leases)

        def close(self):
            self.closed = True

    monkeypatch.setattr("nanolab.tasks.soak.runtime.socket.socket", Lease)
    deployment = create_local_deployment(value, tmp_path)
    document = (
        json.loads(deployment.project.file.read_text())
        if hasattr(deployment.project, "file")
        else json.loads((tmp_path / "soak-compose.json").read_text())
    )
    assert all(
        "@sha256:" in service["image"] and "build" not in service
        for service in document["services"].values()
    )
    assert document["networks"] == {"owned": {}}
    assert (
        document["services"]["control-plane"]["environment"]["NANOFAAS_METRICS_PROFILE"]
        == expected_profile
    )
    assert (
        document["services"]["function-1"]["environment"]["NANOFAAS_METRICS_PROFILE"]
        == expected_profile
    )
    assert (
        "NANOFAAS_METRICS_PROFILE"
        not in document["services"]["function-2"]["environment"]
    )
    assert deployment.ownership.always_release
    deployment.ownership.acquire(TaskInputs.empty())
    assert all(lease.closed for lease in leases)
    assert all(
        function.manifest().body()["executionMode"] == "EXTERNAL"
        for function in deployment.request.functions
    )
    value.writer.close()


def test_control_plane_gets_a_writable_catalog_directory(tmp_path, monkeypatch):
    """The soak runs containers as the host user, not the image's own user.

    The control-plane image ships /var/lib/nanofaas owned by its distroless
    user, so the catalog write the platform does at startup is denied. Only
    the declared registry path is made writable; nothing else is relaxed.
    """
    import os

    value = prepared(tmp_path)
    leases = []

    class Lease:
        def __init__(self):
            leases.append(self)

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return "127.0.0.1", 20000 + len(leases)

        def close(self):
            pass

    monkeypatch.setattr("nanolab.tasks.soak.runtime.socket.socket", Lease)
    create_local_deployment(value, tmp_path)
    document = json.loads((tmp_path / "soak-compose.json").read_text())

    mounts = [
        mount
        for mount in document["services"]["control-plane"].get("volumes", [])
        if mount.get("target") == "/var/lib/nanofaas"
    ]
    assert len(mounts) == 1
    source = Path(mounts[0]["source"])
    assert mounts[0]["type"] == "bind" and not mounts[0].get("read_only")
    assert source.is_dir() and not source.is_symlink()
    assert source.stat().st_uid == os.getuid()
    assert os.access(source, os.W_OK)
    # The roles that do not declare a catalog get no extra writable mount.
    for service, spec in document["services"].items():
        if service != "control-plane":
            assert not [
                m
                for m in spec.get("volumes", [])
                if m.get("target") == "/var/lib/nanofaas"
            ]
    value.writer.close()


def test_public_task_is_inert_and_failure_persists_terminal(tmp_path, monkeypatch):
    import nanolab.tasks.soak.runtime as module

    called = []

    def prepare(*args, **kwargs):
        called.append("prepare")
        raise ValueError("actual pre-build prerequisite missing")

    monkeypatch.setattr(module, "prepare_soak", prepare)
    task = RunSingleVersionSoak(
        cast(ScenarioConfig, inert_scenario()),
        host_bindings(),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    assert called == []
    workflow = Workflow("test")
    workflow.add(task)
    with pytest.raises(ValueError, match="actual pre-build prerequisite missing"):
        workflow.run()
    assert called == ["prepare"]
    assert (
        json.loads((tmp_path / "run/terminal.json").read_text())["status"]
        == "INCONCLUSIVE"
    )
    with pytest.raises(RuntimeError, match=r"single-version soak cannot resume or"):
        workflow.run()


def test_cancelled_preparation_persists_aborted(tmp_path, monkeypatch):
    import nanolab.tasks.soak.runtime as module

    def prepare(*args, **kwargs):
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(module, "prepare_soak", prepare)
    task = RunSingleVersionSoak(
        cast(ScenarioConfig, inert_scenario()),
        host_bindings(),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    workflow = Workflow("test")
    workflow.add(task)
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        workflow.run()
    assert (
        json.loads((tmp_path / "run/terminal.json").read_text())["status"] == "ABORTED"
    )


def test_missing_effective_evidence_blocks_workload_and_writes_preflight(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.runtime as module

    value = prepared(tmp_path)
    targets = tuple(
        Target(role, "a" * 64, index + 1, "started", value.images[role], policy.runtime)
        for index, (role, policy) in enumerate(value.config.roles.items())
    )
    deployment = fake_deployment(
        discover=lambda: targets,
        metrics_endpoints={},
        observations=lambda targets: {},
        api_endpoint="http://127.0.0.1:10000",
    )

    class MissingTransport:
        def collect(self, *args):
            return {}

    monkeypatch.setattr(
        module,
        "make_workload_driver_factory",
        lambda *a, **k: (
            lambda phase: pytest.fail("workload started after missing preflight")
        ),
    )
    lifecycle = create_soak_lifecycle(
        value, deployment=deployment, run_dir=tmp_path, transport=MissingTransport()
    )
    workflow = Workflow("test")
    workflow.add(lifecycle)
    with pytest.raises(RuntimeError, match="INCONCLUSIVE"):
        workflow.run()
    preflight = json.loads((value.evidence_dir / "preflight.json").read_text())
    assert any(item["status"] == "INCONCLUSIVE" for item in preflight["criteria"])
    assert (
        json.loads((tmp_path / "terminal.json").read_text())["status"] == "INCONCLUSIVE"
    )
    value.writer.close()


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_real_deferred_lifecycle_preserves_evaluation_without_early_terminal(
    tmp_path, status
):
    from nanolab.tasks.soak.workflow import LifecycleHooks

    value = prepared(tmp_path)
    targets = tuple(
        Target(role, "a" * 64, index + 1, "started", value.images[role], policy.runtime)
        for index, (role, policy) in enumerate(value.config.roles.items())
    )
    deployment = fake_deployment(
        discover=lambda: targets,
        metrics_endpoints={},
        observations=lambda targets: {},
        api_endpoint="http://127.0.0.1:10000",
    )
    lifecycle = create_soak_lifecycle(
        value,
        deployment=deployment,
        run_dir=tmp_path,
        transport=SimpleNamespace(),
        defer_terminal=True,
    )

    class Clock:
        elapsed = 0.0

        def monotonic(self):
            self.elapsed += 1.0
            return self.elapsed

        def wait_until(self, deadline_s, cancelled):
            self.elapsed = deadline_s
            return True

    class Observer:
        def start(self, phase):
            pass

        def set_phase(self, phase):
            pass

        def stop(self, timeout_s):
            pass

    class Driver:
        def run(self, output_dir, duration_s, cancelled):
            return tmp_path / "workload.json"

        def stop(self, timeout_s):
            pass

    lifecycle.clock = Clock()
    lifecycle.observer = Observer()
    lifecycle.driver_factory = lambda phase: Driver()
    lifecycle.hooks = LifecycleHooks(
        preflight=lambda: None,
        prerequisites=lambda: None,
        baseline_capture=lambda state, timeout: None,
        final_capture=lambda state, timeout: None,
        evaluate=lambda state: {"status": status},
        report=lambda state: tmp_path / "report.json",
    )
    workflow = Workflow("deferred")
    workflow.add(lifecycle)
    if status == "FAIL":
        with pytest.raises(RuntimeError, match="FAIL"):
            workflow.run()
    else:
        workflow.run()
    assert lifecycle.state.evaluation == {"status": status}
    assert not (tmp_path / "terminal.json").exists()
    value.writer.close()


def test_entire_measurement_runs_and_emits_manifest_without_inventing_pass(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.models import Sample

    value = prepared(tmp_path)
    root = value.evidence_dir
    (root / "builds").mkdir()
    (root / "source").mkdir()
    (root / "source/snapshot.json").write_text("{}")
    for index, (recipe, receipt) in enumerate(
        zip(value.recipes, value.receipts, strict=True)
    ):
        value.writer.write_json(f"recipe-{index}.json", asdict(recipe))
        (root / f"builds/build-{index}.json").write_text(json.dumps(asdict(receipt)))
    targets = tuple(
        Target(role, "a" * 64, index + 1, "started", value.images[role], policy.runtime)
        for index, (role, policy) in enumerate(value.config.roles.items())
    )
    events = []

    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

        def wait_until(self, deadline, cancelled):
            self.now = deadline
            return True

    clock = Clock()

    class Sampling:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, phase):
            events.append(phase)

        def set_phase(self, phase):
            events.append(phase)

        def stop(self, timeout_s):
            events.append("observer-stop")

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def sample(self, target, phase, scheduled):
            return tuple(
                Sample(
                    target,
                    phase,
                    scheduled,
                    scheduled,
                    scheduled,
                    metric,
                    (),
                    "bytes",
                    1.0,
                    "observed",
                    "synthetic-probe",
                    None,
                )
                for metric in value.config.roles[target.role].required_metrics
            )

    def observed(targets):
        return {
            "retention_s": value.config.retention_s,
            "free_bytes": value.config.artifact_limit_bytes * 2,
            "generator": {"available": True, "max_vus": value.config.workload.max_vus},
            "roles": {
                target.role: {
                    "cpu": value.config.roles[target.role].expected_cpu,
                    "memory_bytes": value.config.roles[target.role].memory_limit_bytes,
                    "limit_sources": {
                        "cpu": "synthetic-cpu",
                        "memory_bytes": "synthetic-memory",
                    },
                    "image_digest": target.image_digest,
                    "runtime": target.runtime,
                    "runtime_options": value.config.roles[target.role].runtime_options,
                    "capabilities": value.config.roles[
                        target.role
                    ].required_capabilities,
                    "collection_sources": value.config.roles[
                        target.role
                    ].collection_sources,
                    "diagnostics": [],
                    "modules": value.config.images[target.role].modules,
                }
                for target in targets
            },
        }

    def drivers(*args, **kwargs):
        def create(phase):
            class Driver:
                def run(self, output_dir, duration_s, cancelled):
                    assert output_dir == root / phase
                    output_dir.mkdir()
                    events.append("load-" + phase)
                    clock.now += duration_s
                    for name in ("workload-inputs.json", "workload-config.json"):
                        (output_dir / name).write_text("{}")
                    (output_dir / "soak-workload.js").write_text(
                        "// synthetic missing evidence"
                    )
                    receipt = output_dir / "workload-receipt.json"
                    receipt.write_text(
                        json.dumps(
                            {
                                "schema": "nanolab-soak-v1",
                                "kind": "workload",
                                "completed": False,
                                "generator_end_s": clock.now,
                            }
                        )
                    )
                    return receipt

                def stop(self, timeout_s):
                    events.append("stop-" + phase)

            return Driver()

        return create

    evaluate = module.evaluate_run

    def evaluate_saved(root):
        assert events[-1] == "observer-stop"
        events.append("evaluate")
        return evaluate(root)

    monkeypatch.setattr(module, "Observer", Sampling)
    monkeypatch.setattr(module, "RoleBoundProbe", Probe)
    monkeypatch.setattr(module, "make_workload_driver_factory", drivers)
    monkeypatch.setattr(module, "evaluate_run", evaluate_saved)
    deployment = fake_deployment(
        discover=lambda: targets,
        metrics_endpoints={},
        observations=observed,
        api_endpoint="http://127.0.0.1:10000",
    )
    lifecycle = create_soak_lifecycle(
        value, deployment=deployment, run_dir=tmp_path, clock=clock
    )
    workflow = Workflow("measurement")
    workflow.add(lifecycle)
    with pytest.raises(RuntimeError, match="INCONCLUSIVE"):
        workflow.run()
    manifest = json.loads((root / "acceptance-manifest.json").read_text())
    assert set(manifest["phases"]) == {
        "preflight",
        "prerequisites",
        "warmup",
        "baseline_drain",
        "baseline",
        # The baseline checkpoint's captures are their own step, so a run's
        # phase keys do not depend on which readings it declares.
        "baseline_diagnostics",
        "steady",
        "drain",
    }
    assert manifest["completed"] is True
    assert manifest["workload"]["path"] == "steady/workload-receipt.json"
    assert events == [
        "warmup",
        "load-warmup",
        "stop-warmup",
        "baseline",
        "baseline",
        "steady",
        "load-steady",
        "stop-steady",
        "drain",
        "observer-stop",
        "evaluate",
    ]
    assert (
        json.loads((tmp_path / "terminal.json").read_text())["status"] == "INCONCLUSIVE"
    )
    report = lifecycle.state.report
    assert report is not None and report.is_file()
    value.writer.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("writer_close_fails", [False, True])
def test_public_terminal_is_published_only_after_cleanup(
    tmp_path, monkeypatch, cleanup_fails, writer_close_fails
):
    from sonata_engine import Resource, Task, TaskOutcome

    import nanolab.plans.soak as plans
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.runtime import RuntimeOptions

    events = []

    def close_writer():
        events.append("writer-close")
        assert not (tmp_path / "terminal.json").exists()
        if writer_close_fails:
            raise OSError("writer close failed")

    value = SimpleNamespace(
        writer=SimpleNamespace(close=close_writer),
        config=SimpleNamespace(
            prerequisites=SimpleNamespace(mode="run", required_coverage=[]),
            cancellation_timeout_s=30.0,
        ),
    )
    monkeypatch.setattr(module, "prepare_soak", lambda *a, **k: value)

    class Measurement(Task):
        title = "Synthetic assessment"
        state = SimpleNamespace(
            evaluation={"status": "PASS"}, report=tmp_path / "report.json"
        )

        def run(self, inputs):
            events.append("measurement")
            assert not (tmp_path / "terminal.json").exists()
            return TaskOutcome(value=self.state)

    def create(*args, **kwargs):
        assert kwargs["defer_terminal"] is True
        return Measurement()

    monkeypatch.setattr(module, "create_soak_lifecycle", create)

    def release(inputs, value):
        events.append("cleanup")
        assert not (tmp_path / "terminal.json").exists()
        if cleanup_fails:
            raise RuntimeError("cleanup is unconfirmed")

    owner = Resource(
        title="Synthetic owner",
        acquire=lambda inputs: None,
        release=release,
        always_release=True,
    )

    def compose(*args, **kwargs):
        workflow = Workflow("synthetic-runtime")
        workflow.add(kwargs["measurement"], requires=(owner,))
        return workflow

    monkeypatch.setattr(plans, "compose_frozen_soak_workflow", compose)
    deployment = fake_deployment(
        request=None,
        project=None,
        ownership=owner,
        api_endpoint="http://127.0.0.1:10000",
    )
    task = RunSingleVersionSoak(
        cast(ScenarioConfig, inert_scenario()),
        host_bindings(),
        run_dir=tmp_path,
        repo_root=tmp_path,
        tool_root=tmp_path,
        options=RuntimeOptions(deployment_factory=lambda *a: deployment),  # type: ignore[arg-type],
    )
    workflow = Workflow("public")
    workflow.add(task)
    if cleanup_fails or writer_close_fails:
        with pytest.raises(Exception, match=r"cleanup|writer"):
            workflow.run()
    else:
        workflow.run()
    assert events == ["measurement", "cleanup", "writer-close"]
    assert json.loads((tmp_path / "terminal.json").read_text())["status"] == (
        "INCONCLUSIVE" if cleanup_fails or writer_close_fails else "PASS"
    )


def test_runtime_options_accept_explicit_memory_helper_digest_without_io():
    from nanolab.tasks.soak.runtime import RuntimeOptions

    digest = (
        "localhost:5000/nanolab/p24-diagnostic-helper@sha256:"
        "b617e4ced631bc03de242f1cbba015398771378513cf5fc5eaa5fc67b09b3ef9"
    )
    assert RuntimeOptions(memory_helper_image=digest).memory_helper_image == digest
    with pytest.raises(ValueError, match=r"memory helper requires an explicit"):
        RuntimeOptions(memory_helper_image="localhost:5000/helper:latest")


_MEMORY_IMAGE = (
    "localhost:5000/nanolab/p24-diagnostic-helper@sha256:"
    "b617e4ced631bc03de242f1cbba015398771378513cf5fc5eaa5fc67b09b3ef9"
)


def memory_environment(tmp_path, monkeypatch):
    import nanolab.tasks.soak.runtime as module

    handles = []
    specs = []
    credentials = tmp_path / "proc/123"
    credentials.mkdir(parents=True)
    (credentials / "status").write_text(
        "Uid:\t1000\t65532\t65532\t65532\nGid:\t1000\t65531\t65531\t65531\n"
    )
    monkeypatch.setattr(
        module,
        "Path",
        lambda value: tmp_path / "proc" if value == "/proc" else Path(value),
    )
    monkeypatch.setattr(module, "_docker_get", lambda *args: {"Architecture": "arm64"})

    class Handle:
        def __init__(self, spec):
            self.spec = spec
            self.closed = 0
            self.reads = 0
            self.error = None
            self.close_error = False
            self.smaps = "Pss: 7 kB\n"
            self.target = spec.target
            handles.append(self)

        def read_memory(self, *, timeout_s):
            assert 0 < timeout_s <= 5
            self.reads += 1
            if self.error:
                raise self.error
            return {
                "schema": "nanolab-soak-memory-helper-v1",
                "target": asdict(self.target),
                "before": {"uid": 65532, "gid": 65531},
                "after": {"uid": 65532, "gid": 65531},
                "status": "VmRSS: 50 kB\n",
                "smaps_rollup": self.smaps,
                "errors": {}
                if self.smaps is not None
                else {"smaps_rollup": "Permission denied"},
            }

        def close(self):
            self.closed += 1
            if self.close_error:
                raise RuntimeError("cannot remove helper")

    class Provisioner:
        def __init__(self, **kwargs):
            pass

        def prepare_memory(self, spec, *, timeout_s):
            specs.append(spec)
            assert timeout_s == 30
            return Handle(spec)

        def prepare(self, *args, **kwargs):
            pytest.fail("diagnostic preparation must not run")

    monkeypatch.setattr(module, "LocalDockerDiagnosticProvisioner", Provisioner)
    return module, handles, specs


def memory_transport(module, tmp_path, target):
    class Base:
        def collect(self, actual, endpoint, timeout_s):
            identity = {
                "container_id": actual.container_id,
                "process_id": actual.process_id,
                "process_started_at": actual.process_started_at,
                "running": True,
                "image_digests": [actual.image_digest],
            }
            return {
                "before": identity,
                "after": identity,
                "procfs": {
                    "status": "VmRSS: 999 kB\n",
                    "smaps_rollup": "Pss: 999 kB\n",
                },
                "errors": {"procfs": "host denied"},
                "stats": {
                    "memory_stats": {"usage": 12345, "limit": 99999, "stats": {}}
                },
                "exposition": "process_pss_bytes 99999999\nrequests_total 3\n",
            }

    return module._MemoryHelperTransport(
        Base(),
        images={target.role: _MEMORY_IMAGE},
        project_name="soak-owned",
        output_root=tmp_path / "memory",
        docker_socket="/var/run/docker.sock",
        cancelled=None,
    )


def memory_target(runtime="native", role="control-plane"):
    return Target(
        role,
        "a" * 64,
        123,
        "2026-09-13T10:00:00Z",
        "registry/app@sha256:" + "b" * 64,
        runtime,
    )


def memory_samples(module, transport, target):
    probe = module.RoleBoundProbe(
        (
            module.RoleBinding(
                target,
                None,
                {
                    "process_rss_bytes": "bytes",
                    "process_pss_bytes": "bytes",
                    "cgroup_memory_usage_bytes": "bytes",
                    "requests_total": "count",
                },
            ),
        ),
        transport,
        timeout_s=5,
    )
    return {row.metric: row for row in probe.sample(target, "preflight", 1.0)}


@pytest.mark.parametrize("runtime", ["native", "jvm", "node"])
def test_memory_helpers_use_actual_credentials_and_parse_distinct_pss(
    tmp_path, monkeypatch, runtime
):
    from nanolab.tasks.soak.diagnostic_helper import helper_create_argv

    module, handles, specs = memory_environment(tmp_path, monkeypatch)
    target = memory_target(runtime)
    transport = memory_transport(module, tmp_path, target)
    assert handles == []
    transport.prepare((target,))
    spec = specs[0]
    assert (spec.uid, spec.gid) == (65532, 65531)
    assert spec.architecture == "arm64"
    assert spec.helper_image == _MEMORY_IMAGE
    assert spec.memory_only is True
    assert spec.allow_target_stop_on_cancel is False
    argv = helper_create_argv(spec, "owned-memory-reader")
    assert "--mount" not in argv
    assert "--cap-add" not in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    rows = memory_samples(module, transport, target)
    assert rows["process_pss_bytes"].value == 7168
    assert rows["process_rss_bytes"].value == 51200
    assert rows["cgroup_memory_usage_bytes"].value == 12345
    assert rows["requests_total"].value == 3
    assert rows["process_pss_bytes"].source == "procfs"
    assert handles[0].reads == 1
    transport.close()
    transport.close()
    assert handles[0].closed == 1


@pytest.mark.parametrize("smaps", [None, "Rss: 50 kB\n", "Pss: nonsense kB\n"])
def test_missing_pss_never_becomes_zero_rss_or_exposition(tmp_path, monkeypatch, smaps):
    module, handles, _ = memory_environment(tmp_path, monkeypatch)
    target = memory_target()
    transport = memory_transport(module, tmp_path, target)
    transport.prepare((target,))
    handles[0].smaps = smaps
    row = memory_samples(module, transport, target)["process_pss_bytes"]
    assert row.value is None
    assert row.availability == "unavailable"
    transport.close()


def test_memory_read_failure_cannot_reuse_host_procfs(tmp_path, monkeypatch):
    module, handles, _ = memory_environment(tmp_path, monkeypatch)
    target = memory_target()
    transport = memory_transport(module, tmp_path, target)
    transport.prepare((target,))
    handles[0].error = RuntimeError("target capabilities deny smaps")
    row = memory_samples(module, transport, target)["process_pss_bytes"]
    assert row.value is None
    assert row.availability == "unavailable"
    assert "capabilities" in row.reason
    transport.close()


def test_memory_sample_for_changed_target_is_unavailable(tmp_path, monkeypatch):
    module, handles, _ = memory_environment(tmp_path, monkeypatch)
    target = memory_target()
    transport = memory_transport(module, tmp_path, target)
    transport.prepare((target,))
    handles[0].target = memory_target(role="another-function")
    row = memory_samples(module, transport, target)["process_pss_bytes"]
    assert row.value is None
    assert row.availability == "unavailable"
    transport.close()


def test_helper_cleanup_attempts_every_handle_and_reports_failure(
    tmp_path, monkeypatch
):
    module, handles, _ = memory_environment(tmp_path, monkeypatch)
    first, second = memory_target(), memory_target(role="second")
    transport = memory_transport(module, tmp_path, first)
    transport.images[second.role] = _MEMORY_IMAGE
    transport.prepare((first, second))
    handles[0].close_error = True
    with pytest.raises(ExceptionGroup, match="memory helper cleanup unconfirmed"):
        transport.close()
    assert [handle.closed for handle in handles] == [1, 1]
    handles[0].close_error = False
    transport.close()
    assert [handle.closed for handle in handles] == [2, 1]


def test_failed_helper_preparation_closes_previously_prepared_helpers(
    tmp_path, monkeypatch
):
    module, handles, _ = memory_environment(tmp_path, monkeypatch)
    first, second = memory_target(), memory_target(role="second")
    transport = memory_transport(module, tmp_path, first)
    transport.images[second.role] = _MEMORY_IMAGE
    calls = []

    def identity(target):
        calls.append(target)
        if len(calls) == 2:
            raise PermissionError("cannot observe second UID")
        return 65532, 65531

    monkeypatch.setattr(module, "_target_effective_ids", identity)
    with pytest.raises(PermissionError, match="cannot observe second UID"):
        transport.prepare((first, second))
    assert handles[0].closed == 1


def test_frozen_helper_map_runs_without_diagnostics_and_closes_on_preflight_error(
    tmp_path, monkeypatch
):
    module, handles, specs = memory_environment(tmp_path, monkeypatch)
    value = prepared(tmp_path)
    value.config.diagnostics.helper_images.update(
        dict.fromkeys(value.config.roles, _MEMORY_IMAGE)
    )
    targets = tuple(
        memory_target(policy.runtime, role)
        for role, policy in value.config.roles.items()
    )
    deployment = fake_deployment(
        project=SimpleNamespace(name="soak-owned"),
        discover=lambda: targets,
        metrics_endpoints={},
        api_endpoint="http://127.0.0.1:18080",
        observations=lambda targets: {},
    )
    base = memory_transport(module, tmp_path, targets[0]).base
    lifecycle = create_soak_lifecycle(
        value, deployment=deployment, run_dir=tmp_path, transport=base
    )
    assert handles == []
    with pytest.raises(RuntimeError, match="INCONCLUSIVE"):
        Workflow("missing-other-evidence").add(lifecycle).run()
    assert len(specs) == len(targets)
    assert all(handle.closed == 1 for handle in handles)
    assert value.config.diagnostics.operations == {}
    saved = json.loads((value.evidence_dir / "memory-helper-inputs.json").read_text())
    assert saved["images"] == value.config.diagnostics.helper_images
    assert saved["allow_target_stop_on_cancel"] is False
    value.writer.close()


def test_explicit_helper_option_cannot_override_frozen_map(tmp_path):
    value = prepared(tmp_path)
    value.config.diagnostics.helper_images["control-plane"] = _MEMORY_IMAGE
    deployment = fake_deployment(
        discover=lambda: (memory_target(),), api_endpoint="http://127.0.0.1:18080"
    )
    with pytest.raises(ValueError, match=r"memory helper option conflicts with"):
        create_soak_lifecycle(
            value,
            deployment=deployment,
            run_dir=tmp_path,
            memory_helper_image="registry/other@sha256:" + "c" * 64,
        )
    value.writer.close()


def process_arguments_fixture(tmp_path, monkeypatch, body=b"-XX:TieredStopAtLevel=1\n"):
    import nanolab.tasks.soak.runtime as module

    directory = tmp_path / "proc/123"
    cgroup = directory / "root/sys/fs/cgroup"
    cgroup.mkdir(parents=True)
    (cgroup / "cpu.max").write_text("200000 100000\n")
    (cgroup / "cpuset.cpus.effective").write_text("0-1\n")
    (cgroup / "memory.max").write_text("1073741824\n")
    (directory / "root/app").mkdir()
    (directory / "root/app/jvm.options").write_bytes(body)
    (directory / "exe").symlink_to("/opt/jre/bin/java")
    (directory / "cmdline").write_bytes(
        b"/opt/jre/bin/java\0@/app/jvm.options\0-XX:MaxRAMPercentage=70\0"
        b"-Xss256k\0-jar\0/app/app.jar\0-XX:TieredStopAtLevel=9\0"
    )
    (directory / "environ").write_bytes(b"JAVA_TOOL_OPTIONS=-XX:TieredStopAtLevel=4\0")
    target = memory_target("jvm")
    monkeypatch.setattr(
        module,
        "Path",
        lambda path: tmp_path / "proc" if path == "/proc" else Path(path),
    )
    monkeypatch.setattr(
        module, "collect_procfs", lambda *args: {"start_ticks": "12345"}
    )
    monkeypatch.setattr(
        module,
        "_docker_get",
        lambda *args: {
            "Id": target.container_id,
            "Config": {"Image": target.image_digest},
            "State": {
                "Pid": target.process_id,
                "StartedAt": target.process_started_at,
                "Running": True,
            },
        },
    )
    return module, target, directory


def test_jvm_argfile_override_and_following_flags_are_observed(tmp_path, monkeypatch):
    import base64
    import hashlib

    body = b"# actual image tuning\n-XX:TieredStopAtLevel=1\n"
    module, target, _ = process_arguments_fixture(tmp_path, monkeypatch, body)
    actual = module.observe_local_process(target)
    assert actual["runtime_options"] == [
        "-XX:TieredStopAtLevel=4",
        "-XX:TieredStopAtLevel=1",
        "-XX:MaxRAMPercentage=70",
        "-Xss256k",
    ]
    proof = actual["runtime_argument_evidence"]
    assert proof["target"] == asdict(target)
    assert proof["process_start_ticks"] == "12345"
    assert proof["argfiles"][0]["path"] == "/app/jvm.options"
    assert base64.b64decode(proof["argfiles"][0]["bytes_base64"]) == body
    assert proof["argfiles"][0]["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(
    "body",
    [
        b'-Dlabel="unsupported quoting"\n',
        b"-Dlabel=escaped\\ value\n",
        b"@/app/nested.options\n",
        b"--disable-@files\n",
        b"-XX:VMOptionsFile=/app/hidden.options\n",
        b"x" * 65537,
    ],
)
def test_unsupported_argfile_never_claims_environment_only_flags(
    tmp_path, monkeypatch, body
):
    module, target, _ = process_arguments_fixture(tmp_path, monkeypatch, body)
    actual = module.observe_local_process(target)
    assert actual["runtime_options"] is None
    assert actual["runtime_argument_evidence"]["unavailable"]


def test_owned_argfile_symlink_cannot_read_host_file(tmp_path, monkeypatch):
    module, target, directory = process_arguments_fixture(tmp_path, monkeypatch)
    path = directory / "root/app/jvm.options"
    path.unlink()
    path.symlink_to("/etc/passwd")
    actual = module.observe_local_process(target)
    assert actual["runtime_options"] is None
    assert actual["runtime_argument_evidence"]["argfiles"] == []


def test_process_change_during_argfile_read_is_rejected(tmp_path, monkeypatch):
    module, target, _ = process_arguments_fixture(tmp_path, monkeypatch)
    starts = iter(("12345", "67890"))
    monkeypatch.setattr(
        module, "collect_procfs", lambda *args: {"start_ticks": next(starts)}
    )
    with pytest.raises(ValueError, match=r"process identity changed while"):
        module.observe_local_process(target)


_DIAGNOSTIC_IMAGE = (
    "localhost:5000/nanolab/p24-diagnostic-helper@sha256:"
    "5ac642accc637de4f0a5621d0804cc5ab784880186f90ea159c1edbd9f7d02ce"
)


def diagnostic_prepared(tmp_path, *, baseline=None):
    """Synthetic budgets intentionally exercise quotas above memory/4."""
    import nanolab.tasks.soak.runtime as module

    value = prepared(tmp_path)
    roles = dict(value.config.roles)
    selected = {}
    for runtime, memory in (("jvm", 1024**3), ("node", 512 * 1024**2)):
        role = next(name for name, policy in roles.items() if policy.runtime == runtime)
        policy = roles[role]
        options = list(policy.runtime_options)
        if runtime == "node":
            options.append(module._NODE_PRELOAD)
        roles[role] = policy.model_copy(
            update={"memory_limit_bytes": memory, "runtime_options": options}
        )
        selected[role] = ["gc", "heap_dump"]
    diagnostics = value.config.diagnostics.model_copy(
        update={
            "operations": selected,
            "baseline_operations": baseline or {},
            "max_dumps": 100,
            "max_dump_bytes": 800 * 1024**2,
            "helper_images": dict.fromkeys(selected, _DIAGNOSTIC_IMAGE),
            "gc_completion_evidence": {
                role: "jdk.GarbageCollection"
                if roles[role].runtime == "jvm"
                else "node:perf_hooks:major-gc"
                for role in selected
            },
        }
    )
    return replace(
        value,
        config=value.config.model_copy(
            update={
                "roles": roles,
                "diagnostics": diagnostics,
                "artifact_limit_bytes": 2 * 1024**3,
            }
        ),
    )


def test_diagnostic_quota_uses_actual_dump_count_and_shared_reservations(tmp_path):
    import nanolab.tasks.soak.runtime as module

    value = diagnostic_prepared(tmp_path)
    original = value.config.model_dump(mode="json")
    inputs = module._diagnostic_resource_inputs(value, allow_target_stop=True)
    assert inputs["dump_count"] == 2 and inputs["max_dumps"] == 100
    budget = module.DiagnosticBudget(
        100, inputs["max_dump_bytes"], inputs["available_artifact_bytes"]
    )
    payload = total = dumps = 0
    for role, decision in inputs["roles"].items():
        quota = decision["quota_bytes"]
        memory = value.config.roles[role].memory_limit_bytes
        assert (
            quota
            == min(
                inputs["payload_budget_bytes"] // 4,
                memory,
                inputs["max_dump_bytes"] // 2,
            )
            // 4096
            * 4096
        )
        assert quota > memory // 4
        assert decision["helper_memory_bytes"] == quota + max(
            64 * 1024**2, memory // 4 // 4096 * 4096
        )
        for operation in decision["operations"]:
            budget.reserve(quota, dump=operation == "heap_dump")
            payload += quota
            total += quota + 65536
            dumps += quota if operation == "heap_dump" else 0
    assert payload <= inputs["payload_budget_bytes"]
    assert total <= inputs["available_artifact_bytes"]
    assert dumps <= value.config.diagnostics.max_dump_bytes
    assert value.config.model_dump(mode="json") == original
    value.writer.close()


def test_no_operations_need_neither_diagnostic_permission_nor_inputs(tmp_path):
    import nanolab.tasks.soak.runtime as module

    value = prepared(tmp_path)
    assert module._diagnostic_resource_inputs(value, allow_target_stop=False) == {}
    assert module.RuntimeOptions().allow_diagnostic_target_stop_on_cancel is False
    value.writer.close()


@pytest.mark.parametrize("reason", ["permission", "dump-count", "preload", "budget"])
def test_diagnostic_policy_rejected_before_deployment(tmp_path, reason):
    import nanolab.tasks.soak.runtime as module

    value = diagnostic_prepared(tmp_path)
    if reason == "dump-count":
        value.config.diagnostics.max_dumps = 1
    elif reason == "preload":
        for policy in value.config.roles.values():
            if policy.runtime == "node":
                policy.runtime_options.remove(module._NODE_PRELOAD)
    elif reason == "budget":
        value.config.diagnostics.max_dump_bytes = 1
    with pytest.raises(
        ValueError, match=r"permission|max_dumps|runtime_options|budget"
    ):
        module.create_local_deployment(
            value,
            tmp_path,
            allow_diagnostic_target_stop_on_cancel=reason != "permission",
        )
    assert not (tmp_path / "soak-compose.json").exists()
    assert not (value.evidence_dir / "diagnostic-resource-inputs.json").exists()
    value.writer.close()


def diagnostic_deployment(value, tmp_path, monkeypatch):
    import nanolab.tasks.soak.runtime as module

    ports = iter(range(20000, 20100))
    monkeypatch.setattr(
        module.socket,
        "socket",
        lambda: SimpleNamespace(
            bind=lambda address: None,
            getsockname=lambda: ("127.0.0.1", next(ports)),
            close=lambda: None,
        ),
    )
    return module.create_local_deployment(
        value, tmp_path, allow_diagnostic_target_stop_on_cancel=True
    )


def test_owned_diagnostic_compose_and_controller_are_frozen_before_acquire(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.retention import compose_file_fingerprint

    value = diagnostic_prepared(tmp_path)
    original = value.config.model_dump(mode="json")
    deployment = diagnostic_deployment(value, tmp_path, monkeypatch)
    document = json.loads(deployment.project.file.read_text())
    inputs = json.loads(
        (value.evidence_dir / "diagnostic-resource-inputs.json").read_text()
    )
    assert inputs == deployment.diagnostic_inputs
    assert compose_file_fingerprint(deployment.project, tmp_path)
    for role, decision in inputs["roles"].items():
        volume = document["volumes"][decision["volume_key"]]
        assert volume["driver"] == "local"
        assert "name" not in volume and "external" not in volume
        assert volume["labels"] == {
            "nanolab.run": value.run_id,
            "nanolab.diagnostic.tmp": "true",
        }
        assert f"size={decision['quota_bytes']}," in volume["driver_opts"]["o"]
        if value.config.roles[role].runtime == "node":
            controller = decision["controller"]
            assert module.describe_artifact(Path(controller["path"])) == controller
            assert Path(controller["path"]).parent == tmp_path
    for service in document["services"].values():
        shared = [m for m in service.get("volumes", ()) if m["target"] == "/tmp"]
        if shared:
            assert service["cap_drop"] == ["ALL"]
            assert shared[0]["volume"] == {"nocopy": True}
    assert value.config.model_dump(mode="json") == original
    deployment.ownership.acquire(TaskInputs.empty())
    value.writer.close()


def diagnostic_environment(tmp_path, monkeypatch, *, baseline=None):
    """Mock live helper/collector boundaries; retain real lifecycle hooks/budget."""
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.models import Sample

    value = diagnostic_prepared(tmp_path, baseline=baseline)
    deployment = diagnostic_deployment(value, tmp_path, monkeypatch)
    targets = tuple(
        memory_target(policy.runtime, role)
        for role, policy in value.config.roles.items()
    )
    observations = {
        "roles": {
            target.role: {
                "memory_bytes": value.config.roles[target.role].memory_limit_bytes,
                "limit_sources": {"memory_bytes": "observed-test-cgroup"},
            }
            for target in targets
        }
    }
    events, helpers, specs, budgets = [], [], [], []

    class Helper:
        def __init__(self, spec):
            self.spec = spec
            self.receipt = value.writer.write_json(
                f"prepared-{spec.target.role}.json",
                {
                    "full_gc_source": "jdk.GarbageCollection"
                    if spec.target.runtime == "jvm"
                    else "node:perf_hooks:major-gc",
                },
            )
            self.closed = 0
            helpers.append(self)

        def adapter(
            self,
            *,
            budget,
            natural_checkpoint,
            max_capture_bytes,
            natural_phase="drain",
        ):
            budgets.append(budget)
            target = self.spec.target

            class Adapter:
                def capabilities(self, actual):
                    return (
                        frozenset({"gc", "heap_dump"})
                        if actual == target
                        else frozenset()
                    )

                def capture(self, actual, operation, output, timeout_s):
                    # Every role's natural evidence for this checkpoint must
                    # precede every capture taken from it.
                    declared = (
                        value.config.diagnostics.baseline_operations
                        if natural_phase == "baseline"
                        else value.config.diagnostics.operations
                    )
                    for role in declared:
                        checkpoint = json.loads(
                            (
                                value.evidence_dir
                                / f"natural-{natural_phase}-{role}.json"
                            ).read_text()
                        )
                        assert (
                            checkpoint["kind"] == "natural_checkpoint"
                            and checkpoint["completed"]
                        )
                        for record in checkpoint["artifacts"]:
                            artifact = value.evidence_dir / record["path"]
                            assert (
                                module._reference(value.evidence_dir, artifact)
                                == record
                            )
                    assert (
                        natural_checkpoint.name
                        == f"natural-{natural_phase}-{actual.role}.json"
                    )
                    budget.reserve(max_capture_bytes, dump=operation == "heap_dump")
                    events.append((actual.role, operation))
                    output.mkdir()
                    receipt = output / "diagnostic.json"
                    receipt.write_text(
                        '{"status":"INCONCLUSIVE","reason":"synthetic capture"}'
                    )
                    return receipt

            return Adapter()

        def close(self):
            self.closed += 1

        def cancel(self, **kwargs):
            events.append("cancel-target")

    class Provisioner:
        def __init__(self, **kwargs):
            pass

        def prepare(self, spec, *, timeout_s):
            specs.append(spec)
            return Helper(spec)

        def prepare_memory(self, *args, **kwargs):
            pytest.fail("diagnostic roles must reuse the diagnostic helper for PSS")

    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def wait_until(self, deadline, cancelled):
            self.now = deadline
            return not cancelled.is_set()

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def sample(self, target, phase, scheduled):
            requirements = {
                (name, (), "bytes")
                for name in value.config.roles[target.role].required_metrics
            }
            requirements.update(
                (c.metric, tuple(sorted(c.label_selector.items())), c.unit)
                for c in value.config.criteria
                if c.role == target.role and c.phase == "drain"
            )
            return tuple(
                Sample(
                    target,
                    phase,
                    scheduled,
                    scheduled,
                    scheduled,
                    metric,
                    labels,
                    unit,
                    7.0,
                    "observed",
                    "synthetic-probe",
                    None,
                )
                for metric, labels, unit in sorted(requirements)
            )

    monkeypatch.setattr(module, "LocalDockerDiagnosticProvisioner", Provisioner)
    monkeypatch.setattr(
        module,
        "_target_effective_ids",
        lambda target: (module.os.getuid(), module.os.getgid()),
    )

    def docker(path, *args):
        if path.startswith("/containers/"):
            controller = tmp_path / "node-diagnostic-control.cjs"
            return {
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(controller),
                        "Destination": module._NODE_CONTROLLER,
                        "RW": False,
                    }
                ]
            }
        return {"Architecture": "arm64"}

    monkeypatch.setattr(module, "_docker_get", docker)
    monkeypatch.setattr(module, "RoleBoundProbe", Probe)
    deployment = replace(
        deployment, discover=lambda: targets, observations=lambda targets: observations
    )
    lifecycle = module.create_soak_lifecycle(
        value,
        deployment=deployment,
        run_dir=tmp_path,
        clock=Clock(),
        allow_diagnostic_target_stop_on_cancel=True,
    )
    return module, value, lifecycle, observations, events, helpers, specs, budgets


def test_preparation_uses_effective_ids_caps_and_shared_helper_budget(
    tmp_path, monkeypatch
):
    module, value, lifecycle, _observed, events, helpers, specs, budgets = (
        diagnostic_environment(tmp_path, monkeypatch)
    )
    try:
        with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
            # Other required evidence is deliberately absent.
            lifecycle.hooks.preflight()
        saved = json.loads((value.evidence_dir / "preflight.json").read_text())
        assert len(specs) == 2 and len({id(budget) for budget in budgets}) == 1
        for spec in specs:
            decision = json.loads(
                (value.evidence_dir / "diagnostic-resource-inputs.json").read_text()
            )["roles"][spec.target.role]
            assert not spec.memory_only and spec.allow_target_stop_on_cancel
            assert (spec.uid, spec.gid) == (module.os.getuid(), module.os.getgid())
            assert spec.target_tmp_volume == decision["target_tmp_volume"]
            assert spec.quota_bytes == decision["quota_bytes"]
            assert spec.helper_memory_bytes > spec.quota_bytes
            actual = saved["observations"]["roles"][spec.target.role]
            assert (
                actual["gc_completion_evidence"]
                == value.config.diagnostics.gc_completion_evidence[spec.target.role]
            )
            assert actual["diagnostics"] == ["gc", "heap_dump"]
        assert events == []
    finally:
        lifecycle.stop_observer()
        value.writer.close()
    assert all(helper.closed == 1 for helper in helpers)
    assert "cancel-target" not in events


@pytest.mark.parametrize("memory", [None, 12345])
def test_missing_or_changed_effective_memory_blocks_diagnostic_prepare(
    tmp_path, monkeypatch, memory
):
    _, value, lifecycle, observed, events, _helpers, specs, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    for actual in observed["roles"].values():
        actual["memory_bytes"] = memory
    with pytest.raises(ValueError, match="diagnostic effective memory"):
        lifecycle.hooks.preflight()
    assert specs == [] and events == []
    lifecycle.stop_observer()
    value.writer.close()


def test_node_controller_drift_is_rejected_and_prepared_helpers_close(
    tmp_path, monkeypatch
):
    _, value, lifecycle, _, events, helpers, specs, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    path = tmp_path / "node-diagnostic-control.cjs"
    path.chmod(0o644)
    path.write_text("changed controller")
    with pytest.raises(ValueError, match=r"owned Node controller changed since"):
        lifecycle.hooks.preflight()
    assert all(helper.closed == 1 for helper in helpers)
    assert all(spec.target.runtime != "node" for spec in specs)
    assert events == []
    lifecycle.stop_observer()
    value.writer.close()


@pytest.mark.parametrize("state_kind", ["complete", "aborted", "partial", "identity"])
def test_capture_only_after_completed_owned_natural_drain(
    tmp_path, monkeypatch, state_kind
):
    _, value, lifecycle, _, events, helpers, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    # Direct hook test, not a synthetic claim that the public workflow passed.
    labels: list[str] = []
    lifecycle.observer.set_phase = lambda phase: labels.append(phase)
    start = lifecycle.clock.monotonic()
    if state_kind != "partial":
        lifecycle._natural("drain", value.config.phases.drain_s)
    lifecycle.state.windows["drain"] = (start, lifecycle.clock.monotonic())
    if state_kind == "aborted":
        lifecycle.state.primary_error = KeyboardInterrupt("aborted after drain")
    if state_kind == "identity":
        lifecycle.cancelled.set()
    if state_kind == "complete":
        lifecycle.hooks.final_capture(lifecycle.state, 30)
        # The capture relabels the observer, so the ticks it perturbs are not
        # written with the drain label past the drain window's end.
        assert labels == ["drain", "diagnostic"]
        assert len(events) == 4
        index = json.loads((value.evidence_dir / "diagnostics.json").read_text())
        assert len(index["entries"]) == 4
    else:
        with pytest.raises(RuntimeError, match=r"completed owned natural final"):
            lifecycle.hooks.final_capture(lifecycle.state, 30)
        # The guard refuses before the first perturbing read, so a capture that
        # never runs never claims to have perturbed anything.
        assert "diagnostic" not in labels
        assert events == []
        assert not list(value.evidence_dir.glob("natural-*.json"))
    lifecycle.stop_observer()
    assert all(helper.closed == 1 for helper in helpers)
    value.writer.close()


def test_a_capture_survives_an_observer_that_is_no_longer_running(
    tmp_path, monkeypatch
):
    """A dead observer must not cost the run its diagnostics.

    The label only changes how the ticks taken during a capture are judged; it
    decides nothing about whether the capture happens. `Observer.set_phase`
    raises when the sampler is not running, so the label is best-effort.
    """
    _, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()

    def set_phase(phase):
        if phase == "diagnostic":
            raise RuntimeError("observer is not running")

    lifecycle.observer.set_phase = set_phase
    start = lifecycle.clock.monotonic()
    lifecycle._natural("drain", value.config.phases.drain_s)
    lifecycle.state.windows["drain"] = (start, lifecycle.clock.monotonic())

    lifecycle.hooks.final_capture(lifecycle.state, 30)

    assert len(events) == 4
    index = json.loads((value.evidence_dir / "diagnostics.json").read_text())
    assert len(index["entries"]) == 4
    lifecycle.stop_observer()
    value.writer.close()


def test_baseline_capture_takes_its_readings_at_its_own_checkpoint(
    tmp_path, monkeypatch
):
    """The baseline checkpoint is a second one, not a rename of the final one.

    It freezes its own natural window, publishes a receipt carrying that phase,
    and captures what the baseline map declares -- and it does so before the
    measured phases it is the reference for, because a difference measured from a
    checkpoint taken afterwards is not a difference at all.
    """
    _, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch, baseline={"control-plane": ["gc"]}
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    labels: list[str] = []
    lifecycle.observer.set_phase = lambda phase: labels.append(phase)
    start = lifecycle.clock.monotonic()
    lifecycle._natural("baseline", value.config.phases.baseline_window_s)
    lifecycle.state.windows["baseline"] = (start, lifecycle.clock.monotonic())

    lifecycle.hooks.baseline_capture(lifecycle.state, 30)

    # Its perturbation is labelled, and only its declared reading ran.
    assert labels == ["baseline", "diagnostic"]
    assert events == [("control-plane", "gc")]
    checkpoint = json.loads(
        (value.evidence_dir / "natural-baseline-control-plane.json").read_text()
    )
    assert checkpoint["phase"] == "baseline"
    assert checkpoint["kind"] == "natural_checkpoint"
    assert checkpoint["completed"] is True
    assert not (value.evidence_dir / "natural-drain-control-plane.json").exists()
    lifecycle.stop_observer()
    value.writer.close()


def test_a_baseline_capture_without_a_completed_window_is_refused(
    tmp_path, monkeypatch
):
    """A checkpoint that never closed is no reference to read a difference from."""
    _, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch, baseline={"control-plane": ["gc"]}
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    lifecycle.observer.set_phase = lambda phase: None

    with pytest.raises(RuntimeError, match=r"natural baseline checkpoint"):
        lifecycle.hooks.baseline_capture(lifecycle.state, 30)

    assert events == []
    lifecycle.stop_observer()
    value.writer.close()


def test_a_run_that_declares_nothing_has_no_baseline_capture(tmp_path, monkeypatch):
    """The step is unconditional, so phase keys do not depend on the policy.

    It reads and writes nothing when no checkpoint declares a baseline reading,
    so the step costs a run without diagnostics exactly one recorded window.
    """
    _, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    labels: list[str] = []
    lifecycle.observer.set_phase = lambda phase: labels.append(phase)

    lifecycle.hooks.baseline_capture(lifecycle.state, 30)

    assert labels == [] and events == []
    assert not list(value.evidence_dir.glob("natural-baseline-*.json"))
    lifecycle.stop_observer()
    value.writer.close()


def test_the_provider_gate_routes_what_the_helper_dispatches(tmp_path, monkeypatch):
    """A fifth copy of the operation set lived here, and read one map only.

    It named `gc` and `heap_dump` itself, so it rejected the text readings the
    helper had just been taught to dispatch -- and it read only the final
    checkpoint, so a baseline reading would have reached preparation and failed
    there, before any measurement, with a message naming the provider rather than
    the operation.
    """
    module, value, _, _, _, _, _, _ = diagnostic_environment(tmp_path, monkeypatch)
    jvm = next(
        name for name, policy in value.config.roles.items() if policy.runtime == "jvm"
    )
    options = module.RuntimeOptions(allow_diagnostic_target_stop_on_cancel=True)

    def declared(operations, baseline):
        diagnostics = value.config.diagnostics.model_copy(
            update={"operations": operations, "baseline_operations": baseline}
        )
        return value.config.model_copy(update={"diagnostics": diagnostics})

    routed = module._runtime_preparation_options(
        declared({jvm: ["gc", "native_memory"]}, {jvm: ["native_memory_baseline"]}),
        options,
    )
    assert routed.diagnostic_provider_available is True

    # `jfr` is in the vocabulary and not provisionable: the gate must tell the
    # two apart rather than accept any name the type system allows.
    with pytest.raises(ValueError, match="cannot route requested operations"):
        module._runtime_preparation_options(
            declared({jvm: ["jfr"]}, {}),
            options,
        )


@pytest.mark.parametrize(
    "scenario",
    [
        "memory-soak-p24-nmt-spike-container.yaml",
        "memory-soak-p24-serialgc-shrink-spike-container.yaml",
    ],
)
def test_the_shipped_nmt_spikes_provision_and_reserve(tmp_path, monkeypatch, scenario):
    """The scenarios this repository ships must cross the gates they declare.

    Both set `-XX:NativeMemoryTracking` and ask for the reading pair, so both are
    a run that only works if the provider routes the text readings, if the budget
    sizes four captures rather than three, and if the baseline map is admitted
    everywhere the final one is.
    """
    import nanolab.tasks.soak.runtime as module

    value = prepared(tmp_path, scenario=scenario)
    policy = value.config.diagnostics
    assert policy.baseline_operations == {"control-plane": ["native_memory_baseline"]}
    assert policy.operations["control-plane"] == [
        "native_memory_diff",
        "native_memory",
        "gc",
    ]

    # Stamp the helper digest the way a run does, rather than hand-filling it:
    # which roles get one is itself a gate that reads the declared maps.
    built = "localhost:5000/nanolab/diagnostic-helper@sha256:" + "c" * 64
    monkeypatch.setattr(
        "nanolab.tasks.soak.helper_build.build_helper_image", lambda request: built
    )
    config = module._with_built_helper(
        value.config, run_dir=tmp_path / "run", options=module.RuntimeOptions()
    )
    assert set(config.diagnostics.helper_images) == {"control-plane"}

    inputs = module._diagnostic_resource_inputs(
        replace(value, config=config), allow_target_stop=True
    )

    assert inputs["operation_count"] == 4
    assert inputs["dump_count"] == 0
    quota = inputs["roles"]["control-plane"]["quota_bytes"]
    assert quota % 4096 == 0 and 0 < quota <= 1024**3
    assert quota * 4 <= inputs["available_artifact_bytes"]
    module._runtime_preparation_options(
        config,
        module.RuntimeOptions(allow_diagnostic_target_stop_on_cancel=True),
    )
    value.writer.close()


def test_prerequisite_body_budget_is_independent_of_diagnostic_capture(tmp_path):
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.prerequisite_runtime import required_body_budget

    value = prepared(tmp_path, scenario="memory-soak-p24-nmt-spike-container.yaml")
    value.config.diagnostics.timeout_s = 1
    _, frozen, supervision = module._make_runtime_prerequisites(
        value, module.RuntimeOptions(), host_bindings(), tmp_path
    )
    assert supervision["body_timeout_s"] == required_body_budget(frozen)
    assert supervision["body_timeout_s"] > 35
    assert value.config.diagnostics.timeout_s == 1
    value.writer.close()


def test_the_first_prerequisite_reservation_precedes_its_ownership_root(tmp_path):
    """The reservation hook runs before the factory creates what it owns.

    Both owned subtrees are charged at their full reservation rather than their
    actual use, so the hook subtracts them from the run total. Before the first
    lifetime is acquired neither may exist: the factory creates its ownership
    root inside `__call__`, which is the acquisition the hook precedes. Measuring
    an absent subtree is what made every soak run fail at prerequisites.
    """
    import nanolab.tasks.soak.runtime as module

    value = prepared(tmp_path, scenario="memory-soak-p24-nmt-spike-container.yaml")
    options = module.RuntimeOptions(
        prerequisite_inputs=module._freeze_prerequisite_inputs(value)
    )
    _, frozen, supervision = module._make_runtime_prerequisites(
        value, options, host_bindings(), tmp_path
    )

    # The acceptance gate compares each declared projection against the live
    # policy. Spell both sides here exactly as they are spelled there, on the
    # profile the production freeze just built for a shipped scenario.
    normalized = value.config.model_dump(mode="json")
    declared = value.config.prerequisites.relevant_config_keys["sync"]
    assert declared == ["images", "roles", "retention_s", "workload"]
    for key in declared:
        assert frozen["relevant_config"]["sync"][key] == select_relevant_config(
            normalized, key
        )

    owned = value.evidence_dir / "prerequisite-platforms"
    parent = value.evidence_dir / "prerequisites"
    assert not owned.exists()

    supervision["before_fork"](
        "71374cc5bfd048038e9a2f5ee50b9ee4",
        ArtifactWriter(parent, supervision["parent_artifact_bytes"]),
    )

    reservation = json.loads(
        (parent / "reservation-71374cc5bfd048038e9a2f5ee50b9ee4.json").read_text()
    )
    assert reservation["schema"] == "nanolab-soak-prerequisite-reservation-v1"
    assert reservation["other_run_artifact_bytes"] >= 0
    assert not owned.exists()
    value.writer.close()


@pytest.mark.parametrize("kind", ["interrupt", "event"])
def test_capture_abort_cancels_exact_remote_helper(kind):
    from threading import Event

    from nanolab.tasks.soak.runtime import _capture_owned_diagnostic

    cancelled, remote_stopped = Event(), Event()
    calls = []

    def cancel(**kwargs):
        calls.append("cancel")
        remote_stopped.set()

    def capture(*args):
        if kind == "interrupt":
            raise KeyboardInterrupt("capture interrupted")
        cancelled.set()
        assert remote_stopped.wait(2), "remote writer was not cancelled during capture"
        return Path("synthetic-receipt.json")

    with pytest.raises(
        KeyboardInterrupt,
        match=r"capture interrupted|soak cancelled during diagnostic",
    ):
        _capture_owned_diagnostic(
            SimpleNamespace(capture=capture),
            SimpleNamespace(cancel=cancel),
            memory_target("jvm"),
            "gc",
            Path("unused"),
            5,
            cancelled,
        )
    assert calls == ["cancel"]


def test_public_builder_explicitly_authorizes_only_default_owned_cleanup(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.runtime as module
    from nanolab.plans.soak import build_soak_plan

    tasks = []
    original = module.RunSingleVersionSoak

    def record(*args, **kwargs):
        task = original(*args, **kwargs)
        tasks.append(task)
        return task

    monkeypatch.setattr(module, "RunSingleVersionSoak", record)
    for options in (None, module.RuntimeOptions()):
        build_soak_plan(
            cast(ScenarioConfig, SimpleNamespace(workflow="soak", soak=object())),
            cast(EnvironmentConfig, SimpleNamespace(provider="local")),
            host_bindings(),
            run_dir=tmp_path,
            repo_root=tmp_path,
            tool_root=tmp_path,
            runtime_options=options,
        )
    assert tasks[0].options.allow_diagnostic_target_stop_on_cancel is True
    assert tasks[1].options.allow_diagnostic_target_stop_on_cancel is False


def test_unprepared_public_runtime_declares_provider_before_support_gate(
    tmp_path, monkeypatch
):
    import sys

    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.preparation import (
        PreparationOptions,
        check_preparation_support,
    )

    value = diagnostic_prepared(tmp_path)
    preparation = PreparationOptions(
        generator_command=(sys.executable,),
        support_check=lambda config: None,
    )
    options = module.RuntimeOptions(
        preparation=preparation,
        allow_diagnostic_target_stop_on_cancel=True,
    )
    seen = []

    def prepare(config, **kwargs):
        declared = kwargs["options"]
        assert declared.diagnostic_adapter is None
        assert declared.diagnostic_provider_available is True
        check_preparation_support(config, declared)
        seen.append("support passed")
        raise RuntimeError("stop before source/build side effects")

    monkeypatch.setattr(module, "prepare_soak", prepare)
    task = module.RunSingleVersionSoak(
        SimpleNamespace(soak=value.config),
        host_bindings(),
        run_dir=tmp_path / "public-run",
        repo_root=tmp_path,
        tool_root=tmp_path,
        options=options,
    )
    assert seen == [] and options.prepared is None
    with pytest.raises(RuntimeError, match="stop before source"):
        task.run(TaskInputs.empty())
    assert seen == ["support passed"]
    assert preparation.diagnostic_provider_available is False
    value.writer.close()


@pytest.mark.parametrize("missing", ["permission", "image", "preload"])
def test_automatic_provider_declaration_rejects_missing_wiring(tmp_path, missing):
    import nanolab.tasks.soak.runtime as module

    value = diagnostic_prepared(tmp_path)
    if missing == "image":
        value.config.diagnostics.helper_images.clear()
    if missing == "preload":
        for policy in value.config.roles.values():
            if policy.runtime == "node":
                policy.runtime_options.remove(module._NODE_PRELOAD)
    options = module.RuntimeOptions(
        allow_diagnostic_target_stop_on_cancel=missing != "permission"
    )
    with pytest.raises(ValueError, match=r"permission|helper_images|preload"):
        module._runtime_preparation_options(value.config, options)
    assert options.preparation.diagnostic_adapter is None
    assert not options.preparation.diagnostic_provider_available
    value.writer.close()


@pytest.mark.parametrize(
    "invalid",
    ["unavailable", "nan", "inf", "bool", "selector", "unit", "identity", "absent"],
)
def test_natural_checkpoint_requires_finite_available_matching_selectors(invalid):
    import nanolab.tasks.soak.runtime as module
    from nanolab.tasks.soak.models import Sample

    target = memory_target("jvm")
    config = SimpleNamespace(
        roles={target.role: SimpleNamespace(required_metrics=["memory"])},
        criteria=[
            SimpleNamespace(
                role=target.role,
                phase="drain",
                metric="memory",
                label_selector={"pool": "old"},
                unit="bytes",
            )
        ],
    )
    row = Sample(
        target,
        "drain",
        1.0,
        1.0,
        1.0,
        "memory",
        (("pool", "old"),),
        "bytes",
        7.0,
        "observed",
        "test",
        None,
    )
    changes = {
        "unavailable": {"availability": "unavailable", "value": None},
        "nan": {"value": float("nan")},
        "inf": {"value": float("inf")},
        "bool": {"value": True},
        "selector": {"labels": (("pool", "young"),)},
        "unit": {"unit": "unknown"},
        "identity": {"target": replace(target, process_id=456)},
        "absent": {"metric": "different"},
    }
    module._validate_natural_samples(config, target, (row,), "drain")
    with pytest.raises(
        RuntimeError,
        match=r"natural checkpoint observation|owned natural checkpoint",
    ):
        module._validate_natural_samples(
            config, target, (replace(row, **changes[invalid]),), "drain"
        )


def complete_direct_drain(value, lifecycle):
    lifecycle.observer.set_phase = lambda phase: None
    start = lifecycle.clock.monotonic()
    lifecycle._natural("drain", value.config.phases.drain_s)
    lifecycle.state.windows["drain"] = (start, lifecycle.clock.monotonic())


def test_unavailable_checkpoint_blocks_all_markers_and_capture(tmp_path, monkeypatch):
    module, value, lifecycle, _, events, helpers, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    original = module.RoleBoundProbe

    class Missing(original):
        def sample(self, target, phase, scheduled):
            return tuple(
                replace(row, availability="unavailable", value=None)
                for row in super().sample(target, phase, scheduled)
            )

    monkeypatch.setattr(module, "RoleBoundProbe", Missing)
    complete_direct_drain(value, lifecycle)
    with pytest.raises(RuntimeError, match=r"natural checkpoint observation"):
        lifecycle.hooks.final_capture(lifecycle.state, 30)
    assert events == [] and not list(value.evidence_dir.glob("natural-*.json"))
    lifecycle.stop_observer()
    assert all(helper.closed == 1 for helper in helpers)
    value.writer.close()


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_checkpoint_loop_shares_remaining_deadline_and_checks_cancellation(
    tmp_path, monkeypatch, stop
):
    module, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    original, timeouts, now = module.RoleBoundProbe, [], [1000.0]

    class Timed(original):
        def __init__(self, *args, timeout_s, **kwargs):
            timeouts.append(timeout_s)
            super().__init__(*args, timeout_s=timeout_s, **kwargs)

        def sample(self, target, phase, scheduled):
            rows = super().sample(target, phase, scheduled)
            now[0] += 0.02
            if stop == "cancel":
                lifecycle.cancelled.set()
            return rows

    monkeypatch.setattr(module, "RoleBoundProbe", Timed)
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    complete_direct_drain(value, lifecycle)
    error = TimeoutError if stop == "deadline" else KeyboardInterrupt
    with pytest.raises(error, match=r"deadline|cancelled"):
        lifecycle.hooks.final_capture(lifecycle.state, 0.03)
    assert timeouts[0] == pytest.approx(0.03)
    if stop == "deadline":
        assert timeouts[1] == pytest.approx(0.01)
    else:
        assert len(timeouts) == 1
    assert events == [] and not list(value.evidence_dir.glob("natural-*.json"))
    lifecycle.stop_observer()
    value.writer.close()


def test_checkpoint_remainder_reaches_kill_and_wait_subprocess_transport(
    tmp_path, monkeypatch
):
    import sys

    import nanolab.tasks.soak.adapters as adapters

    module, value, lifecycle, _, events, _, _, _ = diagnostic_environment(
        tmp_path, monkeypatch
    )
    with pytest.raises(RuntimeError, match="effective preflight INCONCLUSIVE"):
        lifecycle.hooks.preflight()
    run, timeouts = adapters.subprocess.run, []

    def bounded_child(argv, **kwargs):
        assert argv[1:3] == ["-m", "nanolab.tasks.soak.collector"]
        timeouts.append(kwargs["timeout"])
        assert 0 < kwargs["timeout"] <= 0.05
        # Exercise real subprocess.run kill-and-wait, but NEVER run the collector
        # or access Docker. This child has no descendants or external side effects.
        return run([sys.executable, "-c", "import time; time.sleep(2)"], **kwargs)

    monkeypatch.setattr(adapters.subprocess, "run", bounded_child)
    monkeypatch.setattr(module, "RoleBoundProbe", adapters.RoleBoundProbe)
    complete_direct_drain(value, lifecycle)
    with pytest.raises((RuntimeError, TimeoutError), match=r"unavailable|deadline"):
        lifecycle.hooks.final_capture(lifecycle.state, 0.05)
    assert len(timeouts) == 1
    assert events == [] and not list(value.evidence_dir.glob("natural-*.json"))
    lifecycle.stop_observer()
    value.writer.close()


def test_external_manifest_uses_the_field_name_the_control_plane_accepts():
    """FunctionSpec is a Jackson record: an unknown property is a 400, not a no-op.

    The field is endpointUrl. The dispatcher POSTs to it verbatim, so the
    SDK's own /invoke route stays part of the value.
    """
    from nanolab.tasks.soak.runtime import _ExternalFunction

    body = (
        _ExternalFunction(
            name="word-stats-java",
            image="localhost:5000/x@sha256:" + "0" * 64,
            payload=cast(str, None),
            build_argv=cast("tuple[str, ...]", None),
            endpoint="http://function-1:8080/invoke",
        )
        .manifest()
        .body()
    )

    assert body["endpointUrl"] == "http://function-1:8080/invoke"
    assert "endpoint" not in body
    assert body["executionMode"] == "EXTERNAL"


def test_generator_inspection_names_its_config_on_the_command_line(tmp_path):
    """k6 inspect does not read the process environment into __ENV.

    Only `k6 run` does, so relying on the env for both silently broke the
    inspection with an empty open() filename.
    """
    import subprocess

    script = tmp_path / "probe.js"
    script.write_text(
        "const c = JSON.parse(open(__ENV.NANOLAB_SOAK_CONFIG));\n"
        "export const options = {scenarios: c.scenarios};\n"
        "export function invoke() {}\n"
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "scenarios": {
                    "s": {
                        "executor": "constant-arrival-rate",
                        "rate": 1,
                        "timeUnit": "1s",
                        "duration": "1s",
                        "preAllocatedVUs": 1,
                        "maxVUs": 2,
                        "exec": "invoke",
                    }
                }
            }
        )
    )

    def inspect(*extra):
        return subprocess.run(
            ("k6", "inspect", "--execution-requirements", *extra, str(script)),
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": os.environ["PATH"],
                "NANOLAB_SOAK_CONFIG": str(config),
                "K6_NO_USAGE_REPORT": "true",
            },
        )

    if shutil.which("k6") is None:
        pytest.skip("k6 is not available")
    # The environment alone is what the runtime used to rely on.
    assert inspect().returncode != 0
    named = inspect(f"--env=NANOLAB_SOAK_CONFIG={config}")
    assert named.returncode == 0, named.stderr
    assert json.loads(named.stdout)["maxVUs"] == 2


def test_artifact_inventory_defers_the_source_tree_to_its_own_manifest(tmp_path):
    """One JSON record cannot hold thousands of source files, and need not.

    snapshot.json seals source-manifest.jsonl with manifest_sha256, and both
    stay in the inventory, so every source file is still hash-bound.
    """
    from nanolab.tasks.soak.artifacts import MAX_RECORD_BYTES

    root = tmp_path / "evidence"
    (root / "source" / "tree" / "platform").mkdir(parents=True)
    for index in range(5000):
        (root / "source" / "tree" / "platform" / f"f{index}.java").write_bytes(b"x")
    (root / "source" / "snapshot.json").write_text('{"manifest_sha256": "a"}')
    (root / "source" / "source-manifest.jsonl").write_text("{}\n")
    (root / "samples.jsonl").write_text("{}\n")

    listed = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(root / "source" / "tree")
    )

    assert listed == [
        "samples.jsonl",
        "source/snapshot.json",
        "source/source-manifest.jsonl",
    ]
    assert len(json.dumps(listed).encode()) < MAX_RECORD_BYTES
