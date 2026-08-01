# ruff: noqa: F403, F405
from __future__ import annotations

from ._release_support import *

from nanolab.release import build as release_build


def test_source_tests_reuse_gradle_and_uv_and_pin_container_toolchains() -> None:
    commands = release_run.source_test_commands(Path("/srv/nanofaas-source"))

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


def test_amd64_build_commands_bind_every_bake_to_the_named_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    commands = release_build.amd64_build_commands(
        plan,
        remote_bake_file="/srv/release/docker-bake.json",
        remote_buildkit_config="/srv/release/buildkitd.toml",
        remote_source_dir="/srv/source",
    )

    create = next(command for command in commands if command.task_id == "release.buildx.create")
    assert create.argv == (
        "docker",
        "buildx",
        "create",
        "--name",
        BUILDER_NAME,
        "--driver",
        "docker-container",
        "--buildkitd-config",
        "/srv/release/buildkitd.toml",
        "--use",
    )
    bake = next(command for command in commands if command.task_id == "release.images.bake.amd64")
    assert bake.argv == (
        "docker",
        "buildx",
        "bake",
        "--builder",
        BUILDER_NAME,
        "--file",
        "/srv/release/docker-bake.json",
        "--load",
        "docker-amd64",
    )
    assert sum(command.task_id.startswith("release.images.native.") for command in commands) == len(
        plan.image_plan.gradle_cells
    )
    assert all("arm64" not in " ".join(command.argv).lower() for command in commands)
    assert all("ghcr.io" not in " ".join(command.argv) for command in commands)


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
    assert evidence.digest == release_run.digest_path(archive)
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
    digest = release_run.digest_path(archive)
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


def test_release_runs_arm64_only_after_the_passed_amd64_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def record_boundary(boundary: str) -> None:
        events.append(f"phase:{boundary}")

    decision = release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
        failure_injector=record_boundary,
    )

    assert decision.passed is True
    gate = events.index("phase:regression-gate:after-action")
    tunnel = next(index for index, event in enumerate(events) if "TCP-LISTEN:5000" in event)
    assert gate < tunnel
    arm_pushes = [
        event for event in events if event.startswith("exec:docker push") and "-arm64" in event
    ]
    architecture_inspects = [
        event
        for event in events
        if event.startswith("exec:docker image inspect --format={{.Architecture}}")
    ]
    server_runs = [
        event
        for event in events
        if event.startswith("exec:docker run --detach --rm --name nanofaas-arm64-smoke-")
    ]
    server_removals = [
        event
        for event in events
        if event.startswith("exec:docker rm --force nanofaas-arm64-smoke-")
    ]
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root,
        plan.version,
        registry=plan.image_plan.registry,
    )
    smoke_count = len(arm.server_smoke_specs(arm_plan))
    assert len(arm_pushes) == len(arm_plan.cells)
    assert len(architecture_inspects) == len(arm_plan.cells)
    assert len(server_runs) == smoke_count
    assert len(server_removals) == smoke_count
    assert all("--platform linux/arm64" in event for event in server_runs)
    # One per builder VM: buildx state is per-daemon, so the ARM builder cannot
    # reuse the one the stack VM created.
    assert sum(event.startswith("exec:docker buildx create") for event in events) == 2
    health_checks = [event for event in events if event.startswith("exec:curl")]
    assert len(health_checks) == smoke_count
    assert all("--retry-max-time 120" in event for event in health_checks)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert [payload["phase"] for payload in payloads] == list(release_run.RELEASE_PHASES)


def test_failed_arm64_server_start_still_attempts_container_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, "start")

    with pytest.raises(RuntimeError, match="arm server failed"):
        _run_with_arm_failure(plan, events, provider)

    assert "exec:docker rm --force nanofaas-arm64-smoke-1" in events


