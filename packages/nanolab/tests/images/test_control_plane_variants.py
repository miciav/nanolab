from __future__ import annotations

import pytest

from nanolab.images.control_plane_variants import (
    VARIANTS,
    build_operations,
    resolve_variants,
)

REGISTRY = "localhost:5000"
MODULES = "k8s-deployment-provider,async-queue,sync-queue"


def _variant(key: str):
    return next(v for v in VARIANTS if v.key == key)


def test_every_variant_produces_a_distinct_image_tag() -> None:
    """Two variants sharing a tag would silently benchmark one build twice."""
    tags = [variant.image(REGISTRY) for variant in VARIANTS]
    assert len(set(tags)) == len(tags)


def test_resolve_variants_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown control-plane variants: nope"):
        resolve_variants(("jvm", "nope"))


def test_jvm_variant_builds_the_boot_jar_before_the_image() -> None:
    """The JVM Dockerfile copies a jar; without the gradle step it packages a stale one."""
    ops = build_operations(_variant("jvm"), registry=REGISTRY, modules=MODULES)

    assert [op.operation_id for op in ops] == [
        "variant.jvm.boot_jar",
        "variant.jvm.image",
        "variant.jvm.push",
    ]
    assert f"-PcontrolPlaneModules={MODULES}" in ops[0].argv


def test_native_variants_pass_their_flags_through_the_environment() -> None:
    os_ops = build_operations(_variant("native-os"), registry=REGISTRY, modules=MODULES)
    g1_ops = build_operations(_variant("native-o3-g1"), registry=REGISTRY, modules=MODULES)

    assert os_ops[0].argv[:2] == ("./scripts/native-java-image.sh", "control-plane")
    assert os_ops[0].env["NATIVE_OPTIMIZATION"] == "s"
    assert "NATIVE_GC" not in os_ops[0].env
    assert g1_ops[0].env["NATIVE_OPTIMIZATION"] == "3"
    assert g1_ops[0].env["NATIVE_GC"] == "G1"


def test_every_variant_compiles_the_same_module_set() -> None:
    """A variant built with different modules would compare two platforms, not two builds."""
    for variant in VARIANTS:
        ops = build_operations(variant, registry=REGISTRY, modules=MODULES)
        selectors = [
            arg
            for op in ops
            for arg in (*op.argv, *op.env.values())
            if MODULES in arg
        ]
        assert selectors, f"{variant.key} does not pin the module set"


def test_every_variant_runs_on_the_vm() -> None:
    """A build that ran on the host would produce an arm64 image for an amd64 node."""
    for variant in VARIANTS:
        for op in build_operations(variant, registry=REGISTRY, modules=MODULES):
            assert op.execution_target == "vm", op.operation_id


def test_a_tuned_jvm_variant_is_built_as_a_jvm_image_not_a_native_one() -> None:
    """key == "jvm" would send every tuned JVM down the native-image path."""
    ops = build_operations(_variant("jvm-c2"), registry=REGISTRY, modules="all")
    build = next(op for op in ops if op.operation_id.endswith(".image"))

    assert "platform/control-plane/Dockerfile" in build.argv
    assert "--build-arg" in build.argv
    assert "JVM_TUNING=-XX:+UseSerialGC" in build.argv


def test_the_baseline_jvm_build_line_is_left_exactly_as_it_was() -> None:
    """A --build-arg the variant never asked for would move its cache key."""
    ops = build_operations(_variant("jvm"), registry=REGISTRY, modules="all")
    build = next(op for op in ops if op.operation_id.endswith(".image"))

    assert "--build-arg" not in build.argv
