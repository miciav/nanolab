from __future__ import annotations

from pathlib import Path

import pytest

from nanolab.cli.execution import prometheus_over_ssh
from nanolab.config.environment import EnvironmentConfig

class _FakeProcess:
    def __init__(self, *, exits_with: int | None = None, stderr: bytes = b"") -> None:
        self._exits_with = exits_with
        self.terminated = False
        self.killed = False
        self.stderr = _FakeStderr(stderr)

    def poll(self) -> int | None:
        return self._exits_with

    def terminate(self) -> None:
        self.terminated = True
        self._exits_with = -15

    def kill(self) -> None:
        self.killed = True
        self._exits_with = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._exits_with or 0

class _FakeStderr:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

def _multipass() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        {"provider": "multipass", "roles": {"stack": {"name": "nanofaas-stack"}}}
    )

def _spawn(recorder: list[list[str]], process: _FakeProcess):  # pyright: ignore[reportMissingParameterType]
    def spawn(argv, **_kwargs):  # pyright: ignore[reportMissingParameterType]
        recorder.append(list(argv))
        return process
    return spawn

def test_it_forwards_the_vm_port_to_loopback() -> None:
    commands: list[list[str]] = []
    process = _FakeProcess()

    with prometheus_over_ssh(
        _multipass(),
        "http://192.168.64.3:30090",
        spawn=_spawn(commands, process),
        ready=lambda _port: True,
    ) as url:
        assert url.startswith("http://127.0.0.1:")
        port = int(url.rsplit(":", 1)[1])

    argv = commands[0]
    assert argv[0] == "ssh"
    assert "-N" in argv
    assert f"127.0.0.1:{port}:127.0.0.1:30090" in argv
    assert argv[-1] == "ubuntu@192.168.64.3"

def test_it_closes_the_tunnel_on_the_way_out() -> None:
    process = _FakeProcess()

    with prometheus_over_ssh(
        _multipass(),
        "http://192.168.64.3:30090",
        spawn=_spawn([], process),
        ready=lambda _port: True,
    ):
        assert not process.terminated

    assert process.terminated

def test_it_closes_the_tunnel_when_the_body_raises() -> None:
    process = _FakeProcess()

    with pytest.raises(ZeroDivisionError):
        with prometheus_over_ssh(
            _multipass(),
            "http://192.168.64.3:30090",
            spawn=_spawn([], process),
            ready=lambda _port: True,
        ):
            _ = 1 / 0

    assert process.terminated

def test_an_ssh_that_dies_reports_its_own_error() -> None:
    """A tunnel that never came up must not surface later as a Prometheus
    timeout three tasks away."""
    process = _FakeProcess(exits_with=255, stderr=b"bind: Address already in use")

    with pytest.raises(RuntimeError, match="Address already in use"):
        with prometheus_over_ssh(
            _multipass(),
            "http://192.168.64.3:30090",
            spawn=_spawn([], process),
            ready=lambda _port: False,
        ):
            pass

def test_a_tunnel_that_never_answers_times_out() -> None:
    process = _FakeProcess()

    with pytest.raises(RuntimeError, match="never accepted a connection"):
        with prometheus_over_ssh(
            _multipass(),
            "http://192.168.64.3:30090",
            spawn=_spawn([], process),
            ready=lambda _port: False,
            timeout_seconds=0.3,
        ):
            pass

    assert process.terminated

def test_a_local_environment_is_left_alone() -> None:
    def spawn(*_args, **_kwargs):  # pyright: ignore[reportMissingParameterType]
        raise AssertionError("a local run has nothing to forward")

    with prometheus_over_ssh(
        EnvironmentConfig(provider="local"), "http://127.0.0.1:9090", spawn=spawn
    ) as url:
        assert url == "http://127.0.0.1:9090"

def test_an_explicit_url_is_left_alone() -> None:
    """The caller may already be forwarding it themselves."""

    def spawn(*_args, **_kwargs):  # pyright: ignore[reportMissingParameterType]
        raise AssertionError("an explicit URL must be used as given")

    with prometheus_over_ssh(
        _multipass(), "http://elsewhere:9090", enabled=False, spawn=spawn
    ) as url:
        assert url == "http://elsewhere:9090"


def _azure() -> EnvironmentConfig:
    import yaml

    return EnvironmentConfig.model_validate(
        yaml.safe_load(
            Path("packages/nanolab/environments/azure-comparison.yaml.example").read_text()
        )
    )


def test_azure_is_tunnelled_too() -> None:
    """Not for multipass's reason, but it lands in the same place.

    The host otherwise reaches Prometheus at the public address, which the
    security rules admit only from the operator's own address — and a domestic
    connection changed address three times in one afternoon here, twice in the
    middle of a cell. SSH is not narrowed that way, so the tunnel is immune to a
    drift that was costing a cell each time.
    """
    commands: list[list[str]] = []

    with prometheus_over_ssh(
        _azure(),
        "http://20.101.80.57:30090",
        spawn=_spawn(commands, _FakeProcess()),
        ready=lambda _port: True,
    ) as url:
        assert url.startswith("http://127.0.0.1:")

    argv = commands[0]
    assert argv[0] == "ssh"
    assert argv[-1].endswith("@20.101.80.57")
