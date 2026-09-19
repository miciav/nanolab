import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
import typer
from typer.testing import CliRunner


def _heap_analysis_scenario_yaml(tmp_path: Path) -> Path:
    """Build a minimal, fully valid heap-analysis scenario file for CLI tests.

    Every selected function needs its own role and image: `workload.rates`,
    `roles`, `images` and `functions` must all name the same set, or the
    scenario is rejected before a run can start.
    """
    scenario = tmp_path / "heap-analysis.yaml"
    scenario.write_text(
        textwrap.dedent(
            """
            workflow: heap-analysis
            backend: container
            functions: [word-stats-java, word-stats-javascript]
            heapAnalysis:
              target: control-plane
              warmup_s: 2
              steady_s: 4
              drain_s: 3
              roles:
                control-plane:
                  runtime: jvm
                  expected_cpu: 2.0
                  memory_limit_bytes: 1073741824
                  required_metrics: [process_rss_bytes, cgroup_memory_usage_bytes]
                  required_capabilities: [rss, cgroup]
                  runtime_options: [-Xmx512m]
                  collection_sources: [procfs, cgroup]
                word-stats-java:
                  runtime: jvm
                  expected_cpu: 2.0
                  memory_limit_bytes: 1073741824
                  required_metrics: [process_rss_bytes, cgroup_memory_usage_bytes]
                  required_capabilities: [rss, cgroup]
                  runtime_options: []
                  collection_sources: [procfs, cgroup]
                word-stats-javascript:
                  runtime: node
                  expected_cpu: 1.0
                  memory_limit_bytes: 536870912
                  required_metrics: [process_rss_bytes, cgroup_memory_usage_bytes]
                  required_capabilities: [rss, cgroup]
                  runtime_options: []
                  collection_sources: [procfs, cgroup]
              images:
                control-plane: {variant: jvm, platform: linux/amd64}
                word-stats-java: {variant: jvm, platform: linux/amd64}
                word-stats-javascript: {variant: default, platform: linux/amd64}
              workload:
                rates: {word-stats-java: 100, word-stats-javascript: 100}
                preallocated_vus: 200
                max_vus: 200
                max_error_ratio: 0
                max_dropped_iterations: 0
              max_dumps: 2
              max_dump_bytes: 1073741824
              artifact_limit_bytes: 4294967296
              diagnostic_timeout_s: 60
              mat_memory_mib: 2048
              mat_cpus: 2.0
              mat_timeout_s: 300
            """
        ).replace("DIGEST", "a" * 64)
    )
    return scenario


@pytest.mark.parametrize(
    ("status", "code"),
    [("PASS", 0), ("FAIL", 1), ("INCONCLUSIVE", 2), ("ABORTED", 130)],
)
def test_exit_codes_preserve_verdict(status, code):
    from nanolab.cli.soak import soak_exit_code

    assert soak_exit_code(status) == code


@pytest.mark.parametrize(
    "selection",
    [{"resume": True}, {"only": "steady"}, {"start": "drain"}, {"until": "steady"}],
)
def test_soak_cannot_resume_or_select_partial_lifetime(selection):
    from nanolab.cli.soak import validate_soak_selection

    options = {"resume": False, "only": None, "start": None, "until": None}
    options.update(selection)
    with pytest.raises(ValueError, match=r"soak requires its complete"):
        validate_soak_selection(**options)


def test_whole_run_selection_is_allowed():
    from nanolab.cli.soak import validate_soak_selection

    validate_soak_selection(resume=False, only=None, start=None, until=None)


def test_missing_or_invalid_terminal_never_passes(tmp_path):
    from nanolab.cli.soak import terminal_status

    assert terminal_status(tmp_path) == "INCONCLUSIVE"
    (tmp_path / "terminal.json").write_text('{"status":"PASS"}')
    assert terminal_status(tmp_path) == "INCONCLUSIVE"
    (tmp_path / "terminal.json").write_text(
        '{"schema":"nanolab-soak-v1","status":"PASS"}'
    )
    assert terminal_status(tmp_path) == "PASS"


