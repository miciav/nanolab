from dataclasses import dataclass
from pathlib import Path

import pytest

import nanolab.cli.execution as execution
from nanolab.cli.execution import build_role_bindings, resolve_loadtest_urls
from nanolab.config.environment import EnvironmentConfig
from sonata_tasks.tasks.models import CommandTaskSpec
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.vm.models import VmRequest


@dataclass
class _Result:
    """A real type, so the checker can see what the runner hands back."""

    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None, dict[str, str], bool]] = []

    def run(self, command, /, *, cwd, env, dry_run):
        self.calls.append((command, cwd, env, dry_run))
        return _Result()


class RecordingVmProvider:
    def __init__(self) -> None:
        self.exec_calls = []
        self.fetch_calls = []

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> _Result:
        self.exec_calls.append((request, tuple(argv), env, remote_dir, dry_run))
        return _Result()

    def transfer_from(self, request: VmRequest, *, source: str, destination: Path) -> _Result:
        self.fetch_calls.append((request, source, destination))
        return _Result()

    def transfer_to(self, request: VmRequest, *, source: Path, destination: str) -> _Result:
        del request, source, destination
        return _Result()

    def connection_host(self, request):
        return "20.30.40.50"

    def guest_host(self, request):
        return "10.0.0.50"

    def publish_port(self, request, *, service, guest_port):
        assert service == "PROMETHEUS_HTTP"
        assert guest_port == 30090
        return "pve.example", 43090


def test_external_stack_uses_ssh_in_remote_repository() -> None:
    runner = RecordingRunner()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {"stack": {"host": "vm.example", "user": "alice", "home": "/srv/alice"}},
        }
    )

    bindings, _ = build_role_bindings(environment, runner=runner)
    bindings.executor_for("stack").run(
        CommandTaskSpec(
            task_id="check",
            summary="check",
            argv=("ansible-playbook", "site.yml"),
            role="stack",
        )
    )

    assert runner.calls[0][0][:4] == ["ssh", "-o", "BatchMode=yes", "alice@vm.example"]
    assert (
        "cd /srv/alice/nanofaas && env KUBECONFIG=/srv/alice/.kube/config ansible-playbook site.yml"
    ) in runner.calls[0][0][-1]


def test_external_stack_maps_local_project_cwd_to_remote_subdirectory(tmp_path) -> None:
    runner = RecordingRunner()
    root = tmp_path / "project with spaces"
    root.mkdir()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {"stack": {"host": "vm.example", "user": "alice", "home": "/srv/alice"}},
        }
    )

    bindings, _ = build_role_bindings(environment, runner=runner, repo_root=root)
    bindings.executor_for("stack").run(
        CommandTaskSpec(
            task_id="check",
            summary="check",
            argv=("pwd",),
            role="stack",
            options=CommandOptions(cwd=root / "deploy" / "charts"),
        )
    )

    assert "cd /srv/alice/nanofaas/deploy/charts && env " in runner.calls[0][0][-1]
    assert runner.calls[0][0][-1].endswith(" pwd'")


def test_external_stack_rejects_cwd_outside_the_synced_project(tmp_path) -> None:
    runner = RecordingRunner()
    root = tmp_path / "project"
    root.mkdir()
    environment = EnvironmentConfig.model_validate(
        {"provider": "external", "roles": {"stack": {"host": "vm.example"}}}
    )
    bindings, _ = build_role_bindings(environment, runner=runner, repo_root=root)

    with pytest.raises(ValueError, match="outside project root"):
        bindings.executor_for("stack").run(
            CommandTaskSpec(
                task_id="check",
                summary="check",
                argv=("pwd",),
                role="stack",
                options=CommandOptions(cwd=tmp_path / "elsewhere"),
            )
        )


def test_remote_command_rejects_both_local_and_remote_directory(tmp_path) -> None:
    runner = RecordingRunner()
    root = tmp_path / "project"
    root.mkdir()
    environment = EnvironmentConfig.model_validate(
        {"provider": "external", "roles": {"stack": {"host": "vm.example"}}}
    )
    bindings, _ = build_role_bindings(environment, runner=runner, repo_root=root)

    with pytest.raises(ValueError, match="both cwd and remote_dir"):
        bindings.executor_for("stack").run(
            CommandTaskSpec(
                task_id="check",
                summary="check",
                argv=("pwd",),
                role="stack",
                options=CommandOptions(cwd=root, remote_dir="/explicit"),
            )
        )


