from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_engine import Workflow as SonataWorkflow
from typer.testing import CliRunner

from nanolab.app.main import app
import nanolab.cli.release as release_cli
import nanolab.cli.product as product_module
import nanolab.plans.release as release_plan
from nanolab.release import run as release_run
from nanolab.release.environment import release_lock_path, release_run_lock
from nanolab.release.metrics import RegressionDecision
from nanolab.release.run import GitState
from nanolab.release.versioning import read_project_version
from nanolab.workspace.paths import ToolPaths

from ..conftest import RejectingProvider


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)


def _file(path: Path, value: str = "fixture") -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _common_release_args(tmp_path: Path) -> list[str]:
    return [
        CURRENT_VERSION,
        "--environment",
        str(_file(tmp_path / "environment.yaml")),
        "--release-config",
        str(_file(tmp_path / "release.yaml")),
        "--run-dir",
        str(tmp_path / "run"),
    ]


def _credential_args(tmp_path: Path) -> list[str]:
    return [
        "--ghcr-token-file",
        str(_file(tmp_path / "ghcr-token")),
        "--cosign-key-file",
        str(_file(tmp_path / "cosign.key")),
        "--cosign-password-file",
        str(_file(tmp_path / "cosign.password")),
    ]


def test_release_command_surface_is_prepare_plan_and_run() -> None:
    result = CliRunner().invoke(app, ["release", "--help"])

    assert result.exit_code == 0, result.output
    release_group = next(group for group in app.registered_groups if group.name == "release")
    assert release_group.typer_instance is not None
    commands = {command.name for command in release_group.typer_instance.registered_commands}
    assert commands == {"prepare", "plan", "run"}


def test_release_prepare_requires_clean_source_before_version_changes(
    monkeypatch,
) -> None:
    called = False
    monkeypatch.setattr(
        release_cli,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=False),
    )

    def prepare(_root, _version):
        nonlocal called
        called = True

    monkeypatch.setattr(release_cli, "prepare_version", prepare)

    result = CliRunner().invoke(app, ["release", "prepare", "0.18.0"])

    assert result.exit_code != 0
    assert "clean Git tree" in result.output
    assert called is False


def test_release_prepare_delegates_to_curated_version_preparation(monkeypatch) -> None:
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        release_cli,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )
    monkeypatch.setattr(
        release_cli,
        "prepare_version",
        lambda root, version: seen.append((root, version)) or (root / "build.gradle",),
    )

    result = CliRunner().invoke(app, ["release", "prepare", "v0.18.0"])

    assert result.exit_code == 0, result.output
    assert seen == [(release_cli.default_tool_paths().nanofaas_root, "v0.18.0")]
    assert "build.gradle" in result.output
    assert "commit" in result.output.lower()


