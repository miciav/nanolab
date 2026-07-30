from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask

COSIGN_IMAGE = (
    "gcr.io/projectsigstore/cosign@sha256:"
    "f1946d0f30fc8e3777b02f2201e02efdba9fe38f4918162f937052fac98e083f"
)

CosignOperation = Literal[
    "sign", "attest", "attach sbom", "verify", "verify-attestation"
]


class CosignTask(CommandTask):
    """Run a cosign operation via the pinned cosign Docker image.

    The cosign password is read from a file by a shell wrapper and passed to
    the container as an environment variable, so the secret never appears in
    any process argv.

    Operations
    ----------
    sign
        ``cosign sign --key /key.cosign <image>``
    attest
        ``cosign attest --key /key.cosign --predicate /predicate.json <image>``
    attach sbom
        ``cosign attach sbom --sbom /sbom.json <image>``
    verify
        ``cosign verify --key /pub.key <image>``
    verify-attestation
        ``cosign verify-attestation --key /pub.key <image>``
    """

    def __init__(
        self,
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
    ) -> None:
        # Build the docker run arguments.
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
        if operation in ("sign", "attest"):
            run.extend(["-v", f"{key_file}:/key.cosign:ro"])
        if public_key_file is not None and operation in ("verify", "verify-attestation"):
            run.extend(["-v", f"{public_key_file}:/pub.key:ro"])
        if predicate_file is not None and operation == "attest":
            run.extend(["-v", f"{predicate_file}:/predicate.json:ro"])
        if sbom_file is not None and operation == "attach sbom":
            run.extend(["-v", f"{sbom_file}:/sbom.json:ro"])

        # Build the cosign subcommand run inside the container.
        cosign: tuple[str, ...]
        if operation == "sign":
            cosign = ("sign", "--key", "/key.cosign", image)
        elif operation == "attest":
            cosign = (
                "attest",
                "--key",
                "/key.cosign",
                "--predicate",
                "/predicate.json",
                image,
            )
        elif operation == "attach sbom":
            cosign = ("attach", "sbom", "--sbom", "/sbom.json", image)
        elif operation == "verify":
            cosign = ("verify", "--key", "/pub.key", image)
        elif operation == "verify-attestation":
            cosign = ("verify-attestation", "--key", "/pub.key", image)
        else:
            raise ValueError(f"unknown cosign operation: {operation}")

        run.append(COSIGN_IMAGE)
        run.extend(cosign)

        super().__init__(
            title=title or f"cosign {operation} {image}",
            # Password is read from a file by the shell wrapper and passed via
            # environment variable -- never appears in the process argv.
            argv=(
                "sh",
                "-c",
                # ponytail: shell wrapper reads password file, shifts it away,
                # then execs the real command with the password in the env.
                'pw=$(cat "$1"); shift; COSIGN_PASSWORD="$pw" exec "$@"',
                "--",
                password_file,
                *run,
            ),
            executor=executor,
            role=role,
            cwd=cwd,
            verify=verify,
        )
