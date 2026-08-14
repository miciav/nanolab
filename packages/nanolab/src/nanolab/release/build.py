"""Release image build, staging, digest, and ARM smoke helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import textwrap
from typing import Any

from nanolab.images.bake import render_bake_json
from nanolab.images.plan import ImagePlan
from nanolab.release import arm
from nanolab.release.model import Amd64ReleasePlan, ArtifactEvidence, digest_path, git_state
from nanolab.release.remote_retry import retry_on_connection_death
from sonata_tasks.tasks.models import CommandTaskSpec

_GO_TOOLCHAIN = (
    "golang:1.24-alpine@sha256:757779acac4af1b349a20f357c7296097b4a0b89da4ad0e370b339060077282a"
)
_NODE_TOOLCHAIN = (
    "node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2"
)
_RUST_TOOLCHAIN = (
    "rust:1.97.1-alpine3.21@sha256:e5c73e7a712b368eb90b1190c6e1c4a01a3ebb0fe0abfff68c3bcd2df26ecc41"
)

_SHA256_PREFIX = "sha256:"
_ARM64_BUILDER_ID = "release.arm64.builder"

ArchiveBuilder = Callable[[Path, str, Path], ArtifactEvidence]


def _provider_exec(
    provider: object,
    request: object,
    argv: tuple[str, ...],
    *,
    remote_dir: str | None = None,
    env: dict[str, str] | None = None,
    bounded: bool = False,
) -> object:
    if bounded:
        # The SSH executor waits for the exit status before draining output,
        # so a command whose output exceeds the channel window (~2MB)
        # deadlocks: the remote writer blocks and the command never exits.
        # Bulk commands (tests, image builds, pushes) buffer output remotely
        # and return only a 64KB tail — plenty for diagnosis, and stderr is
        # folded into stdout. Commands whose stdout gets parsed must NOT be
        # bounded.
        script = shlex.join(argv)
        argv = (
            "sh",
            "-c",
            "{ " + script + " ; } >/tmp/release-cmd.log 2>&1; "
                            "ec=$?; tail -c 65536 /tmp/release-cmd.log; exit $ec",
        )
    result = retry_on_connection_death(
        lambda: provider.exec_argv(  # type: ignore[attr-defined]
            request, argv, env=env, remote_dir=remote_dir, dry_run=False
        ),
        describe="remote command",
    )
    return _require_result(result, "remote release command")


def _provider_transfer_to(
    provider: object,
    request: object,
    *,
    source: Path,
    destination: str,
    action: str,
) -> object:
    result = retry_on_connection_death(
        lambda: provider.transfer_to(  # type: ignore[attr-defined]
            request, source=source, destination=destination
        ),
        describe=f"transfer {source.name}",
    )
    return _require_result(result, action)


def _require_result(result: object, action: str) -> object:
    return_code = int(getattr(result, "return_code", 0))
    if return_code != 0:
        detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        raise RuntimeError(detail or f"{action} failed (exit {return_code})")
    return result


def _assert_guarded_source(plan: Amd64ReleasePlan) -> None:
    source = git_state(plan.repo_root)
    if not source.clean:
        raise ValueError("release requires a clean Git tree")
    if source.commit != plan.identity.source_commit:
        raise ValueError("release source commit changed after planning")


def _write_json(path: Path, payload: Mapping[str, Any]) -> ArtifactEvidence:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactEvidence("local", str(path), digest_path(path))


def source_test_commands(remote_source_dir: Path) -> tuple[CommandTaskSpec, ...]:
    source = str(remote_source_dir)
    container_prefix = (
        "docker",
        "run",
        "--rm",
        "-v",
        f"{source}:/source:ro",
        "-w",
        "/workspace",
    )
    copy_source = "set -eu; cp -a /source/. /workspace && "
    diagnostics = str(Path(remote_source_dir).parent / "diagnostics")
    diagnostic_script = textwrap.dedent(
        f"""\
        set -uo pipefail

        DIAG={shlex.quote(diagnostics)}
        rm -rf "$DIAG"
        mkdir -p "$DIAG"

        {{
            echo "===== DATE ====="
            date --iso-8601=seconds

            echo
            echo "===== IDENTITY ====="
            id
            hostname
            pwd

            echo
            echo "===== ENVIRONMENT ====="
            env | sort

            echo
            echo "===== LIMITS ====="
            ulimit -a

            echo
            echo "===== SYSTEM ====="
            uname -a
            free -h
            df -h .

            echo
            echo "===== JAVA ====="
            command -v java
            readlink -f "$(command -v java)"
            java -version

            echo
            echo "===== GRADLE ====="
            ./gradlew --version
            ./gradlew -q javaToolchains
        }} >"$DIAG/environment.txt" 2>&1

        set +e

        env \
            -u KUBECONFIG \
            -u NANOFAAS_RUN_K8S_E2E \
            -u NANOFAAS_E2E_NAMESPACE \
            ./gradlew test \
            --no-parallel \
            --console=plain \
            --info \
            --stacktrace \
            2>&1 | tee "$DIAG/gradle.log"

        status=${{PIPESTATUS[0]}}

        if [ "$status" -ne 0 ]; then
            echo "Gradle failed with status $status"

            while IFS= read -r -d '' report; do
                cp --parents "$report" "$DIAG"
            done < <(
                find . \
                    -type f \
                    -path '*/build/test-results/test/TEST-*.xml' \
                    -print0
            )

            python3 - <<'PY' | tee "$DIAG/failures.txt"
        from pathlib import Path
        import xml.etree.ElementTree as ET

        found = False

        for report in sorted(Path(".").glob(
            "**/build/test-results/test/TEST-*.xml"
        )):
            suite = ET.parse(report).getroot()

            for case in suite.findall(".//testcase"):
                problem = case.find("failure")
                kind = "FAILURE"

                if problem is None:
                    problem = case.find("error")
                    kind = "ERROR"

                if problem is None:
                    continue

                found = True
                print("=" * 100)
                print(f"{{kind}}: {{case.get('classname')}}.{{case.get('name')}}")
                print(f"REPORT: {{report}}")
                print(f"MESSAGE: {{problem.get('message', '')}}")
                print()
                print((problem.text or "").strip())

        if not found:
            print("No failure/error elements found in JUnit XML files.")
        PY
        fi

        exit "$status"
        """
    )

    return (
        CommandTaskSpec(
            task_id="release.source.gradle",
            summary="Run Java source tests",
            argv=("bash", "-c", diagnostic_script),
            role="stack",
            remote_dir=source,
        ),
        CommandTaskSpec(
            task_id="release.source.python-sdk",
            summary="Run Python SDK and function source tests",
            argv=(
                "uv",
                "run",
                "--project",
                "sdks/python",
                "--extra",
                "test",
                "--locked",
                "pytest",
                "-q",
                "sdks/python/tests",
                "functions/python/word-stats/tests",
                "functions/python/json-transform/tests",
                "functions/python/roman-numeral/tests",
            ),
            role="stack",
            remote_dir=source,
        ),
        CommandTaskSpec(
            task_id="release.source.go",
            summary="Run Go source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _GO_TOOLCHAIN,
                "sh",
                "-c",
                copy_source
                + "for d in sdks/go functions/go/word-stats functions/go/json-transform "
                'functions/go/roman-numeral; do (cd "$d" && go test ./...); done',
            ),
            role="stack",
            remote_dir=source,
        ),
        CommandTaskSpec(
            task_id="release.source.node",
            summary="Run JavaScript source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _NODE_TOOLCHAIN,
                "sh",
                "-c",
                copy_source
                + "npm --prefix sdks/javascript ci && npm --prefix sdks/javascript test && "
                "for d in functions/javascript/word-stats functions/javascript/json-transform "
                'functions/javascript/roman-numeral; do (cd "$d" && npm ci && npm test); done',
            ),
            role="stack",
            remote_dir=source,
        ),
        CommandTaskSpec(
            task_id="release.source.rust",
            summary="Run Rust source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _RUST_TOOLCHAIN,
                "sh",
                "-c",
                copy_source
                + "apk add --no-cache bash curl jq netcat-openbsd python3 >/dev/null && "
                "cargo test --manifest-path runtimes/watchdog/Cargo.toml && "
                "bash runtimes/watchdog/test-local.sh",
            ),
            role="stack",
            remote_dir=source,
        ),
        CommandTaskSpec(
            task_id="release.source.bash",
            summary="Run Bash source tests in pinned toolchain",
            argv=(
                *container_prefix,
                _NODE_TOOLCHAIN,
                "sh",
                "-c",
                copy_source + "apk add --no-cache bash jq >/dev/null && "
                "bash functions/bash/roman-numeral/tests/test_handler.sh",
            ),
            role="stack",
            remote_dir=source,
        ),
    )


def amd64_build_commands(
    plan: ImagePlan,
    *,
    builder_name: str,
    remote_bake_file: str,
    remote_source_dir: str,
) -> tuple[CommandTaskSpec, ...]:
    """Prepare, bake and natively build every AMD64 cell.

    Mirrors `arm64_build_commands`: the builder itself is acquired by a
    resource, so nothing here creates or bootstraps it.
    """
    commands: list[CommandTaskSpec] = []
    seen: set[str] = set()
    for cell in plan.cells:
        prerequisite = cell.prerequisite_command
        if prerequisite is None or cell.target.name in seen:
            continue
        seen.add(cell.target.name)
        commands.append(
            CommandTaskSpec(
                task_id=f"release.images.prepare.{cell.target.name}",
                summary=f"Prepare {cell.target.name} JVM image",
                argv=prerequisite,
                role="stack",
                remote_dir=remote_source_dir,
            )
        )
    commands.append(
        CommandTaskSpec(
            task_id="release.images.bake.amd64",
            summary="Build AMD64 Dockerfile images",
            argv=(
                "docker",
                "buildx",
                "bake",
                "--builder",
                builder_name,
                "--file",
                remote_bake_file,
                "--load",
                "docker-amd64",
            ),
            role="stack",
            remote_dir=remote_source_dir,
        )
    )
    return tuple(commands)


def extract_commit_tree(repo_root: Path, commit: str, destination: Path) -> Path:
    """Materialize one commit as a plain tree, free of worktree state.

    Planning reads this instead of the checkout, so ignored build output and
    untracked files cannot add phantom targets to the image matrix.
    """
    output = Path(destination)
    archive: Path | None = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        if any(output.iterdir()):
            raise ValueError(f"extraction destination is not empty: {output}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".commit-tree.", suffix=".tar")
        os.close(descriptor)
        archive = Path(temporary_name)
        subprocess.run(
            ("git", "archive", "--format=tar", f"--output={archive}", commit),
            cwd=Path(repo_root),
            check=True,
            capture_output=True,
        )
        with tarfile.open(archive) as bundle:
            bundle.extractall(output, filter="data")
    except (subprocess.CalledProcessError, tarfile.TarError, OSError) as error:
        # The CLI turns ValueError from the preflight into a clean BadParameter;
        # git and tarfile raise neither, so normalize here rather than leaking a
        # traceback out of an offline preflight. mkdir/mkstemp are inside the
        # try too: an OSError from either must not escape unnormalized either.
        raise ValueError(f"could not extract release source for {commit}") from error
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
    return output


def create_source_archive(
    repo_root: Path,
    guarded_commit: str,
    destination: Path,
) -> ArtifactEvidence:
    root = Path(repo_root)
    before = git_state(root)
    if not before.clean:
        raise ValueError("release requires a clean Git tree")
    if before.commit != guarded_commit:
        raise ValueError("release source commit changed after planning")
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite source archive: {output}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            ("git", "archive", "--format=tar", f"--output={temporary}", guarded_commit),
            cwd=root,
            check=True,
        )
        after = git_state(root)
        if after != before:
            raise ValueError("release source changed while creating archive")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return ArtifactEvidence("local", str(output), digest_path(output))


def stage_source_archive(
    provider: object,
    request: object,
    *,
    archive: Path,
    remote_archive: str,
    remote_source_dir: str,
    expected_digest: str | None = None,
) -> None:
    local_digest = digest_path(archive)
    if expected_digest is not None and local_digest != expected_digest:
        raise RuntimeError("source-tests evidence changed before consumption")
    _provider_exec(provider, request, ("rm", "-rf", "--", remote_source_dir))
    _provider_exec(provider, request, ("mkdir", "-p", remote_source_dir))
    _provider_transfer_to(
        provider,
        request,
        source=archive,
        destination=remote_archive,
        action="source archive transfer",
    )
    checksum = _provider_exec(provider, request, ("sha256sum", remote_archive))
    actual = str(getattr(checksum, "stdout", "")).split(maxsplit=1)[0]
    expected = (expected_digest or local_digest).removeprefix(_SHA256_PREFIX)
    if actual != expected:
        raise RuntimeError("source archive checksum mismatch")
    _provider_exec(
        provider,
        request,
        ("tar", "-xf", remote_archive, "-C", remote_source_dir),
    )


def _build_arm64_images(
    plan: Amd64ReleasePlan,
    image_plan: ImagePlan,
    bake_file: Path,
    provider: object,
    request: object,
    remote_bake: str,
    remote_buildkit: str,
    remote_source_dir: str,
    *,
    registry_upstream: str,
    stage_inputs: bool = True,
    manage_resources: bool = True,
) -> tuple[ArtifactEvidence, ...]:
    if stage_inputs:
        bake_file.write_text(render_bake_json(image_plan), encoding="utf-8")
        _provider_exec(provider, request, ("mkdir", "-p", str(Path(remote_bake).parent)))
        for source, destination in (
            (bake_file, remote_bake),
            (plan.buildkit_config, remote_buildkit),
        ):
            _provider_transfer_to(
                provider,
                request,
                source=source,
                destination=destination,
                action=f"transfer {source.name}",
            )
    if manage_resources:
        _reset_named_builder(plan, provider, request)
    commands = arm.arm64_build_commands(
        image_plan,
        builder_name=plan.builder.name,
        remote_bake_file=remote_bake,
        remote_buildkit_config=remote_buildkit,
        remote_source_dir=remote_source_dir,
        registry_upstream=registry_upstream,
    )
    if not manage_resources:
        commands = tuple(
            command
            for command in commands
            if command.task_id
            not in {
                "release.arm64.registry-tunnel",
                "release.arm64.builder-create",
                _ARM64_BUILDER_ID,
            }
        )
    for command in commands:
        result = _provider_exec(
            provider,
            request,
            command.argv,
            remote_dir=command.remote_dir,
            # The builder task's stdout is parsed below — keep it clean.
            bounded=command.task_id != _ARM64_BUILDER_ID,
        )
        if command.task_id == _ARM64_BUILDER_ID:
            arm.require_arm64_builder(str(getattr(result, "stdout", "")))

    for cell in image_plan.cells:
        _require_image_architecture(provider, request, cell.image, "arm64")
        _inspect_image_digest(provider, request, cell.image)
    for cell in image_plan.cells:
        _provider_exec(provider, request, ("docker", "push", cell.image), bounded=True)
    evidence = tuple(
        ArtifactEvidence(
            "remote",
            f"docker://{cell.image}",
            _inspect_registry_digest(provider, request, cell.image),
        )
        for cell in image_plan.cells
    )
    arm.require_complete_arm64_evidence(image_plan, evidence)
    return evidence


def _smoke_arm64_images(
    plan: Amd64ReleasePlan,
    image_plan: ImagePlan,
    provider: object,
    request: object,
    expected_build_evidence: Iterable[ArtifactEvidence],
    *,
    registry_upstream: str,
    ensure_tunnel: bool = True,
) -> tuple[ArtifactEvidence, ...]:
    _assert_guarded_source(plan)
    if ensure_tunnel:
        _provider_exec(provider, request, arm.registry_tunnel_command(registry_upstream))
    expected = tuple(expected_build_evidence)
    arm.require_complete_arm64_evidence(image_plan, expected)
    current = tuple(
        ArtifactEvidence(
            "remote",
            f"docker://{cell.image}",
            _inspect_registry_digest(provider, request, cell.image),
        )
        for cell in image_plan.cells
    )
    if _evidence_map(current) != _evidence_map(expected):
        raise RuntimeError("arm64-build evidence changed before smoke")
    digests = {artifact.reference: artifact.digest for artifact in expected}
    checked_servers: list[str] = []
    for smoke in arm.server_smoke_specs(image_plan):
        digest = digests[f"docker://{smoke.cell.image}"]
        _smoke_arm64_server(provider, request, smoke, _pinned_image(smoke.cell.image, digest))
        checked_servers.append(smoke.cell.image)

    watchdog = arm.watchdog_cell(image_plan)
    watchdog_digest = digests[f"docker://{watchdog.image}"]
    watchdog_result = provider.exec_argv(  # type: ignore[attr-defined]
        request,
        (
            "docker",
            "run",
            "--rm",
            "--platform",
            arm.ARM64_PLATFORM,
            "--env",
            "WARM=true",
            "--env",
            "WATCHDOG_CMD=/nanofaas-arm64-smoke-missing-child",
            _pinned_image(watchdog.image, watchdog_digest),
        ),
        env=None,
        remote_dir=None,
        dry_run=False,
    )
    arm.require_expected_watchdog_exit(
        int(getattr(watchdog_result, "return_code", 0)),
        str(getattr(watchdog_result, "stdout", "")),
        str(getattr(watchdog_result, "stderr", "")),
    )
    marker = _write_json(
        plan.run_dir / "arm64-smoke.json",
        {
            "architecture": arm.ARM64_PLATFORM,
            "images": {cell.image: digests[f"docker://{cell.image}"] for cell in image_plan.cells},
            "serverHealthChecks": checked_servers,
            "watchdog": {
                "image": watchdog.image,
                "expectedExitCode": 1,
                "expectedFailure": "missing child executable",
            },
        },
    )
    return (marker,)


def _require_image_architecture(
    provider: object,
    request: object,
    reference: str,
    expected: str,
) -> None:
    result = _provider_exec(
        provider,
        request,
        ("docker", "image", "inspect", "--format={{.Architecture}}", reference),
    )
    actual = str(getattr(result, "stdout", "")).strip()
    if actual != expected:
        raise RuntimeError(
            f"image architecture mismatch for {reference}: expected {expected}, got {actual or 'empty'}"
        )


def _smoke_arm64_server(
    provider: object,
    request: object,
    smoke: arm.ServerSmokeSpec,
    image: str,
) -> None:
    try:
        _provider_exec(
            provider,
            request,
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                smoke.container_name,
                "--platform",
                arm.ARM64_PLATFORM,
                "--publish",
                f"127.0.0.1::{smoke.container_port}",
                image,
            ),
        )
        port = _provider_exec(
            provider,
            request,
            ("docker", "port", smoke.container_name, f"{smoke.container_port}/tcp"),
        )
        endpoint = str(getattr(port, "stdout", "")).strip()
        host, separator, value = endpoint.rpartition(":")
        if host != "127.0.0.1" or separator != ":" or not value.isdigit():
            raise RuntimeError(f"invalid ARM64 smoke port mapping: {endpoint or 'empty'}")
        _provider_exec(
            provider,
            request,
            (
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--connect-timeout",
                "2",
                "--max-time",
                "2",
                "--retry",
                "59",
                "--retry-delay",
                "2",
                "--retry-all-errors",
                "--retry-max-time",
                "120",
                f"http://{endpoint}{smoke.health_path}",  # NOSONAR (S5332): smoke-test health probe
            ),
        )
    except BaseException:
        try:
            provider.exec_argv(  # type: ignore[attr-defined]
                request,
                ("docker", "rm", "--force", smoke.container_name),
                env=None,
                remote_dir=None,
                dry_run=False,
            )
        except (OSError, RuntimeError):
            pass
        raise
    _provider_exec(
        provider,
        request,
        ("docker", "rm", "--force", smoke.container_name),
    )


def _pinned_image(tagged: str, digest: str) -> str:
    repository, _ = tagged.rsplit(":", 1)
    return f"{repository}@{digest}"


def _reset_named_builder(
    plan: Amd64ReleasePlan,
    provider: object,
    request: object,
) -> None:
    result = provider.exec_argv(  # type: ignore[attr-defined]
        request,
        ("docker", "buildx", "inspect", plan.builder.name),
        env=None,
        remote_dir=None,
        dry_run=False,
    )
    if int(getattr(result, "return_code", 0)) == 0:
        _provider_exec(
            provider,
            request,
            ("docker", "buildx", "rm", "--force", plan.builder.name),
        )


def _evidence_map(
    artifacts: Iterable[ArtifactEvidence],
) -> dict[tuple[str, str], str]:
    return {(artifact.location, artifact.reference): artifact.digest for artifact in artifacts}


def _inspect_image_digest(provider: object, request: object, reference: str) -> str:
    result = _provider_exec(
        provider,
        request,
        ("docker", "image", "inspect", "--format={{.Id}}", reference),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid image digest for {reference}")
    return digest


def _remote_image_digest(
    provider: object,
    request: object,
    location: str,
    reference: str,
    *,
    ghcr_authfile: str | None = None,
) -> str | None:
    if location != "remote":
        return None
    try:
        if reference.startswith("docker-daemon:"):
            return _inspect_image_digest(
                provider, request, reference.removeprefix("docker-daemon:")
            )
        if reference.startswith("docker://"):
            registry_reference = reference.removeprefix("docker://")
            if registry_reference.startswith("ghcr.io/"):
                if ghcr_authfile is None:
                    # fail closed: unverifiable GHCR evidence is never reused
                    return None
                return _inspect_ghcr_digest(
                    provider, request, registry_reference, authfile=ghcr_authfile
                )
            return _inspect_registry_digest(provider, request, registry_reference)
        return None
    except Exception:
        return None


def _inspect_ghcr_digest(
    provider: object,
    request: object,
    reference: str,
    *,
    authfile: str,
) -> str:
    result = _provider_exec(
        provider,
        request,
        (
            "skopeo",
            "inspect",
            f"--authfile={authfile}",
            "--format={{.Digest}}",
            f"docker://{reference}",
        ),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid registry digest for {reference}")
    return digest


def _inspect_registry_digest(provider: object, request: object, reference: str) -> str:
    result = _provider_exec(
        provider,
        request,
        (
            "skopeo",
            "inspect",
            "--tls-verify=false",
            "--format={{.Digest}}",
            f"docker://{reference}",
        ),
    )
    digest = str(getattr(result, "stdout", "")).strip()
    if not digest.startswith(_SHA256_PREFIX) or len(digest) != 71:
        raise RuntimeError(f"invalid registry digest for {reference}")
    return digest


def _registry_digest_map(
    image_plan: ImagePlan,
    artifacts: Iterable[ArtifactEvidence],
) -> dict[str, str]:
    by_reference = {artifact.reference: artifact.digest for artifact in artifacts}
    expected = {f"docker://{cell.image}" for cell in image_plan.cells}
    if set(by_reference) != expected:
        raise ValueError("local-registry-push evidence does not cover the image matrix")
    return {cell.image: by_reference[f"docker://{cell.image}"] for cell in image_plan.cells}
