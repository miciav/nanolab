from __future__ import annotations

import json
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import tempfile
from typing import cast

import typer
import yaml
from sonata_engine import (
    CompiledWorkflow,
    Selection,
    SelectionError,
    UnknownRetainedResourceError,
    WorkflowObserver,
    release_retained,
)
from sonata_engine import Workflow as SonataWorkflow
from sonata_engine.journal import JournalConfig
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_tasks.loadtest.adapters import HttpPrometheusClient
from sonata_tasks.provisioning.providers import provider_for
from sonata_tasks.telegram import telegram_observer_from_environment

from nanolab.cli import diagnostics
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.cli.execution import (
    build_role_bindings,
    prometheus_over_ssh,
    resolve_loadtest_urls,
)
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.cli.provisioning import provision_environment
from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.plans.offload import build_offload_plan
from nanolab.plans.offload_loadtest import build_offload_loadtest_plan, format_offload_summary
from nanolab.plans.cli import build_cli_plan
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.plans.release import (
    ReleaseRequest,
    build_release_request,
    build_release_workflow,
    release_journal_config,
    release_verifiers,
)
from nanolab.release.resources import build_release_resources
from nanolab.release.tasks import versioned_release_run_dir
from nanolab.release.versioning import normalize_version
from nanolab.release.environment import (
    ReleaseRunInProgressError,
    release_lock_path,
    release_run_lock,
)
from nanolab.plans.validate import build_validate_plan
from nanolab.workspace.paths import ToolPaths, default_tool_paths, discover_tool_root
from nanolab.workspace.provenance import git_provenance


_JOURNAL_FILENAME = "sonata.jsonl"
_LOCAL_PROMETHEUS_URL = "http://127.0.0.1:9090"


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


def _workflow_observers(scenario_path: Path) -> tuple[WorkflowObserver, ...]:
    observer = telegram_observer_from_environment(scenario_path.name)
    return (observer,) if observer is not None else ()


def _print_offload_summary(run_dir: Path) -> None:
    path = run_dir / "offload-report.json"
    if not path.is_file():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    numbers = report.get("numbers")
    if isinstance(numbers, dict):
        typer.echo(format_offload_summary({key: float(value) for key, value in numbers.items()}))


