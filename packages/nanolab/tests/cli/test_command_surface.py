from typer.testing import CliRunner
import json
import subprocess
from pathlib import Path
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sonata_engine import Selection, Task, TaskInputs, TaskOutcome
from sonata_engine import Workflow as SonataWorkflow

# These tests pass repo-relative paths (scenarios-v2/..., environments/...) to the
# CLI, so they must run from the project root regardless of pytest's cwd.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _run_from_project_root(monkeypatch):
    monkeypatch.chdir(_PROJECT_ROOT)

from nanolab.app.main import app
from nanolab.workspace.provenance import git_provenance
import nanolab.cli.product as product_module


@dataclass
class _Task:
    task_id: str = "test.task"
    title: str = "Test task"

    def run(self) -> None:
        pass


def _sonata_workflow(*, fails: str | None = None) -> SonataWorkflow:
    """A real one-task Sonata workflow, for scenarios the CLI now runs on Sonata.

    A real workflow rather than a fake: the CLI calls compile() and run(select=)
    on these, and a stand-in that only grew those two methods would prove less
    than the engine itself does."""

    class _One(Task[None]):
        title = "Test task"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            del inputs
            if fails is not None:
                raise RuntimeError(fails)
            return TaskOutcome()

    workflow = SonataWorkflow(workflow_id="test")
    workflow.add(_One())
    return workflow


def test_top_level_exposes_only_the_intended_product_commands() -> None:
    """The surface is a deliberate list, not whatever happens to be registered.

    `compare` earns its place by being the one thing `run` cannot express: it
    holds one cluster across many runs and varies the control-plane build between
    them, where `run` compiles a single scenario once.
    """
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    commands = {command.name for command in app.registered_commands}
    assert commands == {"run", "plan", "list", "workflow", "workflows", "inspect", "doctor", "tui", "compare"}


def test_list_does_not_require_nanofaas_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOFAAS_ROOT", raising=False)

    result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert "deployment-lifecycle-container.yaml" in result.output


def test_workflows_lists_supported_environment_providers() -> None:
    result = CliRunner().invoke(app, ["workflows"])

    assert result.exit_code == 0, result.output
    assert "validate" in result.output
    assert "local, multipass, external, azure, proxmox" in result.output
    assert "release" in result.output
    assert "azure" in result.output


def test_workflow_lists_scenarios_in_a_formatted_table() -> None:
    result = CliRunner().invoke(app, ["workflow"])

    assert result.exit_code == 0, result.output
    assert "Workflow" in result.output
    assert "Scenarios" in result.output
    assert "autoscaling-cycle-container.yaml" in result.output


def test_workflows_discovers_added_and_removed_scenarios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios = tmp_path / "scenarios-v2"
    scenarios.mkdir()
    scenario = scenarios / "temporary.yaml"
    scenario.write_text("workflow: temporary\nbackend: k8s\n", encoding="utf-8")
    monkeypatch.setattr(product_module, "discover_tool_root", lambda: tmp_path)

    result = CliRunner().invoke(app, ["workflows"])
    assert "temporary" in result.output
    assert "local, multipass, external, azure, proxmox" in result.output

    scenario.unlink()
    result = CliRunner().invoke(app, ["workflows"])
    assert "temporary:" not in result.output


