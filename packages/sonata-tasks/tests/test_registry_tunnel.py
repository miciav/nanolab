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

    assert len(provider.calls) == 3
    assert all(seen_request is request for seen_request, _argv in provider.calls)
    assert provider.calls[0][1] == ("sudo", "systemctl", "stop", "nanofaas-registry-tunnel")
    assert provider.calls[1][1] == ("sudo", "systemctl", "reset-failed", "nanofaas-registry-tunnel")
    assert provider.calls[2][1] == (
        "sudo",
        "systemd-run",
        "--unit",
        "nanofaas-registry-tunnel",
        "socat",
        "TCP-LISTEN:5000,fork,reuseaddr",
        "TCP:10.0.0.42:5000",
    )


def test_acquire_tolerates_an_absent_previous_transient_unit():
    class Provider(_RecordingProvider):
        def exec_argv(self, request, argv, *, env=None, cwd=None, dry_run=False):
            self.calls.append((request, argv))
            if argv[:2] == ("sudo", "systemctl"):
                return _result(return_code=5, stderr="Unit nanofaas-registry-tunnel not loaded")
            return _result()

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.42", provider=Provider(), request=object()
    )

    resource.acquire(TaskInputs.empty())


def test_release_stops_the_tunnel():
    provider = _RecordingProvider()
    resource = registry_tunnel_resource(
        registry_upstream="stack-registry.nanofaas.svc.cluster.local",
        provider=provider,
        request=object(),
    )

    resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), None)

    assert len(provider.calls) == 5
    _req, argv = provider.calls[3]
    assert "systemctl" in argv
    assert "stop" in argv
    assert "nanofaas-registry-tunnel" in argv


@pytest.mark.parametrize(("fail_on", "calls"), ((0, 3), (1, 4), (2, 5)))
def test_acquire_raises_and_compensates_on_each_failed_step(fail_on: int, calls: int):
    provider = _RecordingProvider(fail_on=fail_on)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="registry tunnel acquire failed"):
        resource.acquire(TaskInputs.empty())

    assert len(provider.calls) == calls
    assert provider.calls[-2][1] == ("sudo", "systemctl", "stop", "nanofaas-registry-tunnel")
    assert provider.calls[-1][1] == (
        "sudo",
        "systemctl",
        "reset-failed",
        "nanofaas-registry-tunnel",
    )


def test_release_raises_on_provider_failure():
    provider = _RecordingProvider(fail_on=3)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
    )

    resource.acquire(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="registry tunnel release failed"):
        resource.release(TaskInputs.empty(), None)