def _workflow(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    control_plane_url: str | None = None,
    prometheus_url: str = _LOCAL_PROMETHEUS_URL,
    run_dir: Path | None = None,
    dry_run: bool = False,
):
    bindings, fetcher = build_role_bindings(environment)
    paths = default_tool_paths()
    if scenario.workflow == "validate":
        return build_validate_plan(
            scenario,
            bindings,
            repo_root=paths.nanofaas_root,
            tool_root=paths.tool_root,
            environment=environment,
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


def _teardown_release(
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    run_dir: Path | None,
) -> None:
    """Give back what a `--keep` run held on to, without re-running anything.

    Deliberately skips `build_release_request`: that preflight wants a clean
    nanoFaaS tree, a matching prepared version, credential files and a non-empty
    image matrix. After a failed release the operator has usually dirtied the
    tree or moved the version on -- and the VMs still need closing. The journal
    already records what was retained and the values their releases need, so the
    version in the scenario is enough to find it.
    """
    if scenario_config.release is None:
        raise typer.BadParameter("--teardown requires a release scenario")
    paths = default_tool_paths()
    version = normalize_version(scenario_config.release.version)[0]
    release_dir = versioned_release_run_dir(run_dir or paths.runs_dir / "release", version)
    journal_paths = [release_dir / _JOURNAL_FILENAME]
    journal_paths.extend(
        path / _JOURNAL_FILENAME
        for path in sorted(release_dir.parent.glob(f"{release_dir.name}.superseded-*"))
        if (path / _JOURNAL_FILENAME).is_file()
    )
    journals = tuple(JournalConfig(path) for path in journal_paths if path.is_file())
    if not journals:
        typer.echo(f"nothing to tear down: no release journal at {journal_paths[0]}")
        return

    provider = provider_for(vm_request_for_role(environment_config, "stack"), paths.tool_root)
    resources = build_release_resources(environment_config, paths.nanofaas_root, provider)
    by_title = {resource.title: resource for resource in (*resources.vms, resources.endpoints)}
    unknown: list[UnknownRetainedResourceError] = []
    released: list[str] = []
    with release_run_lock(release_lock_path(environment_config)):
        for journal in journals:
            try:
                released.extend(release_retained(by_title, journal))
            except UnknownRetainedResourceError as error:
                # Sonata releases everything it was given before reporting the rest,
                # and the rest is always in-VM state: buildx builders, the registry
                # tunnel, the staged source. Destroying the VMs above took them with
                # it, so this is a note, not a failure -- a teardown that exits
                # non-zero every time is a teardown nobody reads.
                unknown.append(error)
    for title in released:
        typer.echo(f"released: {title}")
    for error in unknown:
        typer.echo(f"note: {error}")
        typer.echo("those live inside the released VMs and went with them")
    if not released and not unknown:
        typer.echo("nothing to tear down: the journal records nothing still held")


def _supersede_release_run(release_dir: Path) -> Path | None:
    """Move a previous run aside so a fresh one starts clean, without erasing it.

    Rotating rather than deleting, because the journal never sits alone: the phase
    receipts the publication barriers read live beside it. Removing only the
    journal would leave those unattributed, and removing the directory would
    destroy the record of a failed release -- past the publish phases, the only
    local trace of what was pushed. Returns the new location, or None when there
    was nothing to move.
    """
    if not release_dir.is_dir() or not any(release_dir.iterdir()):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    superseded = release_dir.with_name(f"{release_dir.name}.superseded-{stamp}")
    attempt = 1
    while superseded.exists():
        attempt += 1
        superseded = release_dir.with_name(f"{release_dir.name}.superseded-{stamp}-{attempt}")
    release_dir.rename(superseded)
    return superseded


def _release_request(
    scenario_path: Path,
    environment_path: Path | None,
    release_config: Path | None,
    run_dir: Path | None,
    *,
    executable: bool,
    source_tree: Path,
) -> tuple[ReleaseRequest, object]:
    """Validate every local release input offline, then bind the VM provider."""
    if environment_path is None:
        raise typer.BadParameter("release workflow requires --environment")
    paths = default_tool_paths()
    try:
        request = build_release_request(
            repo_root=paths.tool_root,
            nanofaas_root=paths.nanofaas_root,
            scenario_path=scenario_path,
            environment_path=environment_path,
            release_config_path=release_config,
            run_dir=run_dir or paths.runs_dir / "release",
            performance_root=paths.nanofaas_root / "docs" / "performance",
            source_tree=source_tree,
            executable=executable,
        )
    except (ValueError, subprocess.CalledProcessError) as error:
        raise typer.BadParameter(str(error)) from None
    return request, provider_for(vm_request_for_role(request.environment, "stack"), request.repo_root)


def _require_cli_endpoint(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    control_plane_url: str | None,
) -> None:
    if (
        scenario.workflow == "cli"
        and scenario.backend == "k8s"
        and environment.provider == "local"
        and control_plane_url is None
    ):
        raise typer.BadParameter("--control-plane-url is required for a k8s cli scenario")


def _validate_cli_container_options(
    scenario: ScenarioConfig,
    environment: EnvironmentConfig,
    *,
    keep: bool = False,
) -> None:
    if scenario.workflow != "cli" or scenario.backend != "container":
        return
    if environment.provider != "local":
        raise typer.BadParameter("cli container scenario requires a local environment")
    if keep:
        raise typer.BadParameter("--keep is not supported for a cli container scenario")


def _require_local_loadtest_tools(scenario: ScenarioConfig, environment: EnvironmentConfig) -> None:
    if scenario.workflow != "loadtest" or scenario.backend != "container":
        return
    if environment.provider != "local":
        return
    if diagnostics.missing_executables(("k6",)):
        raise typer.BadParameter("container load-test requires k6 on the host")


def _render_compiled(compiled: CompiledWorkflow) -> None:
    for compiled_task in compiled.tasks:
        typer.echo(f"{compiled_task.task_id}  {compiled_task.task.title}")




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
    (run_dir / "run-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _default_run_dir(run_dir: Path | None, workflow: str, runs_dir: Path) -> Path | None:
    if run_dir is None and workflow in ("loadtest", "offload-loadtest"):
        return runs_dir / "latest"
    return run_dir


def _run_teardown(
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    run_dir: Path | None,
    *,
    resume: bool,
    keep: bool,
    only: str | None,
    start: str | None,
    until: str | None,
    release_config: Path | None,
) -> None:
    if resume or keep or only or start or until:
        raise typer.BadParameter(
            "--teardown releases what a kept run held and runs nothing else; it "
            "cannot be combined with --resume, --keep, --only, --from or --until"
        )
    if release_config is not None:
        typer.echo("--teardown ignores --release-config; it releases resources only")
    try:
        _teardown_release(scenario_config, environment_config, run_dir)
    except ReleaseRunInProgressError as error:
        raise typer.BadParameter(str(error)) from None


def _prepare_run(
    lifetime: ExitStack,
    *,
    release: bool,
    scenario: Path,
    environment: Path | None,
    release_config: Path | None,
    run_dir: Path | None,
    resume: bool,
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    paths: ToolPaths,
    effective_run_dir: Path | None,
) -> tuple[
    ConsoleProgressSink,
    datetime,
    dict[str, object],
    Path | None,
    ReleaseRequest | None,
    object | None,
    JournalConfig | None,
]:
    """Enter the release lock and scratch tree, then start the run clock.

    Everything acquired here lives on `lifetime`; the caller closes it on every
    exit path, so a preflight rejection cannot leak the extracted tree.
    """
    release_request: ReleaseRequest | None = None
    release_provider: object | None = None
    release_journal: JournalConfig | None = None
    if release:
        lifetime.enter_context(release_run_lock(release_lock_path(environment_config)))
        source_tree = Path(
            lifetime.enter_context(
                tempfile.TemporaryDirectory(prefix="nanofaas-release-")
            )
        )
        release_request, release_provider = _release_request(
            scenario,
            environment,
            release_config,
            run_dir,
            executable=True,
            source_tree=source_tree,
        )
        release_journal = release_journal_config(release_request)
        # Evidence, receipts and metadata all live beside the journal, one
        # directory per prepared version -- never a reused `latest`.
        effective_run_dir = release_journal.path.parent
        if resume and not release_journal.path.is_file():
            # Outside the raised message on purpose: Rich hard-wraps a long
            # path inside its error box and splits it across the borders.
            typer.echo(f"no release journal at: {release_journal.path}")
            raise typer.BadParameter("--resume requires an existing release journal")
        if not resume:
            superseded = _supersede_release_run(effective_run_dir)
            if superseded is not None:
                typer.echo(f"previous release run moved aside: {superseded}")
    sink = ConsoleProgressSink()
    started_at = datetime.now(UTC)
    provenance = git_provenance(paths.nanofaas_root)
    return (
        sink,
        started_at,
        provenance,
        effective_run_dir,
        release_request,
        release_provider,
        release_journal,
    )


def _provisioning_context(
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    paths: ToolPaths,
    keep: bool,
):
    if (
        scenario_config.workflow != "release"
        and environment_config.provider != "local"
        and not (
            scenario_config.workflow == "cli" and scenario_config.backend == "k8s"
        )
    ):
        return provision_environment(
            scenario_config,
            environment_config,
            repo_root=paths.nanofaas_root,
            keep=keep,
        )
    return nullcontext()


def _forward_loadtest_urls(
    environment_config: EnvironmentConfig,
    scenario_config: ScenarioConfig,
    control_plane_url: str | None,
    prometheus_url: str | None,
    forwarding: ExitStack,
) -> tuple[str | None, str | None]:
    chosen_prometheus_url = prometheus_url
    control_plane_url, prometheus_url = resolve_loadtest_urls(
        environment_config,
        backend=scenario_config.backend or "k8s",
        control_plane_url=control_plane_url,
        prometheus_url=prometheus_url,
    )
    prometheus_url = forwarding.enter_context(
        prometheus_over_ssh(
            environment_config,
            prometheus_url,
            enabled=chosen_prometheus_url is None,
        )
    )
    return control_plane_url, prometheus_url


def _build_run_workflow(
    *,
    release_request: ReleaseRequest | None,
    release_provider: object | None,
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    control_plane_url: str | None,
    prometheus_url: str | None,
    effective_run_dir: Path | None,
) -> SonataWorkflow:
    if release_request is not None:
        return build_release_workflow(release_request, provider=release_provider)
    return cast(
        SonataWorkflow,
        _workflow(
            scenario_config,
            environment_config,
            control_plane_url=control_plane_url,
            prometheus_url=prometheus_url or _LOCAL_PROMETHEUS_URL,
            run_dir=effective_run_dir,
        ),
    )


def _run_selected_workflow(
    sonata_workflow: SonataWorkflow,
    *,
    release_request: ReleaseRequest | None,
    release_provider: object | None,
    release_journal: JournalConfig | None,
    resume: bool,
    selection: Selection,
    observers: tuple[WorkflowObserver, ...],
) -> None:
    if release_request is not None:
        verifiers = release_verifiers(release_request, release_provider)
        if observers:
            sonata_workflow.run(
                journal=release_journal,
                resume=resume,
                verifiers=verifiers,
                select=selection,
                observers=observers,
            )
        else:
            sonata_workflow.run(
                journal=release_journal,
                resume=resume,
                verifiers=verifiers,
                select=selection,
            )
    elif observers:
        sonata_workflow.run(select=selection, observers=observers)
    else:
        sonata_workflow.run(select=selection)


def _execute_workflow(
    *,
    sink: ConsoleProgressSink,
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    paths: ToolPaths,
    keep: bool,
    control_plane_url: str | None,
    prometheus_url: str | None,
    effective_run_dir: Path | None,
    only: str | None,
    start: str | None,
    until: str | None,
    scenario: Path,
    release_request: ReleaseRequest | None,
    release_provider: object | None,
    release_journal: JournalConfig | None,
    resume: bool,
) -> None:
    # Command output routing (SubprocessShell._emit_output) reads the
    # same contextvar, so one bind covers the whole execution layer.
    with bind_workflow_sink(sink):
        provisioning = _provisioning_context(
            scenario_config,
            environment_config,
            paths,
            keep,
        )
        with provisioning, ExitStack() as forwarding:
            if scenario_config.workflow == "loadtest":
                control_plane_url, prometheus_url = _forward_loadtest_urls(
                    environment_config,
                    scenario_config,
                    control_plane_url,
                    prometheus_url,
                    forwarding,
                )
            sonata_workflow = _build_run_workflow(
                release_request=release_request,
                release_provider=release_provider,
                scenario_config=scenario_config,
                environment_config=environment_config,
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url,
                effective_run_dir=effective_run_dir,
            )
            sonata_workflow.keep = keep
            selection = Selection(only=only, start=start, until=until)
            observers = _workflow_observers(scenario)
            try:
                _run_selected_workflow(
                    sonata_workflow,
                    release_request=release_request,
                    release_provider=release_provider,
                    release_journal=release_journal,
                    resume=resume,
                    selection=selection,
                    observers=observers,
                )
                if (
                    scenario_config.workflow == "offload-loadtest"
                    and effective_run_dir is not None
                ):
                    _print_offload_summary(effective_run_dir)
            except SelectionError as error:
                raise typer.BadParameter(str(error)) from None


def _write_failure_metadata(
    effective_run_dir: Path | None,
    exc: BaseException,
    *,
    started_at: datetime,
    scenario_path: Path,
    scenario: ScenarioConfig,
    environment_path: Path | None,
    environment: EnvironmentConfig,
    sink: ConsoleProgressSink,
    provenance: dict[str, object],
) -> None:
    if effective_run_dir is not None:
        try:
            _write_run_metadata(
                effective_run_dir,
                status="failed",
                error=str(exc),
                started_at=started_at,
                scenario_path=scenario_path,
                scenario=scenario,
                environment_path=environment_path,
                environment=environment,
                sink=sink,
                provenance=provenance,
            )
        except OSError:
            pass


def _write_success_metadata(
    effective_run_dir: Path | None,
    *,
    started_at: datetime,
    scenario_path: Path,
    scenario: ScenarioConfig,
    environment_path: Path | None,
    environment: EnvironmentConfig,
    sink: ConsoleProgressSink,
    provenance: dict[str, object],
) -> None:
    if effective_run_dir is not None:
        _write_run_metadata(
            effective_run_dir,
            status="passed",
            error=None,
            started_at=started_at,
            scenario_path=scenario_path,
            scenario=scenario,
            environment_path=environment_path,
            environment=environment,
            sink=sink,
            provenance=provenance,
        )


def install_product_commands(app: typer.Typer) -> None:
    @app.command("run")
    def run_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path | None = typer.Option(None, "--environment", exists=True),
        keep: bool = typer.Option(False, "--keep"),
        teardown: bool = typer.Option(
            False,
            "--teardown",
            help="Release what a --keep run held on to, then exit. Runs no workflow.",
        ),
        resume: bool = typer.Option(False, "--resume"),
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
        run_dir: Path | None = typer.Option(
            None,
            "--run-dir",
            help=(
                "Where results are written. A release treats this as a root and "
                "writes each run under releases/<version>/."
            ),
        ),
        release_config: Path | None = typer.Option(None, "--release-config", exists=True),
    ) -> None:
        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        release = scenario_config.workflow == "release"
        if release and environment is None:
            raise typer.BadParameter("release workflow requires --environment")
        if teardown:
            _run_teardown(
                scenario_config,
                environment_config,
                run_dir,
                resume=resume,
                keep=keep,
                only=only,
                start=start,
                until=until,
                release_config=release_config,
            )
            return
        if resume and not release:
            raise typer.BadParameter("--resume is only supported for release workflows")
        _validate_cli_container_options(
            scenario_config,
            environment_config,
            keep=keep,
        )
        _require_local_loadtest_tools(scenario_config, environment_config)
        _require_cli_endpoint(scenario_config, environment_config, control_plane_url)
        paths = default_tool_paths()
        effective_run_dir = _default_run_dir(run_dir, scenario_config.workflow, paths.runs_dir)
        # The extracted tree is throwaway, but the `finally` below only closes
        # it once the whole release run finishes, so it is held (~15 MB) for
        # the run's entire duration, not just through workflow compilation.
        # A dedicated guard covers the preflight plus the setup that follows
        # it (sink/started_at/provenance, the latter shelling out to git): it
        # must not route through the workflow-execution try/except below,
        # which would write a failure run-metadata file for a plain preflight
        # rejection. This guard, plus the `finally` on the workflow-execution
        # try below, together close the stack on every exit path from here on.
        lifetime = ExitStack()
        try:
            (
                sink,
                started_at,
                provenance,
                effective_run_dir,
                release_request,
                release_provider,
                release_journal,
            ) = _prepare_run(
                lifetime,
                release=release,
                scenario=scenario,
                environment=environment,
                release_config=release_config,
                run_dir=run_dir,
                resume=resume,
                scenario_config=scenario_config,
                environment_config=environment_config,
                paths=paths,
                effective_run_dir=effective_run_dir,
            )
        except ReleaseRunInProgressError as error:
            lifetime.close()
            raise typer.BadParameter(str(error)) from None
        except BaseException:
            lifetime.close()
            raise
        try:
            _execute_workflow(
                sink=sink,
                scenario_config=scenario_config,
                environment_config=environment_config,
                paths=paths,
                keep=keep,
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url,
                effective_run_dir=effective_run_dir,
                only=only,
                start=start,
                until=until,
                scenario=scenario,
                release_request=release_request,
                release_provider=release_provider,
                release_journal=release_journal,
                resume=resume,
            )
        except ReleaseRunInProgressError as error:
            raise typer.BadParameter(str(error)) from None
        except BaseException as exc:
            _write_failure_metadata(
                effective_run_dir,
                exc,
                started_at=started_at,
                scenario_path=scenario,
                scenario=scenario_config,
                environment_path=environment,
                environment=environment_config,
                sink=sink,
                provenance=provenance,
            )
            raise
        else:
            _write_success_metadata(
                effective_run_dir,
                started_at=started_at,
                scenario_path=scenario,
                scenario=scenario_config,
                environment_path=environment,
                environment=environment_config,
                sink=sink,
                provenance=provenance,
            )
        finally:
            lifetime.close()

    @app.command("plan")
    def plan_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path | None = typer.Option(None, "--environment", exists=True),
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
        run_dir: Path | None = typer.Option(
            None,
            "--run-dir",
            help=(
                "Where results are written. A release treats this as a root and "
                "writes each run under releases/<version>/."
            ),
        ),
        release_config: Path | None = typer.Option(None, "--release-config", exists=True),
    ) -> None:
        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        _validate_cli_container_options(scenario_config, environment_config)
        _require_cli_endpoint(scenario_config, environment_config, control_plane_url)
        if scenario_config.workflow == "loadtest":
            control_plane_url, prometheus_url = resolve_loadtest_urls(
                environment_config,
                backend=scenario_config.backend or "k8s",
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url,
                dry_run=True,
            )
        if scenario_config.workflow == "release":
            with tempfile.TemporaryDirectory(prefix="nanofaas-plan-") as source_tree:
                request, provider = _release_request(
                    scenario, environment, release_config, run_dir,
                    executable=False,
                    source_tree=Path(source_tree),
                )
                sonata_workflow = build_release_workflow(request, provider=provider)
                try:
                    compiled = sonata_workflow.compile(
                        select=Selection(only=only, start=start, until=until)
                    )
                except SelectionError as error:
                    raise typer.BadParameter(str(error)) from None
            _render_compiled(compiled)
            return
        sonata_workflow = cast(
            SonataWorkflow,
            _workflow(
                scenario_config,
                environment_config,
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url or _LOCAL_PROMETHEUS_URL,
                run_dir=run_dir,
                dry_run=True,
            ),
        )
        try:
            compiled = sonata_workflow.compile(
                select=Selection(only=only, start=start, until=until)
            )
        except SelectionError as error:
            raise typer.BadParameter(str(error)) from None
        _render_compiled(compiled)

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
