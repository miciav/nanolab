"""Fail-closed Sonata evidence verifiers for release artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re

from sonata_engine import Evidence, Verifier

from nanolab.release.build import _remote_image_digest
from nanolab.release.state import digest_path


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
DigestReader = Callable[[str], str | None]


def is_sha256_digest(value: str | None) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


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
