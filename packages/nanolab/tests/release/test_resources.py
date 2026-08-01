from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.registry_tunnel import registry_tunnel_resource

import nanolab.release.resources as release_resources
from nanolab.config.environment import EnvironmentConfig
from nanolab.release.state import ArtifactEvidence


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {
                "stack": {"name": "nanofaas-azure-release", "disk": "128G"},
                "loadgen": {"name": "nanofaas-azure-release-loadgen", "disk": "30G"},
                "arm-builder": {"name": "nanofaas-azure-release-arm", "disk": "64G"},
            },
            "azure": {
                "resource_group": "release-rg",
                "location": "westeurope",
                "vm_size": "Standard_D8s_v5",
                "loadgen_vm_size": "Standard_D2s_v5",
                "arm_vm_size": "Standard_D8ps_v5",
                "image_urn": "Canonical:ubuntu-24_04-lts:server:24.04.202607140",
                "arm_image_urn": ("Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140"),
                "operator_source_cidr": "8.8.8.8/32",
            },
        }
    )


def test_execution_guard_validates_metadata_before_cloud_resource(tmp_path: Path) -> None:
    events: list[str] = []

    class Credentials:
        def validate(self, *, repo_root: Path):
            events.append(f"validate:{repo_root.name}")
            return self

    guard = release_resources.release_execution_guard(
        Credentials(), repo_roots=(tmp_path / "tool", tmp_path / "source")
    )
    cloud = Resource(
        title="Acquire cloud",
        acquire=lambda _inputs: events.append("cloud"),
        release=lambda _inputs, _value: None,
        requires=(guard,),
        infrastructure=True,
    )

    @dataclass
    class Consume(Task[None]):
        title: str = "Consume cloud"

        def run(self, _inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    workflow = Workflow("guard-order")
    workflow.add(Consume(), requires=(cloud,))
    workflow.run()

    assert events == ["validate:tool", "validate:source", "cloud"]


class FakeProvider:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_ensure: str | None = None
        self.fail_facts: str | None = None
        self.restrictions: list[tuple[str, tuple[int, ...], tuple[str, ...], int]] = []
        self.hosts = {
            "nanofaas-azure-release": "20.0.0.10",
            "nanofaas-azure-release-loadgen": "20.0.0.11",
            "nanofaas-azure-release-arm": "20.0.0.12",
        }

    def ensure_running(self, request):
        name = request.name
        self.events.append(f"ensure:{name}")
        if name == self.fail_ensure:
            raise RuntimeError(f"ensure failed: {name}")
        return SimpleNamespace(return_code=0)

    def connection_host(self, request):
        return self.hosts[request.name]

    def teardown(self, request):
        self.events.append(f"destroy:{request.name}")
        return SimpleNamespace(return_code=0)

    def release_vm_facts(self, request):
        name = request.name
        self.events.append(f"facts:{name}")
        if name == self.fail_facts:
            return SimpleNamespace(
                location="westeurope",
                vm_size="wrong",
                disk_size_gb=30,
                image_urn="wrong",
            )
        loadgen = name.endswith("-loadgen")
        arm = name.endswith("-arm")
        return SimpleNamespace(
            location="westeurope",
            vm_size=(
                "Standard_D8ps_v5" if arm else "Standard_D2s_v5" if loadgen else "Standard_D8s_v5"
            ),
            disk_size_gb=64 if arm else 30 if loadgen else 128,
            image_urn=(
                "Canonical:ubuntu-24_04-lts:server-arm64:24.04.202607140"
                if arm
                else "Canonical:ubuntu-24_04-lts:server:24.04.202607140"
            ),
        )

    def restrict_inbound_sources(self, request, *, ports, source_cidrs, priority_base=1010) -> None:
        self.events.append(f"restrict:{request.name}:{','.join(map(str, ports))}")
        self.restrictions.append((request.name, ports, source_cidrs, priority_base))


@dataclass
class ConsumeEndpoints(Task[None]):
    endpoints: Resource[release_resources.ReleaseEndpoints]
    title: str = "Consume release endpoints"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        endpoints = inputs.resource(self.endpoints)
        assert endpoints.control_plane == "http://20.0.0.10:30080"
        assert endpoints.prometheus == "http://20.0.0.10:30090"
        return TaskOutcome(value=None)


def _workflow(provider: FakeProvider, monkeypatch: pytest.MonkeyPatch) -> tuple[Workflow, object]:
    def bootstrap(_environment, _provider, _root, role, _request, _info) -> None:
        provider.events.append(f"bootstrap:{role}")

    monkeypatch.setattr(release_resources, "_bootstrap_role", bootstrap)
    resources = release_resources.build_release_resources(_environment(), Path("/repo"), provider)
    workflow = Workflow("release-resources")
    workflow.add(
        ConsumeEndpoints(resources.endpoints),
        requires=(*resources.vms, resources.endpoints),
    )
    return workflow, resources


def test_release_resources_acquire_verify_secure_and_bootstrap_in_role_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    workflow, _resources = _workflow(provider, monkeypatch)

    workflow.run()

    assert [
        event for event in provider.events if event.startswith(("ensure:", "facts:", "bootstrap:"))
    ] == [
        "ensure:nanofaas-azure-release",
        "facts:nanofaas-azure-release",
        "bootstrap:stack",
        "ensure:nanofaas-azure-release-loadgen",
        "facts:nanofaas-azure-release-loadgen",
        "bootstrap:loadgen",
        "ensure:nanofaas-azure-release-arm",
        "facts:nanofaas-azure-release-arm",
        "bootstrap:arm-builder",
    ]
    assert provider.restrictions == [
        (
            "nanofaas-azure-release",
            (30080, 30081, 30090),
            ("8.8.8.8/32",),
            1010,
        ),
        (
            "nanofaas-azure-release",
            (30080, 30081, 30090),
            ("20.0.0.11/32", "8.8.8.8/32"),
            1010,
        ),
        (
            "nanofaas-azure-release",
            (5000,),
            ("20.0.0.12/32",),
            1020,
        ),
    ]
    assert [event for event in provider.events if event.startswith("destroy:")] == [
        "destroy:nanofaas-azure-release-arm",
        "destroy:nanofaas-azure-release-loadgen",
        "destroy:nanofaas-azure-release",
    ]


def test_failed_ensure_compensates_and_cleans_up_already_acquired_vms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    provider.fail_ensure = "nanofaas-azure-release-loadgen"
    workflow, _resources = _workflow(provider, monkeypatch)

    with pytest.raises(RuntimeError, match="ensure failed"):
        workflow.run()

    assert [event for event in provider.events if event.startswith("destroy:")] == [
        "destroy:nanofaas-azure-release-loadgen",
        "destroy:nanofaas-azure-release",
    ]


def test_post_ensure_fact_failure_stops_before_bootstrap_and_compensates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider()
    provider.fail_facts = "nanofaas-azure-release-loadgen"
    workflow, _resources = _workflow(provider, monkeypatch)

    with pytest.raises(RuntimeError, match="facts mismatch"):
        workflow.run()

    assert "bootstrap:loadgen" not in provider.events
    assert [event for event in provider.events if event.startswith("destroy:")] == [
        "destroy:nanofaas-azure-release-loadgen",
        "destroy:nanofaas-azure-release",
    ]


def test_keep_retains_only_vm_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    workflow, resources = _workflow(provider, monkeypatch)
    released: list[str] = []
    ancillary = Resource(
        title="Acquire staged secret",
        acquire=lambda _inputs: "secret",
        release=lambda _inputs, _value: released.append("secret"),
        requires=(resources.endpoints,),
    )
    workflow.add(ConsumeEndpoints(resources.endpoints), requires=(ancillary, resources.endpoints))
    workflow.keep_infrastructure = True

    workflow.run()

    assert released == ["secret"]
    assert not [event for event in provider.events if event.startswith("destroy:")]
    assert all(resource.infrastructure for resource in resources.vms)
    assert resources.endpoints.infrastructure is False


def test_keep_does_not_retain_the_registry_tunnel() -> None:
    tunnel = registry_tunnel_resource(
        registry_upstream="stack",
        provider=object(),
        request=object(),
    )

    assert tunnel.infrastructure is False


def test_source_archive_is_created_once_and_checksum_verified_on_stack_and_arm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staged: list[tuple[object, str]] = []
    digest = "sha256:" + "d" * 64

    def create(_root: Path, _commit: str, destination: Path) -> ArtifactEvidence:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"archive")
        return ArtifactEvidence("local", str(destination), digest)

    def stage(_provider, request, **kwargs) -> None:
        assert kwargs["expected_digest"] == digest
        staged.append((request, kwargs["remote_source_dir"]))

    monkeypatch.setattr(release_resources, "create_source_archive", create)
    monkeypatch.setattr(release_resources, "stage_source_archive", stage)
    stack, arm = object(), object()
    resources = release_resources.build_release_source_resources(
        repo_root=tmp_path,
        commit="a" * 40,
        run_dir=tmp_path / "run",
        remote_source_dir="/home/user/nanofaas-release/v1/source",
        remote_archive="/home/user/nanofaas-release/v1/source.tar",
        provider=SimpleNamespace(exec_argv=lambda *_args, **_kwargs: None),
        stack_request=stack,
        arm_request=arm,
    )
    # Acquire/release directly keeps the contract test independent of a dummy task.
    local = resources.local.acquire(TaskInputs.empty())
    inputs = TaskInputs._for_resources({resources.local: local}, {resources.local})
    resources.stack.acquire(inputs)
    resources.arm.acquire(inputs)

    assert staged == [
        (stack, "/home/user/nanofaas-release/v1/source"),
        (arm, "/home/user/nanofaas-release/v1/source"),
    ]