@pytest.mark.parametrize(
    ("failure", "error", "failed_phase"),
    (
        ("builder", "does not support linux/arm64", "arm64-build"),
        ("bake", "arm bake failed", "arm64-build"),
        ("architecture", "image architecture mismatch", "arm64-build"),
        ("digest", "invalid image digest", "arm64-build"),
        ("push", "arm push failed", "arm64-build"),
        ("registry", "invalid registry digest", "arm64-build"),
        ("health", "arm health failed", "arm64-smoke"),
        ("watchdog", "exec format error", "arm64-smoke"),
    ),
)
def test_arm64_failures_are_journaled_and_cannot_reach_publication(
    failure: str,
    error: str,
    failed_phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, failure)

    with pytest.raises(RuntimeError, match=error):
        _run_with_arm_failure(plan, events, provider)

    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert payloads[-1]["phase"] == failed_phase
    assert payloads[-1]["outcome"] == "failed"
    rendered = "\n".join(events).lower()
    assert "ghcr.io" not in rendered
    assert "docker login" not in rendered
    assert "skopeo copy" not in rendered
    assert "imagetools" not in rendered


def test_arm64_health_failure_is_preserved_when_container_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, "health-cleanup")

    with pytest.raises(RuntimeError, match="arm health failed"):
        _run_with_arm_failure(plan, events, provider)

    assert "exec:docker rm --force nanofaas-arm64-smoke-1" in events


def test_arm64_health_failure_is_preserved_when_cleanup_executor_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider = _ArmFailureProvider(events, "health-cleanup-raises")

    with pytest.raises(RuntimeError, match="arm health failed"):
        _run_with_arm_failure(plan, events, provider)

    assert "exec:docker rm --force nanofaas-arm64-smoke-1" in events


@pytest.mark.parametrize("failed_phase", arm.ARM64_PHASES)
@pytest.mark.parametrize("boundary_suffix", ("", ":after-action"))
def test_injected_arm64_phase_failure_never_reaches_publication(
    failed_phase: str,
    boundary_suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def fail(boundary: str) -> None:
        if boundary == f"{failed_phase}{boundary_suffix}":
            raise RuntimeError(f"injected:{boundary}")

    with pytest.raises(RuntimeError, match="injected"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
            failure_injector=fail,
        )

    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert payloads[-1]["phase"] == failed_phase
    assert payloads[-1]["outcome"] == "failed"
    rendered = "\n".join(events).lower()
    assert "ghcr.io" not in rendered
    assert "docker login" not in rendered
    assert "skopeo copy" not in rendered
    assert "imagetools" not in rendered


def test_mutated_source_archive_is_rejected_before_the_clean_build_restage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def mutate(boundary: str) -> None:
        if boundary == "source-tests:after-action":
            (plan.run_dir / "source.tar").write_bytes(b"mutated-source")

    with pytest.raises(RuntimeError, match="source-tests evidence changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
            failure_injector=mutate,
        )

    assert not any("docker buildx create" in event for event in events)


def test_amd64_pre_action_remote_source_mutation_is_overwritten_by_clean_restage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def mutate(boundary: str) -> None:
        if boundary == "amd64-build":
            provider.remote_source_mutated = True

    decision = release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
        failure_injector=mutate,
    )

    assert decision.passed is True
    assert provider.remote_source_mutated is False


def test_mutated_amd64_image_evidence_is_rejected_before_any_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)
    mutated_image = plan.image_plan.cells[0].image

    def mutate(boundary: str) -> None:
        if boundary == "amd64-build:after-action":
            provider.local_digests[mutated_image] = "sha256:" + "f" * 64

    with pytest.raises(RuntimeError, match="amd64-build evidence changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
            failure_injector=mutate,
        )

    assert not any(event.startswith("exec:docker push") for event in events)