def test_release_plan_only_renders_the_guarded_offline_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        release_cli,
        "build_amd64_release_plan",
        lambda **kwargs: (
            seen.append(kwargs) or SimpleNamespace(render=lambda: "offline amd64 release plan\n")
        ),
    )
    monkeypatch.setattr(
        release_cli,
        "run_amd64_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plan must not start release execution")
        ),
    )

    result = CliRunner().invoke(app, ["release", "plan", *_common_release_args(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output == "offline amd64 release plan\n"
    assert seen[0]["version"] == CURRENT_VERSION
    assert seen[0]["run_dir"] == tmp_path / "run"
    assert seen[0]["credentials"] is None

    help_result = CliRunner().invoke(app, ["release", "plan", "--help"])
    assert "ghcr-token-file" not in help_result.output


def test_release_plan_uses_the_real_planner_without_cloud_or_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: GitState(commit="a" * 40, clean=True),
    )

    def reject_cloud(*_args, **_kwargs):
        raise AssertionError("offline plan must not touch cloud execution")

    monkeypatch.setattr(release_run, "vm_provider_for_environment", reject_cloud)
    monkeypatch.setattr(release_run, "provision_environment", reject_cloud)
    monkeypatch.setattr(release_run, "build_loadtest_plan", reject_cloud)

    # The shipped example intentionally carries an RFC 5737 documentation CIDR,
    # which preflight rejects as a placeholder; an operator supplies a real one.
    environment = tmp_path / "azure-release.yaml"
    environment.write_text(
        (NANOLAB_ROOT / "environments/azure-release.yaml.example")
        .read_text(encoding="utf-8")
        .replace("203.0.113.0/24", "8.8.8.8/32"),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "release",
            "plan",
            CURRENT_VERSION,
            "--environment",
            str(environment),
            "--release-config",
            str(NANOLAB_ROOT / "release.yaml"),
            "--run-dir",
            str(tmp_path / "run"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "provider azure: stack + loadgen" in result.output
    assert "credentials: deferred for offline plan" in result.output


def test_release_run_forwards_explicit_resume_and_keep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = object()
    calls: list[tuple[object, bool, bool]] = []
    monkeypatch.setattr(release_cli, "build_amd64_release_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        release_cli,
        "run_amd64_release",
        lambda value, *, resume, keep: (
            calls.append((value, resume, keep)) or RegressionDecision(True, True, ())
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "release",
            "run",
            *_common_release_args(tmp_path),
            *_credential_args(tmp_path),
            "--resume",
            "--keep",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(plan, True, True)]
    assert "passed" in result.output.lower()


def test_fresh_release_run_requires_explicit_provision_acknowledgement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    built = False

    def build(**_kwargs):
        nonlocal built
        built = True

    monkeypatch.setattr(release_cli, "build_amd64_release_plan", build)
    args = [
        "release",
        "run",
        *_common_release_args(tmp_path),
        *_credential_args(tmp_path),
    ]

    rejected = CliRunner().invoke(app, args)

    assert rejected.exit_code != 0
    assert "--provision" in rejected.output
    assert built is False


def test_fresh_release_run_accepts_explicit_provision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = object()
    calls: list[tuple[object, bool, bool]] = []
    monkeypatch.setattr(release_cli, "build_amd64_release_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        release_cli,
        "run_amd64_release",
        lambda value, *, resume, keep: (
            calls.append((value, resume, keep)) or RegressionDecision(True, True, ())
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "release",
            "run",
            *_common_release_args(tmp_path),
            *_credential_args(tmp_path),
            "--provision",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(plan, False, False)]


# --- Generic `nanolab run <release scenario>` surface (Sonata release) ---------


class _SpyWorkflow(SonataWorkflow):
    """A real Sonata workflow that records how the CLI invoked run()."""

    def run(self, **kwargs):
        self.run_kwargs = dict(kwargs)  # pyright: ignore[reportAttributeAccessIssue]
        return super().run(**kwargs)


class _Phase(Task[None]):
    title = "Run source tests"

    def __init__(self, events: list[str], failure: BaseException | None) -> None:
        self.events = events
        self.failure = failure

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        self.events.append("phase")
        if self.failure is not None:
            raise self.failure
        return TaskOutcome()


@pytest.fixture
def release_cli_harness(
    monkeypatch,
    tmp_path: Path,
    canonical_release_configs: tuple[Path, Path],
    nanofaas_root: Path,
):
    """Drive `nanolab run <release scenario>` with no cloud and a one-phase DAG."""
    tool_root = tmp_path / "tool"
    tool_root.mkdir()
    scenario_path, environment_path = canonical_release_configs
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    release_config = tmp_path / "release-config.yaml"
    release_config.write_text(
        yaml.safe_dump(
            {
                "ghcr_token_file": str(_file(secrets / "ghcr-token")),
                "cosign_key_file": str(_file(secrets / "cosign.key")),
                "cosign_password_file": str(_file(secrets / "cosign.password")),
            }
        ),
        encoding="utf-8",
    )

    state = SimpleNamespace(
        events=[],
        built=[],
        workflows=[],
        failure=None,
        paths=ToolPaths.from_roots(nanofaas_root, tool_root),
        scenario=scenario_path,
        environment=environment_path,
        release_config=release_config,
        version=read_project_version(nanofaas_root),
        monkeypatch=monkeypatch,
    )

    monkeypatch.setattr(product_module, "default_tool_paths", lambda: state.paths)
    monkeypatch.setattr(
        release_plan, "git_state", lambda _root: GitState(commit="a" * 40, clean=True)
    )
    # The guarded commit is fake, so extraction can't really `git archive` it;
    # plan straight from the real checkout instead, same as tests/plans/test_release.py.
    monkeypatch.setattr(
        release_plan,
        "extract_commit_tree",
        lambda _repo_root, _commit, _destination: nanofaas_root,
    )
    monkeypatch.setattr(
        product_module, "vm_provider_for_environment", lambda *_args, **_kwargs: RejectingProvider()
    )

    def build(request, *, provider=None):
        state.built.append((request, provider))
        workflow = _SpyWorkflow(workflow_id="release-test")
        resource = Resource(
            title="Acquire release stack VM",
            acquire=lambda _inputs: state.events.append("acquire") or "vm",
            release=lambda _inputs, _value: state.events.append("release"),
            infrastructure=True,
        )
        workflow.add(_Phase(state.events, state.failure), requires=(resource,))
        state.workflows.append(workflow)
        return workflow

    monkeypatch.setattr(product_module, "build_release_workflow", build)

    def invoke(*extra: str, run_dir: Path | None = tmp_path / "run"):
        args = [
            "run",
            str(state.scenario),
            "--environment",
            str(state.environment),
            "--release-config",
            str(state.release_config),
            *(("--run-dir", str(run_dir)) if run_dir is not None else ()),
            *extra,
        ]
        return CliRunner().invoke(app, args)

    state.invoke = invoke
    state.release_dir = tmp_path / "run" / "releases" / state.version
    return state


def test_generic_release_run_requires_explicit_provision_acknowledgement(
    release_cli_harness,
) -> None:
    result = release_cli_harness.invoke()

    assert result.exit_code != 0
    assert "--provision" in result.output
    assert "Traceback" not in result.output
    assert release_cli_harness.built == []
    assert release_cli_harness.events == []


def test_generic_release_run_provisions_journals_and_passes(release_cli_harness) -> None:
    result = release_cli_harness.invoke("--provision")

    assert result.exit_code == 0, result.output
    assert release_cli_harness.events == ["acquire", "phase", "release"]
    journal = release_cli_harness.release_dir / "sonata.jsonl"
    assert journal.is_file()
    kwargs = release_cli_harness.workflows[0].run_kwargs
    assert kwargs["journal"].path == journal
    assert kwargs["resume"] is False
    assert set(kwargs["verifiers"]) == {
        "file-digest",
        "local-image-digest",
        "local-registry-digest",
        "ghcr-digest",
    }
    metadata = json.loads(
        (release_cli_harness.release_dir / "run-metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["status"] == "passed"


def test_generic_release_run_uses_a_versioned_default_run_directory(
    release_cli_harness,
) -> None:
    result = release_cli_harness.invoke("--provision", run_dir=None)

    assert result.exit_code == 0, result.output
    journal = release_cli_harness.workflows[0].run_kwargs["journal"].path
    expected = (
        release_cli_harness.paths.runs_dir
        / "release"
        / "releases"
        / release_cli_harness.version
        / "sonata.jsonl"
    )
    assert journal == expected
    assert "latest" not in str(journal)


def test_generic_release_run_forwards_keep_and_selection(release_cli_harness) -> None:
    result = release_cli_harness.invoke("--provision", "--keep", "--until", "run-source-tests")

    assert result.exit_code == 0, result.output
    workflow = release_cli_harness.workflows[0]
    assert workflow.keep_infrastructure is True
    assert workflow.run_kwargs["select"].until == "run-source-tests"
    # `--keep` retains only infrastructure, so the VM resource is never released.
    assert release_cli_harness.events == ["acquire", "phase"]


def test_generic_release_resume_requires_an_existing_journal(release_cli_harness) -> None:
    result = release_cli_harness.invoke("--resume")

    assert result.exit_code != 0
    assert "--resume requires an existing release journal" in result.output
    assert "Traceback" not in result.output
    assert release_cli_harness.built == []


def test_generic_release_fresh_run_refuses_to_overwrite_a_journal(release_cli_harness) -> None:
    assert release_cli_harness.invoke("--provision").exit_code == 0

    repeated = release_cli_harness.invoke("--provision")

    assert repeated.exit_code != 0
    assert "pass --resume" in repeated.output
    assert "Traceback" not in repeated.output


def test_generic_release_resume_reuses_the_verified_journal(release_cli_harness) -> None:
    assert release_cli_harness.invoke("--provision").exit_code == 0
    release_cli_harness.events.clear()

    result = release_cli_harness.invoke("--resume")

    assert result.exit_code == 0, result.output
    assert release_cli_harness.workflows[1].run_kwargs["resume"] is True


def test_generic_release_failure_releases_resources_and_records_metadata(
    release_cli_harness,
) -> None:
    release_cli_harness.failure = RuntimeError("source tests failed")
    trees: list[Path] = []
    original = release_plan.build_release_request

    def record(**kwargs):
        trees.append(Path(kwargs["source_tree"]))
        return original(**kwargs)

    release_cli_harness.monkeypatch.setattr(product_module, "build_release_request", record)

    result = release_cli_harness.invoke("--provision")

    assert result.exit_code != 0
    assert release_cli_harness.events == ["acquire", "phase", "release"]
    metadata = json.loads(
        (release_cli_harness.release_dir / "run-metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["status"] == "failed"
    assert "source tests failed" in metadata["error"]
    # A workflow failure keeps CliRunner's Result holding the exception and its
    # traceback alive, which keeps the frame (and `lifetime`) alive too -- unlike
    # a clean return, refcounting alone won't tear the tempdir down here. This is
    # what actually pins the `finally: lifetime.close()` on the workflow-execution
    # try/except.
    assert len(trees) == 1
    assert not trees[0].exists()


def test_generic_release_interrupt_still_releases_infrastructure(release_cli_harness) -> None:
    release_cli_harness.failure = KeyboardInterrupt()

    result = release_cli_harness.invoke("--provision")

    assert result.exit_code != 0
    assert release_cli_harness.events == ["acquire", "phase", "release"]


def test_generic_release_run_rejects_a_concurrent_coordinator(release_cli_harness) -> None:
    environment = product_module._environment(release_cli_harness.environment)

    with release_run_lock(release_lock_path(environment)):
        result = release_cli_harness.invoke("--provision")

    assert result.exit_code != 0
    assert "already in progress" in result.output
    assert "Traceback" not in result.output
    assert release_cli_harness.events == []


def test_generic_release_run_removes_the_extracted_tree(release_cli_harness) -> None:
    """Pins that `source_tree` is a real absolute path plumbed through to
    `build_release_request`, and that it's gone after a clean run.

    This does NOT discriminate the cleanup guard on its own: on a clean
    return with no exception, CPython refcounting tears the `ExitStack` (and
    its `TemporaryDirectory`) down as soon as `run_command` returns, whether
    or not `finally: lifetime.close()` exists. See
    `test_generic_release_failure_releases_resources_and_records_metadata`
    and `test_generic_release_preflight_rejection_removes_the_extracted_tree`
    for the failure paths where a live traceback keeps frames (and the
    guards) alive, and cleanup only happens if the guard is doing its job.
    """
    trees: list[Path] = []
    original = release_plan.build_release_request

    def record(**kwargs):
        trees.append(Path(kwargs["source_tree"]))
        return original(**kwargs)

    release_cli_harness.monkeypatch.setattr(product_module, "build_release_request", record)

    result = release_cli_harness.invoke("--provision")

    assert result.exit_code == 0, result.output
    assert len(trees) == 1
    assert trees[0].is_absolute()
    assert not trees[0].exists()


def test_generic_release_preflight_rejection_removes_the_extracted_tree(
    release_cli_harness,
) -> None:
    """A preflight rejection (not just a clean run) must not leak the tree."""
    assert release_cli_harness.invoke("--provision").exit_code == 0

    trees: list[Path] = []
    original = release_plan.build_release_request

    def record(**kwargs):
        trees.append(Path(kwargs["source_tree"]))
        return original(**kwargs)

    release_cli_harness.monkeypatch.setattr(product_module, "build_release_request", record)

    result = release_cli_harness.invoke("--provision")

    assert result.exit_code != 0
    assert "pass --resume" in result.output
    assert len(trees) == 1
    assert trees[0].is_absolute()
    assert not trees[0].exists()


def test_generic_release_plan_stays_offline(release_cli_harness) -> None:
    result = CliRunner().invoke(
        app,
        [
            "plan",
            str(release_cli_harness.scenario),
            "--environment",
            str(release_cli_harness.environment),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "run-source-tests" in result.output
    assert release_cli_harness.events == []
