from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path

import pytest

from nanolab.functions.catalog import list_functions
from nanolab.images.plan import NATIVE_JAVA_DOCKERFILE, build_image_plan


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
REGISTRY = "registry.test:5000/nanofaas"


def _plan(**kwargs):  # noqa: ANN003, ANN202
    return build_image_plan(NANOFAAS_ROOT, "v0.18.0", registry=REGISTRY, **kwargs)


def _function_image_name(runtime: str, family: str) -> str:
    prefix = {"exec": "bash", "java-lite": "java-lite"}.get(runtime, runtime)
    return f"{prefix}-{family}"


def test_plan_contains_platform_and_every_discovered_function() -> None:
    plan = _plan()
    expected_functions = {
        _function_image_name(function.runtime, function.family)
        for function in list_functions()
        if function.example_dir is not None
    }

    assert {"control-plane", "java-warm-echo", "watchdog"}.issubset(plan.target_names)
    assert expected_functions.issubset(plan.target_names)
    assert "tool-metrics-echo" not in plan.target_names


def _spring_targets() -> set[str]:
    """Platform Spring apps plus every full-Java function in the live catalog."""
    return {"control-plane", "java-warm-echo"} | {
        _function_image_name(function.runtime, function.family)
        for function in list_functions()
        if function.runtime == "java" and function.example_dir is not None
    }


def _java_lite_targets() -> set[str]:
    return {
        _function_image_name(function.runtime, function.family)
        for function in list_functions()
        if function.runtime == "java-lite" and function.example_dir is not None
    }


def test_plan_expands_every_target_across_both_architectures() -> None:
    """The catalog grows, so assert the expansion rule rather than a cell count."""
    plan = _plan()
    cells = [(cell.target.name, cell.architecture, cell.flavor) for cell in plan.cells]

    assert len(cells) == len(set(cells))
    amd64 = {(name, flavor) for name, arch, flavor in cells if arch == "amd64"}
    arm64 = {(name, flavor) for name, arch, flavor in cells if arch == "arm64"}
    assert amd64 == arm64
    assert {target.name for target in plan.targets} == {name for name, _ in amd64}


def test_runtime_flavors_are_expanded_by_build_capability() -> None:
    plan = _plan()
    flavors_by_target = {
        target.name: {cell.flavor for cell in plan.cells if cell.target == target}
        for target in plan.targets
    }

    spring_targets = _spring_targets()
    java_lite_targets = _java_lite_targets()
    assert {name: flavors_by_target[name] for name in spring_targets} == {
        name: {"jvm", "native"} for name in spring_targets
    }
    assert {name: flavors_by_target[name] for name in java_lite_targets} == {
        name: {"native"} for name in java_lite_targets
    }
    assert all(
        flavors == {"default"}
        for name, flavors in flavors_by_target.items()
        if name not in spring_targets | java_lite_targets
    )


def test_candidate_tags_include_architecture_and_non_default_flavor() -> None:
    plan = _plan()

    assert all(
        cell.tag
        == (
            f"v0.18.0-{cell.architecture}"
            if cell.flavor == "default"
            else f"v0.18.0-{cell.architecture}-{cell.flavor}"
        )
        for cell in plan.cells
    )
    assert all(cell.image == f"{REGISTRY}/{cell.target.name}:{cell.tag}" for cell in plan.cells)


def test_default_candidate_registry_is_stack_local_never_ghcr() -> None:
    plan = build_image_plan(NANOFAAS_ROOT, "v0.18.0", selectors=("watchdog",))

    assert plan.registry == "127.0.0.1:5000/nanofaas"
    assert all(cell.image.startswith("127.0.0.1:5000/nanofaas/") for cell in plan.cells)
    assert all("ghcr.io" not in cell.image for cell in plan.cells)


def test_all_amd64_cells_precede_all_arm64_cells() -> None:
    architectures = [cell.architecture for cell in _plan().cells]

    order = {"amd64": 0, "arm64": 1}
    assert architectures == sorted(architectures, key=lambda a: order[a])