def test_arm_build_inputs_transfer_bake_and_buildkit_and_cleanup_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_resources, "render_bake_json", lambda _plan: "{}\n")

    class Provider:
        def __init__(self) -> None:
            self.transfers: list[tuple[str, str]] = []
            self.commands: list[tuple[str, ...]] = []

        def transfer_to(self, _request, *, source: Path, destination: str):
            self.transfers.append((source.name, destination))
            return SimpleNamespace(return_code=1 if len(self.transfers) == 2 else 0, stderr="boom")

        def exec_argv(self, _request, argv):
            self.commands.append(argv)
            return SimpleNamespace(return_code=0)

    provider = Provider()
    resource = release_resources.arm_build_inputs_resource(
        image_plan=SimpleNamespace(cells=()),
        max_parallelism=3,
        run_dir=tmp_path,
        remote_root="/home/user/nanofaas-release/v1",
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="buildkitd.toml"):
        resource.acquire(TaskInputs.empty())

    assert provider.transfers == [
        (
            "docker-bake-arm64.json",
            "/home/user/nanofaas-release/v1/docker-bake-arm64.json",
        ),
        ("buildkitd.toml", "/home/user/nanofaas-release/v1/buildkitd.toml"),
    ]
    assert provider.commands[-1] == (
        "rm",
        "-f",
        "--",
        "/home/user/nanofaas-release/v1/docker-bake-arm64.json",
        "/home/user/nanofaas-release/v1/buildkitd.toml",
    )
    assert not (tmp_path / "docker-bake-arm64.json").exists()
    assert not (tmp_path / "buildkitd.toml").exists()


