"""The containerd soak runner preserves the shared terminal contract."""

import json
import stat
import subprocess
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from sonata_engine import TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandTaskSpec
from sonata_tasks.tasks.models import TaskResult

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.containerd_runtime import (
    BuildExecutionRecorder,
    ContainerdSoakRun,
    finalize_containerd_terminal,
)
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.sources import SourceEntry


def _scenario():
    path = Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    return ScenarioConfig.model_validate(yaml.safe_load(path.read_text()))


def test_build_receipt_uses_executed_task_command_and_result():
    class Executor:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            return TaskResult(task.task_id, "passed", 0)

    recorder = BuildExecutionRecorder(Executor())
    actual = ("./gradlew", ":control-plane:bootJar", "-PcontainerdMavenLocal=true")
    recorder.run(CommandTaskSpec("cp", "Build control plane", actual, "stack"))
    observed = recorder.require_build("Build control plane")
    assert observed.argv == actual
    assert observed.status == "passed" and observed.return_code == 0
    with pytest.raises(ValueError, match="missing or repeated"):
        recorder.require_build("Build image word-stats-java")


@pytest.mark.parametrize(
    ("measured", "cleanup_error", "expected"),
    [
        ("PASS", None, "PASS"),
        ("FAIL", RuntimeError("cleanup failed"), "FAIL"),
        ("PASS", RuntimeError("cleanup failed"), "INCONCLUSIVE"),
    ],
)
def test_terminal_is_sealed_after_cleanup_with_evaluated_verdict(
    tmp_path, measured, cleanup_error, expected
):
    run = tmp_path / "run"
    run.mkdir()
    (run / "measurement-verdict.json").write_text(
        json.dumps({"schema": "nanolab-containerd-measurement-v1", "status": measured})
    )
    finalize_containerd_terminal(run, cleanup_error)
    terminal = json.loads((run / "terminal.json").read_text())
    assert terminal["status"] == expected
    if cleanup_error:
        assert "cleanup failed" in terminal["reason"]


def test_corrupt_measurement_verdict_cannot_publish_pass(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "measurement-verdict.json").write_text("{")
    finalize_containerd_terminal(run, None)
    assert json.loads((run / "terminal.json").read_text())["status"] == "INCONCLUSIVE"


def test_failed_owned_target_inspection_records_inconclusive_terminal(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.containerd_runtime as module

    class MissingTarget:
        def __init__(self, run, executor):
            pass

        def inspect(self, role):
            raise OSError("owned task absent")

    monkeypatch.setattr(module, "RootlessCollectionTransport", MissingTarget)
    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun("run123", tmp_path / "repo", tmp_path / "session.sh"),
        EnvironmentConfig.model_validate({"provider": "local"}),
        Mock(spec=CommandTaskExecutor),
        run_dir=tmp_path / "run",
        repo_root=tmp_path / "repo",
    )
    with pytest.raises(OSError, match="owned task absent"):
        task.run(TaskInputs({}, frozenset()))
    assert not (tmp_path / "run/terminal.json").exists()
    finalize_containerd_terminal(tmp_path / "run", OSError("owned task absent"))
    terminal = json.loads((tmp_path / "run/terminal.json").read_text())
    assert terminal["status"] == "INCONCLUSIVE"


