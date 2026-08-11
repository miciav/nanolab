from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from nanolab.images.plan import build_image_plan
from nanolab.release import arm
from nanolab.release import build as release_build
from nanolab.release.model import digest_path

from ._release_support import NANOFAAS_ROOT, _ArchiveProvider, _ArmFailureProvider, _ReleaseProvider, _plan



def test_source_tests_reuse_gradle_and_uv_and_pin_container_toolchains() -> None:
    commands = release_build.source_test_commands(Path("/srv/nanofaas-source"))

    gradle_cmd = commands[0]
    assert gradle_cmd.task_id == "release.source.gradle"
    assert gradle_cmd.role == "stack"
    assert gradle_cmd.remote_dir == "/srv/nanofaas-source"
    assert gradle_cmd.argv[:2] == ("bash", "-c")
    script = gradle_cmd.argv[2]
    assert "./gradlew test" in script
    assert "--no-parallel" in script
    assert "-u KUBECONFIG" in script
    assert "-u NANOFAAS_RUN_K8S_E2E" in script
    assert "-u NANOFAAS_E2E_NAMESPACE" in script
    python_commands = [command for command in commands if command.argv[:2] == ("uv", "run")]
    assert {command.task_id for command in python_commands} == {
        "release.source.python-sdk",
    }
    by_id = {command.task_id: command.argv for command in python_commands}
    assert by_id["release.source.python-sdk"][-4:] == (
        "sdks/python/tests",
        "functions/python/word-stats/tests",
        "functions/python/json-transform/tests",
        "functions/python/roman-numeral/tests",
    )
    container_commands = [command for command in commands if command.argv[:2] == ("docker", "run")]
    assert {command.task_id for command in container_commands} == {
        "release.source.go",
        "release.source.node",
        "release.source.rust",
        "release.source.bash",
    }
    for command in container_commands:
        assert command.argv[:2] == ("docker", "run")
        mount = command.argv[command.argv.index("-v") + 1]
        assert mount == "/srv/nanofaas-source:/source:ro"
        assert command.argv[command.argv.index("-w") + 1] == "/workspace"
        assert command.argv[-1].startswith("set -eu; cp -a /source/. /workspace && ")
        image = next(value for value in command.argv if "@sha256:" in value)
        assert len(image.rsplit("@sha256:", 1)[1]) == 64
        assert command.remote_dir == "/srv/nanofaas-source"


def test_amd64_build_commands_prepare_jvm_cells_and_bake_everything() -> None:
    """Native cells no longer get a separate build step: the bake already
    covers them, since every cell is a Bake cell now.
    """
    plan = build_image_plan(NANOFAAS_ROOT, "v9.9.9", architectures=("amd64",))

    commands = release_build.amd64_build_commands(
        plan,
        builder_name="release-amd64-9.9.9",
        remote_bake_file="/remote/docker-bake.json",
        remote_source_dir="/remote/source",
    )

    task_ids = [command.task_id for command in commands]
    # The builder is the buildx resource's job, not these commands'.
    assert not any("buildx" in task_id for task_id in task_ids)
    # Every JVM bake cell gets its bootJar prepared before the bake.
    prepares = [c for c in commands if c.task_id.startswith("release.images.prepare.")]
    assert prepares, "no JVM prerequisite generated"
    assert all(c.argv[0] == "./gradlew" for c in prepares)
    bake_index = task_ids.index("release.images.bake.amd64")
    assert all(task_ids.index(c.task_id) < bake_index for c in prepares)
    # The bake uses the builder that was passed in.
    bake = commands[bake_index]
    assert bake.argv == (
        "docker",
        "buildx",
        "bake",
        "--builder",
        "release-amd64-9.9.9",
        "--file",
        "/remote/docker-bake.json",
        "--load",
        "docker-amd64",
    )
    # No separate native build step exists any more; the bake is the last command.
    assert bake_index == len(commands) - 1
    assert all(c.role == "stack" and c.remote_dir == "/remote/source" for c in commands)


def test_amd64_commands_contain_no_gradle_image_builds() -> None:
    plan = build_image_plan(NANOFAAS_ROOT, "v9.9.9", architectures=("amd64",))

    commands = release_build.amd64_build_commands(
        plan,
        builder_name="release-amd64-9.9.9",
        remote_bake_file="/remote/docker-bake.json",
        remote_source_dir="/remote/source",
    )

    assert not any(spec.task_id.startswith("release.images.native.") for spec in commands)
    assert not any("bootBuildImage" in " ".join(spec.argv) for spec in commands)
    assert any(spec.task_id == "release.images.bake.amd64" for spec in commands)