def test_doctor_uses_shared_diagnostics(monkeypatch) -> None:
    shared_check = MagicMock(return_value=["docker"])
    monkeypatch.setattr(
        product_module,
        "diagnostics",
        SimpleNamespace(missing_executables=shared_check),
        raising=False,
    )
    monkeypatch.setattr(
        product_module,
        "shutil",
        SimpleNamespace(
            which=lambda _name: (_ for _ in ()).throw(
                AssertionError("CLI duplicated the executable check")
            )
        ),
        raising=False,
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "missing executables: docker" in result.output
    shared_check.assert_called_once_with()


def test_plan_builds_shared_validate_workflow() -> None:
    result = CliRunner().invoke(
        app,
        ["plan", "scenarios-v2/deployment-lifecycle-k8s.yaml", "--environment", "environments/local.yaml"],
    )

    assert result.exit_code == 0
    assert "005.build-application-artifact-word-stats-java" in result.stdout
    assert "run-kubernetes-e2e-test" not in result.stdout
    assert "inspect-resources-of-fn-word-stats-java" in result.stdout


def test_run_renders_normalized_task_progress(monkeypatch) -> None:
    monkeypatch.setattr(
        "nanolab.cli.product._workflow",
        lambda *args, **kwargs: _sonata_workflow(),
    )

    result = CliRunner().invoke(app, ["run", "scenarios-v2/autoscaling-cycle-k8s.yaml"])

    assert result.exit_code == 0
    # The compiler owns the id, so progress lines carry NNN.slug.
    assert "[001.test-task] running" in result.stdout
    assert "[001.test-task] passed" in result.stdout


def test_run_container_loadtest_requires_k6_before_building_the_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock(side_effect=AssertionError("workflow must not be built"))
    monkeypatch.setattr(product_module, "_workflow", build)
    monkeypatch.setattr(
        product_module.diagnostics,
        "missing_executables",
        lambda required=(): ["k6"] if required == ("k6",) else [],
    )

    result = CliRunner().invoke(app, ["run", "scenarios-v2/autoscaling-cycle-container.yaml"])

    assert result.exit_code != 0
    assert "requires k6 on the host" in result.output
    build.assert_not_called()


def test_generic_release_run_requires_an_environment_and_never_provisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provision = MagicMock(side_effect=AssertionError("must not provision"))
    build = MagicMock(side_effect=AssertionError("must not build workflow"))
    monkeypatch.setattr(product_module, "provision_environment", provision)
    monkeypatch.setattr(product_module, "build_release_workflow", build)

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/release.yaml"],
    )

    assert result.exit_code != 0
    assert "release workflow requires --environment" in result.output
    assert "Traceback" not in result.output
    provision.assert_not_called()
    build.assert_not_called()


def test_generic_run_help_exposes_resume() -> None:
    result = CliRunner().invoke(app, ["run", "--help"])

    assert result.exit_code == 0, result.output
    assert "--resume" in result.output


def test_generic_run_rejects_resume_for_non_release_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/autoscaling-cycle-k8s.yaml", "--resume"],
    )

    assert result.exit_code != 0
    assert "--resume is only supported for release workflows" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()


def test_run_requires_an_explicit_url_for_k8s_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(app, ["run", "scenarios-v2/cli-contract-k8s.yaml"])

    assert result.exit_code != 0
    assert "--control-plane-url is required for a k8s cli scenario" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()


def test_run_container_cli_builds_and_runs_without_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = MagicMock()
    build = MagicMock(return_value=workflow)
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/cli-contract-container.yaml"],
    )

    assert result.exit_code == 0, result.output
    assert build.call_args.kwargs["control_plane_url"] is None
    workflow.run.assert_called_once()


def test_run_container_cli_requires_local_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/cli-contract-container.yaml",
            "--environment",
            "environments/multipass.yaml",
        ],
    )

    assert result.exit_code != 0
    assert "cli container scenario requires a local environment" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()


def test_run_container_cli_rejects_nonlocal_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A local environment is rejected generically; using Multipass here proves
    # the container workflow rejects the non-local case on its own terms.
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/cli-contract-container.yaml",
            "--environment",
            "environments/multipass.yaml",
        ],
    )

    assert result.exit_code != 0
    assert "cli container scenario requires a local environment" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()


def test_run_container_cli_rejects_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/cli-contract-container.yaml", "--keep"],
    )

    assert result.exit_code != 0
    assert "--keep is not supported for a cli container scenario" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()


def test_run_passes_custom_control_plane_url_to_cli_plan(
    monkeypatch,
) -> None:
    # The cli scenario is now built and run by Sonata (a real sonata_engine.Workflow).
    # This test only cares about the args build_cli_plan was called with,
    # so a mock of the Sonata API (keep assignment + run(select=...))
    # stands in without adding any compatibility shim.
    build_cli_plan = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(product_module, "build_cli_plan", build_cli_plan)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/cli-contract-k8s.yaml",
            "--control-plane-url",
            "http://control-plane.example:8181",
        ],
    )

    assert result.exit_code == 0
    assert build_cli_plan.call_args.kwargs["endpoint"] == (
        "http://control-plane.example:8181"
    )