def test_policy_overlay_is_explicit_and_only_replaces_criteria(tmp_path):
    from nanolab.cli.soak import load_soak_policy

    policy = tmp_path / "policy.yaml"
    policy.write_text("schema: nanolab-soak-policy-v1\ncriteria:\n  - id: chosen\n")
    data = {
        "workflow": "soak",
        "soakPolicyFile": "policy.yaml",
        "soak": {"criteria": [], "purpose": "p24"},
    }
    resolved, receipt = load_soak_policy(data, tmp_path / "scenario.yaml")
    assert receipt is not None
    soak = cast("dict[str, Any]", resolved["soak"])
    assert soak["criteria"] == [{"id": "chosen"}]
    assert soak["purpose"] == "p24"
    assert "soakPolicyFile" not in resolved
    assert data["soak"]["criteria"] == []
    assert receipt["sha256"] and receipt["resolved_criteria_fingerprint"]


def test_policy_cannot_override_images_or_timing(tmp_path):
    from nanolab.cli.soak import resolve_soak_policy

    (tmp_path / "policy.yaml").write_text(
        "schema: nanolab-soak-policy-v1\ncriteria: []\nimages: {}\n"
    )
    with pytest.raises(ValueError, match=r"soak policy must contain schema"):
        resolve_soak_policy(
            {"workflow": "soak", "soakPolicyFile": "policy.yaml", "soak": {}},
            tmp_path / "scenario.yaml",
        )


def test_missing_policy_is_not_filled_with_permissive_defaults(tmp_path):
    from nanolab.cli.soak import resolve_soak_policy

    with pytest.raises(ValueError, match="required soak policy file unavailable"):
        resolve_soak_policy(
            {"workflow": "soak", "soakPolicyFile": "missing.yaml", "soak": {}},
            tmp_path / "scenario.yaml",
        )