def test_each_discovered_function_dockerfile_maps_to_one_target() -> None:
    plan = _plan()
    function_dockerfiles = {
        function.example_dir.relative_to(NANOFAAS_ROOT) / "Dockerfile"
        for function in list_functions()
        if function.example_dir is not None
    }
    mapped = [
        target.dockerfile
        for target in plan.targets
        if target.dockerfile in function_dockerfiles
    ]

    assert set(mapped) == function_dockerfiles
    assert len(mapped) == len(function_dockerfiles)


def test_plan_cells_expand_symmetrically_per_architecture() -> None:
    """Every cell bakes from a Dockerfile now; assert the expansion rule, not a count.

    Each target contributes exactly its declared flavors as cells, and the
    resulting (target, flavor) shape is identical on both architectures.
    """
    plan = _plan()

    shapes_by_architecture = {
        architecture: frozenset(
            (cell.target.name, cell.flavor)
            for cell in plan.cells
            if cell.architecture == architecture
        )
        for architecture in ("amd64", "arm64")
    }
    assert shapes_by_architecture["amd64"]
    assert shapes_by_architecture["amd64"] == shapes_by_architecture["arm64"]
    for target in plan.targets:
        cell_flavors = frozenset(
            cell.flavor for cell in plan.cells if cell.target.name == target.name
        )
        assert cell_flavors == frozenset(target.flavors)


def test_target_selector_filters_before_cell_expansion() -> None:
    plan = _plan(selectors=("watchdog", "java-word-stats"))

    assert plan.target_names == frozenset({"watchdog", "java-word-stats"})
    assert [(cell.target.name, cell.architecture, cell.flavor) for cell in plan.cells] == [
        ("java-word-stats", "amd64", "jvm"),
        ("java-word-stats", "amd64", "native"),
        ("watchdog", "amd64", "default"),
        ("java-word-stats", "arm64", "jvm"),
        ("java-word-stats", "arm64", "native"),
        ("watchdog", "arm64", "default"),
    ]


def test_target_selector_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown image target: missing"):
        _plan(selectors=("missing",))


def test_spring_jvm_cells_require_boot_jar_before_bake() -> None:
    plan = _plan(selectors=("control-plane", "java-warm-echo", "java-roman-numeral"))
    commands = {
        cell.target.name: cell.prerequisite_command
        for cell in plan.cells
        if cell.architecture == "amd64" and cell.flavor == "jvm"
    }

    assert commands == {
        "control-plane": (
            "./gradlew",
            ":control-plane:bootJar",
            "-PcontrolPlaneModules=all",
            "-PnanofaasBuildType=jvm",
            "-PnanofaasBuildVariant=jvm-g1-c2",
            "-PnanofaasBuildOptimization=c2",
        ),
        "java-warm-echo": ("./gradlew", ":services:java:warm-echo:bootJar"),
        "java-roman-numeral": ("./gradlew", ":functions:java:roman-numeral:bootJar"),
    }


def test_plan_metadata_is_immutable() -> None:
    plan = _plan(selectors=("watchdog",))

    with pytest.raises(FrozenInstanceError):
        plan.cells[0].tag = "mutable"  # type: ignore[misc]


def test_plan_discovers_functions_under_the_given_root(tmp_path: Path) -> None:
    """The matrix follows the root it is handed, not NANOFAAS_ROOT.

    Before this, _all_targets read the global catalog while _function_target
    computed example_dir.relative_to(repo_root), so a divergence raised an
    opaque ValueError instead of planning the given tree.
    """
    for relative in (
        "platform/control-plane",
        "services/java/warm-echo",
        "runtimes/watchdog",
        "functions/python/solo",
    ):
        target = tmp_path / relative
        target.mkdir(parents=True)
        (target / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / "functions/python/solo/function.yaml").write_text(
        "name: solo\nruntime: python\nfamily: solo\n", encoding="utf-8"
    )
    # control-plane and java-warm-echo carry a native_build, so the shared
    # native Dockerfile must exist too, or _validate_targets rejects the tree.
    (tmp_path / NATIVE_JAVA_DOCKERFILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / NATIVE_JAVA_DOCKERFILE).write_text("FROM scratch\n", encoding="utf-8")

    plan = build_image_plan(tmp_path, "v0.0.1", registry=REGISTRY)

    # control-plane, java-warm-echo and watchdog are hardcoded platform targets;
    # everything else must come from the given root, so nothing from the real
    # NANOFAAS_ROOT checkout may appear here.
    assert plan.target_names == frozenset(
        {"control-plane", "java-warm-echo", "watchdog", "python-solo"}
    )


