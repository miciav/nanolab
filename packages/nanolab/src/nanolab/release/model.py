"""The release plan data shared by everything that reads a release.

Extracted from the procedural runner it used to live in: these are plain
descriptions of what a release is, not part of how one executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import subprocess

from nanolab.config import EnvironmentConfig, ScenarioConfig
from nanolab.images.plan import ImagePlan
from nanolab.release.secrets import validate_secret_file
from nanolab.release.versioning import normalize_version


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_KNOWN_FIXTURE_SECRETS = (
    "fixture-secret-must-not-leak",
    "fixture-ghcr-token-must-not-leak",
    "fixture-cosign-key-must-not-leak",
    "fixture-cosign-password-must-not-leak",
)


class ResumeValidationError(ValueError):
    """Raised when a caller supplies invalid release evidence."""


@dataclass(frozen=True, slots=True)
class GitState:
    commit: str
    clean: bool


@dataclass(frozen=True, slots=True)
class CredentialFiles:
    ghcr_token: Path = field(repr=False)
    cosign_key: Path = field(repr=False)
    cosign_password: Path | None = field(repr=False)

    def validate(self, *, repo_root: Path | None = None) -> "CredentialFiles":
        if self.cosign_password is None:
            raise ValueError("cosign password file is required")
        paths = (self.ghcr_token, self.cosign_key, self.cosign_password)
        for path in paths:
            validate_secret_file(path)
        if repo_root is not None:
            root = Path(repo_root).resolve(strict=True)
            for path in paths:
                try:
                    path.resolve(strict=True).relative_to(root)
                except ValueError:
                    continue
                raise ValueError("release credential files must be outside the repository")
        return self


@dataclass(frozen=True, slots=True)
class BuilderConfiguration:
    name: str
    max_parallelism: int


@dataclass(frozen=True, slots=True)
class ReleaseSettings:
    max_parallelism: int
    scenario: Path
    scenario_name: str
    benchmark_runs: int
    profile: str
    throughput_max_loss_percent: float
    p95_max_increase_percent: float
    error_rate_max: float


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Values that must match exactly before a phase may be reused."""

    source_commit: str
    prepared_version: str
    release_config_digest: str
    environment_digest: str

    def __post_init__(self) -> None:
        _reject_fixture_secret(self.source_commit)
        _reject_fixture_secret(self.prepared_version)
        _reject_fixture_secret(self.release_config_digest)
        _reject_fixture_secret(self.environment_digest)
        if not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise ResumeValidationError("source commit must be a 40-character lowercase SHA")
        try:
            normalized, _ = normalize_version(self.prepared_version)
        except ValueError as error:
            raise ResumeValidationError("prepared version must be a semantic version") from error
        if normalized != self.prepared_version:
            raise ResumeValidationError("prepared version must not use a container-tag prefix")
        for field_name in ("release_config_digest", "environment_digest"):
            if not _DIGEST.fullmatch(getattr(self, field_name)):
                raise ResumeValidationError(f"{field_name} must be a sha256 digest")

    def as_entry(self) -> dict[str, str]:
        return {
            "sourceCommit": self.source_commit,
            "preparedVersion": self.prepared_version,
            "releaseConfigDigest": self.release_config_digest,
            "environmentDigest": self.environment_digest,
        }


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    """A local file or remote object whose digest makes a phase reusable."""

    location: str
    reference: str
    digest: str

    def __post_init__(self) -> None:
        _reject_fixture_secret(self.location)
        _reject_fixture_secret(self.reference)
        _reject_fixture_secret(self.digest)
        if self.location not in {"local", "remote"}:
            raise ResumeValidationError("artifact location must be local or remote")
        if not self.reference:
            raise ResumeValidationError("artifact reference must not be empty")
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError("artifact digest must be a sha256 digest")

    def as_entry(self) -> dict[str, str]:
        return {"location": self.location, "reference": self.reference, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class Amd64ReleasePlan:
    repo_root: Path
    run_dir: Path
    version: str
    identity: ReleaseIdentity
    environment: EnvironmentConfig
    scenario: ScenarioConfig
    settings: ReleaseSettings
    image_plan: ImagePlan
    builder: BuilderConfiguration
    bake_file: Path
    buildkit_config: Path
    performance_root: Path
    credentials: CredentialFiles | None = field(repr=False)


def git_state(repo_root: Path) -> GitState:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ("git", "status", "--porcelain=v1"),
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return GitState(commit=commit, clean=not status.strip())


def digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _reject_fixture_secret(value: str) -> None:
    if any(secret in value for secret in _KNOWN_FIXTURE_SECRETS):
        raise ValueError("release journal values must not contain fixture secrets")
