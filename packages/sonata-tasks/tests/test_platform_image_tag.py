from __future__ import annotations

from sonata_tasks.platform import PlatformFunction, PlatformRequest


def _request(**overrides: object) -> PlatformRequest:
    defaults: dict[str, object] = {
        "backend": "k8s",
        "functions": (
            PlatformFunction(
                name="echo",
                image="localhost:5000/nanofaas/echo:e2e",
                payload="{}",
                build_argv=("./gradlew", "build"),
            ),
        ),
    }
    return PlatformRequest(**{**defaults, **overrides})  # pyright: ignore[reportArgumentType]


def test_without_a_fingerprint_the_tag_stays_fixed() -> None:
    """Nothing to fingerprint (no git) must not mean a random tag every run."""
    assert _request().control_plane_image_reference().endswith(":e2e")


def test_a_fingerprint_names_the_image_after_the_source() -> None:
    reference = _request(source_fingerprint="abc123").control_plane_image_reference()

    assert reference.startswith("localhost:5000/nanofaas/control-plane:e2e-")
    assert not reference.endswith(":e2e")


def test_the_same_source_and_modules_give_the_same_tag() -> None:
    first = _request(source_fingerprint="abc123", additional_modules=("async-queue",))
    second = _request(source_fingerprint="abc123", additional_modules=("async-queue",))

    assert first.control_plane_image_tag() == second.control_plane_image_tag()


def test_different_modules_give_different_tags() -> None:
    """The modules are a build input: the same checkout compiled with and without
    the autoscaler is two different binaries. Sharing one tag would let the
    second one never reach the cluster."""
    with_autoscaler = _request(
        source_fingerprint="abc123",
        additional_modules=("autoscaler", "async-queue", "sync-queue"),
    )
    without = _request(
        source_fingerprint="abc123", additional_modules=("async-queue", "sync-queue")
    )

    assert with_autoscaler.control_plane_image_tag() != without.control_plane_image_tag()


def test_different_backends_give_different_tags() -> None:
    k8s = _request(source_fingerprint="abc123")
    container = _request(backend="container", source_fingerprint="abc123")

    assert k8s.control_plane_image_tag() != container.control_plane_image_tag()


def test_an_explicit_image_still_wins() -> None:
    """Release runs pass an already-published, digest-pinned reference."""
    reference = _request(
        source_fingerprint="abc123",
        build_images=False,
        control_plane_image="ghcr.io/nanofaas/control-plane@sha256:deadbeef",
    ).control_plane_image_reference()

    assert reference == "ghcr.io/nanofaas/control-plane@sha256:deadbeef"
