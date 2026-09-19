import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sonata_engine import Resource, Task, TaskOutcome
from sonata_tasks.execution.bindings import RoleBindings

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.soak import (
    build_soak_plan,
    compose_frozen_soak_workflow,
)
from nanolab.tasks.compose import DockerComposeProject
from nanolab.tasks.platform import PlatformFunction, PlatformRequest
from nanolab.tasks.soak.containerd_runtime import (
    BuildExecutionRecorder,
    ContainerdSoakRun,
)


def test_constructor_compiles_deferred_pipeline_without_side_effects(tmp_path):
    workflow = build_soak_plan(
        SimpleNamespace(workflow="soak", soak=object()),  # pyright: ignore[reportArgumentType]
        SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    measure = next(
        i for i, title in enumerate(titles) if "single-version soak" in title
    )
    # Both are acquired before the measurement and released after it:
    # preparation pushes application images into the registry, and the helper
    # build both pushes into it and builds through the builder.
    before, after = titles[:measure], titles[measure + 1 :]
    assert any("Acquire local registry" in title for title in before)
    assert any("buildx builder" in title for title in before)
    assert any("Release local registry" in title for title in after)
    assert any("buildx builder" in title for title in after)
    assert not (tmp_path / "run").exists()


def test_compiled_platform_resources_outlive_measurement(tmp_path, monkeypatch):
    import nanolab.tasks.soak.teardown as teardown

    cleanup = {}

    def cleanup_commands(run_dir, timeout_s, artifact_limit):
        cleanup.update(
            run_dir=run_dir,
            timeout_s=timeout_s,
            artifact_limit=artifact_limit,
        )
        return object()

    monkeypatch.setattr(teardown, "LocalCleanupCommands", cleanup_commands)
    digest = "registry/image@sha256:" + "a" * 64
    request = PlatformRequest(
        backend="container",
        functions=(PlatformFunction("fn", digest, "{}", ()),),
        build_images=False,
        build_control_plane=False,
        control_plane_image=digest,
    )

    class Measure(Task):
        title = "Measure and report"
        observer = object()

        def stop_observer(self):
            pass

        def run(self, inputs):
            return TaskOutcome(value=None)

    ownership = Resource(
        title="Own endpoint",
        acquire=lambda inputs: None,
        release=lambda inputs, value: None,
    )
    workflow = compose_frozen_soak_workflow(
        request,
        RoleBindings({"host": _CompileOnlyExecutor()}),
        project=DockerComposeProject(
            "unique-soak",
            tmp_path / "compose.yml",
            "http://127.0.0.1:18080",
            build=False,
        ),
        measurement=Measure(),
        ownership=ownership,
        cwd=tmp_path,
        api_endpoint="http://127.0.0.1:18080",
        release_timeout_s=75.0,
    )
    tasks = workflow.compile().tasks
    consumer = next(
        index
        for index, task in enumerate(tasks)
        if task.task.title == "Measure and report"
    )
    assert all(
        index > consumer for index, task in enumerate(tasks) if task.kind == "release"
    )
    assert len(tasks[consumer].required_resources) >= 4
    assert not any("Build image" in task.task.title for task in tasks)
    assert cleanup["timeout_s"] == 75.0


class _CompileOnlyExecutor:
    def binding_key(self, role):
        return f"compile-only:{role}"

    def run(self, task, *, dry_run=False):
        raise AssertionError("plan construction must not execute commands")


def test_function_registration_uses_api_not_management_readiness(tmp_path, monkeypatch):
    import nanolab.plans.soak as module

    digest = "registry/image@sha256:" + "b" * 64
    request = PlatformRequest(
        backend="container",
        functions=(PlatformFunction("fn", digest, "{}", ()),),
        build_images=False,
        build_control_plane=False,
        control_plane_image=digest,
    )
    ownership = Resource(
        title="Own endpoint",
        acquire=lambda inputs: None,
        release=lambda inputs, value: None,
    )
    observed = {}

    def add_platform(workflow, request, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(resources=(), functions=(ownership,))

    monkeypatch.setattr(module, "add_platform", add_platform)
    monkeypatch.setattr(module, "isolated_compose_resource", lambda *a, **k: ownership)
    compose_frozen_soak_workflow(
        request,
        RoleBindings({"host": _CompileOnlyExecutor()}),
        project=DockerComposeProject(
            "owned",
            tmp_path / "compose.yaml",
            "http://127.0.0.1:18081/actuator/health/readiness",
            build=False,
        ),
        measurement=SimpleNamespace(observer=object()),  # pyright: ignore[reportArgumentType]
        ownership=ownership,
        cwd=tmp_path,
        api_endpoint="http://127.0.0.1:18080",
    )
    assert observed["local_endpoint"] == "http://127.0.0.1:18080"


def test_the_plan_acquires_a_builder_that_can_reach_the_local_registry(
    tmp_path, monkeypatch
):
    """Nothing else pins the driver option, and without it the run cannot publish.

    A `docker-container` builder runs buildkitd in a container of its own, so its
    `localhost` is itself: the build succeeds and the push into the registry this
    run just acquired does not. That failure lands after the build, so the option
    is pinned here rather than discovered there.
    """
    import nanolab.plans.soak as plan

    seen: list[dict] = []
    real = plan.buildx_builder_resource
    monkeypatch.setattr(
        plan,
        "buildx_builder_resource",
        lambda **kwargs: (seen.append(kwargs), real(**kwargs))[1],
    )

    build_soak_plan(
        SimpleNamespace(workflow="soak", soak=object()),  # pyright: ignore[reportArgumentType]
        SimpleNamespace(provider="local"),  # pyright: ignore[reportArgumentType]
        RoleBindings({"host": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=tmp_path,
        tool_root=tmp_path,
    )

    assert len(seen) == 1
    assert seen[0]["driver_options"] == ("network=host",)


def test_containerd_soak_plan_uses_stack_runtime_without_compose(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "owned-soak-vm"}},
            "containerdMavenRepository": "/tmp/test-containerd-maven",
        }
    )
    workflow = build_soak_plan(
        config,
        environment,
        RoleBindings({"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}),
        run_dir=tmp_path / "run",
        repo_root=Path(os.environ["NANOFAAS_ROOT"]),
        tool_root=Path(__file__).parents[4],
    )
    titles = [task.task.title for task in workflow.compile().tasks]
    assert any("rootless containerd test registry" in title for title in titles)
    assert any("rootless containerd test runtime" in title for title in titles)
    assert any("soak measurement" in title for title in titles)
    assert not any("Compose" in title or "Docker project" in title for title in titles)
    assert not (tmp_path / "run").exists()
    tasks = [item.task for item in workflow.compile().tasks]
    build = next(task for task in tasks if task.title == "Build control plane")
    measure = next(task for task in tasks if isinstance(task, ContainerdSoakRun))
    from sonata_tasks.command import CommandTask

    assert isinstance(build, CommandTask)
    assert isinstance(build.executor, BuildExecutionRecorder)
    assert build.executor is measure.executor
    assert not callable(build.argv)
    assert "-PcontainerdMavenLocal=true" in build.argv


def test_containerd_soak_rejects_native_until_binary_build_is_bound(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    data = yaml.safe_load(scenario.read_text())
    data["soak"]["roles"]["control-plane"]["runtime"] = "native"
    data["soak"]["images"]["control-plane"]["variant"] = "native"
    config = ScenarioConfig.model_validate(data)
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "owned-soak-vm"}}}
    )
    with pytest.raises(ValueError, match="JVM process artifact"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )


