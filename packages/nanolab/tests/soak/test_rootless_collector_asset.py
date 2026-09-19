"""Exercise the installed VM helper's real ownership and PID selection route."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ASSET = Path(__file__).parents[2] / "assets/containerd-rootless/soak_collect.py"


def _module():
    spec = importlib.util.spec_from_file_location("rootless_soak_collect", ASSET)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_function_inspection_reads_owned_containerd_task_not_docker(monkeypatch):
    module = _module()
    calls = []
    identifier = "nanofaas-word-stats-java-0123456789-r1"

    def command(*argv, **kwargs):
        calls.append(argv)
        if argv[0] == "bash":
            return json.dumps({"ID": identifier, "Image": "127.0.0.1:5000/fn:run123"})
        if argv[0] == "ctr":
            return f"TASK PID STATUS\n{identifier} 123 RUNNING\n"
        if argv[0] == "nerdctl":
            return json.dumps(
                [
                    {
                        "RepoDigests": ["127.0.0.1:5000/fn@sha256:" + "b" * 64],
                        "Architecture": "arm64",
                        "Os": "linux",
                    }
                ]
            )
        raise AssertionError(argv)

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(
        module, "run_paths", lambda run: (Path("/state"), "/socket", "nanofaas-run123")
    )
    monkeypatch.setattr(module, "start_ticks", lambda pid: "456")
    monkeypatch.setattr(Path, "readlink", lambda path: Path("/usr/bin/java"))
    value = module.inspect(
        "run123",
        "word-stats-java",
        Path("/home/ubuntu/nanofaas"),
        Path("/home/ubuntu/nanolab-assets/containerd-rootless/session.sh"),
    )
    assert value["container_id"] == identifier
    assert value["process_id"] == 123
    assert value["image_digest"].endswith("b" * 64)
    assert value["platform"] == "linux/arm64"
    assert [item[0] for item in calls] == ["bash", "ctr", "nerdctl"]
    assert "inspect-owned" in calls[0]
    assert calls[0][1] == "/home/ubuntu/nanolab-assets/containerd-rootless/session.sh"


def test_function_inspection_rejects_task_without_running_pid(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "command", lambda *a, **k: "TASK PID STATUS\n")
    with pytest.raises(ValueError, match="absent or not running"):
        module.task_pid("/socket", "nanofaas-run123", "a" * 64)


def test_control_plane_platform_comes_from_actual_host(monkeypatch):
    module = _module()
    monkeypatch.setattr(module.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    assert module.host_platform() == "linux/arm64"
    monkeypatch.setattr(module.platform, "machine", lambda: "riscv64")
    with pytest.raises(ValueError, match="unsupported"):
        module.host_platform()


def test_function_exposition_enters_owning_user_and_network_namespaces(monkeypatch):
    module = _module()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=b"function_metric 1\n")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.exposition({"role": "word-stats-java", "process_id": 123}, 2) == (
        "function_metric 1\n"
    )
    argv, options = calls[0]
    assert argv[:8] == (
        "nsenter",
        "--target",
        "123",
        "--user",
        "--preserve-credentials",
        "--net",
        "--",
        "curl",
    )
    assert options["timeout"] == 3
    assert options["check"] is True
