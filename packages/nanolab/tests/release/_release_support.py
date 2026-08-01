# ruff: noqa: F401 - imports are fixtures re-exported to split test modules
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
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

from nanolab.release import arm
from nanolab.release import run as release_run
from nanolab.release.metrics import build_release_record
from nanolab.release.versioning import read_project_version


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)
CURRENT_TAG = f"v{CURRENT_VERSION}"
BUILDER_NAME = f"nanofaas-release-{CURRENT_TAG.replace('.', '-')}"
_VERSION_PARTS = tuple(int(part) for part in CURRENT_VERSION.split("."))
MISMATCH_VERSION = ".".join(str(part) for part in (*_VERSION_PARTS[:2], _VERSION_PARTS[2] + 1))


def _secret(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _environment(tmp_path: Path) -> Path:
    source = NANOLAB_ROOT / "environments/azure-release.yaml.example"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data["azure"]["operator_source_cidr"] = "8.8.8.8/32"
    destination = tmp_path / "azure-release.yaml"
    destination.write_text(yaml.safe_dump(data), encoding="utf-8")
    return destination


def _plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="a" * 40, clean=True),
    )
    credentials = release_run.CredentialFiles(
        ghcr_token=_secret(tmp_path / "ghcr-token", "fixture-token"),
        cosign_key=_secret(tmp_path / "cosign.key", "fixture-key"),
        cosign_password=_secret(tmp_path / "cosign.password", "fixture-password"),
    )
    return release_run.build_amd64_release_plan(
        repo_root=NANOFAAS_ROOT,
        version=CURRENT_TAG,
        environment_path=_environment(tmp_path),
        release_config_path=NANOLAB_ROOT / "release.yaml",
        run_dir=tmp_path / "run",
        credentials=credentials,
        # finalization must never write into the real repository docs in tests
        performance_root=tmp_path / "performance-docs",
    )