def test_run_provisioned_k8s_cli_skips_the_legacy_provisioning_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cli/k8s owns its own VM/Helm lifecycle inside the compiled
    # Sonata plan (see nanolab.plans.cli.build_cli_plan); it must never also
    # go through the legacy provision_environment context manager.
    workflow = MagicMock()
    build_cli_plan = MagicMock(return_value=workflow)
    monkeypatch.setattr(product_module, "build_cli_plan", build_cli_plan)

    def _legacy_provision_must_not_run(*args, **kwargs):
        raise AssertionError("legacy provision_environment must not run for cli/k8s")

    monkeypatch.setattr(product_module, "provision_environment", _legacy_provision_must_not_run)

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/cli-contract-k8s.yaml",
            "--environment",
            "environments/multipass.yaml",
        ],
    )

    assert result.exit_code == 0, result.output
    assert build_cli_plan.call_args.kwargs["endpoint"] is None
    assert build_cli_plan.call_args.kwargs["environment"].provider == "multipass"
    workflow.run.assert_called_once_with(
        select=Selection(only=None, start=None, until=None)
    )


def test_plan_provisioned_k8s_cli_shows_the_whole_contract_workflow() -> None:
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "scenarios-v2/cli-contract-k8s.yaml",
            "--environment",
            "environments/multipass.yaml",
        ],
    )

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 24
    assert "001.build-nanofaas-cli" in result.stdout
    assert "002.acquire-stack-vm" in result.stdout
    assert "024.release-stack-vm" in result.stdout
    assert "registry" not in result.stdout


