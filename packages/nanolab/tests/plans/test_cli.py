from __future__ import annotations

from dataclasses import dataclass, field

from typing import cast

import pytest
import yaml
from sonata_engine import Selection
from sonata_tasks.components import bootstrap
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult
from sonata_tasks.vm import multipass
from sonata_tasks.vm.models import VmRequest

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.functions.catalog import list_functions
from nanolab.plans import cli
from nanolab.plans.cli import PROVISIONED_ENDPOINT, build_cli_plan
from nanolab.release.publish import (
    GHCR_REPOSITORY,
    build_publish_plan,
)
from nanolab.release.versioning import normalize_version, read_project_version
from nanolab.workspace.paths import default_tool_paths

SUCCESS = '{"status":"success","output":{"words":2}}'
@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=SUCCESS)


@dataclass
class FakeMultipassOrchestrator:
    """A fake VM lifecycle for a Multipass-shaped environment.

    Satisfies the structural protocol `VmLifecycleAdapter` needs from an
    orchestrator (`ensure_running`/`connection_host`/`teardown`) without ever
    shelling out to the real `multipass` CLI.
    """

    host: str = "10.10.10.10"
    ensured: list[VmRequest] = field(default_factory=list)
    torn_down: list[VmRequest] = field(default_factory=list)

    def ensure_running(self, request: VmRequest) -> None:
        self.ensured.append(request)

    def connection_host(self, request: VmRequest) -> str:
        return self.host

    def teardown(self, request: VmRequest) -> None:
        self.torn_down.append(request)


@dataclass
class FakeAzureOrchestrator(FakeMultipassOrchestrator):
    restrictions: list[tuple[str | None, tuple[int, ...], tuple[str, ...]]] = field(
        default_factory=list
    )

    def restrict_inbound_sources(self, request, *, ports, source_cidrs, priority_base=1010) -> None:
        self.restrictions.append((request.name, ports, source_cidrs))


def _multipass_environment(**role_overrides: object) -> EnvironmentConfig:
    role = {"name": "nanofaas-e2e-cli", **role_overrides}
    return EnvironmentConfig.model_validate({"provider": "multipass", "roles": {"stack": role}})


def _azure_environment() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {"stack": {}},
            "azure": {
                "resource_group": "rg",
                "location": "westeurope",
                "operator_source_cidr": "203.0.113.42/32",
            },
        }
    )


def _provisioned_plan(
    bindings: RoleBindings,
    *,
    orchestrator: FakeMultipassOrchestrator | None = None,
    **overrides: object,
):
    fake = orchestrator or FakeMultipassOrchestrator()
    return build_cli_plan(
        _scenario(backend="k8s", **overrides),
        bindings,
        repo_root=default_tool_paths().nanofaas_root,
        environment=_multipass_environment(),
        orchestrator_factory=lambda _root: fake,
    )



def _argv(task: object) -> tuple[str, ...]:
    """The argv of a compiled command task.

    CommandTask.argv may also be a callable resolved from upstream; these tasks
    are built with a literal, and saying so once beats three casts.
    """
    argv = cast(CommandTask, task).argv
    assert not callable(argv)
    return argv


def _scenario(**overrides: object) -> ScenarioConfig:
    payload: dict[str, object] = {
        "workflow": "cli",
        "backend": "k8s",
        "functions": ["word-stats-java"],
    }
    payload.update(overrides)
    return ScenarioConfig.model_validate(payload)


def test_cli_plan_uses_the_selected_role_binding() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=host, stack=stack),
        cli_role="stack",
        endpoint="http://stack.example:30080",
    ).run()

    assert host.seen == []
    assert [spec.summary for spec in stack.seen] == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_cli_plan_compiles_every_selected_function() -> None:
    plan = build_cli_plan(
        _scenario(
            functions=["word-stats-java", "json-transform-python"],
            resources={"word-stats-java": {"limits": {"memoryMiB": 512}}},
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    )

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.acquire-json-transform-python",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.invoke-json-transform-python",
        "008.release-json-transform-python",
    ]