def test_soak_routes_before_comparison_and_plan_has_no_side_effects(
    tmp_path, monkeypatch
):
    from nanolab.cli import product

    calls = []
    module = ModuleType("nanolab.plans.soak")
    module.build_soak_plan = lambda *args, **kwargs: (  # pyright: ignore[reportAttributeAccessIssue]
        calls.append((args, kwargs)) or "soak-plan"
    )
    monkeypatch.setitem(sys.modules, "nanolab.plans.soak", module)
    monkeypatch.setattr(
        product, "build_role_bindings", lambda environment: ("bindings", "fetcher")
    )
    monkeypatch.setattr(
        product,
        "default_tool_paths",
        lambda: SimpleNamespace(
            nanofaas_root=tmp_path, tool_root=tmp_path, runs_dir=tmp_path / "runs"
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("soak must not fall through to a comparison/loadtest")

    monkeypatch.setattr(product, "is_runtime_comparison", forbidden)
    scenario, environment = (
        SimpleNamespace(workflow="soak", backend="container"),
        SimpleNamespace(provider="local"),
    )
    assert product._workflow(scenario, environment, dry_run=True) == "soak-plan"  # pyright: ignore[reportArgumentType]
    assert calls[0][0] == (scenario, environment, "bindings")
    assert not (tmp_path / "runs").exists()


def test_soak_default_run_directories_are_unique(tmp_path):
    from nanolab.cli.product import _default_run_dir

    assert _default_run_dir(None, "soak", tmp_path) != _default_run_dir(
        None, "soak", tmp_path
    )


def test_tui_forwards_soak_dry_run(monkeypatch):
    from nanolab.tui import app

    calls = []
    monkeypatch.setattr(
        app, "_workflow", lambda *args, **kwargs: calls.append(kwargs) or "soak"
    )
    assert (
        app.NanofaasTUI._build_workflow(
            SimpleNamespace(workflow="soak"), object(), dry_run=True
        )
        == "soak"
    )
    assert calls == [{"dry_run": True}]


def test_offline_command_is_registered_and_preserves_inconclusive(
    tmp_path, monkeypatch
):
    from nanolab.cli import product, soak
    from nanolab.tasks.soak.models import CriterionResult

    monkeypatch.setattr(
        soak,
        "evaluate_run",
        lambda *args: (
            CriterionResult("coverage", "INCONCLUSIVE", "missing receipts", ()),
        ),
    )
    report = tmp_path / "report.json"
    report.write_text("{}")
    monkeypatch.setattr(soak, "write_report", lambda *args: report)
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(app, ["soak-evaluate", str(tmp_path)])
    assert result.exit_code == 2
    assert "INCONCLUSIVE" in result.output
    assert str(report) in result.output


def test_soak_success_metadata_uses_terminal_status(tmp_path, monkeypatch):
    from nanolab.cli import product

    (tmp_path / "terminal.json").write_text(
        '{"schema":"nanolab-soak-v1","status":"INCONCLUSIVE"}'
    )
    calls = []
    monkeypatch.setattr(
        product, "_write_run_metadata", lambda *args, **kwargs: calls.append(kwargs)
    )
    product._write_success_metadata(
        tmp_path,
        started_at=None,  # pyright: ignore[reportArgumentType]
        scenario_path=Path("scenario.yaml"),
        scenario=SimpleNamespace(workflow="soak"),  # pyright: ignore[reportArgumentType]
        environment_path=None,
        environment=None,  # pyright: ignore[reportArgumentType]
        sink=None,  # pyright: ignore[reportArgumentType]
        provenance={},
    )
    assert calls[0]["status"] == "inconclusive"


def test_heap_analysis_routes_before_comparison_and_plan_has_no_side_effects(
    tmp_path, monkeypatch
):
    from nanolab.cli import product

    calls = []
    module = ModuleType("nanolab.plans.heap_analysis")
    module.build_heap_analysis_plan = lambda *args, **kwargs: (  # pyright: ignore[reportAttributeAccessIssue]
        calls.append((args, kwargs)) or "heap-analysis-plan"
    )
    module.unique_heap_analysis_run_dir = lambda runs_dir: runs_dir / "heap-analysis-x"  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "nanolab.plans.heap_analysis", module)
    monkeypatch.setattr(
        product, "build_role_bindings", lambda environment: ("bindings", "fetcher")
    )
    monkeypatch.setattr(
        product,
        "default_tool_paths",
        lambda: SimpleNamespace(
            nanofaas_root=tmp_path, tool_root=tmp_path, runs_dir=tmp_path / "runs"
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("heap-analysis must not fall through to a loadtest")

    monkeypatch.setattr(product, "is_runtime_comparison", forbidden)
    scenario, environment = (
        SimpleNamespace(workflow="heap-analysis"),
        SimpleNamespace(provider="local"),
    )
    assert (
        product._workflow(scenario, environment, dry_run=True)  # pyright: ignore[reportArgumentType]
        == "heap-analysis-plan"
    )
    assert calls[0][0] == (scenario, environment, "bindings")
    assert not (tmp_path / "runs").exists()


def test_heap_analysis_rejects_non_local_environment(monkeypatch):
    from nanolab.cli import product

    monkeypatch.setattr(
        product, "build_role_bindings", lambda environment: ("bindings", "fetcher")
    )
    scenario, environment = (
        SimpleNamespace(workflow="heap-analysis"),
        SimpleNamespace(provider="azure"),
    )
    with pytest.raises(ValueError, match="requires a local container environment"):
        product._workflow(scenario, environment)  # pyright: ignore[reportArgumentType]


def test_heap_analysis_default_run_directories_are_unique(tmp_path):
    from nanolab.cli.product import _default_run_dir

    first = _default_run_dir(None, "heap-analysis", tmp_path)
    second = _default_run_dir(None, "heap-analysis", tmp_path)
    assert first != second
    assert first is not None and first.name.startswith("heap-analysis-")


def test_run_rejects_heap_analysis_endpoint_override(tmp_path):
    from nanolab.cli import product

    scenario = _heap_analysis_scenario_yaml(tmp_path)
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(
        app, ["run", str(scenario), "--control-plane-url", "http://x"]
    )
    assert result.exit_code != 0
    assert "heap analysis owns its endpoints" in result.output


def test_run_rejects_keep_for_heap_analysis(tmp_path):
    from nanolab.cli import product

    scenario = _heap_analysis_scenario_yaml(tmp_path)
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(app, ["run", str(scenario), "--keep"])
    assert result.exit_code != 0
    assert "--keep is not supported for heap analysis" in result.output


def test_run_requires_docker_and_k6_for_heap_analysis(tmp_path, monkeypatch):
    from nanolab.cli import diagnostics, product

    scenario = _heap_analysis_scenario_yaml(tmp_path)
    monkeypatch.setattr(
        diagnostics, "missing_executables", lambda tools=(): ("docker",)
    )
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(app, ["run", str(scenario)])
    assert result.exit_code != 0
    assert "heap analysis requires local executables: docker" in result.output


def test_plan_rejects_heap_analysis_endpoint_override(tmp_path):
    from nanolab.cli import product

    scenario = _heap_analysis_scenario_yaml(tmp_path)
    app = typer.Typer()
    product.install_product_commands(app)
    result = CliRunner().invoke(
        app, ["plan", str(scenario), "--prometheus-url", "http://x"]
    )
    assert result.exit_code != 0
    assert "heap analysis owns its endpoints" in result.output