def test_sonata_owned_arm_resources_are_not_recreated_and_every_image_is_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    events: list[str] = []
    provider = _ReleaseProvider(events)

    evidence = release_build._build_arm64_images(
        plan,
        arm_plan,
        tmp_path / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )

    assert len(evidence) == len(arm_plan.cells)
    assert sum(event.startswith("exec:docker push") for event in events) == len(arm_plan.cells)
    assert not any("buildx create" in event for event in events)
    assert not any("nanofaas-registry-tunnel" in event for event in events)


def _arm64_build_and_smoke(plan, provider, events: list[str]):
    """Drive the two ARM64 phases the Sonata DAG calls, in DAG order."""
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    built = release_build._build_arm64_images(
        plan,
        arm_plan,
        plan.run_dir / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )
    return arm_plan, release_build._smoke_arm64_images(
        plan,
        arm_plan,
        provider,
        object(),
        built,
        registry_upstream="",
        ensure_tunnel=False,
    )


def test_arm64_smoke_health_checks_every_server_and_probes_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ReleaseProvider(events)

    arm_plan, evidence = _arm64_build_and_smoke(plan, provider, events)

    marker = json.loads((plan.run_dir / "arm64-smoke.json").read_text(encoding="utf-8"))
    assert [artifact.reference for artifact in evidence] == [str(plan.run_dir / "arm64-smoke.json")]
    assert marker["architecture"] == arm.ARM64_PLATFORM
    assert set(marker["images"]) == {cell.image for cell in arm_plan.cells}
    assert marker["serverHealthChecks"] == [
        smoke.cell.image for smoke in arm.server_smoke_specs(arm_plan)
    ]
    assert marker["watchdog"]["image"] == arm.watchdog_cell(arm_plan).image
    # every smoke container is started by digest and torn down again
    assert all("@sha256:" in event for event in events if event.startswith("exec:docker run"))
    assert sum(event.startswith("exec:docker rm --force") for event in events) == len(
        marker["serverHealthChecks"]
    )
    # no tunnel is opened: the Sonata DAG owns that resource
    assert not any("nanofaas-registry-tunnel" in event for event in events)


def test_arm64_smoke_refuses_evidence_that_moved_since_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ReleaseProvider(events)
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root, plan.version, registry=plan.image_plan.registry
    )
    built = release_build._build_arm64_images(
        plan,
        arm_plan,
        plan.run_dir / "docker-bake-arm64.json",
        provider,
        object(),
        "/release/docker-bake-arm64.json",
        "/release/buildkitd.toml",
        "/release/source",
        registry_upstream="",
        stage_inputs=False,
        manage_resources=False,
    )
    moved = arm_plan.cells[0].image
    provider.registry_digests[moved] = "sha256:" + "f" * 64

    with pytest.raises(RuntimeError, match="evidence changed before smoke"):
        release_build._smoke_arm64_images(
            plan,
            arm_plan,
            provider,
            object(),
            built,
            registry_upstream="",
            ensure_tunnel=False,
        )

    assert not (plan.run_dir / "arm64-smoke.json").exists()


@pytest.mark.parametrize(
    ("failure", "error"),
    (
        ("start", "arm server failed"),
        ("health", "arm health failed"),
        # the original failure must survive a cleanup that also fails
        ("health-cleanup", "arm health failed"),
        ("health-cleanup-raises", "arm health failed"),
    ),
)
def test_arm64_smoke_server_failures_still_remove_the_container(
    failure: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, failure)

    with pytest.raises(RuntimeError, match=error):
        _arm64_build_and_smoke(plan, provider, events)

    assert "exec:docker rm --force nanofaas-arm64-smoke-1" in events
    assert not (plan.run_dir / "arm64-smoke.json").exists()


def test_arm64_smoke_rejects_a_watchdog_that_fails_the_wrong_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, "watchdog")

    with pytest.raises(RuntimeError, match="exec format error"):
        _arm64_build_and_smoke(plan, provider, events)

    assert not (plan.run_dir / "arm64-smoke.json").exists()