def test_cli_plan_passes_the_endpoint_and_resolved_resources_through() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(resources={"word-stats-java": {"limits": {"memoryMiB": 512}}}),
        RoleBindings(host=executor, stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    ).run()

    apply_script = next(
        spec.argv[-1] for spec in executor.seen if spec.summary.startswith("Apply")
    )
    assert "http://stack.example:8080" in apply_script
    assert '"memoryMiB":512' in apply_script


def test_cli_plan_sends_only_the_payload_input_to_invoke() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
        endpoint="http://stack.example:30080",
    ).run()

    invoke = next(spec for spec in executor.seen if spec.summary.startswith("Invoke"))
    assert '"input"' not in " ".join(invoke.argv)


def test_cli_plan_supports_slicing_by_slug() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
        endpoint="http://stack.example:30080",
    ).run(select=Selection(only="list-functions"))

    assert [spec.summary for spec in executor.seen] == [
        "Apply word-stats-java",
        "List functions",
        "Delete word-stats-java",
    ]


def test_cli_plan_rejects_a_non_cli_scenario() -> None:
    with pytest.raises(ValueError, match="cli scenario"):
        build_cli_plan(
            ScenarioConfig.model_validate(
                {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
            ),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )


def test_container_backend_wraps_the_workflow_in_a_local_control_plane() -> None:
    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-local-control-plane",
        "003.build-application-artifact-word-stats-java",
        "004.build-image-word-stats-java",
        "005.acquire-local-registry",
        "006.push-image-127-0-0-1-5000-nanofaas-java-word-stats-e2e",
        "007.acquire-local-control-plane",
        "008.acquire-word-stats-java",
        "009.list-functions",
        "010.invoke-word-stats-java",
        "011.release-word-stats-java",
        "012.release-local-control-plane",
        "013.release-local-registry",
    ]


def test_container_backend_builds_the_control_plane_with_the_container_module() -> None:
    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )
    build = next(
        task for task in plan.compile().tasks
        if task.task_id.endswith(".build-local-control-plane")
    )

    assert _argv(build.task) == (
        "./gradlew",
        ":control-plane:bootJar",
        "-PcontrolPlaneModules=container-deployment-provider",
        "--no-daemon",
    )


def test_container_backend_targets_the_local_control_plane_port() -> None:
    executor = RecordingExecutor()

    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    )
    invoke = next(
        task for task in plan.compile().tasks if task.task_id.endswith(".invoke-word-stats-java")
    )

    assert "http://127.0.0.1:18080" in " ".join(_argv(invoke.task))


def test_k8s_backend_keeps_the_explicit_endpoint_and_starts_nothing() -> None:
    plan = build_cli_plan(
        _scenario(backend="k8s"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        endpoint="http://stack.example:30080",
    )
    task_ids = [task.task_id for task in plan.compile().tasks]
    invoke = next(
        task for task in plan.compile().tasks
        if task.task_id.endswith(".invoke-word-stats-java")
    )

    assert not any("local-control-plane" in task_id for task_id in task_ids)
    assert not any("build-local-control-plane" in task_id for task_id in task_ids)
    assert not any("build-image" in task_id for task_id in task_ids)
    assert "http://stack.example:30080" in " ".join(_argv(invoke.task))


def test_k8s_backend_requires_an_explicit_endpoint() -> None:
    with pytest.raises(ValueError, match="explicit control-plane URL"):
        build_cli_plan(
            _scenario(backend="k8s"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )


def test_container_backend_runs_only_on_the_host_role() -> None:
    with pytest.raises(ValueError, match="host role"):
        build_cli_plan(
            _scenario(backend="container"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
            cli_role="stack",
        )


def test_provisioned_k8s_plan_compiles_the_expected_12_task_topology() -> None:
    plan = _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()))

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-stack-vm",
        "003.provision-base-vm-dependencies",
        "004.install-k3s",
        "005.sync-repository-into-vm",
        "006.acquire-control-plane-helm-release",
        "007.acquire-word-stats-java",
        "008.list-functions",
        "009.invoke-word-stats-java",
        "010.release-word-stats-java",
        "011.release-control-plane-helm-release",
        "012.release-stack-vm",
    ]