@pytest.mark.parametrize(
    ("source", "archive"),
    (
        ("relative/source", "relative/source.tar"),
        ("/", "/source.tar"),
        ("/home/user/nanofaas-release/v1/../source", "/home/user/nanofaas-release/v1/source.tar"),
        ("/tmp/source", "/tmp/source.tar"),
        ("/home/user/nanofaas-release/v1/source", "/home/user/nanofaas-release/v2/source.tar"),
        ("/home/user/nanofaas-release/v1/source.tar", "/home/user/nanofaas-release/v1/source"),
    ),
)
def test_release_source_resources_reject_unsafe_remote_paths(
    source: str, archive: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="release remote"):
        release_resources.build_release_source_resources(
            repo_root=tmp_path,
            commit="a" * 40,
            run_dir=tmp_path,
            remote_source_dir=source,
            remote_archive=archive,
            provider=object(),
            stack_request=object(),
            arm_request=object(),
        )


@pytest.mark.parametrize(
    "remote_root",
    ("relative", "/", "/tmp/release", "/home/user/nanofaas-release/v1/.."),
)
def test_arm_inputs_reject_unsafe_remote_root(remote_root: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="release remote"):
        release_resources.arm_build_inputs_resource(
            image_plan=SimpleNamespace(cells=()),
            max_parallelism=1,
            run_dir=tmp_path,
            remote_root=remote_root,
            provider=object(),
            request=object(),
        )


def test_source_resource_normal_release_propagates_remote_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "run" / "source.tar"
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(
        release_resources,
        "create_source_archive",
        lambda _root, _commit, destination: (
            destination.parent.mkdir(parents=True, exist_ok=True),
            destination.write_bytes(b"source"),
            ArtifactEvidence("local", str(destination), digest),
        )[-1],
    )
    monkeypatch.setattr(release_resources, "stage_source_archive", lambda *_args, **_kwargs: None)

    class Provider:
        def exec_argv(self, _request, _argv):
            return SimpleNamespace(return_code=1, stderr="cleanup failed")

    resources = release_resources.build_release_source_resources(
        repo_root=tmp_path,
        commit="a" * 40,
        run_dir=archive.parent,
        remote_source_dir="/home/user/nanofaas-release/v1/source",
        remote_archive="/home/user/nanofaas-release/v1/source.tar",
        provider=Provider(),
        stack_request=object(),
        arm_request=object(),
    )
    local = resources.local.acquire(TaskInputs.empty())
    inputs = TaskInputs._for_resources({resources.local: local}, {resources.local})
    state = resources.stack.acquire(inputs)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        resources.stack.release(inputs, state)


def test_arm_inputs_normal_release_propagates_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_resources, "render_bake_json", lambda _plan: "{}\n")

    class Provider:
        failing = False

        def transfer_to(self, _request, *, source, destination):
            return SimpleNamespace(return_code=0)

        def exec_argv(self, _request, _argv):
            return SimpleNamespace(
                return_code=1 if self.failing else 0,
                stderr="cleanup failed" if self.failing else "",
            )

    provider = Provider()
    resource = release_resources.arm_build_inputs_resource(
        image_plan=SimpleNamespace(cells=()),
        max_parallelism=1,
        run_dir=tmp_path,
        remote_root="/home/user/nanofaas-release/v1",
        provider=provider,
        request=object(),
    )
    state = resource.acquire(TaskInputs.empty())
    provider.failing = True

    with pytest.raises(RuntimeError, match="cleanup failed"):
        resource.release(TaskInputs.empty(), state)

    assert not (tmp_path / "docker-bake-arm64.json").exists()
    assert not (tmp_path / "buildkitd.toml").exists()
