# ruff: noqa: F401 - imports are fixtures re-exported to split test modules
from __future__ import annotations

from pathlib import Path
from dataclasses import asdict, replace
import hashlib
import json
import os
import shlex
import subprocess
import tarfile
from types import SimpleNamespace

import pytest
import yaml

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.images.plan import DEFAULT_REGISTRY, build_image_plan
from nanolab.release import arm
from nanolab.release import build as release_build
from nanolab.release.metrics import build_release_record
from nanolab.release.model import (
    Amd64ReleasePlan,
    BuilderConfiguration,
    GitState,
    ReleaseIdentity,
    ReleaseSettings,
    digest_path,
)
from nanolab.release.versioning import read_project_version


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)
CURRENT_TAG = f"v{CURRENT_VERSION}"
GUARDED_COMMIT = "a" * 40


def _environment(tmp_path: Path) -> Path:
    source = NANOLAB_ROOT / "environments/azure-release.yaml.example"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["azure"]["operator_source_cidr"] = "8.8.8.8/32"
    destination = tmp_path / "azure-release.yaml"
    destination.write_text(yaml.safe_dump(data), encoding="utf-8")
    return destination


def _settings() -> ReleaseSettings:
    """The shipped release policy, read rather than restated.

    `test_metrics.test_release_configuration_owns_the_versioned_policy` pins the
    file itself, so reading it here keeps the fixture from drifting from it.
    """
    config = yaml.safe_load((NANOLAB_ROOT / "release.yaml").read_text(encoding="utf-8"))
    benchmark = config["benchmark"]
    regression = benchmark["regression"]
    return ReleaseSettings(
        max_parallelism=int(config["build"]["maxParallelism"]),
        scenario=NANOLAB_ROOT / str(benchmark["scenario"]),
        scenario_name=str(benchmark["scenario"]),
        benchmark_runs=int(benchmark["runs"]),
        profile=str(benchmark["profile"]),
        throughput_max_loss_percent=float(regression["throughputMaxLossPercent"]),
        p95_max_increase_percent=float(regression["p95MaxIncreasePercent"]),
        error_rate_max=float(regression["errorRateMax"]),
    )


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Amd64ReleasePlan:
    """An `Amd64ReleasePlan` shaped like the one the Sonata DAG builds.

    The guarded commit is faked so the phases that re-check it work against a
    checkout the test never has to keep clean.
    """
    monkeypatch.setattr(
        release_build,
        "git_state",
        lambda _root: GitState(commit=GUARDED_COMMIT, clean=True),
    )
    environment_file = _environment(tmp_path)
    settings = _settings()
    run_dir = (tmp_path / "run").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    return Amd64ReleasePlan(
        repo_root=NANOFAAS_ROOT,
        run_dir=run_dir,
        version=CURRENT_VERSION,
        identity=ReleaseIdentity(
            source_commit=GUARDED_COMMIT,
            prepared_version=CURRENT_VERSION,
            release_config_digest=digest_path(NANOLAB_ROOT / "release.yaml"),
            environment_digest=digest_path(environment_file),
        ),
        environment=EnvironmentConfig.model_validate(
            yaml.safe_load(environment_file.read_text(encoding="utf-8"))
        ),
        scenario=ScenarioConfig.model_validate(
            yaml.safe_load(settings.scenario.read_text(encoding="utf-8"))
        ),
        settings=settings,
        image_plan=build_image_plan(
            NANOFAAS_ROOT,
            CURRENT_TAG,
            registry=DEFAULT_REGISTRY,
            architectures=("amd64",),
        ),
        builder=BuilderConfiguration(
            name=f"nanofaas-release-{CURRENT_TAG.replace('.', '-')}",
            max_parallelism=settings.max_parallelism,
        ),
        bake_file=run_dir / "docker-bake.json",
        buildkit_config=run_dir / "buildkitd.toml",
        # finalization must never write into the real repository docs in tests
        performance_root=tmp_path / "performance-docs",
        credentials=None,
    )


class _TransferResult:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        return_code: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code


class _ArchiveProvider:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.actions: list[tuple[object, ...]] = []

    def transfer_to(self, request: object, *, source: Path, destination: str) -> _TransferResult:
        self.actions.append(("transfer", request, source, destination))
        return _TransferResult()

    def exec_argv(
        self,
        request: object,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None,
        cwd: str | None,
        dry_run: bool,
    ) -> _TransferResult:
        self.actions.append(("exec", request, argv, env, cwd, dry_run))
        if argv[0] == "sha256sum":
            return _TransferResult(stdout=f"{self.digest.removeprefix('sha256:')}  {argv[1]}\n")
        return _TransferResult()


def _summary(value: float) -> dict[str, object]:
    return {
        "k6": {
            "http_reqs": {"values": {"rate": value}},
            "http_req_failed": {"values": {"rate": 0.0}},
            "http_req_duration": {
                "values": {"p(50)": value + 1, "p(95)": value + 2, "p(99)": value + 3}
            },
        },
        "prometheus": {
            "function_queue_wait_count": {"delta": 10},
            "function_queue_wait_sum": {"delta": value * 10},
            "function_cold_start_total": {"delta": value + 4},
            "process_cpu_usage": {"max": value / 100},
            "jvm_heap_used_bytes": {"max": value * 1024},
        },
        "autoscaling": {"max_replicas_observed": 5, "final_desired_replicas": 0},
    }


def _registry_digest(reference: str) -> str:
    return "sha256:" + hashlib.sha256(f"registry:{reference}".encode()).hexdigest()


