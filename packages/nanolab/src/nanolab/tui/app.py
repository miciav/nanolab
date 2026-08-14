"""Menu navigation for the interactive control-plane tool."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
from typing import Any

from rich.table import Table
from rich.text import Text

from nanolab.cli import diagnostics
from nanolab.cli.execution import resolve_loadtest_urls
from nanolab.cli.product import (
    _environment,
    _scenario,
    _validate_cli_container_options,
    _workflow,
    _workflow_observers,
)
from nanolab.cli.provisioning import provision_environment
from nanolab.tui.workflow_controller import TuiWorkflowController
from nanolab.workspace.paths import default_tool_paths, discover_tool_root
from tui_toolkit import Choice, render_screen_frame, select
from tui_toolkit.console import console as default_console

_INSPECT_SCENARIO_TITLE = "Inspect scenario"

MAIN_MENU = [
    Choice(
        "Validation",
        "validation",
        "Build this source tree and prove it serves a function on a backend.",
    ),
    Choice(
        "CLI",
        "cli",
        "Exercise the nanofaas CLI against a control plane it sets up for you.",
    ),
    Choice(
        "Load Testing",
        "loadtest",
        "Run the current k6 and autoscaling workflow.",
    ),
    Choice(
        "Tools",
        "tools",
        "Inspect scenarios and check local prerequisites.",
    ),
    Choice("Exit", "exit", "Leave the interactive control-plane tool."),
]

# Validation answers "does the code in this checkout work?", so every entry builds
# from source and ends by reading back the function's declared CPU and memory off
# the real container or pod. CLI answers "does the client work?", so those entries
# care about reaching a control plane, not about what built it.
VALIDATION_MENU = [
    Choice(
        "Container",
        "container",
        "Build, serve a function on local Docker, check its resource limits landed.",
    ),
    Choice(
        "Kubernetes",
        "kubernetes",
        "Build and push, deploy with Helm on k3s, check the pod's resource limits.",
    ),
    Choice(
        "Offload",
        "offload",
        "Two local control planes: check sync invocations proxy from edge to cloud.",
    ),
]
CLI_MENU = [
    Choice(
        "Container",
        "container",
        "Start a control plane on local Docker, then drive it with the CLI.",
    ),
    Choice(
        "Kubernetes (provisioned)",
        "kubernetes",
        "Provision a VM with k3s and Helm, then run the CLI inside it.",
    ),
]
LOADTEST_MENU = [
    Choice("Run load test", "run", "Run the current k6 and autoscaling workflow."),
    Choice(
        "Offload load test",
        "offload",
        "Run mixed-policy k6 traffic against edge and cloud control planes.",
    ),
]
TOOLS_MENU = [
    Choice(_INSPECT_SCENARIO_TITLE, "inspect", "Inspect a supported scenario."),
    Choice("Doctor", "doctor", "Check required local executables."),
]

_SECTION_MENUS = {
    "validation": VALIDATION_MENU,
    "cli": CLI_MENU,
    "loadtest": LOADTEST_MENU,
    "tools": TOOLS_MENU,
}
_SECTION_TITLES = {
    "validation": "Validation",
    "cli": "CLI",
    "loadtest": "Load Testing",
    "tools": "Tools",
}
_SCENARIO_FILES = {
    ("validation", "container"): "deployment-lifecycle-container.yaml",
    ("validation", "kubernetes"): "deployment-lifecycle-k8s.yaml",
    ("validation", "offload"): "edge-cloud-offload-contract.yaml",
    ("cli", "container"): "cli-contract-container.yaml",
    ("cli", "kubernetes"): "cli-contract-k8s.yaml",
    ("loadtest", "run"): "autoscaling-cycle-k8s.yaml",
    ("loadtest", "offload"): "edge-cloud-offload-policy.yaml",
}
_SCENARIO_TITLES = {
    scenario_name: _SECTION_TITLES[section]
    for (section, _action), scenario_name in _SCENARIO_FILES.items()
}
_ACTION_CHOICES = [
    Choice("Plan", "plan", "Show the workflow tasks without running them."),
    Choice("Run", "run", "Run the workflow and follow its live progress."),
]
_CLEANUP_CHOICES = [
    Choice("Cleanup", "cleanup", "Remove infrastructure created by the workflow."),
    Choice("Keep", "keep", "Keep infrastructure after the workflow finishes."),
]
_PROVIDER_SETUP = {
    "azure": ("Azure", "azure.yaml.example", "azure.yaml"),
    "proxmox": ("Proxmox", "proxmox.yaml.example", "proxmox.yaml"),
}
_PROVIDER_GUIDANCE = {
    "azure": (
        "cp packages/nanolab/environments/azure.yaml.example "
        "packages/nanolab/environments/azure.yaml\n\n"
        "Set the Azure provider values and ssh_key_path, then run az login."
    ),
    "proxmox": (
        "cp packages/nanolab/environments/proxmox.yaml.example "
        "packages/nanolab/environments/proxmox.yaml\n\n"
        "Set host, node, template_id, and ssh_key_path. The template's password_env "
        "names PROXMOX_PASSWORD; export that environment variable."
    ),
}

# Control-flow signals returned by the workflow-selection helpers.
class _WorkflowBack:
    """Sentinel: the user backed out to the environment menu."""


class _WorkflowAbort:
    """Sentinel: a configuration error aborts the workflow."""


_WORKFLOW_BACK = _WorkflowBack()
_WORKFLOW_ABORT = _WorkflowAbort()


class NanofaasTUI:
    """Navigate the stable product menu and dispatch selected scenarios."""

    MAIN_MENU = MAIN_MENU
    SECTION_MENUS = _SECTION_MENUS
    SECTION_TITLES = _SECTION_TITLES
    SCENARIO_FILES = _SCENARIO_FILES

    def __init__(
        self,
        choose: Callable[..., str] = select,
        dispatch_scenario: Callable[[str], None] | None = None,
        *,
        controller: TuiWorkflowController | Any | None = None,
        console: Any = default_console,
        input_stream: Any = None,
    ) -> None:
        self._choose = choose
        self._dispatch_scenario = dispatch_scenario or self._workflow_menu
        self._controller = controller or TuiWorkflowController(console=console)
        self._console = console
        self._input_stream = sys.stdin if input_stream is None else input_stream

    def run(self) -> None:
        try:
            while True:
                section = self._choose(
                    "What would you like to do?",
                    choices=self.MAIN_MENU,
                    title="Main",
                    breadcrumb="Main",
                )
                if section == "exit":
                    return
                self._dispatch_section(section)
        except KeyboardInterrupt:
            return

    def _dispatch_section(self, section: str) -> None:
        menu = self.SECTION_MENUS.get(section)
        if menu is None:
            raise ValueError(f"Unsupported TUI section: {section}")

        title = self.SECTION_TITLES[section]
        while True:
            action = self._choose(
                title,
                choices=menu,
                include_back=True,
                escape_value="back",
                title=title,
                breadcrumb=f"Main / {title}",
            )
            if action == "back":
                return
            scenario_file = self.SCENARIO_FILES.get((section, action))
            if scenario_file is not None:
                self._dispatch_scenario(scenario_file)
            elif section == "tools":
                self._dispatch_tool(action)

    def _dispatch_tool(self, action: str) -> None:
        if action == "inspect":
            scenario_names = list(dict.fromkeys(self.SCENARIO_FILES.values()))
            selected = self._choose(
                "Scenario",
                choices=[
                    Choice(Path(name).stem, name, f"Inspect {name}.")
                    for name in scenario_names
                ],
                include_back=True,
                escape_value="back",
                title=_INSPECT_SCENARIO_TITLE,
                breadcrumb="Main / Tools / Inspect scenario",
            )
            if selected == "back":
                return
            try:
                scenario = _scenario(
                    discover_tool_root() / "scenarios-v2" / selected
                )
                body = scenario.model_dump_json(by_alias=True, indent=2)
            except Exception as exc:
                body = str(exc)
            self._show_static(
                title=_INSPECT_SCENARIO_TITLE,
                breadcrumb="Main / Tools / Inspect scenario",
                body=body,
            )
        elif action == "doctor":
            missing = diagnostics.missing_executables()
            body = f"missing executables: {', '.join(missing)}" if missing else "ok"
            self._show_static(
                title="Doctor",
                breadcrumb="Main / Tools / Doctor",
                body=body,
            )

    def _select_environment(self) -> Path | None:
        environment_dir = discover_tool_root() / "environments"
        while True:
            environment_paths = [
                path
                for path in sorted(environment_dir.glob("*.yaml"))
                if path.is_file() and ".example" not in path.name
            ]
            choices = [
                Choice(path.stem, str(path), f"Use {path.name}.")
                for path in environment_paths
            ]
            for provider, (label, template_name, target_name) in _PROVIDER_SETUP.items():
                if (environment_dir / template_name).is_file() and not (
                    environment_dir / target_name
                ).is_file():
                    choices.append(
                        Choice(
                            f"{label} (setup required)",
                            f"setup:{provider}",
                            f"Configure {target_name} before use.",
                        )
                    )
            if not choices:
                raise RuntimeError("at least one YAML environment or template is required")

            selected = self._choose(
                "Environment",
                choices=choices,
                include_back=True,
                escape_value="back",
                title="Environment",
                breadcrumb="Main / Environment",
            )
            if selected == "back":
                return None
            if selected.startswith("setup:"):
                provider = selected.removeprefix("setup:")
                self._show_static(
                    title=f"{_PROVIDER_SETUP[provider][0]} setup required",
                    breadcrumb="Main / Environment",
                    body=_PROVIDER_GUIDANCE[provider],
                )
                continue
            return Path(selected)

    def _workflow_menu(self, scenario_name: str) -> None:
        scenario_path = discover_tool_root() / "scenarios-v2" / scenario_name
        title = _SCENARIO_TITLES[scenario_name]
        environment_path: Path | None = None
        scenario: Any = None
        environment: Any = None
        keep = False

        while True:
            environment_path = self._select_environment()
            if environment_path is None:
                return
            selected = self._select_action_flow(scenario_path, environment_path, title)
            if isinstance(selected, _WorkflowBack):
                continue
            if isinstance(selected, _WorkflowAbort):
                return
            action, scenario, environment, keep = selected
            if action == "plan":
                self._show_plan(scenario, environment, title)
                return
            break

        assert environment_path is not None
        preview = self._preview_workflow(scenario, environment, title)
        if preview is None:
            return
        self._execute_workflow(
            scenario,
            environment,
            scenario_path,
            environment_path,
            title,
            preview,
            keep,
        )

    def _select_action_flow(
        self,
        scenario_path: Path,
        environment_path: Path,
        title: str,
    ) -> tuple[str, Any, Any, bool] | _WorkflowBack | _WorkflowAbort:
        """Resolve the scenario's action and cleanup policy.

        Returns a ``(action, scenario, environment, keep)`` tuple on success,
        ``_WORKFLOW_BACK`` when the user backs out to the environment menu, or
        ``_WORKFLOW_ABORT`` on a configuration error.
        """
        scenario: Any = None
        environment: Any = None
        while True:
            action = self._select_action(title)
            if action is None:
                return _WORKFLOW_BACK
            loaded = self._load_config(scenario_path, environment_path, title)
            if loaded is None:
                return _WORKFLOW_ABORT
            scenario, environment = loaded
            if action == "plan":
                return (action, scenario, environment, False)
            cleanup = self._select_cleanup_policy(scenario, environment, title)
            if isinstance(cleanup, _WorkflowAbort):
                return _WORKFLOW_ABORT
            if cleanup is None:
                continue
            return (action, scenario, environment, cleanup)

    def _select_action(self, title: str) -> str | None:
        action = self._choose(
            "Action",
            choices=_ACTION_CHOICES,
            include_back=True,
            escape_value="back",
            title=title,
            breadcrumb=f"Main / {title}",
        )
        return None if action == "back" else action

    def _load_config(
        self,
        scenario_path: Path,
        environment_path: Path,
        title: str,
    ) -> tuple[Any, Any] | None:
        try:
            scenario = _scenario(scenario_path)
            environment = _environment(environment_path)
            _validate_cli_container_options(scenario, environment)
            return scenario, environment
        except Exception as exc:
            self._show_static(
                title="Configuration error",
                breadcrumb=f"Main / {title}",
                body=str(exc),
            )
            return None

    def _select_cleanup_policy(
        self,
        scenario: Any,
        environment: Any,
        title: str,
    ) -> bool | None | _WorkflowAbort:
        """Return the keep policy, ``None`` on "back", ``_WORKFLOW_ABORT`` on error."""
        cleanup_choice = self._choose(
            "Cleanup policy",
            choices=_CLEANUP_CHOICES,
            include_back=True,
            escape_value="back",
            title=title,
            breadcrumb=f"Main / {title}",
        )
        if cleanup_choice == "back":
            return None
        keep = cleanup_choice == "keep"
        if not self._validate_config(scenario, environment, title, keep=keep):
            return _WORKFLOW_ABORT
        return keep

    def _validate_config(
        self,
        scenario: Any,
        environment: Any,
        title: str,
        *,
        keep: bool,
    ) -> bool:
        try:
            _validate_cli_container_options(scenario, environment, keep=keep)
            return True
        except Exception as exc:
            self._show_static(
                title="Configuration error",
                breadcrumb=f"Main / {title}",
                body=str(exc),
            )
            return False

    def _show_plan(self, scenario: Any, environment: Any, title: str) -> None:
        workflow = self._preview_workflow(scenario, environment, title)
        if workflow is None:
            return
        self._render_plan(title=title, workflow=workflow)

    def _preview_workflow(
        self,
        scenario: Any,
        environment: Any,
        title: str,
    ) -> Any | None:
        try:
            return self._build_workflow(scenario, environment, dry_run=True)
        except Exception as exc:
            self._show_static(
                title="Preview error",
                breadcrumb=f"Main / {title}",
                body=str(exc),
            )
            return None

    def _execute_workflow(
        self,
        scenario: Any,
        environment: Any,
        scenario_path: Path,
        environment_path: Path,
        title: str,
        preview: Any,
        keep: bool,
    ) -> None:
        try:
            self._controller.run_live_workflow(
                title=title,
                summary_lines=[
                    f"Scenario: {scenario_path.name}",
                    f"Environment: {environment_path.name}",
                    f"Cleanup: {'keep' if keep else 'cleanup'}",
                ],
                planned_steps=[
                    title for _task_id, title in self._plan_rows(preview)
                ],
                action=lambda _dashboard, _sink: self._run_current_workflow(
                    scenario,
                    environment,
                    scenario_path,
                    keep,
                ),
            )
        except Exception:
            # The controller preserves and acknowledges the failed final dashboard.
            # Returning keeps the user in the scenario's submenu.
            return

    def _run_current_workflow(
        self,
        scenario: Any,
        environment: Any,
        scenario_path: Path,
        keep: bool,
    ) -> Any:
        if (
            scenario.workflow != "release"
            and environment.provider != "local"
            and not (scenario.workflow == "cli" and scenario.backend == "k8s")
        ):
            with provision_environment(
                scenario,
                environment,
                repo_root=default_tool_paths().nanofaas_root,
                keep=keep,
            ):
                workflow = self._build_workflow(
                    scenario,
                    environment,
                    dry_run=False,
                )
                workflow.keep = keep
                observers = _workflow_observers(scenario_path)
                return workflow.run(observers=observers) if observers else workflow.run()
        workflow = self._build_workflow(scenario, environment, dry_run=False)
        workflow.keep = keep
        observers = _workflow_observers(scenario_path)
        return workflow.run(observers=observers) if observers else workflow.run()

    @staticmethod
    def _build_workflow(
        scenario: Any,
        environment: Any,
        *,
        dry_run: bool,
    ) -> Any:
        if scenario.workflow == "loadtest":
            control_plane_url, prometheus_url = resolve_loadtest_urls(
                environment,
                dry_run=dry_run,
            )
            return _workflow(
                scenario,
                environment,
                control_plane_url=control_plane_url,
                prometheus_url=prometheus_url,
            )
        if scenario.workflow == "offload-loadtest":
            return _workflow(scenario, environment, dry_run=dry_run)
        return _workflow(scenario, environment)

    def _plan_rows(self, workflow: Any) -> list[tuple[str, str]]:
        """Return `(task_id, title)` pairs from a compiled Sonata workflow."""
        return [
            (compiled_task.task_id, compiled_task.task.title)
            for compiled_task in workflow.compile().tasks
        ]

    def _render_plan(self, *, title: str, workflow: Any) -> None:
        table = Table(expand=True)
        table.add_column("#", justify="right", style="cyan", no_wrap=True)
        table.add_column("Task", style="bold")
        table.add_column("Description")
        for index, (task_id, task_title) in enumerate(
            self._plan_rows(workflow), start=1
        ):
            table.add_row(f"{index:02d}", task_id, task_title)

        self._show_static(
            title=title,
            body=table,
            breadcrumb=f"Main / {title}",
        )

    def _show_static(self, title: str, breadcrumb: str, body: Any) -> None:
        input_is_tty = bool(
            hasattr(self._input_stream, "isatty") and self._input_stream.isatty()
        )
        self._console.clear()
        try:
            rendered_body = Text(body) if isinstance(body, str) else body
            self._console.print(
                render_screen_frame(
                    title=title,
                    body=rendered_body,
                    breadcrumb=breadcrumb,
                    footer_hint=(
                        "Press Enter to continue" if input_is_tty else "View complete"
                    ),
                )
            )
            if input_is_tty:
                self._input_stream.read(1)
        finally:
            self._console.clear()
