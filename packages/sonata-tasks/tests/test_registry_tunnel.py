from dataclasses import dataclass, field

import pytest
from sonata_engine import TaskInputs

from sonata_tasks.registry_tunnel import registry_tunnel_resource


def _result(*, return_code: int = 0, stdout: str = "", stderr: str = "") -> object:
    return type("Result", (), {"return_code": return_code, "stdout": stdout, "stderr": stderr})()


@dataclass
class _RecordingProvider:
    calls: list[tuple[object, tuple[str, ...]]] = field(default_factory=list)
    fail_on: int | None = None

    def exec_argv(self, request, argv, *, env=None, cwd=None, dry_run=False) -> object:
        self.calls.append((request, argv))
        if self.fail_on is not None and len(self.calls) - 1 == self.fail_on:
            return _result(return_code=1, stderr="command failed")
        return _result(return_code=0)


def test_acquire_runs_socat_tunnel_with_upstream_host():
    provider = _RecordingProvider()
    request = object()
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.42",
        provider=provider,
        request=request,
    )

    resource.acquire(TaskInputs.empty())

    assert len(provider.calls) == 1
    _req, argv = provider.calls[0]
    assert _req is request
    assert "systemd-run" in argv
    assert "--unit" in argv
    assert "nanofaas-registry-tunnel" in argv
    assert "socat" in argv
    assert "TCP-LISTEN:5000,fork,reuseaddr" in argv
    assert "TCP:10.0.0.42:5000" in argv


def test_release_stops_the_tunnel():
    provider = _RecordingProvider()
    resource = registry_tunnel_resource(
        registry_upstream="stack-registry.nanofaas.svc.cluster.local",
        provider=provider,
        request=object(),
    )

    resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), None)

    assert len(provider.calls) == 2
    _req, argv = provider.calls[1]
    assert "systemctl" in argv
    assert "stop" in argv
    assert "nanofaas-registry-tunnel" in argv


def test_acquire_raises_on_provider_failure():
    provider = _RecordingProvider(fail_on=0)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="registry tunnel acquire failed"):
        resource.acquire(TaskInputs.empty())


def test_release_raises_on_provider_failure():
    provider = _RecordingProvider(fail_on=1)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
    )

    resource.acquire(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="registry tunnel release failed"):
        resource.release(TaskInputs.empty(), None)
