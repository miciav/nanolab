from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
import subprocess
from typing import cast

import typer
import yaml
from sonata_engine import CompiledWorkflow, Selection, SelectionError
from sonata_engine import Workflow as SonataWorkflow
from sonata_engine.workflow.context import bind_workflow_sink as bind_sonata_sink
from workflow_tasks.loadtest.adapters import HttpPrometheusClient
from workflow_tasks.workflow.context import bind_workflow_sink

from nanolab.cli import diagnostics
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.cli.execution import build_role_bindings, resolve_loadtest_urls
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.cli.provisioning import provision_environment
from nanolab.plans.offload import build_offload_plan
from nanolab.plans.offload_loadtest import build_offload_loadtest_plan
from nanolab.plans.cli import build_cli_plan
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.plans.validate import build_validate_plan
from nanolab.workspace.paths import default_tool_paths, discover_tool_root


def uses_sonata(scenario: ScenarioConfig) -> bool:
    """Whether this scenario is already executed by Sonata."""
    return scenario.workflow == "cli"


def _read(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def _scenario(path: Path) -> ScenarioConfig:
    return ScenarioConfig.model_validate(_read(path))


def _environment(path: Path | None) -> EnvironmentConfig:
    return (
        EnvironmentConfig.model_validate(_read(path))
        if path
        else EnvironmentConfig(provider="local")
    )


def _workflow(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    control_plane_url: str | None = None,
    prometheus_url: str = "http://127.0.0.1:9090",
    run_dir: Path | None = None,
    dry_run: bool = False,
    provision: bool = False,
):
    bindings, fetcher = build_role_bindings(environment)
    paths = default_tool_paths()
    if scenario.workflow == "validate":
        return build_validate_plan(
            scenario,
            bindings,
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
        )
    if scenario.workflow == "offload":
        return build_offload_plan(scenario, bindings, repo_root=paths.nanofaas_root)
    if scenario.workflow == "offload-loadtest":
        return build_offload_loadtest_plan(
            scenario,
            environment,
            bindings,
            run_dir=run_dir or paths.runs_dir / "latest",
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
            fetcher=fetcher,
            dry_run=dry_run,
        )
    if scenario.workflow == "cli":
        return build_cli_plan(
            scenario,
            bindings,
            endpoint=control_plane_url,
            repo_root=paths.nanofaas_root,
            provision=provision,
            environment=environment,
        )
    return build_loadtest_plan(
        scenario,
        environment,
        bindings,
        control_plane_url=control_plane_url or "http://127.0.0.1:8080",
        prometheus_client=HttpPrometheusClient(prometheus_url),
        run_dir=run_dir or paths.runs_dir / "latest",
        fetcher=fetcher,
        repo_root=paths.nanofaas_root,
        tool_root=paths.tool_root,
    )


def _require_cli_endpoint(
    scenario: ScenarioConfig,
    control_plane_url: str | None,
    *,
    provision: bool = False,
) -> None:
    if (
        scenario.workflow == "cli"
        and scenario.backend == "k8s"
        and not provision
        and control_plane_url is None
    ):
        raise typer.BadParameter(
            "--control-plane-url is required for a k8s cli scenario"
        )


def _cli_provisioned(scenario: ScenarioConfig, *, provision: bool) -> bool:
    """Whether this run/plan is the Sonata-provisioned cli/k8s path.

    That path owns its own VM/Helm lifecycle (built into the compiled plan by
    `build_cli_plan`), so it must never also go through the legacy
    `provision_environment` context manager.
    """
    return provision and scenario.workflow == "cli" and scenario.backend == "k8s"


def _validate_cli_container_options(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    keep: bool = False,
) -> None:
    if scenario.workflow != "cli" or scenario.backend != "container":
        return
    if environment.provider != "local":
        raise typer.BadParameter(
            "cli container scenario requires a local environment"
        )
    if keep:
        raise typer.BadParameter(
            "--keep is not supported for a cli container scenario"
        )


def _render(workflow) -> None:
    for index, task in enumerate(workflow.tasks, start=1):
        typer.echo(f"{index:02d}  {task.task_id}  {task.title}")


def _render_compiled(compiled: CompiledWorkflow) -> None:
    for compiled_task in compiled.tasks:
        typer.echo(f"{compiled_task.task_id}  {compiled_task.task.title}")


def _slice(workflow, *, only: str | None, start: str | None, until: str | None):
    ids = [task.task_id for task in workflow.tasks]
    selected = ids
    sliced = any((only, start, until))
    if only:
        selected = [only]
    else:
        if start:
            selected = selected[ids.index(start) :]
        if until:
            selected = selected[: selected.index(until) + 1]
    unknown = set(selected) - set(ids)
    if unknown:
        raise ValueError(f"unknown task: {', '.join(sorted(unknown))}")
    workflow.tasks = [task for task in workflow.tasks if task.task_id in selected]
    if sliced:
        selected_set = set(selected)

        def acquired_by_selection(task) -> bool:
            cleanup_id = task.task_id
            candidates = {
                cleanup_id.replace(".delete.", ".register."),
                cleanup_id.replace(".uninstall.", ".deploy."),
            }
            return not candidates.isdisjoint(selected_set)

        workflow.cleanup_tasks = [
            task for task in workflow.cleanup_tasks if acquired_by_selection(task)
        ]
    return workflow


def _git_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _git_provenance(repo_root: Path) -> dict[str, object]:
    commit = _git_commit(repo_root)
    try:
        status = subprocess.run(
            ("git", "status", "--porcelain=v1"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        diff = subprocess.run(
            ("git", "diff", "--binary", "HEAD"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = subprocess.run(
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {"git_commit": commit, "git_dirty": None, "git_diff_sha256": None, "git_status": []}
    if status.returncode != 0 or diff.returncode != 0 or untracked.returncode != 0:
        return {"git_commit": commit, "git_dirty": None, "git_diff_sha256": None, "git_status": []}
    digest = hashlib.sha256((status.stdout + "\0" + diff.stdout).encode("utf-8"))
    for relative_path in filter(None, untracked.stdout.split("\0")):
        digest.update(b"\0untracked\0")
        digest.update(relative_path.encode("utf-8"))
        try:
            digest.update((repo_root / relative_path).read_bytes())
        except OSError:
            digest.update(b"\0unavailable")
    return {
        "git_commit": commit,
        "git_dirty": bool(status.stdout.strip()),
        "git_diff_sha256": digest.hexdigest(),
        "git_status": status.stdout.splitlines(),
    }


def _write_run_metadata(
    run_dir: Path,
    *,
    status: str,
    error: str | None,
    started_at: datetime,
    scenario_path: Path,
    scenario: ScenarioConfig,
    environment_path: Path | None,
    environment: EnvironmentConfig,
    sink: ConsoleProgressSink,
    provenance: dict[str, object],
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1,
        "status": status,
        "error": error,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        **provenance,
        "scenario": {
            "source": str(scenario_path),
            "config": scenario.model_dump(mode="json", by_alias=True),
        },
        "environment": {
            "source": str(environment_path) if environment_path else None,
            "config": environment.model_dump(mode="json", by_alias=True),
        },
        "tasks": sink.records,
    }
    (run_dir / "run-metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


def install_product_commands(app: typer.Typer) -> None:
    @app.command("run")
    def run_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path | None = typer.Option(None, "--environment", exists=True),
        provision: bool = typer.Option(False, "--provision"),
        keep: bool = typer.Option(False, "--keep"),
        only: str | None = typer.Option(
            None,
            "--only",
            help=(
                "Run a single task. Migrated workflows address tasks by title slug "
                "(e.g. list-functions) and do not run their prerequisites for you."
            ),
        ),
        start: str | None = typer.Option(
            None,
            "--from",
            help="Start from this task, inclusive. Same addressing as --only.",
        ),
        until: str | None = typer.Option(
            None,
            "--until",
            help="Stop after this task, inclusive. Same addressing as --only.",
        ),
        control_plane_url: str | None = typer.Option(None, "--control-plane-url"),
        prometheus_url: str | None = typer.Option(None, "--prometheus-url"),
        run_dir: Path | None = typer.Option(None, "--run-dir"),
    ) -> None:
        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        if provision and environment_config.provider == "local":
            raise typer.BadParameter("--provision requires a non-local environment")
        _validate_cli_container_options(
            scenario_config,
            environment_config,
            keep=keep,
        )
        _require_cli_endpoint(scenario_config, control_plane_url, provision=provision)
        cli_provisioned = _cli_provisioned(scenario_config, provision=provision)
        paths = default_tool_paths()
        effective_run_dir = run_dir
        if (
            scenario_config.workflow in ("loadtest", "offload-loadtest")
            and effective_run_dir is None
        ):
            effective_run_dir = paths.runs_dir / "latest"
        sink = ConsoleProgressSink()
        started_at = datetime.now(UTC)
        provenance = _git_provenance(paths.nanofaas_root)
        try:
            # Both binds are required, and not just during the migration: the
            # legacy contextvar is read directly by SubprocessShell._emit_output
            # (workflow_tasks/shell.py), which every command execution goes
            # through regardless of which engine's workflow issued it. Drop the
            # legacy bind only if that execution layer itself stops routing
            # through workflow_log.
            with bind_workflow_sink(sink), bind_sonata_sink(sink):
                provisioning = (
                    provision_environment(
                        scenario_config,
                        environment_config,
                        repo_root=paths.nanofaas_root,
                        keep=keep,
                    )
                    if provision and not cli_provisioned
                    else nullcontext()
                )
                with provisioning:
                    if scenario_config.workflow == "loadtest":
                        control_plane_url, prometheus_url = resolve_loadtest_urls(
                            environment_config,
                            control_plane_url=control_plane_url,
                            prometheus_url=prometheus_url,
                        )
                    workflow = _workflow(
                        scenario_config,
                        environment_config,
                        control_plane_url=control_plane_url,
                        prometheus_url=prometheus_url or "http://127.0.0.1:9090",
                        run_dir=effective_run_dir,
                        provision=provision,
                    )
                    if uses_sonata(scenario_config):
                        sonata_workflow = cast(SonataWorkflow, workflow)
                        sonata_workflow.keep_infrastructure = keep
                        try:
                            sonata_workflow.run(
                                select=Selection(only=only, start=start, until=until)
                            )
                        except SelectionError as error:
                            raise typer.BadParameter(str(error)) from None
                    else:
                        workflow = _slice(workflow, only=only, start=start, until=until)
                        workflow.keep_infrastructure = keep
                        workflow.run()
        except BaseException as exc:
            if effective_run_dir is not None:
                try:
                    _write_run_metadata(
                        effective_run_dir,
                        status="failed",
                        error=str(exc),
                        started_at=started_at,
                        scenario_path=scenario,
                        scenario=scenario_config,
                        environment_path=environment,
                        environment=environment_config,
                        sink=sink,
                        provenance=provenance,
                    )
                except OSError:
                    pass
            raise
        else:
            if effective_run_dir is not None:
                _write_run_metadata(
                    effective_run_dir,
                    status="passed",
                    error=None,
                    started_at=started_at,
                    scenario_path=scenario,
                    scenario=scenario_config,
                    environment_path=environment,
                    environment=environment_config,
                    sink=sink,
                    provenance=provenance,
                )

    @app.command("plan")
    def plan_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path | None = typer.Option(None, "--environment", exists=True),
        provision: bool = typer.Option(False, "--provision"),
        only: str | None = typer.Option(
            None,
            "--only",
            help=(
                "Run a single task. Migrated workflows address tasks by title slug "
                "(e.g. list-functions) and do not run their prerequisites for you."
            ),
        ),
        start: str | None = typer.Option(
            None,
            "--from",
            help="Start from this task, inclusive. Same addressing as --only.",
        ),
        until: str | None = typer.Option(
            None,
            "--until",
            help="Stop after this task, inclusive. Same addressing as --only.",
        ),
        control_plane_url: str | None = typer.Option(None, "--control-plane-url"),
        prometheus_url: str | None = typer.Option(None, "--prometheus-url"),
        run_dir: Path | None = typer.Option(None, "--run-dir"),
    ) -> None:
        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        if provision and environment_config.provider == "local":
            raise typer.BadParameter("--provision requires a non-local environment")
        _validate_cli_container_options(scenario_config, environment_config)
        _require_cli_endpoint(scenario_config, control_plane_url, provision=provision)
        if scenario_config.workflow == "loadtest":
            control_plane_url, prometheus_url = resolve_loadtest_urls(
                environment_config,
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url,
                dry_run=True,
            )
        workflow = _workflow(
            scenario_config,
            environment_config,
            control_plane_url=control_plane_url,
            provision=provision,
            prometheus_url=prometheus_url or "http://127.0.0.1:9090",
            run_dir=run_dir,
            dry_run=True,
        )
        if uses_sonata(scenario_config):
            sonata_workflow = cast(SonataWorkflow, workflow)
            try:
                compiled = sonata_workflow.compile(
                    select=Selection(only=only, start=start, until=until)
                )
            except SelectionError as error:
                raise typer.BadParameter(str(error)) from None
            _render_compiled(compiled)
        else:
            _render(_slice(workflow, only=only, start=start, until=until))

    @app.command("list")
    def list_command() -> None:
        for path in sorted((discover_tool_root() / "scenarios-v2").glob("*.yaml")):
            typer.echo(path)

    @app.command("inspect")
    def inspect_command(scenario: Path = typer.Argument(..., exists=True)) -> None:
        typer.echo(json.dumps(_scenario(scenario).model_dump(by_alias=True), indent=2))

    @app.command("doctor")
    def doctor_command() -> None:
        missing = diagnostics.missing_executables()
        if missing:
            raise typer.BadParameter(f"missing executables: {', '.join(missing)}")
        typer.echo("ok")
