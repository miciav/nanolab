from __future__ import annotations

from nanolab.comparison.prepare import (
    function_build_operations,
    pinned_function_images,
    prepare_operations,
)
from nanolab.images.control_plane_variants import resolve_variants
from nanolab.plans.functions import ResolvedFunction

REGISTRY = "127.0.0.1:5000"
MODULES = "k8s-deployment-provider,async-queue,sync-queue"

JAVA = ResolvedFunction(
    key="word-stats-java",
    name="word-stats-java",
    image=f"{REGISTRY}/nanofaas/word-stats-java:e2e",
    build_argv=("./gradlew", ":functions:java:word-stats:bootJar"),
    payload="{}",
    image_build_argv=("docker", "build", "-t", f"{REGISTRY}/nanofaas/word-stats-java:e2e", "."),
)
JS = ResolvedFunction(
    key="word-stats-javascript",
    name="word-stats-javascript",
    image=f"{REGISTRY}/nanofaas/word-stats-javascript:e2e",
    build_argv=("docker", "build", "-t", f"{REGISTRY}/nanofaas/word-stats-javascript:e2e", "."),
    payload="{}",
)


def test_a_function_with_an_artifact_step_builds_it_before_the_image() -> None:
    ops = function_build_operations([JAVA])

    assert [op.operation_id for op in ops] == [
        "prepare.function.word-stats-java.artifact",
        "prepare.function.word-stats-java.image",
        "prepare.function.word-stats-java.push",
    ]


def test_a_function_without_one_goes_straight_to_the_image() -> None:
    """JavaScript has no compile step; emitting an empty one would run the docker build twice."""
    ops = function_build_operations([JS])

    assert [op.operation_id for op in ops] == [
        "prepare.function.word-stats-javascript.image",
        "prepare.function.word-stats-javascript.push",
    ]
    assert ops[0].argv == JS.build_argv


def test_functions_are_built_before_the_native_images() -> None:
    """Learning the checkout does not compile after 40 minutes of native-image work is bad."""
    ops = prepare_operations(
        functions=[JAVA, JS],
        variants=resolve_variants(("native-o3",)),
        registry=REGISTRY,
        modules=MODULES,
    )
    ids = [op.operation_id for op in ops]

    assert ids.index("prepare.function.word-stats-java.image") < ids.index(
        "variant.native-o3.image"
    )


def test_everything_prepared_runs_on_the_vm() -> None:
    for op in prepare_operations(
        functions=[JAVA, JS],
        variants=resolve_variants(("jvm", "native-o3")),
        registry=REGISTRY,
        modules=MODULES,
    ):
        assert op.execution_target == "vm", op.operation_id


def test_pinned_images_are_keyed_by_catalogue_key() -> None:
    """`_resolve_with_prebuilt_images` looks up `key`; a map keyed by `name`
    reports every entry it holds as missing."""
    assert pinned_function_images([JAVA, JS]) == {
        "word-stats-java": JAVA.image,
        "word-stats-javascript": JS.image,
    }