@pytest.mark.parametrize(
    ("role", "field", "value"),
    [
        ("word-stats-java", "variant", "native"),
        ("word-stats-javascript", "build_options", {"RUNTIME_IMAGE": "other"}),
        ("word-stats-java", "mode", "prebuilt"),
    ],
)
def test_containerd_soak_rejects_unapplied_image_recipe_before_acquisition(
    tmp_path, role, field, value
):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    data = yaml.safe_load(scenario.read_text())
    data["soak"]["images"][role][field] = value
    if field == "mode":
        data["soak"]["images"][role]["digest"] = "registry/fn@sha256:" + "a" * 64
        data["soak"]["images"][role]["provenance_receipt"] = "receipt.json"
    config = ScenarioConfig.model_validate(data)
    environment = EnvironmentConfig.model_validate(
        {"provider": "local", "containerdMavenRepository": str(tmp_path / "maven")}
    )
    with pytest.raises(ValueError, match=r"unsupported.*recipe"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )
    assert not (tmp_path / "run").exists()


def test_containerd_soak_refuses_unimplemented_p24_diagnostics(tmp_path):
    scenario = (
        Path(__file__).parents[2] / "scenarios-v2/memory-soak-smoke-containerd.yaml"
    )
    config = ScenarioConfig.model_validate(yaml.safe_load(scenario.read_text()))
    assert config.soak is not None
    config.soak.prerequisites.required_coverage.append("sync")
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "owned-soak-vm"}}}
    )
    with pytest.raises(ValueError, match="prerequisite and diagnostic adapters"):
        build_soak_plan(
            config,
            environment,
            RoleBindings(
                {"host": _CompileOnlyExecutor(), "stack": _CompileOnlyExecutor()}
            ),
            run_dir=tmp_path / "run",
            repo_root=Path(os.environ["NANOFAAS_ROOT"]),
            tool_root=Path(__file__).parents[4],
        )
