from pathlib import Path

from sonata_engine import Evidence

from nanolab.release.evidence import (
    file_digest_verifier,
    image_digest_verifier,
    release_evidence_verifiers,
)
from nanolab.release.state import digest_path


def test_file_digest_verifier_fails_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "receipt.json"
    artifact.write_text("original", encoding="utf-8")
    evidence = Evidence("file-digest", str(artifact), digest_path(artifact))

    assert file_digest_verifier(evidence)
    artifact.write_text("changed", encoding="utf-8")
    assert not file_digest_verifier(evidence)
    artifact.unlink()
    assert not file_digest_verifier(evidence)


def test_image_digest_verifier_fails_closed() -> None:
    digest = "sha256:" + "a" * 64
    evidence = Evidence("local-registry-digest", "docker://localhost:5000/image:v1", digest)

    assert image_digest_verifier(lambda _reference: digest)(evidence)
    assert not image_digest_verifier(lambda _reference: "sha256:" + "c" * 64)(evidence)
    assert not image_digest_verifier(lambda _reference: "not-a-digest")(evidence)
    assert not image_digest_verifier(lambda _reference: None)(evidence)

    def unreachable(_reference: str) -> str:
        raise OSError("registry unavailable")

    assert not image_digest_verifier(unreachable)(evidence)


def test_authenticated_ghcr_verifier_uses_authfile_without_exposing_token(
    monkeypatch,
) -> None:
    digest = "sha256:" + "b" * 64
    seen: list[tuple[str, str | None]] = []

    def inspect(_provider, _request, _location, reference, *, ghcr_authfile=None):
        seen.append((reference, ghcr_authfile))
        return digest

    monkeypatch.setattr("nanolab.release.evidence._remote_image_digest", inspect)
    token = "fixture-ghcr-token-must-not-leak"
    verifiers = release_evidence_verifiers(object(), object(), ghcr_authfile="/staged/auth.json")
    evidence = Evidence("ghcr-digest", "docker://ghcr.io/nanofaas/image:v1", digest)
    assert verifiers["ghcr-digest"](evidence)
    assert seen == [(evidence.reference, "/staged/auth.json")]
    assert token not in repr(verifiers)
    assert verifiers.get("unknown-kind") is None
    assert not release_evidence_verifiers(object(), object())["ghcr-digest"](evidence)
