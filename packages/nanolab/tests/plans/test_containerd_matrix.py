"""Every containerd scenario compiles the shared workload over the new runtime."""

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.cli import build_cli_plan
from nanolab.plans.loadtest import build_loadtest_plan
from nanolab.plans.validate import build_validate_plan
from nanolab.workspace.paths import default_tool_paths

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios-v2"
MATRIX = (
    "deployment-lifecycle-containerd.yaml",
    "persistent-recovery-containerd.yaml",
    "validate-async-containerd.yaml",
    "autoscaling-cycle-containerd.yaml",
    "concurrency-cycle-containerd.yaml",
    "concurrency-cycle-containerd-go.yaml",
    "concurrency-cycle-containerd-python.yaml",
    "concurrency-cycle-containerd-javascript.yaml",
    "concurrency-cycle-containerd-budgeted.yaml",
    "concurrency-co-tenancy-containerd.yaml",
    "handler-envelope-containerd.yaml",
    "cli-contract-containerd.yaml",
)


@dataclass
class Executor:
    def binding_key(self, role: str) -> str:
        return f"matrix:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        return TaskResult(
            task_id=task.task_id, status="passed", return_code=0, stdout=""
        )


@pytest.mark.parametrize("filename", MATRIX)
def test_containerd_matrix_compiles_without_docker_lifecycle(
    filename: str, tmp_path: Path
) -> None:
    config = ScenarioConfig.model_validate(
        yaml.safe_load((SCENARIOS / filename).read_text())
    )
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "rootless-stack"}},
            "containerdMavenRepository": "/tmp/test-containerd-maven",
        }
    )
    executor = Executor()
    bindings = RoleBindings({"host": executor, "stack": executor, "loadgen": executor})
    root = default_tool_paths().nanofaas_root

    if config.workflow == "validate":
        plan = build_validate_plan(
            config, bindings, repo_root=root, environment=environment
        )
    elif config.workflow == "cli":
        plan = build_cli_plan(config, bindings, repo_root=root, environment=environment)
    else:
        plan = build_loadtest_plan(
            config,
            environment,
            bindings,
            control_plane_url="http://127.0.0.1:8080",
            prometheus_client=object(),  # type: ignore[arg-type]
            run_dir=tmp_path,
            fetcher=object(),  # type: ignore[arg-type]
            repo_root=root,
        )

    titles = [task.task.title for task in plan.compile().tasks]
    assert "Acquire rootless containerd test runtime" in titles
    assert not any("Docker Compose" in title for title in titles)
    build = next(
        task.task.argv
        for task in plan.compile().tasks
        if isinstance(task.task, CommandTask)
        and task.task.title in {"Build control plane", "Build local control plane"}
    )
    assert not callable(build)
    assert "-PcontainerdMavenLocal=true" in build
    assert any(arg.startswith("-Dmaven.repo.local=/home/ubuntu/") for arg in build)