def _unwrap_bounded(argv: tuple[str, ...]) -> tuple[str, ...]:
    # _provider_exec(bounded=True) wraps bulk commands in a bounded-output
    # shell; recover the original argv so dispatch and event strings stay
    # stable.
    if len(argv) == 3 and argv[:2] == ("sh", "-c") and "/tmp/release-cmd.log" in argv[2]:
        inner = argv[2].split("{ ", 1)[1].rsplit(" ; }", 1)[0]
        return tuple(shlex.split(inner))
    return argv


class _ReleaseProvider(_ArchiveProvider):
    def __init__(self, events: list[str]) -> None:
        super().__init__("sha256:" + "0" * 64)
        self.events = events
        self.local_digests: dict[str, str] = {}
        self.remote_source_mutated = False
        self.remote_digests: dict[str, str] = {}
        self.registry_digests: dict[str, str] = {}

    def _resolve_registry(self, reference: str) -> str | None:
        """Resolve a tag or a `repository@sha256:` pin, like a real registry does."""
        stored = self.registry_digests.get(reference)
        if stored is not None:
            return stored
        repository, separator, digest = reference.partition("@")
        if not separator:
            return None
        return next(
            (
                value
                for key, value in self.registry_digests.items()
                if value == digest and key.rsplit(":", 1)[0] == repository
            ),
            None,
        )

    def transfer_to(self, request: object, *, source: Path, destination: str) -> _TransferResult:
        self.events.append(f"transfer:{source.name}")
        self.remote_digests[destination] = digest_path(source)
        return super().transfer_to(request, source=source, destination=destination)

    def exec_argv(
        self,
        request: object,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None,
        cwd: str | None,
        dry_run: bool,
    ) -> _TransferResult:
        del request, env, dry_run
        argv = _unwrap_bounded(argv)
        self.actions.append(("exec", object(), argv, None, cwd, False))
        self.events.append("exec:" + " ".join(argv))
        if argv[:3] == ("docker", "buildx", "create") and self.remote_source_mutated:
            return _TransferResult(return_code=1)
        if argv[:3] == ("docker", "buildx", "inspect") and "--bootstrap" in argv:
            return _TransferResult(stdout="Platforms: linux/amd64, linux/arm64\n")
        if argv[0] == "sha256sum":
            digest = self.remote_digests[argv[1]].removeprefix("sha256:")
            return _TransferResult(stdout=f"{digest}  {argv[1]}\n")
        if argv[:3] == ("docker", "image", "inspect"):
            if argv[3] == "--format={{.Architecture}}":
                return _TransferResult(stdout="arm64\n")
            reference = argv[-1]
            digest = self.local_digests.get(reference)
            if digest is None:
                digest = "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
            return _TransferResult(stdout=f"{digest}\n")
        if argv[:2] == ("docker", "push"):
            self.registry_digests[argv[-1]] = _registry_digest(argv[-1])
            return _TransferResult()
        if argv[:2] == ("skopeo", "inspect"):
            digest = self._resolve_registry(argv[-1].removeprefix("docker://"))
            return (
                _TransferResult(stdout=f"{digest}\n")
                if digest is not None
                else _TransferResult(return_code=1)
            )
        if argv[:2] == ("docker", "port"):
            return _TransferResult(stdout="127.0.0.1:32768\n")
        if (
            argv[:2] == ("docker", "run")
            and "WATCHDOG_CMD=/nanofaas-arm64-smoke-missing-child" in argv
        ):
            return _TransferResult(
                stderr="Failed to spawn runtime: No such file or directory (os error 2)",
                return_code=1,
            )
        return _TransferResult()

    def connection_host(self, request: object) -> str:
        name = str(getattr(request, "name", ""))
        return "198.51.100.42" if name.endswith("-loadgen") else "203.0.113.10"


class _ArmFailureProvider(_ReleaseProvider):
    def __init__(self, events: list[str], failure: str) -> None:
        super().__init__(events)
        self.failure = failure

    def exec_argv(
        self,
        request: object,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None,
        cwd: str | None,
        dry_run: bool,
    ) -> _TransferResult:
        argv = _unwrap_bounded(argv)
        result = super().exec_argv(
            request,
            argv,
            env=env,
            cwd=cwd,
            dry_run=dry_run,
        )
        if (
            self.failure == "bake"
            and argv[:3] == ("docker", "buildx", "bake")
            and "docker-arm64" in argv
        ):
            return _TransferResult(stderr="arm bake failed", return_code=1)
        if (
            self.failure == "architecture"
            and argv[:3] == ("docker", "image", "inspect")
            and argv[3] == "--format={{.Architecture}}"
        ):
            return _TransferResult(stdout="amd64\n")
        if self.failure == "push" and argv[:2] == ("docker", "push") and "-arm64" in argv[-1]:
            return _TransferResult(stderr="arm push failed", return_code=1)
        if self.failure == "start" and argv[:3] == ("docker", "run", "--detach"):
            return _TransferResult(stderr="arm server failed", return_code=1)
        if (
            self.failure in {"health", "health-cleanup", "health-cleanup-raises"}
            and argv[0] == "curl"
        ):
            return _TransferResult(stderr="arm health failed", return_code=1)
        if self.failure == "health-cleanup" and argv[:3] == ("docker", "rm", "--force"):
            return _TransferResult(stderr="cleanup failed", return_code=1)
        if self.failure == "health-cleanup-raises" and argv[:3] == ("docker", "rm", "--force"):
            raise RuntimeError("cleanup exploded")
        if (
            self.failure == "watchdog"
            and argv[:2] == ("docker", "run")
            and "WATCHDOG_CMD=/nanofaas-arm64-smoke-missing-child" in argv
        ):
            return _TransferResult(stderr="exec format error", return_code=1)
        return result


__all__ = tuple(name for name in globals() if not name.startswith("__"))