def test_run_rechecks_the_guarded_commit_immediately_before_arm64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    checks = 0

    def moving_source(_root: Path) -> release_run.GitState:
        nonlocal checks
        checks += 1
        commit = "a" * 40 if checks < 3 else "b" * 40
        return release_run.GitState(commit=commit, clean=True)

    monkeypatch.setattr(release_run, "git_state", moving_source)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    with pytest.raises(ValueError, match="source commit changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert not any("TCP-LISTEN:5000" in event for event in events)
    journal = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert journal[-1]["phase"] == "arm64-build"
    assert journal[-1]["outcome"] == "failed"


def test_run_rechecks_the_guarded_commit_immediately_before_arm64_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    checks = 0

    def moving_source(_root: Path) -> release_run.GitState:
        nonlocal checks
        checks += 1
        commit = "a" * 40 if checks < 4 else "b" * 40
        return release_run.GitState(commit=commit, clean=True)

    monkeypatch.setattr(release_run, "git_state", moving_source)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    with pytest.raises(ValueError, match="source commit changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert not any("docker run --detach" in event for event in events)
    journal = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert journal[-1]["phase"] == "arm64-smoke"
    assert journal[-1]["outcome"] == "failed"


@pytest.mark.parametrize("input_name", ("bake_file", "buildkit_config"))
def test_amd64_build_rejects_mutated_generated_inputs_before_remote_actions(
    input_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    generated_input = getattr(plan, input_name)
    assert isinstance(generated_input, Path)
    generated_input.write_text("mutated\n", encoding="utf-8")
    provider = _ReleaseProvider([])

    with pytest.raises(ValueError, match="generated build input changed"):
        release_build._build_amd64_images(
            plan,
            provider,
            object(),
            "/srv/release/docker-bake.json",
            "/srv/release/buildkitd.toml",
            "/srv/release/source",
        )

    assert provider.actions == []


def test_resume_invalidates_arm_build_and_smoke_when_arm_digest_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    first_events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, first_events)
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )
    arm_plan = arm.build_arm64_image_plan(
        plan.repo_root,
        plan.version,
        registry=plan.image_plan.registry,
    )
    provider.registry_digests[arm_plan.cells[0].image] = "sha256:" + "f" * 64
    second_events: list[str] = []
    provider.events = second_events
    _, _, _, resumed_loadtest, _, calls = _runtime_fakes(plan, second_events)

    decision = release_run.run_amd64_release(
        plan,
        resume=True,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=resumed_loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    assert calls == []
    assert any("TCP-LISTEN:5000" in event for event in second_events)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    invalidation = next(
        payload
        for payload in payloads
        if payload.get("kind") == "invalidation" and payload.get("invalidateFrom") == "arm64-build"
    )
    assert invalidation["affectedPhases"] == [
        "arm64-build",
        "arm64-smoke",
        "publish-architectures",
        "publish-manifests",
        "publish-aliases",
        "attest",
        "finalize",
    ]


def test_resume_repeats_only_arm_smoke_when_its_local_marker_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    first_events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, first_events)
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )
    (plan.run_dir / "arm64-smoke.json").write_text("{}\n", encoding="utf-8")
    second_events: list[str] = []
    provider.events = second_events
    _, _, _, resumed_loadtest, _, calls = _runtime_fakes(plan, second_events)

    decision = release_run.run_amd64_release(
        plan,
        resume=True,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=resumed_loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    assert calls == []
    assert any("TCP-LISTEN:5000" in event for event in second_events)
    assert not any("docker push" in event for event in second_events)
    assert any("docker run --detach" in event for event in second_events)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    invalidation = next(
        payload
        for payload in payloads
        if payload.get("kind") == "invalidation" and payload.get("invalidateFrom") == "arm64-smoke"
    )
    assert invalidation["affectedPhases"] == [
        "arm64-smoke",
        "publish-architectures",
        "publish-manifests",
        "publish-aliases",
        "attest",
        "finalize",
    ]


def test_resume_on_recreated_vm_restages_verified_source_before_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    first_events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, first_events)
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )
    second_events: list[str] = []
    recreated = _RecreatedReleaseProvider(second_events)
    recreated, provisioner, builder, loadtest, archive, _ = _runtime_fakes(
        plan, second_events, recreated
    )

    decision = release_run.run_amd64_release(
        plan,
        resume=True,
        provider_factory=lambda _environment, _root: recreated,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    restage = second_events.index("transfer:source.tar")
    rebuild = next(
        index for index, event in enumerate(second_events) if "docker buildx create" in event
    )
    assert restage < rebuild
    assert not any("./gradlew test" in event for event in second_events)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    invalidation = next(payload for payload in payloads if payload["kind"] == "invalidation")
    assert invalidation["invalidateFrom"] == "amd64-build"


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
