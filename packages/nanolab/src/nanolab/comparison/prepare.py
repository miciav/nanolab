"""Everything the matrix builds once, before any cell runs.

A cell measures a control-plane build, so nothing a cell does may build anything:
`platform.py` gates the control-plane image and the function images behind the
same `build_images` flag, and a run that rebuilt its own artefacts could not
promise that cell 1 and cell 12 ran the same function.

So the images are compiled here, once, and every cell is handed pinned tags. The
functions in particular are built once on purpose — they are held fixed across
the whole matrix, and rebuilding them twelve times would let base-image drift or
a dependency resolved on a different day become a difference between variants.

Everything runs on the VM. Native images are compiled for the machine that runs
them, and a build made on an arm64 laptop is not the artefact under measurement.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sonata_tasks.components.operations import RemoteCommandOperation

from nanolab.images.control_plane_variants import (
    ControlPlaneVariant,
    build_operations,
)


def function_build_operations(
    functions: Sequence[Any],
) -> tuple[RemoteCommandOperation, ...]:
    """Build and publish each function image exactly once.

    Mirrors what `platform.py` would do per run — the application artefact first
    where the function needs one, then the image, then the push — but hoisted out
    of the cells so all twelve invoke the same bytes.
    """
    operations: list[RemoteCommandOperation] = []
    for function in functions:
        if function.image_build_argv is not None:
            operations.append(
                RemoteCommandOperation(
                    operation_id=f"prepare.function.{function.name}.artifact",
                    summary=f"Build application artifact: {function.name}",
                    argv=tuple(function.build_argv),
                    execution_target="vm",
                )
            )
        operations.append(
            RemoteCommandOperation(
                operation_id=f"prepare.function.{function.name}.image",
                summary=f"Build image {function.name}",
                argv=tuple(function.image_build_argv or function.build_argv),
                execution_target="vm",
            )
        )
        operations.append(
            RemoteCommandOperation(
                operation_id=f"prepare.function.{function.name}.push",
                summary=f"Push image {function.name}",
                argv=("docker", "push", function.image),
                execution_target="vm",
            )
        )
    return tuple(operations)


def prepare_operations(
    *,
    functions: Sequence[Any],
    variants: Sequence[ControlPlaneVariant],
    registry: str,
    modules: str,
    build_memory: str | None = None,
    parallelism: int | None = None,
) -> tuple[RemoteCommandOperation, ...]:
    """The whole prepare phase, functions first.

    Functions first because they are the cheap half and the one most likely to
    expose a broken checkout: finding out that the source does not compile after
    forty minutes of native-image work is a bad way to learn it.
    """
    operations = list(
        leftover_cleanup_operations([function.name for function in functions])
    )
    operations.extend(function_build_operations(functions))
    for variant in variants:
        operations.extend(
            build_operations(
                variant,
                registry=registry,
                modules=modules,
                build_memory=build_memory,
                parallelism=parallelism,
            )
        )
    return tuple(operations)


def pinned_function_images(functions: Sequence[Any]) -> dict[str, str]:
    """The tags the cells are handed.

    Keyed by `key`, the catalogue name, not by `name`: that is what
    `_resolve_with_prebuilt_images` looks up, and the two differ for any function
    whose registered name is not its catalogue key. A map keyed the other way
    fails as "missing prebuilt function images" for every entry it in fact holds.
    """
    return {function.key: function.image for function in functions}


# The control-plane API on the VM. The matrix reuses one cluster for every cell,
# so a run that was interrupted leaves whatever the interrupted cell had already
# registered — and the next run dies on its first cell with a 409, having done
# nothing wrong.
CONTROL_PLANE_NODE_PORT = 30080


def leftover_cleanup_operations(
    function_names: Sequence[str],
    *,
    node_port: int = CONTROL_PLANE_NODE_PORT,
) -> tuple[RemoteCommandOperation, ...]:
    """Remove anything a previous, interrupted matrix left registered.

    Deliberately tolerant of every failure it can meet: the control plane may not
    be deployed yet on a fresh VM, and the function is usually absent, which is
    the point. Both are ordinary, so neither may stop a run — the only thing this
    must not do is leave a 409 waiting for the first cell.

    Not a substitute for the workflow's own compensation, which deregisters on a
    clean exit. This is for the exit that was not clean.
    """
    if not function_names:
        return ()
    deletes = " ; ".join(
        f"curl -s -o /dev/null -m 5 -X DELETE "
        f"http://127.0.0.1:{node_port}/v1/functions/{name} || true"
        for name in function_names
    )
    return (
        RemoteCommandOperation(
            operation_id="prepare.cleanup.leftover_functions",
            summary="Deregister functions left by an interrupted run",
            argv=("sh", "-c", deletes),
            execution_target="vm",
        ),
    )
