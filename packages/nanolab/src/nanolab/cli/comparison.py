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

import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import typer

from sonata_tasks.components.bootstrap import remote_project_dir
from sonata_tasks.deployment import LOCAL_REGISTRY
from sonata_tasks.loadtest.comparison_report import WriteComparisonReport
from sonata_tasks.execution.bindings import RoleBoundCommandTaskExecutor

from sonata_engine.workflow.context import bind_workflow_sink

from nanolab.cli.execution import build_role_bindings
from nanolab.cli.vm_provider import vm_request_for_role
from nanolab.cli.progress import ConsoleProgressSink
from nanolab.comparison.matrix import ComparisonCell, build_matrix, pending, write_manifest
from nanolab.comparison.prepare import prepare_operations, prepare_workflow
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import VARIANTS_BY_KEY, resolve_variants
from nanolab.plans.functions import resolve_function
from nanolab.plans.runtime_comparison import COMPARISON_MODULES

DEFAULT_VARIANTS = tuple(VARIANTS_BY_KEY)


HEARTBEAT_SECONDS = 60.0


@contextmanager
def _heartbeat(summary: str, interval: float = HEARTBEAT_SECONDS) -> Iterator[None]:
    """Say the build is still alive, because nothing else will.

    A native compile takes twenty minutes and prints nothing while it runs. The
    obvious fix — binding a workflow sink so `SubprocessShell._emit_output`
    forwards the command's output — does not work: `ConsoleProgressSink.emit`
    returns early for any event without a `task_id`, so log lines are routed into
    a sink that discards them. Command output is never shown by this CLI, for
    cells or for prepare.

    So the elapsed time is the signal. It cannot distinguish "compiling" from
    "wedged", but it does distinguish both from "the process died", which is the
    question that had to be answered by reading load average off the VM.
    """
    done = threading.Event()

    def tick() -> None:
        waited = 0.0
        while not done.wait(interval):
            waited += interval
            typer.echo(f"prepare: {summary} — still running, {waited / 60:.0f}m")

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1.0)


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
    operations = prepare_operations(
        functions=functions,
        variants=resolve_variants(variants),
        registry=LOCAL_REGISTRY,
        modules=",".join(COMPARISON_MODULES),
        build_memory=build_memory,
        parallelism=parallelism,
    )
    workflow = prepare_workflow(
        operations,
        executor=RoleBoundCommandTaskExecutor(bindings),  # type: ignore[arg-type]
        # Said rather than inherited: the executor defaults to the checkout only
        # on multipass, and to the home directory on Azure and Proxmox.
        remote_dir=remote_project_dir(vm_request_for_role(environment_config, "stack")),
    )
    # log_lines=True because this phase is where the long commands live: an
    # image build reports its own progress and there is nothing else to read it.
    # The heartbeat stays as the backstop for a step that prints nothing at all —
    # a quiet minute is then the only thing separating "working" from "dead".
    with bind_workflow_sink(ConsoleProgressSink(log_lines=True)), _heartbeat("prepare"):
        _ = workflow.run()


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
        fresh: bool = typer.Option(
            False,
            "--fresh",
            help=(
                "Re-run cells that already have results. By default a matrix "
                "resumes: an interruption should not cost the hours of correct "
                "cells already on disk."
            ),
        ),
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
            todo = cells if fresh else pending(cells, root)
            if len(todo) < len(cells):
                typer.echo(
                    f"resuming: {len(cells) - len(todo)} of {len(cells)} cells "
                    "already have results"
                )
            for index, cell in enumerate(todo, start=1):
                typer.echo(f"[{index}/{len(todo)}] {cell.label}")
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
