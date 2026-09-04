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


def test_every_variant_carries_its_key_into_the_build() -> None:
    """Without this, a built image's /modules/build-metadata cannot say which
    comparison variant produced it: the key has to travel with the build
    itself, not be reconstructed later from the image tag.
    """
    for variant in VARIANTS:
        ops = build_operations(variant, registry=REGISTRY, modules=MODULES)
        values = [value for op in ops for value in (*op.argv, *op.env.values())]
        assert any(variant.key in value for value in values), variant.key


def test_jvm_variants_pass_build_type_and_optimization() -> None:
    ops = build_operations(_variant("jvm-c2"), registry=REGISTRY, modules=MODULES)
    boot_jar = next(op for op in ops if op.operation_id.endswith(".boot_jar"))

    assert "-PnanofaasBuildType=jvm" in boot_jar.argv
    assert "-PnanofaasBuildVariant=jvm-c2" in boot_jar.argv
    assert "-PnanofaasBuildOptimization=c2" in boot_jar.argv


def test_jvm_c1_only_variants_report_c1_optimization() -> None:
    """jvm and jvm-g1 both omit TieredStopAtLevel's absence-of-flag reversal:
    only the two variants without the C1-only flag get full tiering."""
    for key in ("jvm", "jvm-g1"):
        ops = build_operations(_variant(key), registry=REGISTRY, modules=MODULES)
        boot_jar = next(op for op in ops if op.operation_id.endswith(".boot_jar"))
        assert "-PnanofaasBuildOptimization=c1" in boot_jar.argv


def test_native_variants_pass_the_variant_key_through_the_environment() -> None:
    ops = build_operations(_variant("native-o3-g1"), registry=REGISTRY, modules=MODULES)
    assert ops[0].env["NANOFAAS_BUILD_VARIANT"] == "native-o3-g1"


def test_loop_count_variants_set_the_event_loop_system_property() -> None:
    """A build-arg without ioWorkerCount would produce the default-loop-count
    image under a loop-count-variant tag - the whole point of the cell lost
    silently."""
    for key in ("jvm-loop1", "jvm-c2-loop1"):
        ops = build_operations(_variant(key), registry=REGISTRY, modules=MODULES)
        build = next(op for op in ops if op.operation_id.endswith(".image"))
        tuning_args = [arg for arg in build.argv if arg.startswith("JVM_TUNING=")]
        assert tuning_args, f"{key} did not pass JVM_TUNING to the image build"
        assert "-Dreactor.netty.ioWorkerCount=1" in tuning_args[0]


def test_loop_count_variants_pair_with_their_baseline_jit_setting() -> None:
    """jvm-loop1 isolates the loop-count axis by holding jvm's C1/serial
    setting fixed; jvm-c2-loop1 does the same against jvm-c2's C2/serial. Get
    either backwards and the 2x2 in Esperimento B compares the wrong cells."""
    loop1 = build_operations(_variant("jvm-loop1"), registry=REGISTRY, modules=MODULES)
    c2_loop1 = build_operations(_variant("jvm-c2-loop1"), registry=REGISTRY, modules=MODULES)
    loop1_boot_jar = next(op for op in loop1 if op.operation_id.endswith(".boot_jar"))
    c2_loop1_boot_jar = next(op for op in c2_loop1 if op.operation_id.endswith(".boot_jar"))

    assert "-PnanofaasBuildOptimization=c1" in loop1_boot_jar.argv
    assert "-PnanofaasBuildOptimization=c2" in c2_loop1_boot_jar.argv