def _release_config(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text("workflow: loadtest\nfunctions: [word-stats-java]\n")
    config: dict[str, object] = {
        "schemaVersion": 1,
        "build": {"maxParallelism": 2},
        "benchmark": {
            "scenario": "scenario.yaml",
            "runs": 3,
            "profile": "test-profile",
            "regression": {
                "throughputMaxLossPercent": 10,
                "p95MaxIncreasePercent": 15,
                "errorRateMax": 0.3,
            },
        },
    }
    return tmp_path / "release.yaml", config


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


class _FlakyProvider:
    """Records exec attempts; each entry is a (return_code|exception) script."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def exec_argv(self, request, argv, *, env, cwd, dry_run):
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(return_code=outcome, stdout="", stderr="")


class _FlakyTransferProvider:
    def __init__(self, outcomes: list[int]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def transfer_to(self, request, *, source, destination):
        outcome = self._outcomes[self.calls]
        self.calls += 1
        return SimpleNamespace(return_code=outcome, stdout="", stderr="")


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
        self.fact_overrides: dict[str, dict[str, object]] = {}
        self.restrictions: list[tuple[object, tuple[int, ...], tuple[str, ...]]] = []

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

    def teardown(self, request: object) -> _TransferResult:
        self.events.append(f"teardown:{getattr(request, 'name', None)}")
        return _TransferResult()

    def release_vm_facts(self, request: object) -> SimpleNamespace:
        name = str(getattr(request, "name", ""))
        loadgen = name.endswith("-loadgen")
        arm_builder = name.endswith("-arm")
        if loadgen:
            size, disk = "Standard_D2s_v5", 30
        elif arm_builder:
            size, disk = "Standard_D8ps_v5", 64
        else:
            size, disk = "Standard_D8s_v5", 128
        values: dict[str, object] = {
            "location": "westeurope",
            "vm_size": size,
            "disk_size_gb": disk,
            "image_urn": (
                "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140"
                if arm_builder
                else "Canonical:ubuntu-24_04-lts:server:24.04.202607140"
            ),
        }
        values.update(self.fact_overrides.get(name, {}))
        self.events.append(f"facts:{name}")
        return SimpleNamespace(**values)

    def restrict_inbound_sources(
        self,
        request: object,
        *,
        ports: tuple[int, ...],
        source_cidrs: tuple[str, ...],
        priority_base: int = 1010,
    ) -> None:
        self.events.append(f"restrict:{getattr(request, 'name', None)}")
        self.restrictions.append((request, ports, source_cidrs))
        assert 100 <= priority_base + len(ports) - 1 <= 4096

    def transfer_to(self, request: object, *, source: Path, destination: str) -> _TransferResult:
        self.events.append(f"transfer:{source.name}")
        if source.name == "source.tar":
            self.remote_source_mutated = False
        self.remote_digests[destination] = release_run.digest_path(source)
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
        if "public-key" in argv:
            return _TransferResult(
                stdout="-----BEGIN PUBLIC KEY-----\nMFkwEwYHfake\n-----END PUBLIC KEY-----"
            )
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
        if argv[:2] == ("mktemp", "-d"):
            return _TransferResult(stdout="/tmp/nanofaas-release-credentials.fake01\n")
        if argv[:2] == ("skopeo", "copy"):
            digest = self._resolve_registry(argv[-2].removeprefix("docker://"))
            if digest is None:
                return _TransferResult(return_code=1)
            self.registry_digests[argv[-1].removeprefix("docker://")] = digest
            return _TransferResult()
        if argv[:4] == ("docker", "buildx", "imagetools", "create"):
            tag = argv[argv.index("--tag") + 1]
            sources = argv[argv.index("--tag") + 2 :]
            if len(sources) == 1 and "@sha256:" in sources[0]:
                self.registry_digests[tag] = "sha256:" + sources[0].rsplit("@sha256:", 1)[1]
            elif len(sources) == 1 and sources[0] in self.registry_digests:
                self.registry_digests[tag] = self.registry_digests[sources[0]]
            else:
                self.registry_digests[tag] = _registry_digest(",".join(sorted(sources)))
            return _TransferResult()
        if argv[:4] == ("docker", "buildx", "imagetools", "inspect"):
            if argv[-1] not in self.registry_digests:
                return _TransferResult(return_code=1)
            return _TransferResult(
                stdout=("Manifests:\n  Platform:    linux/amd64\n  Platform:    linux/arm64\n")
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


class _RecreatedReleaseProvider(_ReleaseProvider):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.images_available = False

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
        if argv[:3] == ("docker", "image", "inspect") and not self.images_available:
            self.events.append("exec:" + " ".join(argv))
            return _TransferResult(return_code=1)
        if argv[:3] == ("docker", "buildx", "bake"):
            self.images_available = True
        return super().exec_argv(
            request,
            argv,
            env=env,
            cwd=cwd,
            dry_run=dry_run,
        )


class _RegistryMutatesAfterEvidenceProvider(_ReleaseProvider):
    def __init__(self, events: list[str], target: str) -> None:
        super().__init__(events)
        self.target = target
        self.mutated = False

    def exec_argv(
        self,
        request: object,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None,
        cwd: str | None,
        dry_run: bool,
    ) -> _TransferResult:
        result = super().exec_argv(
            request,
            argv,
            env=env,
            cwd=cwd,
            dry_run=dry_run,
        )
        if (
            argv[:2] == ("skopeo", "inspect")
            and argv[-1] == f"docker://{self.target}"
            and not self.mutated
        ):
            self.registry_digests[self.target] = "sha256:" + "f" * 64
            self.mutated = True
        return result


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
            self.failure == "builder"
            and argv[:3] == ("docker", "buildx", "inspect")
            and "--bootstrap" in argv
        ):
            return _TransferResult(stdout="Platforms: linux/amd64\n")
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
        if (
            self.failure == "digest"
            and argv[:3] == ("docker", "image", "inspect")
            and argv[3] == "--format={{.Id}}"
            and "-arm64" in argv[-1]
        ):
            return _TransferResult(stdout="missing\n")
        if self.failure == "push" and argv[:2] == ("docker", "push") and "-arm64" in argv[-1]:
            return _TransferResult(stderr="arm push failed", return_code=1)
        if (
            self.failure == "registry"
            and argv[:2] == ("skopeo", "inspect")
            and "-arm64" in argv[-1]
        ):
            return _TransferResult(stdout="missing\n")
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


class _LoadtestWorkflow:
    def __init__(self, run_dir: Path, value: float, events: list[str]) -> None:
        self.run_dir = run_dir
        self.value = value
        self.events = events

    def run(self) -> None:
        self.events.append(f"loadtest:{self.run_dir.name}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "summary.json").write_text(
            json.dumps(_summary(self.value)), encoding="utf-8"
        )


def _runtime_fakes(plan, events: list[str], provider=None):
    provider = provider or _ReleaseProvider(events)

    @contextmanager
    def provisioner(*args, **kwargs):
        del args
        verifier = kwargs.pop("post_ensure_verifier", None)
        assert not kwargs.keys() - {"repo_root", "orchestrator_factory", "keep"}
        provider.events.append("provision:enter")
        try:
            if verifier is not None:
                verifier(
                    "stack",
                    release_run.vm_request_for_role(plan.environment, "stack", loadtest=True),
                )
                verifier(
                    "loadgen",
                    release_run.vm_request_for_role(plan.environment, "loadgen", loadtest=True),
                )
                verifier(
                    "arm-builder",
                    release_run.vm_request_for_role(plan.environment, "arm-builder"),
                )
            yield
        finally:
            provider.events.append("provision:exit")

    def builder_provisioner(provider_arg, request, repo_root):
        del repo_root
        assert provider_arg is provider
        provider.events.append(f"release-builder:{getattr(request, 'name', '?')}")

    loadtest_calls: list[dict[str, object]] = []

    def loadtest_builder(*args, **kwargs):
        del args
        loadtest_calls.append(kwargs)
        return _LoadtestWorkflow(kwargs["run_dir"], float(len(loadtest_calls) * 10), events)

    def archive_builder(repo_root: Path, commit: str, destination: Path):
        del repo_root, commit
        destination.write_bytes(b"exact-source")
        return release_run.ArtifactEvidence(
            "local", str(destination), release_run.digest_path(destination)
        )

    return (
        provider,
        provisioner,
        builder_provisioner,
        loadtest_builder,
        archive_builder,
        loadtest_calls,
    )


def _provisioner_with_recorded_rsync(plan, provider, wildcard_present):
    @contextmanager
    def provisioner(*args, **kwargs):
        del args
        verifier = kwargs["post_ensure_verifier"]
        provider.events.append("provision:enter")
        try:
            for role in ("stack", "loadgen"):
                verifier(
                    role,
                    release_run.vm_request_for_role(plan.environment, role, loadtest=True),
                )
            verifier(
                "arm-builder",
                release_run.vm_request_for_role(plan.environment, "arm-builder"),
            )
            for role in ("stack", "loadgen"):
                provider.events.append(f"rsync:{role}:wildcard={wildcard_present()}")
            yield
        finally:
            provider.events.append("provision:exit")

    return provisioner


def _run_with_arm_failure(
    plan: release_run.Amd64ReleasePlan,
    events: list[str],
    provider: _ArmFailureProvider,
) -> None:
    _, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events, provider)
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )


__all__ = tuple(name for name in globals() if not name.startswith("__"))