def test_container_loadtest_uses_compose_ports() -> None:
    assert resolve_loadtest_urls(
        EnvironmentConfig(provider="local"),
        backend="container",
    ) == (
        "http://127.0.0.1:8080",
        "http://127.0.0.1:9090",
    )


def test_multipass_stack_uses_provider_ssh_execution(monkeypatch) -> None:
    runner = RecordingRunner()
    provider = RecordingVmProvider()
    monkeypatch.setattr(
        execution, "VmOrchestrator", lambda *_args, **_kwargs: provider, raising=False
    )
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
    )

    bindings, _ = build_role_bindings(environment, runner=runner)
    bindings.executor_for("stack").run(
        CommandTaskSpec(task_id="check", summary="check", argv=("kubectl", "version"), role="stack")
    )

    request, argv, env, cwd, dry_run = provider.exec_calls[0]
    assert request.lifecycle == "multipass"
    assert request.name == "nanofaas-stack"
    assert argv == ("kubectl", "version")
    assert env == {"KUBECONFIG": "/home/ubuntu/.kube/config"}
    assert cwd == "/home/ubuntu/nanofaas"
    assert runner.calls == []


def test_a_role_without_defaults_sends_no_environment(monkeypatch) -> None:
    """Roles that carry no defaults hand the provider None, not an empty map:
    the providers treat the two the same, and only one of them says 'nothing to
    export' to a reader."""
    runner = RecordingRunner()
    provider = RecordingVmProvider()
    monkeypatch.setattr(
        execution, "VmOrchestrator", lambda *_args, **_kwargs: provider, raising=False
    )
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {"stack": {"name": "nanofaas-stack"}, "loadgen": {"name": "nanofaas-load"}},
        }
    )

    bindings, _ = build_role_bindings(environment, runner=runner)
    bindings.executor_for("loadgen").run(
        CommandTaskSpec(task_id="k6", summary="k6", argv=("k6", "version"), role="loadgen")
    )

    _, _, env, _, _ = provider.exec_calls[0]
    assert env is None


def test_remote_stack_exports_its_kubeconfig() -> None:
    runner = RecordingRunner()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {
                    "host": "vm.example",
                    "home": "/srv/nanofaas",
                    "kubeconfig": "/etc/nanofaas/kubeconfig",
                }
            },
        }
    )

    bindings, _ = build_role_bindings(environment, runner=runner)
    bindings.executor_for("stack").run(
        CommandTaskSpec(task_id="check", summary="check", argv=("kubectl", "get", "nodes"))
    )

    assert "env KUBECONFIG=/etc/nanofaas/kubeconfig kubectl get nodes" in runner.calls[0][0][-1]


def test_distinct_external_loadgen_gets_distinct_executor_and_fetcher() -> None:
    runner = RecordingRunner()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "external",
            "roles": {
                "stack": {"host": "stack.example"},
                "loadgen": {"host": "load.example"},
            },
        }
    )

    bindings, fetcher = build_role_bindings(environment, runner=runner)

    assert bindings.executor_for("loadgen") is not bindings.executor_for("stack")
    assert fetcher is not None


def test_multipass_three_role_environment_binds_cloud_like_stack(monkeypatch) -> None:
    runner = RecordingRunner()
    provider = RecordingVmProvider()
    monkeypatch.setattr(execution, "VmOrchestrator", lambda *_args, **_kwargs: provider)
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "multipass",
            "roles": {
                "stack": {"name": "nanofaas-edge"},
                "cloud": {"name": "nanofaas-cloud"},
                "loadgen": {"name": "nanofaas-loadgen"},
            },
        }
    )

    bindings, _ = build_role_bindings(environment, runner=runner)

    bindings.executor_for("cloud").run(
        CommandTaskSpec(task_id="check", summary="check", argv=("kubectl", "version"), role="cloud")
    )

    request, argv, env, cwd, dry_run = provider.exec_calls[0]
    assert request.name == "nanofaas-cloud"
    assert argv == ("kubectl", "version")
    assert env == {"KUBECONFIG": "/home/ubuntu/.kube/config"}
    assert cwd == "/home/ubuntu/nanofaas"


