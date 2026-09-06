"""Composite Steps builders for the nanoFaaS release workflow.

Each function returns a ``Steps`` that groups one phase of the release
pipeline into a single compiled unit, so a workflow can add it as one
entry and the compiler resolves the serial execution of its steps.

Phases that iterate over image cells use nested ``Steps`` — one inner
unit per cell — so the journal replays at cell granularity without
re-running every cell on a partial failure.

Imports from companion tasks
---------------------------
- :mod:`sonata_tasks.docker` :    DockerPushTask
- :mod:`sonata_tasks.skopeo` :    SkopeoInspectTask
- :mod:`sonata_tasks.syft` :      SyftTask
- :mod:`sonata_tasks.cosign` :    CosignTask
- :mod:`sonata_tasks.command` :   CommandTask
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re
from typing import Any

from sonata_engine import Evidence, ReusableTask, Steps, Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from nanolab.tasks.execution import ExecutionRole
from sonata_tasks.command import CommandTask
from sonata_tasks.composites import command_specs_composite
from sonata_tasks.cosign import COSIGN_IMAGE, CosignTask
from sonata_tasks.docker import DockerPushTask
from sonata_tasks.skopeo import SkopeoInspectTask
from sonata_tasks.syft import SYFT_IMAGE, SyftTask

__all__ = [
    "command_specs_composite",
    "registry_push_composite",
    "attest_composite",
]


# ---------------------------------------------------------------------------
# 3. registry_push_composite
# ---------------------------------------------------------------------------


def registry_push_composite(
    plan: Any,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    authfile: str = "",
    *,
    tls_verify: bool = True,
    title: str = "Push images to registry",
) -> Steps:
    """Push each image to the registry, then inspect its digest.

    Produces one inner ``Steps`` per cell containing a
    ``DockerPushTask`` and a ``SkopeoInspectTask``.  The inner
    grouping keeps the push-and-inspect pair replayable at cell
    granularity.

    Parameters
    ----------
    plan :
        ImagePlan-like object with a ``cells`` attribute.
    executor :
        Role-bound executor.
    role :
        Execution role.
    authfile :
        Path to the registry auth file for the skopeo inspect step.
        Pass empty string only when no authentication is needed
        (e.g. local test registries without auth).
    tls_verify :
        Set False for plain-HTTP registries (e.g. localhost:5000).
    title :
        Optional override.
    """
    cell_steps = tuple(
        Steps(
            title=f"Push and inspect {cell.image}",
            steps=(
                DockerPushTask(
                    image=cell.image,
                    executor=executor,
                    role=role,
                ),
                SkopeoInspectTask(
                    reference=cell.image,
                    authfile=authfile,
                    executor=executor,
                    role=role,
                    tls_verify=tls_verify,
                ),
            ),
        )
        for cell in plan.cells
    )
    return Steps(title=title, steps=cell_steps)


# ---------------------------------------------------------------------------
# 4. attest_composite
# ---------------------------------------------------------------------------


class _AttestImageTask(ReusableTask):
    """One digest's whole attestation, skippable as a unit on resume.

    A nested ``Steps`` of plain ``Task``s does not skip: ``decide_resume``
    returns ``"skip"`` only for a ``ReusableTask`` whose evidence still
    verifies, and ``CommandTask``/``CosignTask``/``SyftTask`` are plain tasks.
    This class is that ``ReusableTask``, so a resumed release re-signs the
    digest it died on and leaves the ones already signed alone. The six
    operations stay a nested ``Steps`` beneath it, so the journal still names
    each one.

    The outcome carries the ``cosign-attestation`` evidence for this digest and
    no value: a skipped step may only contribute ``None``.
    """

    # A resumed group must be able to retry the operation it died on, and a
    # `failed` record on a non-idempotent task raises instead of retrying.
    idempotent = True

    def __init__(
        self,
        *,
        image: str,
        steps: tuple[Task[Any], ...],
        identity: Mapping[str, Any],
        signed: list[Evidence],
    ) -> None:
        self._image = image
        self._steps = steps
        self._signed = signed
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        self._reuse_key = f"attest:{image}:sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"
        # The reuse key rides in the title because a step's journal identity is
        # its slug, and this composite is built inside a release phase at run
        # time -- so it never reaches `CompiledWorkflow.fingerprint`, the only
        # place the engine ever consults a `reuse_key`. Without it, a changed
        # predicate path or operation set would skip on a record that no longer
        # describes this work.
        self.title = f"Attest {image} {self._reuse_key[-8:]}"
        self._group = Steps(title=self.title, steps=steps)

    @property
    def reuse_key(self) -> str:
        return self._reuse_key

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._group.run(inputs)
        evidence = Evidence("cosign-attestation", self._image, self._image.split("@", 1)[1])
        self._signed.append(evidence)
        return TaskOutcome(evidence=(evidence,))


def attest_composite(
    images: Sequence[str],
    *,
    predicate_remote: str,
    sbom_dir_remote: str,
    public_key_remote: str,
    cosign_key: str,
    password_file: str,
    docker_config: str,
    executor: CommandTaskExecutor,
    role: ExecutionRole,
    signed: list[Evidence] | None = None,
    title: str = "Attest images",
) -> Steps:
    """SBOM, sign, attest, attach and verify every digest, one group per image.

    The per-image grouping is the point: this phase issues six container runs
    per digest across the whole published matrix, and a network failure two
    thirds of the way through should resume from the digest it died on, not
    from the first one. Each group is a `ReusableTask`, which is what makes the
    engine skip it -- see `_AttestImageTask`.

    `signed` collects one `cosign-attestation` Evidence per group that actually
    ran, in run order, so a caller can record what this run signed rather than
    what it set out to sign. A group the journal skipped appends nothing: it
    was signed by an earlier run, which recorded it then.

    `verify` and `verify-attestation` are both included: `verify` checks the
    simple-signing signature `sign` produced, `verify-attestation` checks the
    separate in-toto
    attestation `attest` produced. One passing says nothing about the other,
    so dropping either loses real coverage.

    `images` must be digest-pinned references (``repo/name@sha256:...``).
    Signing a tag signs whatever the tag points at when cosign resolves it,
    which is not necessarily what the release verified.

    `public_key_remote` must already hold the public half of `cosign_key`:
    ``cosign verify`` rejects an encrypted private key outright. Deriving it is
    a one-shot setup step, so it belongs to the caller, not to this per-image
    composite.
    """
    if not images:
        return Steps(
            title=title,
            steps=(
                CommandTask(
                    title="No images to attest", argv=("true",), executor=executor, role=role
                ),
            ),
        )

    collected = [] if signed is None else signed
    cell_steps: list[Any] = []
    for image in images:
        if "@" not in image:
            raise ValueError(f"attestation needs a digest-pinned reference, got {image!r}")
        sbom_path = f"{sbom_dir_remote}/{_artifact_slug(image)}.spdx.json"
        operations: tuple[Task[Any], ...] = (
            SyftTask(
                image=image,
                output_path=sbom_path,
                docker_config=docker_config,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="sign",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="attest",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                predicate_file=predicate_remote,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="attach sbom",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                sbom_file=sbom_path,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="verify",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                public_key_file=public_key_remote,
                executor=executor,
                role=role,
            ),
            CosignTask(
                operation="verify-attestation",
                image=image,
                key_file=cosign_key,
                password_file=password_file,
                docker_config=docker_config,
                public_key_file=public_key_remote,
                executor=executor,
                role=role,
            ),
        )
        for step in operations:
            # The six are all safe to re-enter: syft regenerates, cosign
            # sign/attest/attach upsert, verify is read-only. Saying so is what
            # lets a resumed group retry the operation it died on instead of
            # refusing the resume outright.
            step.idempotent = True
        cell_steps.append(
            _AttestImageTask(
                image=image,
                steps=operations,
                # Only inputs that are stable across processes belong here: the
                # key rides in the journal step id, so folding in a path that
                # changes every run means nothing is ever skipped. `cosign_key`,
                # `password_file` and `docker_config` are all staged into a
                # fresh `mktemp -d` per process and are deliberately absent --
                # the same key staged elsewhere produces the same signature.
                # What is left lives under the release's own remote root.
                identity={
                    "schema": 2,
                    "image": image,
                    # Titles carry the operation and the reference, so a
                    # dropped, renamed or reordered operation changes the key.
                    "operations": [step.title for step in operations],
                    "predicate": predicate_remote,
                    "sbom": sbom_path,
                    "publicKey": public_key_remote,
                    # The tools that make the signature: resuming across a
                    # cosign or syft bump must not skip on the old one's work.
                    "cosignImage": COSIGN_IMAGE,
                    "syftImage": SYFT_IMAGE,
                },
                signed=collected,
            )
        )
    return Steps(title=title, steps=tuple(cell_steps))


def _artifact_slug(reference: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", reference.split("/")[-1])
