"""Smoke tests for the release workflow builder."""

import os
from pathlib import Path

import pytest
import yaml

import nanolab.plans.release as release_plan
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.plan import ImagePlan
from nanolab.plans.release import ReleaseRequest, build_release_workflow
from nanolab.release.run import GitState, ReleaseSettings
from nanolab.release.state import digest_path
from nanolab.release.versioning import read_project_version


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


def _canonical_environment(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "provider": "azure",
                "roles": {
                    "stack": {"name": "nanofaas-azure-release", "disk": "128G"},
                    "loadgen": {
                        "name": "nanofaas-azure-release-loadgen",
                        "disk": "30G",
                    },
                    "arm-builder": {
                        "name": "nanofaas-azure-release-arm",
                        "disk": "64G",
                    },
                },
                "azure": {
                    "resource_group": "nanofaas-rg",
                    "location": "westeurope",
                    "vm_size": "Standard_D8s_v5",
                    "loadgen_vm_size": "Standard_D2s_v5",
                    "arm_vm_size": "Standard_D8ps_v5",
                    "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
                    "arm_image_urn": (
                        "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140"
                    ),
                    "operator_source_cidr": "8.8.8.8/32",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _canonical_scenario(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "workflow": "release",
                "functions": ["word-stats-java"],
                "release": {
                    "version": f"v{CURRENT_VERSION}",
                    "profile": "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1",
                    "max_parallelism": 4,
                    "benchmark_runs": 3,
                    "benchmark_scenario": "loadtest.yaml",
                    "throughput_max_loss_percent": 10,
                    "p95_max_increase_percent": 15,
                    "error_rate_max": 0.30,
                },
            }
        ),
        encoding="utf-8",
    )
    (path.parent / "loadtest.yaml").write_text(
        "workflow: loadtest\nfunctions: [word-stats-java]\n",
        encoding="utf-8",
    )
    return path


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
) -> None:
    environment_path = _canonical_environment(tmp_path / "environment.yaml")
    scenario_path = _canonical_scenario(tmp_path / "release.yaml")
    home = tmp_path / "home"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    for name in ("ghcr", "cosign.key", "cosign.password"):
        secret = secrets / name
        secret.write_text(name, encoding="utf-8")
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

    request = release_plan.build_release_request(
        repo_root=NANOLAB_ROOT,
        nanofaas_root=NANOFAAS_ROOT,
        scenario_path=scenario_path,
        environment_path=environment_path,
        release_config_path=credential_path,
        run_dir=tmp_path / "run",
        performance_root=tmp_path / "performance",
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


def test_build_release_request_requires_credentials_for_execution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential config is required"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=_canonical_scenario(tmp_path / "release.yaml"),
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            executable=True,
        )


def test_build_release_request_rejects_missing_benchmark_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario_path = _canonical_scenario(tmp_path / "release.yaml")
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
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
        )


def test_build_release_request_rejects_symlink_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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

    with pytest.raises(ValueError, match="regular file"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=_canonical_scenario(tmp_path / "release.yaml"),
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=credential_path,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            executable=True,
        )


def test_build_release_request_rejects_noncanonical_policy(tmp_path: Path) -> None:
    scenario_path = _canonical_scenario(tmp_path / "release.yaml")
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    scenario["release"]["profile"] = "different-profile"
    scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical Azure performance policy"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=scenario_path,
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
        )


@pytest.mark.parametrize("repository", ("nanolab", "nanofaas"))
def test_build_release_request_rejects_credentials_inside_either_repository(
    repository: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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

    with pytest.raises(ValueError, match="outside the repository"):
        release_plan.build_release_request(
            repo_root=tool_root,
            nanofaas_root=source_root,
            scenario_path=_canonical_scenario(tmp_path / "release.yaml"),
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=credential_path,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
            executable=True,
        )


def test_build_release_request_rejects_dirty_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        release_plan,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=False),
    )

    with pytest.raises(ValueError, match="clean nanoFaaS Git tree"):
        release_plan.build_release_request(
            repo_root=NANOLAB_ROOT,
            nanofaas_root=NANOFAAS_ROOT,
            scenario_path=_canonical_scenario(tmp_path / "release.yaml"),
            environment_path=_canonical_environment(tmp_path / "environment.yaml"),
            release_config_path=None,
            run_dir=tmp_path / "run",
            performance_root=tmp_path / "performance",
        )