def test_provisioned_k8s_bootstrap_sets_up_no_local_registry() -> None:
    """Images come from GHCR, so a registry nothing reads must not be provisioned.

    Pinned because the bootstrap sequence is borrowed from `validate`, which does
    build and push locally — reusing it wholesale silently reintroduces both steps.
    """
    plan = _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()))

    task_ids = [task.task_id for task in plan.compile().tasks]

    assert not any("registry" in task_id for task_id in task_ids)


def test_provisioned_k8s_uses_the_published_release_images() -> None:
    product_root = default_tool_paths().nanofaas_root
    values = yaml.safe_load(
        (product_root / "deploy/helm/nanofaas/values.yaml").read_text(encoding="utf-8")
    )
    control_plane = values["controlPlane"]["image"]
    function_image = next(
        function["image"]
        for function in values["demos"]["functions"]
        if function["name"] == "word-stats-java"
    )
    stack = RecordingExecutor()

    _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=stack)).run()

    helm = next(spec for spec in stack.seen if spec.summary.startswith("Install Helm"))
    apply = next(spec for spec in stack.seen if spec.summary.startswith("Apply"))
    assert f"controlPlane.image.repository={control_plane['repository']}" in helm.argv
    assert f"controlPlane.image.tag={control_plane['tag']}" in helm.argv
    assert function_image in apply.argv[-1]
    assert "localhost:5000/nanofaas" not in " ".join((*helm.argv, *apply.argv))


def test_published_image_maps_every_catalog_function_to_a_published_reference() -> None:
    product_root = default_tool_paths().nanofaas_root
    _, version_tag = normalize_version(read_project_version(product_root))
    publish_plan = build_publish_plan(
        product_root,
        version_tag,
        local_registry="localhost:5000/nanofaas",
    )
    published_references = {
        item.reference for item in (*publish_plan.manifests, *publish_plan.aliases)
    }
    helm_values = yaml.safe_load(
        (product_root / "deploy/helm/nanofaas/values.yaml").read_text(encoding="utf-8")
    )
    helm_functions = {
        function["name"] for function in helm_values["demos"]["functions"]
    }
    publishable = [function for function in list_functions() if function.default_image]

    mapped: dict[str, str] = {}
    for function in publishable:
        assert function.default_image is not None
        target = function.default_image.rsplit("/", 1)[-1].split(":", 1)[0]
        mapped[function.key] = cli._published_image(function.default_image, version_tag)
        assert mapped[function.key] == f"{GHCR_REPOSITORY}/{target}:{version_tag}"
        assert mapped[function.key] in published_references

    assert "word-stats-python" in mapped  # non-Java
    assert "roman-numeral-go" in mapped  # absent from Helm defaults
    assert "roman-numeral-go" not in helm_functions
    assert set(mapped) == {function.key for function in publishable}


