"""Tests for FileTransferTask."""

from dataclasses import dataclass
from pathlib import Path

from sonata_engine import TaskInputs

from sonata_tasks.transfer import FileTransferTask


class _FakeTransferProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str]] = []

    def transfer_to(self, request, *, source, destination):
        self.calls.append((source, destination))
        return type("Result", (), {"return_code": 0, "stdout": "", "stderr": ""})()


def test_file_transfer_invokes_provider_and_returns_none():
    provider = _FakeTransferProvider()
    request = object()
    task = FileTransferTask(
        source=Path("/tmp/bake.json"),
        destination="/home/user/bake.json",
        provider=provider,
        request=request,
    )
    outcome = task.run(TaskInputs.empty())
    assert outcome.value is None
    assert provider.calls == [(Path("/tmp/bake.json"), "/home/user/bake.json")]


def test_file_transfer_title_defaults_to_source_filename():
    task = FileTransferTask(
        source=Path("/tmp/buildkitd.toml"),
        destination="/remote/buildkitd.toml",
        provider=object(),
        request=object(),
    )
    assert task.title == "Transfer buildkitd.toml"


def test_file_transfer_raises_on_nonzero_exit():
    import pytest

    class _FailingProvider:
        def transfer_to(self, request, *, source, destination):
            return type("Result", (), {"return_code": 1, "stdout": "", "stderr": "disk full"})()

    task = FileTransferTask(
        source=Path("/tmp/bake.json"), destination="/remote/bake.json",
        provider=_FailingProvider(), request=object(),
    )
    with pytest.raises(RuntimeError, match="Transfer bake.json failed"):
        task.run(TaskInputs.empty())