def test_runner_binds_common_lifecycle_to_real_cgroup_observations(
    tmp_path, monkeypatch
):
    import nanolab.tasks.soak.containerd_runtime as module

    policy = _scenario().soak
    assert policy is not None
    targets = {
        role: Target(
            role,
            f"owned-{role}",
            100 + index,
            "12345",
            "sha256:" + "a" * 64
            if role == "control-plane"
            else "registry/fn@sha256:" + "b" * 64,
            spec.runtime,
        )
        for index, (role, spec) in enumerate(policy.roles.items())
    }

    class ObservedTransport:
        def __init__(self, run, executor):
            assert run.run_id == "run123"

        def inspect(self, role):
            return targets[role], {
                "artifact_path": "/repo/app.jar" if role == "control-plane" else None,
                "platform": policy.images[role].platform,
            }

        def collect(self, target, endpoint, timeout_s):
            assert endpoint is None
            return {
                "configuration": {
                    "cpu_max": [200000, 100000],
                    "cpuset": "0-1",
                    "memory_bytes": policy.roles[target.role].memory_limit_bytes,
                    "limit_sources": {
                        "cpu": "cgroup-v2/cpu.max",
                        "memory_bytes": "cgroup-v2/memory.max",
                    },
                    "runtime": target.runtime,
                    "runtime_options": [],
                    "capabilities": ["procfs", "cgroup-v2"],
                    "collection_sources": ["procfs", "cgroup-v2"],
                }
            }

    evidence = tmp_path / "run/evidence"
    writer = ArtifactWriter(evidence, 1024 * 1024)
    writer.write_json("remote-source.json", {"revision": "feature-commit"})
    monkeypatch.setattr(module, "RootlessCollectionTransport", ObservedTransport)
    monkeypatch.setattr(
        module,
        "resolve_loadtest_urls",
        lambda *a, **k: ("http://127.0.0.1:8080", "http://127.0.0.1:9090"),
    )
    monkeypatch.setattr(
        module,
        "observe_local_configuration",
        lambda *a, **k: {
            "modules": policy.images["control-plane"].modules,
            "retention_s": policy.retention_s,
        },
    )

    def lifecycle(prepared, *, deployment, transport, **kwargs):
        assert isinstance(transport, ObservedTransport)
        assert set(deployment.discover()) == set(targets.values())
        observed = deployment.observations(tuple(targets.values()))
        assert observed["roles"]["control-plane"]["cpu"] == 2
        assert observed["roles"]["control-plane"]["artifact_path"] == "/repo/app.jar"
        assert observed["roles"]["control-plane"]["platform"] == "linux/arm64"
        assert observed["remote_source"]["revision"] == "feature-commit"
        assert deployment.api_endpoint == "http://127.0.0.1:8080"

        class Lifecycle:
            state = SimpleNamespace(report=None, evaluation={"status": "PASS"})

            def run(self, inputs):
                return TaskOutcome(value="shared-workload-complete")

        return Lifecycle()

    monkeypatch.setattr(module, "create_soak_lifecycle", lifecycle)
    monkeypatch.setattr(
        module.ContainerdSoakRun,
        "_prepare",
        lambda *a: SimpleNamespace(
            evidence_dir=evidence,
            writer=writer,
        ),
    )
    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun("run123", tmp_path / "repo", tmp_path / "session.sh"),
        EnvironmentConfig.model_validate({"provider": "local"}),
        Mock(spec=CommandTaskExecutor),
        run_dir=tmp_path / "run",
        repo_root=tmp_path / "repo",
    )
    assert task.run(TaskInputs({}, frozenset())).value == "shared-workload-complete"
    assert not (tmp_path / "run/terminal.json").exists()
    finalize_containerd_terminal(tmp_path / "run", None)
    assert json.loads((tmp_path / "run/terminal.json").read_text())["status"] == "PASS"


def test_remote_source_verification_uses_staged_content_without_git(tmp_path):
    staged = tmp_path / "nanofaas"
    staged.mkdir()
    source = staged / "feature.txt"
    source.write_text("feature commit contents")
    entry = SourceEntry(
        path="feature.txt",
        kind="file",
        mode=stat.S_IMODE(source.stat().st_mode),
        size_bytes=source.stat().st_size,
        sha256=sha256(source.read_bytes()).hexdigest(),
        link_target=None,
    )
    commands = []

    class Executor:
        def binding_key(self, role: str) -> str:
            return f"test:{role}"

        def run(self, task, *, dry_run=False):
            command = task
            commands.append(command.argv)
            process = subprocess.run(
                command.argv, text=True, capture_output=True, check=False
            )
            return TaskResult(
                command.task_id,
                "passed" if process.returncode == 0 else "failed",
                process.returncode,
                stdout=process.stdout,
                stderr=process.stderr,
            )

    task = ContainerdSoakRun(
        _scenario(),
        RootlessRun(
            "run123",
            staged,
            Path(__file__).parents[2] / "assets/containerd-rootless/session.sh",
        ),
        EnvironmentConfig.model_validate({"provider": "local"}),
        Executor(),
        run_dir=tmp_path / "run",
        repo_root=staged,
    )
    snapshot = SimpleNamespace(
        entries=(entry,), revision="commit123", fingerprint="source-hash"
    )
    result = task._verify_remote_source(snapshot)
    assert result["entry_count"] == 1
    assert result["revision"] == "commit123"
    assert all("git" not in argv for argv in commands)
    source.write_text("changed")
    with pytest.raises(RuntimeError, match="remote source verification failed"):
        task._verify_remote_source(snapshot)
