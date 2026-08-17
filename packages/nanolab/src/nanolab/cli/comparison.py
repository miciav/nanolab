"""The `compare` command: build every variant once, then run the matrix.

Two phases, and the split is the point. The prepare phase compiles the function
images and every control-plane build into the VM-local registry; the matrix phase
runs cells that build nothing at all. A cell that could build would rebuild its
functions too — `platform.py` gates both behind one flag — and twelve rebuilds
spread over an hour would let base-image drift arrive as a difference between
variants.

Provisioning is held for the whole matrix (`keep=True`), so twelve cells share
one cluster and one registry. Tearing down between cells would make every cell
pay for a fresh k3s and would also destroy the images the prepare phase built.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path

import typer

from sonata_tasks.tasks.models import CommandTaskSpec
from sonata_tasks.deployment import LOCAL_REGISTRY
from sonata_tasks.loadtest.comparison_report import WriteComparisonReport
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from sonata_engine.workflow.context import bind_workflow_sink

from nanolab.cli.execution import build_role_bindings
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.comparison.matrix import ComparisonCell, build_matrix, write_manifest
from nanolab.comparison.prepare import prepare_operations
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import VARIANTS_BY_KEY, resolve_variants
from nanolab.plans.functions import resolve_function

DEFAULT_VARIANTS = tuple(VARIANTS_BY_KEY)
# The module set every variant is compiled with. Written out rather than derived
# from the scenario: `_additional_modules` returns extras for autoscaling and
# concurrency runs, and the comparison forbids both, so it would return nothing.
COMPARISON_MODULES = "k8s-deployment-provider,async-queue,sync-queue"


def _run_prepare(
    scenario_config: ScenarioConfig,
    environment_config: EnvironmentConfig,
    *,
    variants: tuple[str, ...],
    repo_root: Path,
    tool_root: Path | None,
    bindings: object,
    build_memory: str | None,
    parallelism: int | None,
) -> None:
    """Compile the functions and every control-plane build, on the VM, once."""
    functions = tuple(
        resolve_function(scenario_config, key, source_root=repo_root, tool_root=tool_root)
        for key in scenario_config.functions
    )
    executor = RoleBoundCommandTaskExecutor(bindings)  # type: ignore[arg-type]
    for operation in prepare_operations(
        functions=functions,
        variants=resolve_variants(variants),
        registry=LOCAL_REGISTRY,
        modules=COMPARISON_MODULES,
        build_memory=build_memory,
        parallelism=parallelism,
    ):
        typer.echo(f"prepare: {operation.summary}")
        started = time.monotonic()
        result = executor.run(
            CommandTaskSpec(
                task_id=operation.operation_id,
                summary=operation.summary,
                argv=tuple(operation.argv),
                role="stack",
                env=dict(operation.env),
                remote_dir=None,
            )
        )
        elapsed = time.monotonic() - started
        if result.status != "passed":
            detail = (result.stderr or result.stdout or "no output").strip()
            raise RuntimeError(
                f"{operation.operation_id} failed after {elapsed:.0f}s: {detail}"
            )
        typer.echo(f"prepare: {operation.summary} — done in {elapsed:.0f}s")


def cell_scenario(config: ScenarioConfig, cell: ComparisonCell) -> ScenarioConfig:
    """The scenario for one cell: the base, with the build it measures named.

    A copy rather than a mutation, because the base is read once and every cell
    must differ from it in exactly one field.
    """
    return config.model_copy(update={"control_plane_variant": cell.variant.key})


def register(app: typer.Typer) -> None:
    @app.command("compare")
    def compare_command(
        scenario: Path = typer.Argument(..., exists=True),
        environment: Path = typer.Option(..., "--environment", exists=True),
        repetitions: int = typer.Option(
            3,
            "--repetitions",
            help="Runs per variant. Three is the smallest number that shows spread.",
        ),
        variants: str = typer.Option(
            ",".join(DEFAULT_VARIANTS),
            "--variants",
            help="Comma-separated control-plane builds to compare.",
        ),
        run_dir: Path | None = typer.Option(None, "--run-dir"),
        native_build_memory: str | None = typer.Option(
            None,
            "--native-build-memory",
            help=(
                "Heap for the native-image BUILDER, e.g. 6g. It sizes its own heap "
                "from the machine's total memory and cannot see what else is "
                "running, so on a shared VM it gets OOM-killed. Unset means unbounded."
            ),
        ),
        native_parallelism: int | None = typer.Option(
            None,
            "--native-parallelism",
            help="native-image workers. Fewer cost wall-clock and save peak memory.",
        ),
    ) -> None:
        """Compare control-plane builds under one varying load."""
        from nanolab.cli.product import (  # local: the router imports this module
            _environment,
            _execute_workflow,
            _prepare_run,
            _provisioning_context,
            _scenario,
            default_tool_paths,
        )

        scenario_config = _scenario(scenario)
        environment_config = _environment(environment)
        if scenario_config.load_profile != "comparison":
            raise typer.BadParameter("compare requires a scenario with loadProfile: comparison")
        selected = tuple(part.strip() for part in variants.split(",") if part.strip())
        resolve_variants(selected)  # fail here, not forty minutes into the matrix
        paths = default_tool_paths()
        root = run_dir or paths.runs_dir / "comparison"
        cells = build_matrix(resolve_variants(selected), repetitions)
        write_manifest(
            root,
            cells,
            functions=tuple(scenario_config.functions),
            registry=LOCAL_REGISTRY,
        )

        # One cluster and one registry for the whole matrix. Tearing down between
        # cells would make each one pay for a fresh k3s and would destroy the
        # images the prepare phase just built.
        with _provisioning_context(scenario_config, environment_config, paths, True) as _:
            bindings, _fetcher = build_role_bindings(environment_config)
            # SubprocessShell._emit_output forwards every command's output to the
            # workflow log, but only while a sink is bound. Without this the
            # prepare phase runs silently: a twenty-minute native compile emits
            # one line before it starts and nothing until it ends, so "working"
            # and "hung" look identical from the log — the difference had to be
            # read off the VM's load average instead.
            with bind_workflow_sink(ConsoleProgressSink()):
                _run_prepare(
                    scenario_config,
                    environment_config,
                    variants=selected,
                    repo_root=paths.nanofaas_root,
                    tool_root=paths.tool_root,
                    bindings=bindings,
                    build_memory=native_build_memory,
                    parallelism=native_parallelism,
                )
            for index, cell in enumerate(cells, start=1):
                typer.echo(f"[{index}/{len(cells)}] {cell.label}")
                cell_dir = cell.run_dir(root)
                lifetime = ExitStack()
                try:
                    sink, *_rest = _prepare_run(
                        lifetime,
                        release=False,
                        scenario=scenario,
                        environment=environment,
                        release_config=None,
                        run_dir=cell_dir,
                        resume=False,
                        environment_config=environment_config,
                        paths=paths,
                        effective_run_dir=cell_dir,
                    )
                    _execute_workflow(
                        sink=sink,
                        scenario_config=cell_scenario(scenario_config, cell),
                        environment_config=environment_config,
                        paths=paths,
                        keep=True,
                        control_plane_url=None,
                        prometheus_url=None,
                        effective_run_dir=cell_dir,
                        only=None,
                        start=None,
                        until=None,
                        scenario=scenario,
                        release_request=None,
                        release_provider=None,
                        release_journal=None,
                        resume=False,
                    )
                finally:
                    lifetime.close()
        # Written after the cells, outside the provisioning context: the report
        # reads the run directories and nothing else, so it survives a cluster
        # that has already gone away — and a matrix interrupted partway through
        # still renders from whatever cells did finish.
        report = WriteComparisonReport(
            task_id="",
            title="Control-plane build comparison",
            root=root,
        ).run()
        typer.echo(f"matrix complete: {root}")
        typer.echo(f"report: {report}")
