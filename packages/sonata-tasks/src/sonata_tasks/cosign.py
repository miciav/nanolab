from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.roles import ExecutionRole
from sonata_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

COSIGN_IMAGE = (
    "gcr.io/projectsigstore/cosign@sha256:"
    "f1946d0f30fc8e3777b02f2201e02efdba9fe38f4918162f937052fac98e083f"
)

CosignOperation = Literal[
    "sign", "attest", "attach sbom", "verify", "verify-attestation", "public-key"
]

_ATTACH_SBOM = "attach sbom"
_KEY_COSIGN = "/key.cosign"


class CosignTask(CommandTask):
    """Run a cosign operation via the pinned cosign Docker image.

    The cosign password is read from a file by a shell wrapper and passed to
    the container as an environment variable, so the secret never appears in
    any process argv.

    Operations
    ----------
    sign
        ``cosign sign --yes --key /key.cosign <image>``
    attest
        ``cosign attest --yes --key /key.cosign --type custom --predicate
        /predicate.json <image>``
    attach sbom
        ``cosign attach sbom --sbom /sbom.json --type spdx <image>``
    verify
        ``cosign verify --key /pub.key <image>``
    verify-attestation
        ``cosign verify-attestation --key /pub.key --type custom <image>``
    public-key
        ``cosign public-key --key /key.cosign``, redirected to ``output_file``
    """

    def __init__(
        self,  # NOSONAR (S107): keyword-only task configuration
        *,
        operation: CosignOperation | str,
        image: str,
        key_file: str,
        password_file: str,
        docker_config: str,
        executor: CommandTaskExecutor,
        role: ExecutionRole,
        title: str | None = None,
        cwd: Path | None = None,
        verify: Callable[[TaskResult], None] | None = None,
        predicate_file: str | None = None,
        sbom_file: str | None = None,
        public_key_file: str | None = None,
        output_file: str | None = None,
    ) -> None:
        run = _docker_run_args(
            operation, docker_config, key_file, public_key_file, predicate_file, sbom_file
        )
        cosign = _cosign_command(operation, image, output_file)

        run.append(COSIGN_IMAGE)
        run.extend(cosign)

        if operation == "public-key":
            assert output_file is not None  # validated in the dispatch above
            # output_file is a positional shell parameter ($2), captured into
            # a variable before the shift -- never formatted into the script
            # text, so a value containing `"`, `$`, or a backtick still lands
            # as a literal path, not shell syntax. Redirect instead of exec:
            # public-key writes the key to stdout.
            # COSIGN_PASSWORD names an environment variable; its value comes from "$1".
            script = 'pw=$(cat "$1"); out="$2"; shift 2; COSIGN_PASSWORD="$pw" "$@" > "$out"'  # NOSONAR (S2068)
            positional = (password_file, output_file, *run)
        else:
            # ponytail: shell wrapper reads password file, shifts it away,
            # then execs the real command with the password in the env.
            # COSIGN_PASSWORD names an environment variable; its value comes from "$1".
            script = 'pw=$(cat "$1"); shift; COSIGN_PASSWORD="$pw" exec "$@"'  # NOSONAR (S2068)
            positional = (password_file, *run)

        super().__init__(
            title=title or f"cosign {operation} {image or key_file}",
            # Password is read from a file by the shell wrapper and passed via
            # environment variable -- never appears in the process argv.
            argv=("sh", "-c", script, "--", *positional),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )


def _docker_run_args(
    operation: CosignOperation | str,
    docker_config: str,
    key_file: str,
    public_key_file: str | None,
    predicate_file: str | None,
    sbom_file: str | None,
) -> list[str]:
    """Build the docker run argument list: base flags plus per-operation mounts."""
    run: list[str] = [
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "-e",
        "COSIGN_PASSWORD",
        "-e",
        "DOCKER_CONFIG=/auth",
        "-v",
        f"{docker_config}:/auth:ro",
    ]
    if operation in ("sign", "attest", "public-key"):
        run.extend(["-v", f"{key_file}:/key.cosign:ro"])
    if public_key_file is not None and operation in ("verify", "verify-attestation"):
        run.extend(["-v", f"{public_key_file}:/pub.key:ro"])
    if predicate_file is not None and operation == "attest":
        run.extend(["-v", f"{predicate_file}:/predicate.json:ro"])
    if sbom_file is not None and operation == _ATTACH_SBOM:
        run.extend(["-v", f"{sbom_file}:/sbom.json:ro"])
    return run


def _cosign_command(
    operation: CosignOperation | str, image: str, output_file: str | None
) -> tuple[str, ...]:
    """Build the cosign subcommand run inside the container.

    --yes: these run unattended; without it cosign blocks on a prompt.
    --type: the release predicate is `custom`, and verify-attestation only
    finds an attestation whose type it was told to expect.
    """
    cosign: tuple[str, ...]
    if operation == "sign":
        cosign = ("sign", "--yes", "--key", _KEY_COSIGN, image)
    elif operation == "attest":
        cosign = (
            "attest",
            "--yes",
            "--key",
            _KEY_COSIGN,
            "--type",
            "custom",
            "--predicate",
            "/predicate.json",
            image,
        )
    elif operation == _ATTACH_SBOM:
        cosign = ("attach", "sbom", "--sbom", "/sbom.json", "--type", "spdx", image)
    elif operation == "verify":
        cosign = ("verify", "--key", "/pub.key", image)
    elif operation == "verify-attestation":
        cosign = ("verify-attestation", "--key", "/pub.key", "--type", "custom", image)
    elif operation == "public-key":
        if output_file is None:
            raise ValueError("cosign public-key needs an output_file")
        cosign = ("public-key", "--key", _KEY_COSIGN)
    else:
        raise ValueError(f"unknown cosign operation: {operation}")
    return cosign
