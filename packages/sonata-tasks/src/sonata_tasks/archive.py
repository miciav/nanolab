from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sonata_engine import Resource, TaskInputs

from sonata_tasks.compensation import best_effort


def _exec(provider: Any, request: Any, argv: tuple[str, ...]) -> str:
    """Run ``argv`` on the remote host and return stdout.

    Raises ``RuntimeError`` carrying the remote detail when the command exits
    non-zero.
    """
    result = provider.exec_argv(request, argv=argv)
    rc = int(getattr(result, "return_code", 0))
    if rc != 0:
        detail = (
            getattr(result, "stderr", None)
            or getattr(result, "stdout", None)
            or ""
        )
        raise RuntimeError(
            f"remote command failed (exit {rc})"
            + (f": {detail}" if detail else "")
        )
    return str(getattr(result, "stdout", ""))


def _transfer_archive(
    provider: Any, request: Any, archive_path: Path, remote_archive: str
) -> None:
    """Transfer the local archive to the remote host.

    Raises ``RuntimeError`` carrying the remote detail when the transfer exits
    non-zero.
    """
    transfer_result = provider.transfer_to(
        request, source=archive_path, destination=remote_archive
    )
    rc = int(getattr(transfer_result, "return_code", 0))
    if rc != 0:
        detail = (
            getattr(transfer_result, "stderr", None)
            or getattr(transfer_result, "stdout", None)
            or ""
        )
        raise RuntimeError(
            f"transfer failed (exit {rc})"
            + (f": {detail}" if detail else "")
        )


def _verify_remote_archive(
    provider: Any,
    request: Any,
    remote_archive: str,
    local_checksum: str,
) -> None:
    """Verify the remote archive matches the local checksum.

    Removes the remote archive best-effort when verification fails, then
    re-raises the original error.
    """
    try:
        stdout = _exec(provider, request, ("sha256sum", remote_archive))
        remote_checksum = stdout.split()[0]
        if remote_checksum != local_checksum:
            raise RuntimeError(
                f"sha256sum mismatch: local={local_checksum}, remote={remote_checksum}"
            )
    except BaseException as error:
        best_effort(
            error,
            lambda: provider.exec_argv(
                request, argv=("rm", "-f", remote_archive)
            ),
            what="cleanup remote archive after failed verify",
        )
        raise


def _extract_remote_archive(
    provider: Any,
    request: Any,
    remote_archive: str,
    remote_source_dir: str,
) -> None:
    """Extract the remote archive into the source dir.

    Removes both the source dir and the archive best-effort when extraction
    fails, then re-raises the original error.
    """
    try:
        _exec(provider, request, ("mkdir", "-p", remote_source_dir))
        _exec(
            provider,
            request,
            ("tar", "-xf", remote_archive, "-C", remote_source_dir),
        )
    except BaseException as error:
        best_effort(
            error,
            lambda: provider.exec_argv(
                request,
                argv=("rm", "-rf", remote_source_dir, remote_archive),
            ),
            what="cleanup after failed extract",
        )
        raise


def source_archive_resource(
    *,
    repo_root: Path,
    commit: str,
    remote_source_dir: str,
    remote_archive: str,
    provider: Any,
    request: Any,
) -> Resource[str]:
    """Archive a git repo's source on a remote host.

    Acquire: git archive locally -> transfer_to remote -> sha256sum verify
    -> tar -xf extract.

    Release: rm -rf source dir.

    Returns ``Resource[str]`` whose value is ``remote_source_dir``.
    """

    def acquire(_inputs: TaskInputs) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "source.tar"

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "archive",
                    "--format=tar",
                    commit,
                    "-o",
                    str(archive_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            local_checksum = hashlib.sha256(
                archive_path.read_bytes()
            ).hexdigest()

            _exec(provider, request, ("mkdir", "-p", str(Path(remote_archive).parent)))
            _transfer_archive(provider, request, archive_path, remote_archive)
            _verify_remote_archive(provider, request, remote_archive, local_checksum)
            _extract_remote_archive(provider, request, remote_archive, remote_source_dir)

        return remote_source_dir

    def release(_inputs: TaskInputs, _state: str) -> None:
        try:
            provider.exec_argv(
                request, argv=("rm", "-rf", remote_source_dir)
            )
        except RuntimeError:
            pass

    return Resource(
        title=f"Acquire source archive at {remote_source_dir}",
        acquire=acquire,
        release=release,
    )
