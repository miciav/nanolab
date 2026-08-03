"""Fail-closed Sonata evidence verifiers for release artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
import re

from sonata_engine import Evidence, Verifier

from nanolab.release.build import _remote_image_digest
from nanolab.release.state import ArtifactEvidence, digest_path


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
DigestReader = Callable[[str], str | None]

# Every evidence kind a release receipt may carry. A receipt entry outside this
# set is a schema violation, not something to skip over: the whole point of the
# parser is that an unrecognised claim fails the read instead of passing
# unread. `release_evidence_verifiers` must offer a verifier for each one, or a
# phase carrying that kind can never be skipped on resume.
RECEIPT_KINDS = frozenset(
    {
        "file-digest",
        "local-image-digest",
        "local-registry-digest",
        "ghcr-digest",
        "cosign-attestation",
    }
)


def is_sha256_digest(value: str | None) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def receipt_artifacts(path: Path, phase: str, expected_kind: str) -> tuple[ArtifactEvidence, ...]:
    """Parse one release receipt with a single fail-closed schema.

    A phase may record more than one kind of claim -- `attest` records the
    predicate digest *and* one signature per pinned image -- so entries of
    another kind are filtered out rather than rejected. Filtering is only safe
    because every kind must still be one this module recognises: an entry with
    an unknown kind fails the read, so nothing gets silently skipped.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {phase} receipt") from error
    if (
        not isinstance(payload, Mapping)
        or set(payload) - {"phase", "execution", "evidence"}
        or payload.get("phase") != phase
        or ("execution" in payload and not isinstance(payload["execution"], str))
        or not isinstance(payload.get("evidence"), list)
    ):
        raise ValueError(f"invalid {phase} receipt")
    evidence = payload["evidence"]
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"kind", "reference", "digest"}
        or item.get("kind") not in RECEIPT_KINDS
        or not isinstance(item.get("reference"), str)
        or not isinstance(item.get("digest"), str)
        for item in evidence
    ):
        raise ValueError(f"invalid {phase} receipt")
    location = "remote" if expected_kind == "local-registry-digest" else "local"
    return tuple(
        ArtifactEvidence(location, item["reference"], item["digest"])
        for item in evidence
        if item["kind"] == expected_kind
    )


def file_digest_verifier(evidence: Evidence) -> bool:
    """Verify a regular file without raising on missing or unreadable paths."""
    try:
        return (
            is_sha256_digest(evidence.digest)
            and Path(evidence.reference).is_file()
            and digest_path(Path(evidence.reference)) == evidence.digest
        )
    except OSError:
        return False


def image_digest_verifier(read_digest: DigestReader) -> Verifier:
    """Build a verifier around an injected registry/daemon digest lookup."""

    def verify(evidence: Evidence) -> bool:
        if not is_sha256_digest(evidence.digest):
            return False
        try:
            current = read_digest(evidence.reference)
        except Exception:
            return False
        return current == evidence.digest and current is not None

    return verify


def signature_evidence_verifier(evidence: Evidence) -> bool:
    """Verify a recorded signature names exactly the digest it claims to sign.

    Sonata fails a resume closed on any evidence kind it cannot verify, so the
    attest phase needs a verifier or every resume re-signs the whole matrix.

    ponytail: checks the reference is digest-pinned and self-consistent, not
    that the signature is still in the registry. Upgrade to a real
    `cosign verify` once the GHCR authfile is reachable from a verifier --
    today it is staged inside the workflow run, after verifiers are built.
    """
    reference, _, pinned = evidence.reference.partition("@")
    return bool(reference) and pinned == evidence.digest and is_sha256_digest(evidence.digest)


def release_evidence_verifiers(
    provider: object,
    request: object,
    *,
    ghcr_authfile: str | None = None,
) -> dict[str, Verifier]:
    """Return release verifiers; GHCR fails closed until auth is staged."""

    def remote(reference: str, *, authfile: str | None = None) -> str | None:
        return _remote_image_digest(
            provider,
            request,
            "remote",
            reference,
            ghcr_authfile=authfile,
        )

    return {
        "file-digest": file_digest_verifier,
        "cosign-attestation": signature_evidence_verifier,
        "local-image-digest": image_digest_verifier(
            lambda reference: remote(reference) if reference.startswith("docker-daemon:") else None
        ),
        "local-registry-digest": image_digest_verifier(
            lambda reference: (
                remote(reference)
                if reference.startswith("docker://")
                and not reference.startswith("docker://ghcr.io/")
                else None
            )
        ),
        "ghcr-digest": image_digest_verifier(
            lambda reference: (
                remote(reference, authfile=ghcr_authfile)
                if ghcr_authfile is not None and reference.startswith("docker://ghcr.io/")
                else None
            )
        ),
    }
