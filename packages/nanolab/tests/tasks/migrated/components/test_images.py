from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from nanolab.tasks.components.context import (
    ResolvedFunctionView,
    ScenarioExecutionContext,
)
from nanolab.tasks.components.images import (
    control_image,
    function_image_specs,
    plan_build_core,
    warm_echo_image,
)
from nanolab.tasks.vm.models import VmRequest


WORKSPACE_ROOT = Path(__file__).resolve().parents[6]
LIVE_E2E_SCENARIO_IMAGE_CONSUMERS = (
    "packages/nanolab/src/nanolab/tasks/components/images.py",
    "packages/nanolab/src/nanolab/plans/validate.py",
)
REMOVED_RELEASE_IMAGE_CLI_SYNTAX = ("--arch-suffix", "--arch multi")


@dataclass
class _Fn:
    key: str
    family: str | None
    runtime: str
    image: str | None


@dataclass
class _RS:
    namespace: str | None
    functions: Sequence[ResolvedFunctionView]


def _ctx(*, runtime: str = "java", functions: list | None = None) -> ScenarioExecutionContext:
    return ScenarioExecutionContext(
        repo_root=Path("/repo"),
        scenario_name="s",
        runtime=runtime,
        namespace="ns",
        local_registry="localhost:5000",
        resolved_scenario=_RS(namespace="ns", functions=functions or []),
        vm_request=VmRequest(lifecycle="multipass", name="nanofaas-e2e", user="ubuntu"),
        cleanup_vm=True,
    )


def test_image_name_helpers() -> None:
    assert control_image("reg:5000") == "reg:5000/nanofaas/control-plane:e2e"
    assert warm_echo_image("reg:5000") == "reg:5000/nanofaas/java-warm-echo:e2e"


def test_e2e_image_components_keep_local_e2e_tags() -> None:
    assert control_image("localhost:5000") == "localhost:5000/nanofaas/control-plane:e2e"
    assert warm_echo_image("localhost:5000") == "localhost:5000/nanofaas/java-warm-echo:e2e"


def test_live_e2e_scenario_image_consumers_do_not_use_removed_images_cli_syntax() -> None:
    violations = {
        str(path.relative_to(WORKSPACE_ROOT)): syntax
        for relative_path in LIVE_E2E_SCENARIO_IMAGE_CONSUMERS
        for path in [WORKSPACE_ROOT / relative_path]
        for syntax in REMOVED_RELEASE_IMAGE_CLI_SYNTAX
        if syntax in path.read_text(encoding="utf-8")
    }

    assert violations == {}


def test_function_image_specs_skips_fixtures_and_familyless() -> None:
    fns = [
        _Fn(key="a", family="echo", runtime="java", image=None),
        _Fn(key="b", family=None, runtime="java", image=None),
        _Fn(key="c", family="x", runtime="fixture", image=None),
    ]
    specs = function_image_specs(_RS(namespace=None, functions=fns), "fallback:img")
    assert [s[3] for s in specs] == ["a"]
    assert specs[0][0] == "fallback:img"


def test_plan_build_core_java_builds_jars_and_pushes() -> None:
    ids = [op.operation_id for op in plan_build_core(_ctx(runtime="java"))]
    assert "images.build_core.boot_jars" in ids
    assert "images.build_core.control_image" in ids
    assert "images.build_core.warm_echo_image" in ids
    assert "images.build_core.push_warm_echo_image" in ids


def test_plan_build_core_rust_skips_boot_jars() -> None:
    ids = [op.operation_id for op in plan_build_core(_ctx(runtime="rust"))]
    assert "images.build_core.boot_jars" not in ids
    assert "images.build_core.control_image" in ids