def test_provisioned_k8s_plan_compilation_does_not_discover_ssh_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_credential_discovery(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("SSH credentials must not be read while compiling a plan")

    monkeypatch.setattr(cli, "find_ssh_public_key", reject_credential_discovery)
    monkeypatch.setattr(bootstrap, "find_ssh_public_key", reject_credential_discovery)
    monkeypatch.setattr(multipass, "find_ssh_public_key", reject_credential_discovery)

    plan = build_cli_plan(
        _scenario(backend="k8s"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        repo_root=default_tool_paths().nanofaas_root,
        environment=_multipass_environment(),
    )

    assert len(plan.compile().tasks) == 12


def test_provisioned_k8s_builds_the_cli_on_host_and_runs_everything_else_on_stack() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()

    _provisioned_plan(RoleBindings(host=host, stack=stack)).run()

    assert [spec.summary for spec in host.seen] == [
        "Build nanofaas-cli",
        "Provision base VM dependencies",
        "Install k3s",
        "Sync repository into VM",
    ]
    assert [spec.summary for spec in stack.seen] == [
        "Install Helm release control-plane",
        "Apply word-stats-java",
        "Wait for deployment/fn-word-stats-java",
        "Roll out deployment/fn-word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
        "Uninstall Helm release control-plane",
    ]


def test_provisioned_k8s_bootstrap_argv_is_resolved_from_the_acquired_vm() -> None:
    host = RecordingExecutor()
    orchestrator = FakeMultipassOrchestrator(host="192.0.2.42")

    _provisioned_plan(
        RoleBindings(host=host, stack=RecordingExecutor()), orchestrator=orchestrator
    ).run()

    sync = next(spec for spec in host.seen if spec.summary == "Sync repository into VM")
    assert any("192.0.2.42" in argument for argument in sync.argv)
    assert orchestrator.ensured, "the VM must actually be ensured running"
    assert orchestrator.torn_down, "a non-kept run must destroy the VM at the end"


def test_azure_k8s_plan_restricts_nodeports_to_the_operator() -> None:
    provider = FakeAzureOrchestrator(host="192.0.2.42")

    build_cli_plan(
        _scenario(),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        repo_root=default_tool_paths().nanofaas_root,
        environment=_azure_environment(),
        orchestrator_factory=lambda _root: provider,
    ).run()

    assert provider.restrictions == [
        ("nanofaas-azure", (30080, 30081, 30090), ("203.0.113.42/32",))
    ]


def test_provisioned_k8s_endpoint_is_the_incluster_node_port() -> None:
    stack = RecordingExecutor()

    _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=stack)).run()

    invoke = next(spec for spec in stack.seen if spec.summary.startswith("Invoke"))
    assert PROVISIONED_ENDPOINT == "http://127.0.0.1:30080"
    assert PROVISIONED_ENDPOINT in " ".join(invoke.argv)


def test_provisioned_k8s_helm_requires_the_vm_and_the_function_requires_helm() -> None:
    plan = _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()))

    tasks = {task.task_id: task for task in plan.compile().tasks}
    helm_acquire = tasks["006.acquire-control-plane-helm-release"]
    function_acquire = tasks["007.acquire-word-stats-java"]
    assert helm_acquire.resource is not None
    assert function_acquire.resource is not None
    assert [resource.title for resource in helm_acquire.resource.requires] == ["Acquire stack VM"]
    assert [resource.title for resource in function_acquire.resource.requires] == [
        "Acquire control-plane Helm release"
    ]


def test_provisioned_k8s_slice_keeps_the_vm_helm_and_function() -> None:
    stack = RecordingExecutor()

    _provisioned_plan(RoleBindings(host=RecordingExecutor(), stack=stack)).run(
        select=Selection(only="invoke-word-stats-java")
    )

    assert [spec.summary for spec in stack.seen] == [
        "Install Helm release control-plane",
        "Apply word-stats-java",
        "Wait for deployment/fn-word-stats-java",
        "Roll out deployment/fn-word-stats-java",
        "Invoke word-stats-java",
        "Delete word-stats-java",
        "Uninstall Helm release control-plane",
    ]


def test_provisioned_k8s_keep_preserves_the_vm_and_helm_but_not_the_function() -> None:
    """`--keep` is for what is expensive to rebuild — the VM and the chart on it.
    A registration costs a second, and left behind it makes the next run fail
    with 409, which it did twice against a real cluster."""
    stack = RecordingExecutor()
    orchestrator = FakeMultipassOrchestrator()

    plan = _provisioned_plan(
        RoleBindings(host=RecordingExecutor(), stack=stack), orchestrator=orchestrator
    )
    plan.keep = True
    plan.run()

    summaries = [spec.summary for spec in stack.seen]
    assert "Uninstall Helm release control-plane" not in summaries
    assert "Delete word-stats-java" in summaries
    assert orchestrator.torn_down == []
