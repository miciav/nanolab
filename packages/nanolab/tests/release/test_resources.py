from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.registry_tunnel import registry_tunnel_resource

import nanolab.release.resources as release_resources
from nanolab.config.environment import EnvironmentConfig


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