def test_external_loadtest_urls_use_stack_node_ports() -> None:
    environment = EnvironmentConfig.model_validate(
        {"provider": "external", "roles": {"stack": {"host": "stack.example"}}}
    )

    urls = resolve_loadtest_urls(environment)

    assert urls == ("http://stack.example:30080", "http://stack.example:30090")


def test_multipass_loadtest_urls_resolve_instance_address() -> None:
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
    )

    urls = resolve_loadtest_urls(environment, host_resolver=lambda _: "10.20.30.40")

    assert urls == ("http://10.20.30.40:30080", "http://10.20.30.40:30090")


def test_explicit_loadtest_urls_do_not_resolve_stack_address() -> None:
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
    )

    urls = resolve_loadtest_urls(
        environment,
        control_plane_url="https://control.example",
        prometheus_url="https://metrics.example",
        host_resolver=lambda _: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )

    assert urls == ("https://control.example", "https://metrics.example")


def test_dry_run_uses_stable_multipass_placeholder() -> None:
    environment = EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
    )

    urls = resolve_loadtest_urls(environment, dry_run=True)

    assert urls == (
        "http://<multipass-ip:nanofaas-stack>:30080",
        "http://<multipass-ip:nanofaas-stack>:30090",
    )


def test_azure_role_uses_provider_native_execution_and_fetch(tmp_path: Path) -> None:
    provider = RecordingVmProvider()
    runner = RecordingRunner()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {"stack": {}},
            "azure": {"resource_group": "rg", "location": "westeurope"},
        }
    )

    bindings, fetcher = build_role_bindings(
        environment, runner=runner, vm_provider=provider, repo_root=tmp_path
    )
    bindings.executor_for("stack").run(
        CommandTaskSpec(task_id="check", summary="check", argv=("kubectl", "get", "nodes"))
    )
    assert fetcher is not None
    fetcher.fetch_from("/tmp/result.json", tmp_path / "result.json")

    request, argv, env, cwd, dry_run = provider.exec_calls[0]
    assert request.lifecycle == "azure"
    assert argv == ("kubectl", "get", "nodes")
    assert env == {"KUBECONFIG": "/home/azureuser/.kube/config"}
    assert cwd == "/home/azureuser"
    assert provider.fetch_calls[0][0].lifecycle == "azure"
    assert runner.calls == []


def test_proxmox_role_uses_provider_native_execution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROXMOX_PASSWORD", "secret")
    provider = RecordingVmProvider()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "proxmox",
            "roles": {"stack": {}},
            "proxmox": {"host": "pve.example", "node": "pve1"},
        }
    )

    bindings, _ = build_role_bindings(environment, vm_provider=provider, repo_root=tmp_path)
    bindings.executor_for("stack").run(
        CommandTaskSpec(task_id="check", summary="check", argv=("kubectl", "version"))
    )

    assert provider.exec_calls[0][0].lifecycle == "proxmox"
    assert provider.exec_calls[0][0].proxmox_password == "secret"


def test_azure_loadtest_urls_use_public_ip() -> None:
    provider = RecordingVmProvider()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "azure",
            "roles": {"stack": {}},
            "azure": {"resource_group": "rg", "location": "westeurope"},
        }
    )

    assert resolve_loadtest_urls(environment, vm_provider=provider) == (
        "http://20.30.40.50:30080",
        "http://20.30.40.50:30090",
    )


def test_proxmox_loadtest_urls_use_guest_network_and_published_prometheus(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROXMOX_PASSWORD", "secret")
    provider = RecordingVmProvider()
    environment = EnvironmentConfig.model_validate(
        {
            "provider": "proxmox",
            "roles": {"stack": {}},
            "proxmox": {"host": "pve.example", "node": "pve1"},
        }
    )

    assert resolve_loadtest_urls(environment, vm_provider=provider) == (
        "http://10.0.0.50:30080",
        "http://pve.example:43090",
    )