def test_run_provisions_before_executing_workflow(monkeypatch, tmp_path: Path) -> None:
    actions: list[str] = []
    workflow = _sonata_workflow()

    @contextmanager
    def provision(*args, **kwargs):
        actions.append(f"provision:keep={kwargs['keep']}")
        try:
            yield
        finally:
            actions.append("cleanup")

    monkeypatch.setattr("nanolab.cli.product.provision_environment", provision)
    monkeypatch.setattr(
        "nanolab.cli.product._workflow",
        lambda *args, **kwargs: (
            actions.append(f"build:{kwargs['control_plane_url']}:{kwargs['prometheus_url']}")
            or workflow
        ),
    )
    monkeypatch.setattr(
        "nanolab.cli.product.resolve_loadtest_urls",
        lambda *args, **kwargs: (
            actions.append("resolve") or "http://stack:30080",
            "http://stack:30090",
        ),
    )
    # Forwarding the Prometheus port would open a real ssh to the fake host.
    monkeypatch.setattr(
        "nanolab.cli.product.prometheus_over_ssh",
        lambda _environment, url, **_kwargs: nullcontext(url),
    )
    # Sonata's run takes `select`; the point here is the ordering of provisioning
    # around it, not what it runs.
    monkeypatch.setattr(workflow, "run", lambda **_kwargs: actions.append("run"))
    monkeypatch.setattr(
        "nanolab.cli.product.git_provenance",
        lambda *args: actions.append("provenance")
        or {
            "git_commit": "abc",
            "git_dirty": False,
            "git_diff_sha256": "digest",
            "git_status": [],
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/autoscaling-cycle-k8s.yaml",
            "--environment",
            "environments/multipass.yaml",
            "--run-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert actions == [
        "provenance",
        "provision:keep=False",
        "resolve",
        "build:http://stack:30080:http://stack:30090",
        "run",
        "cleanup",
    ]
    metadata = json.loads((tmp_path / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["status"] == "passed"
    assert metadata["git_commit"]
    assert isinstance(metadata["git_dirty"], bool)
    assert metadata["git_diff_sha256"]
    assert isinstance(metadata["git_status"], list)
    assert metadata["scenario"]["config"]["workflow"] == "loadtest"
    assert metadata["environment"]["config"]["provider"] == "multipass"
    assert metadata["tasks"] == []


def test_git_provenance_fingerprints_tracked_and_untracked_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.name", "Test"), cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("base", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "base"), cwd=repo, check=True)

    clean = git_provenance(repo)
    tracked.write_text("changed", encoding="utf-8")
    tracked_change = git_provenance(repo)
    untracked = repo / "untracked.txt"
    untracked.write_text("one", encoding="utf-8")
    untracked_one = git_provenance(repo)
    untracked.write_text("two", encoding="utf-8")
    untracked_two = git_provenance(repo)

    assert clean["git_dirty"] is False
    assert tracked_change["git_diff_sha256"] != clean["git_diff_sha256"]
    assert untracked_one["git_diff_sha256"] != tracked_change["git_diff_sha256"]
    assert untracked_two["git_diff_sha256"] != untracked_one["git_diff_sha256"]


def test_failed_loadtest_writes_failure_metadata(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "nanolab.cli.product._workflow",
        lambda *args, **kwargs: _sonata_workflow(fails="load exploded"),
    )
    monkeypatch.setattr(
        "nanolab.cli.product.resolve_loadtest_urls",
        lambda *args, **kwargs: ("http://stack:30080", "http://stack:30090"),
    )

    result = CliRunner().invoke(
        app,
        [
            "run",
            "scenarios-v2/autoscaling-cycle-k8s.yaml",
            "--run-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    metadata = json.loads((tmp_path / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["error"] == "load exploded"
    assert metadata["tasks"][-1]["status"] == "failed"


def test_run_rejects_provisioning_for_local_environment() -> None:
    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/deployment-lifecycle-container.yaml", "--provision"],
    )

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_inspect_renders_validated_configuration() -> None:
    result = CliRunner().invoke(app, ["inspect", "scenarios-v2/cli-contract-k8s.yaml"])

    assert result.exit_code == 0
    assert '"workflow": "cli"' in result.stdout


def test_plan_can_select_one_task() -> None:
    result = CliRunner().invoke(
        app,
        ["plan", "scenarios-v2/deployment-lifecycle-k8s.yaml", "--only", "invoke-word-stats-java"],
    )

    assert result.exit_code == 0
    assert "invoke-word-stats-java" in result.stdout
    assert "build-image-word-stats-java" not in result.stdout


def test_plan_accepts_external_ssh_environment(tmp_path: Path) -> None:
    environment = tmp_path / "external.yaml"
    environment.write_text(
        "provider: external\nroles:\n  stack:\n    host: vm.example\n    user: ubuntu\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["plan", "scenarios-v2/deployment-lifecycle-k8s.yaml", "--environment", str(environment)],
    )

    assert result.exit_code == 0
    assert "001.check-kubectl-is-usable" in result.stdout


def test_plan_builds_loadtest_with_operational_defaults(tmp_path: Path) -> None:
    scenario = tmp_path / "autoscaling-cycle-k8s.yaml"
    scenario.write_text("workflow: loadtest\nfunctions:\n  - word-stats-java\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["plan", str(scenario)])

    assert result.exit_code == 0
    # The eight load steps are one composite now, so the plan names the load test
    # rather than its internals.
    assert "010.run-the-load-test" in result.stdout
    assert "008.acquire-helm-release-nanofaas" in result.stdout


def test_plan_renders_the_compiled_cli_workflow() -> None:
    result = CliRunner().invoke(app, ["plan", "scenarios-v2/cli-contract-container.yaml"])

    assert result.exit_code == 0, result.output
    assert "001.build-nanofaas-cli" in result.stdout
    assert "024.release-local-registry" in result.stdout


def test_plan_slices_the_cli_workflow_by_sonata_slug() -> None:
    result = CliRunner().invoke(
        app,
        ["plan", "scenarios-v2/cli-contract-container.yaml", "--only", "list-functions"],
    )

    assert result.exit_code == 0, result.output
    assert "acquire-local-control-plane" in result.stdout
    assert "acquire-word-stats-java" in result.stdout
    assert "list-functions" in result.stdout
    assert "release-word-stats-java" in result.stdout
    assert "release-local-control-plane" in result.stdout
    assert "build-nanofaas-cli" not in result.stdout


def test_run_passes_the_requested_selection_to_sonata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = MagicMock()
    observers = (object(),)
    monkeypatch.setattr(product_module, "_workflow", MagicMock(return_value=workflow))
    monkeypatch.setattr(
        product_module,
        "_workflow_observers",
        lambda _scenario_path: observers,
    )

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/cli-contract-container.yaml", "--only", "list-functions"],
    )

    assert result.exit_code == 0, result.output
    workflow.run.assert_called_once_with(
        select=Selection(only="list-functions", start=None, until=None),
        observers=observers,
    )


def test_plan_reports_an_invalid_sonata_slug_without_a_traceback() -> None:
    result = CliRunner().invoke(
        app,
        ["plan", "scenarios-v2/cli-contract-container.yaml", "--only", "cli.function.list"],
    )

    assert result.exit_code != 0
    assert "no task matches slug 'cli.function.list'" in result.output
    assert "Traceback" not in result.output
