from __future__ import annotations

import hashlib
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
    release_retained,
)
from sonata_engine import Workflow as SonataWorkflow
from sonata_engine.journal import JournalConfig
from sonata_engine.workflow.context import bind_workflow_sink as bind_sonata_sink
from workflow_tasks.loadtest.adapters import HttpPrometheusClient
from workflow_tasks.workflow.context import bind_workflow_sink

from nanolab.cli import diagnostics
from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.cli.execution import build_role_bindings, resolve_loadtest_urls
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.cli.provisioning import provision_environment
from nanolab.cli.vm_provider import vm_provider_for_environment
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
from nanolab.workspace.paths import default_tool_paths, discover_tool_root


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
    journal = JournalConfig(
        versioned_release_run_dir(run_dir or paths.runs_dir / "release", version)
        / "sonata.jsonl"
    )
    if not journal.path.is_file():
        typer.echo(f"nothing to tear down: no release journal at {journal.path}")
        return

    provider = vm_provider_for_environment(environment_config, paths.tool_root)
    resources = build_release_resources(environment_config, paths.nanofaas_root, provider)
    by_title = {resource.title: resource for resource in (*resources.vms, resources.endpoints)}
    unknown: UnknownRetainedResourceError | None = None
    with release_run_lock(release_lock_path(environment_config)):
        try:
            released = release_retained(by_title, journal)
        except UnknownRetainedResourceError as error:
            # Sonata releases everything it was given before reporting the rest,
            # and the rest is always in-VM state: buildx builders, the registry
            # tunnel, the staged source. Destroying the VMs above took them with
            # it, so this is a note, not a failure -- a teardown that exits
            # non-zero every time is a teardown nobody reads.
            unknown = error
            released = ()
    for title in released:
        typer.echo(f"released: {title}")
    if unknown is not None:
        typer.echo(f"note: {unknown}")
        typer.echo("those live inside the released VMs and went with them")
    if not released and unknown is None:
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
    return request, vm_provider_for_environment(request.environment, request.repo_root)


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
        raise typer.BadParameter("--control-plane-url is required for a k8s cli scenario")


def _cli_provisioned(scenario: ScenarioConfig, *, provision: bool) -> bool:
    """Whether this run/plan is the Sonata-provisioned cli/k8s path.

    That path owns its own VM/Helm lifecycle (built into the compiled plan by
    `build_cli_plan`), so it must never also go through the legacy
    `provision_environment` context manager.
    """
    return provision and scenario.workflow == "cli" and scenario.backend == "k8s"


def _uses_legacy_provisioning(
    scenario: ScenarioConfig,
    *,
    provision: bool,
    cli_provisioned: bool,
) -> bool:
    return provision and scenario.workflow != "release" and not cli_provisioned


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


def _render_compiled(compiled: CompiledWorkflow) -> None:
    for compiled_task in compiled.tasks:
        typer.echo(f"{compiled_task.task_id}  {compiled_task.task.title}")


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
    (run_dir / "run-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def install_product_commands(app: typer.Typer) -> None:
    @app.command("run")
    def run_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path | None = typer.Option(None, "--environment", exists=True),
        provision: bool = typer.Option(False, "--provision"),
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
            if provision or resume or keep or only or start or until:
                raise typer.BadParameter(
                    "--teardown releases what a kept run held and runs nothing else; it "
                    "cannot be combined with --provision, --resume, --keep, --only, "
                    "--from or --until"
                )
            if release_config is not None:
                typer.echo("--teardown ignores --release-config; it releases resources only")
            try:
                _teardown_release(scenario_config, environment_config, run_dir)
            except ReleaseRunInProgressError as error:
                raise typer.BadParameter(str(error)) from None
            return
        if resume and not release:
            raise typer.BadParameter("--resume is only supported for release workflows")
        if release and not (provision or resume):
            raise typer.BadParameter(
                "a fresh release run requires --provision; pass --resume to continue an "
                "existing release"
            )
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
        if effective_run_dir is None and scenario_config.workflow in (
            "loadtest",
            "offload-loadtest",
        ):
            effective_run_dir = paths.runs_dir / "latest"
        release_request: ReleaseRequest | None = None
        release_provider: object | None = None
        release_journal = None
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
            if release:
                source_tree = Path(
                    lifetime.enter_context(
                        tempfile.TemporaryDirectory(prefix="nanofaas-release-")
                    )
                )
                release_request, release_provider = _release_request(
                    scenario, environment, release_config, run_dir,
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
            provenance = _git_provenance(paths.nanofaas_root)
        except BaseException:
            lifetime.close()
            raise
        try:
            # Both binds are required, and not just during the migration: the
            # legacy contextvar is read directly by SubprocessShell._emit_output
            # (workflow_tasks/shell.py), which every command execution goes
            # through regardless of which engine's workflow issued it. Drop the
            # legacy bind only if that execution layer itself stops routing
            # through workflow_log.
            with bind_workflow_sink(sink), bind_sonata_sink(sink):
                if _uses_legacy_provisioning(
                    scenario_config, provision=provision, cli_provisioned=cli_provisioned
                ):
                    provisioning = provision_environment(
                        scenario_config,
                        environment_config,
                        repo_root=paths.nanofaas_root,
                        keep=keep,
                    )
                elif release_request is not None:
                    # A release owns its VMs inside the Sonata DAG; the outer
                    # context only holds the lock that keeps two coordinators off
                    # the same Azure VMs.
                    provisioning = release_run_lock(
                        release_lock_path(release_request.environment)
                    )
                else:
                    provisioning = nullcontext()
                with provisioning:
                    if scenario_config.workflow == "loadtest":
                        control_plane_url, prometheus_url = resolve_loadtest_urls(
                            environment_config,
                            backend=scenario_config.backend or "k8s",
                            control_plane_url=control_plane_url,
                            prometheus_url=prometheus_url,
                        )
                    sonata_workflow = (
                        build_release_workflow(release_request, provider=release_provider)
                        if release_request is not None
                        else cast(
                            SonataWorkflow,
                            _workflow(
                                scenario_config,
                                environment_config,
                                control_plane_url=control_plane_url,
                                prometheus_url=prometheus_url or "http://127.0.0.1:9090",
                                run_dir=effective_run_dir,
                                provision=provision,
                            ),
                        )
                    )
                    sonata_workflow.keep = keep
                    selection = Selection(only=only, start=start, until=until)
                    try:
                        if release_request is not None:
                            sonata_workflow.run(
                                journal=release_journal,
                                resume=resume,
                                verifiers=release_verifiers(
                                    release_request, release_provider
                                ),
                                select=selection,
                            )
                        else:
                            sonata_workflow.run(select=selection)
                        if (
                            scenario_config.workflow == "offload-loadtest"
                            and effective_run_dir is not None
                        ):
                            _print_offload_summary(effective_run_dir)
                    except SelectionError as error:
                        raise typer.BadParameter(str(error)) from None
        except ReleaseRunInProgressError as error:
            raise typer.BadParameter(str(error)) from None
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
        finally:
            lifetime.close()

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
        if provision and environment_config.provider == "local":
            raise typer.BadParameter("--provision requires a non-local environment")
        _validate_cli_container_options(scenario_config, environment_config)
        _require_cli_endpoint(scenario_config, control_plane_url, provision=provision)
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
                provision=provision,
                prometheus_url=prometheus_url or "http://127.0.0.1:9090",
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