@pytest.mark.parametrize(
    ("failure", "error"),
    (
        ("bake", "arm bake failed"),
        ("architecture", "image architecture mismatch"),
        ("push", "arm push failed"),
    ),
)
def test_arm64_build_failures_never_produce_evidence(
    failure: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, failure)

    with pytest.raises(RuntimeError, match=error):
        _arm64_build_and_smoke(plan, provider, events)

    rendered = "\n".join(events).lower()
    assert "ghcr.io" not in rendered
    assert "docker login" not in rendered


def test_source_archive_contains_only_the_exact_guarded_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.email", "release@example.test"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.name", "Release Test"), cwd=repo, check=True)
    (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "source"), cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = tmp_path / "source.tar"

    evidence = release_build.create_source_archive(repo, commit, archive)

    assert evidence.reference == str(archive)
    assert evidence.digest == digest_path(archive)
    with tarfile.open(archive) as source:
        assert source.getnames() == ["tracked.txt"]


def test_source_archive_rechecks_clean_commit_and_never_overwrites_on_guard_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.email", "release@example.test"), cwd=repo, check=True)
    subprocess.run(("git", "config", "user.name", "Release Test"), cwd=repo, check=True)
    (repo / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(("git", "add", "tracked.txt"), cwd=repo, check=True)
    subprocess.run(("git", "commit", "-qm", "source"), cwd=repo, check=True)
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"owned")
    (repo / "untracked-secret").write_text("must-not-enter-archive", encoding="utf-8")

    with pytest.raises(ValueError, match="clean Git tree"):
        release_build.create_source_archive(repo, "a" * 40, archive)

    assert archive.read_bytes() == b"owned"


def test_source_transfer_verifies_checksum_before_extracting(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"source")
    digest = digest_path(archive)
    provider = _ArchiveProvider(digest)
    request = object()

    release_build.stage_source_archive(
        provider,
        request,
        archive=archive,
        remote_archive="/srv/release/source.tar",
        remote_source_dir="/srv/release/source",
    )

    kinds = [action[0] for action in provider.actions]
    assert kinds == ["exec", "exec", "transfer", "exec", "exec"]
    assert provider.actions[-2][2] == ("sha256sum", "/srv/release/source.tar")
    assert provider.actions[-1][2] == (
        "tar",
        "-xf",
        "/srv/release/source.tar",
        "-C",
        "/srv/release/source",
    )


def test_source_transfer_rejects_checksum_mismatch_before_extracting(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"source")
    provider = _ArchiveProvider("sha256:" + "f" * 64)

    with pytest.raises(RuntimeError, match="source archive checksum mismatch"):
        release_build.stage_source_archive(
            provider,
            object(),
            archive=archive,
            remote_archive="/srv/release/source.tar",
            remote_source_dir="/srv/release/source",
        )

    assert not any(
        action[0] == "exec" and isinstance(action[2], tuple) and action[2][0] == "tar"
        for action in provider.actions
    )


def test_extract_commit_tree_ignores_worktree_only_paths(tmp_path: Path) -> None:
    """The extraction is the commit, so ignored and untracked junk cannot leak."""
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    (repo / "functions/python/solo").mkdir(parents=True)
    (repo / "functions/python/solo/function.yaml").write_text("name: solo\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        ("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    # Leftovers of the shape that broke the release: gitignored, so the tree stays clean.
    (repo / "functions/java/figlet/build").mkdir(parents=True)
    (repo / "functions/java/figlet/payloads").mkdir(parents=True)

    destination = extract_commit_tree(repo, commit, tmp_path / "tree")

    assert (destination / "functions/python/solo/function.yaml").is_file()
    assert not (destination / "functions/java/figlet").exists()


def test_extract_commit_tree_refuses_a_non_empty_destination(tmp_path: Path) -> None:
    """A non-empty destination could merge leftovers into the extracted tree."""
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    (repo / "tracked.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q"),
        ("git", "add", "-A"),
        ("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True)
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    destination = tmp_path / "tree"
    destination.mkdir()
    (destination / "functions/java/figlet").mkdir(parents=True)

    with pytest.raises(ValueError, match="not empty"):
        extract_commit_tree(repo, commit, destination)

    # The pre-existing leftover must survive untouched, not get merged over.
    assert (destination / "functions/java/figlet").is_dir()


def test_extract_commit_tree_normalizes_git_failures_to_value_error(tmp_path: Path) -> None:
    from nanolab.release.build import extract_commit_tree

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True)

    with pytest.raises(ValueError, match="could not extract release source"):
        extract_commit_tree(repo, "not-a-real-commit", tmp_path / "tree")