def test_java_native_cells_build_from_the_shared_native_dockerfile() -> None:
    plan = _plan(architectures=("amd64",))
    native = [
        cell
        for cell in plan.cells
        if cell.flavor == "native" and cell.target.native_build is not None
    ]
    assert native, "expected Java native cells in the matrix"
    for cell in native:
        assert cell.dockerfile == NATIVE_JAVA_DOCKERFILE
        assert cell.context == Path(".")
        assert set(cell.build_args) == {
            "NATIVE_TASK",
            "NATIVE_BINARY",
            "GRADLE_ARGS",
            "GRAALVM_DISTRIBUTION",
        }


def test_control_plane_native_cell_carries_the_script_build_args() -> None:
    plan = _plan(architectures=("amd64",))
    cell = next(
        cell
        for cell in plan.cells
        if cell.target.name == "control-plane" and cell.flavor == "native"
    )
    assert cell.build_args == {
        "NATIVE_TASK": ":control-plane:nativeCompile",
        "NATIVE_BINARY": "platform/control-plane/build/native/nativeCompile/control-plane",
        "GRADLE_ARGS": (
            "-PcontrolPlaneModules=all -PnativeOptimization=3 -PnativeGc=G1 "
            "-PnanofaasBuildType=native -PnanofaasBuildVariant=native-o3-g1 "
            "-PnanofaasBuildOptimization=3"
        ),
        "GRAALVM_DISTRIBUTION": "oracle",
    }


def test_function_images_do_not_receive_control_plane_build_metadata() -> None:
    """The control plane's identity travels with its own build only: a function
    rebuilt with -PnanofaasBuildType etc. would be a second moving part in every
    comparison the variant matrix is trying to isolate (see control_plane_variants).
    """
    plan = _plan(architectures=("amd64",))
    for cell in plan.cells:
        if cell.target.name == "control-plane":
            continue
        if cell.flavor == "jvm":
            assert cell.prerequisite_command is not None
            assert not any("-Pnanofaas" in arg for arg in cell.prerequisite_command)
        elif cell.flavor == "native" and cell.target.native_build is not None:
            assert "-Pnanofaas" not in cell.build_args["GRADLE_ARGS"]


def test_java_release_cells_use_jvm_g1_c2_and_native_o3_g1_profiles() -> None:
    plan = _plan(architectures=("amd64",))

    for cell in plan.cells:
        if cell.flavor == "jvm":
            assert cell.build_args == {"JVM_TUNING": "-XX:+UseG1GC"}
        elif cell.flavor == "native" and cell.target.native_build is not None:
            assert "-PnativeOptimization=3" in cell.build_args["GRADLE_ARGS"]
            assert "-PnativeGc=G1" in cell.build_args["GRADLE_ARGS"]
            assert cell.build_args["GRAALVM_DISTRIBUTION"] == "oracle"


def test_java_function_native_cells_derive_task_and_binary_from_the_family() -> None:
    plan = _plan(architectures=("amd64",))
    checked = 0
    for cell in plan.cells:
        native = cell.target.native_build
        if cell.flavor != "native" or native is None:
            continue
        if not cell.target.name.startswith("java-") or cell.target.name == "java-warm-echo":
            continue
        family = cell.target.name.removeprefix("java-")
        assert native.task == f":functions:java:{family}:nativeCompile"
        assert native.binary == Path(
            f"functions/java/{family}/build/native/nativeCompile/{family}"
        )
        checked += 1
    assert checked, "expected Java function native cells in the matrix"


def test_no_cell_invokes_the_retired_buildpack_task() -> None:
    for cell in _plan(architectures=("amd64",)).cells:
        native = cell.target.native_build
        assert native is None or "bootBuildImage" not in native.task


def test_jvm_and_default_cells_keep_their_own_dockerfile_and_context() -> None:
    for cell in _plan(architectures=("amd64",)).cells:
        if cell.flavor == "native" and cell.target.native_build is not None:
            continue
        assert cell.dockerfile == cell.target.dockerfile
        assert cell.context == cell.target.context
        assert cell.build_args == (
            {"JVM_TUNING": "-XX:+UseG1GC"} if cell.flavor == "jvm" else {}
        )
