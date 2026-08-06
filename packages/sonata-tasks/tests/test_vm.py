from __future__ import annotations

import pytest
from sonata_engine import Resource, TaskInputs
from sonata_tasks.vm.models import VmConfig, VmInfo

from sonata_tasks.vm import vm_resource


class FakeLifecycle:
    def __init__(self, result: VmInfo | BaseException) -> None:
        self.result = result
        self.configs: list[VmConfig] = []
        self.destroyed: list[VmInfo] = []
        self.destroy_error: BaseException | None = None

    def ensure_running(self, config: VmConfig) -> VmInfo:
        self.configs.append(config)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def destroy(self, info: VmInfo) -> None:
        self.destroyed.append(info)
        if self.destroy_error is not None:
            raise self.destroy_error


def _info(name: str = "created") -> VmInfo:
    return VmInfo(name=name, host="10.0.0.1", user="ubuntu", home="/home/ubuntu")


def _resource(
    lifecycle: FakeLifecycle,
    *,
    fallback_info: VmInfo | None = None,
    external: bool = False,
) -> Resource[VmInfo]:
    return vm_resource(
        title="Acquire VM",
        lifecycle=lifecycle,  # type: ignore[arg-type]
        config=VmConfig(name="target"),
        fallback_info=fallback_info or _info("fallback"),
        external=external,
    )


def test_acquire_returns_the_exact_info_from_ensure_running() -> None:
    lifecycle = FakeLifecycle(_info())

    acquired = _resource(lifecycle).acquire(TaskInputs.empty())

    assert acquired is lifecycle.result
    assert lifecycle.configs == [VmConfig(name="target")]


def test_release_destroys_the_exact_acquired_value_without_mutable_state() -> None:
    lifecycle = FakeLifecycle(_info())
    resource = _resource(lifecycle)
    acquired = resource.acquire(TaskInputs.empty())
    another_info = _info("another")

    resource.release(TaskInputs.empty(), another_info)

    assert acquired is lifecycle.result
    assert lifecycle.destroyed == [another_info]


def test_external_vm_is_not_destroyed() -> None:
    lifecycle = FakeLifecycle(_info())
    resource = _resource(lifecycle, external=True)

    resource.release(TaskInputs.empty(), _info())

    assert lifecycle.destroyed == []


def test_failed_acquire_compensates_by_destroying_fallback_info() -> None:
    failure = RuntimeError("ensure failed")
    lifecycle = FakeLifecycle(failure)
    fallback = _info("fallback")

    with pytest.raises(RuntimeError, match="ensure failed"):
        _resource(lifecycle, fallback_info=fallback).acquire(TaskInputs.empty())

    assert lifecycle.destroyed == [fallback]


def test_failed_compensation_is_noted_without_masking_acquire_failure() -> None:
    failure = RuntimeError("ensure failed")
    lifecycle = FakeLifecycle(failure)
    lifecycle.destroy_error = RuntimeError("destroy failed")

    with pytest.raises(RuntimeError, match="ensure failed") as caught:
        _resource(lifecycle).acquire(TaskInputs.empty())

    assert "destroy failed" in "\n".join(caught.value.__notes__)


def test_resource_is_retained_by_keep() -> None:
    assert _resource(FakeLifecycle(_info())).always_release is False
