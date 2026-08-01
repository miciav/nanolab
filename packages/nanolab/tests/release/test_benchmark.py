# ruff: noqa: F403, F405
from __future__ import annotations

from ._release_support import *

from nanolab.release import benchmark as release_benchmark


def test_performance_profile_uses_the_tool_relative_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = release_benchmark._performance_profile(_plan(tmp_path, monkeypatch))

    assert profile.name == "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1"
    assert profile.provider == "azure"
    assert profile.stack_vm == "Standard_D8s_v5"
    assert profile.loadgen_vm == "Standard_D2s_v5"
    assert profile.architecture == "amd64"
    assert profile.flavor == "native"
    assert profile.scenario == "scenarios-v2/loadtest.yaml"


def test_run_composes_amd64_gate_and_defers_all_credentials_and_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, loadtest_calls = _runtime_fakes(plan, events)

    decision = release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    assert events[:11] == [
        "teardown:nanofaas-azure-release",
        "teardown:nanofaas-azure-release-loadgen",
        "teardown:nanofaas-azure-release-arm",
        "provision:enter",
        "facts:nanofaas-azure-release",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-loadgen",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-arm",
        "restrict:nanofaas-azure-release",
        "release-builder:nanofaas-azure-release",
    ]
    assert events[-1] == "provision:exit"
    reset = events.index(f"exec:docker buildx rm --force {BUILDER_NAME}")
    create = events.index(
        f"exec:docker buildx create --name {BUILDER_NAME} --driver "
        "docker-container --buildkitd-config "
        f"/home/azureuser/nanofaas-release/{CURRENT_VERSION}/"
        f"{plan.buildkit_config.name} --use"
    )
    source_test = next(index for index, event in enumerate(events) if "./gradlew test" in event)
    source_restages = [
        index for index, event in enumerate(events) if event == "transfer:source.tar"
    ]
    assert len(source_restages) == 3
    assert source_test < source_restages[1] < create
    tunnel = next(index for index, event in enumerate(events) if "TCP-LISTEN:5000" in event)
    assert source_restages[2] < tunnel
    assert reset < create
    assert plan.environment.azure is not None
    operator_source = plan.environment.azure.operator_source_cidr
    assert operator_source is not None
    assert [
        (getattr(request, "name"), ports, sources)
        for request, ports, sources in provider.restrictions
    ] == [
        (
            "nanofaas-azure-release",
            (30080, 30081, 30090),
            (operator_source,),
        ),
        (
            "nanofaas-azure-release",
            (30080, 30081, 30090),
            ("198.51.100.42/32", operator_source),
        ),
        (
            "nanofaas-azure-release",
            (5000,),
            ("203.0.113.10/32",),
        ),
    ]
    assert [call["run_dir"] for call in loadtest_calls] == [
        plan.run_dir / "run-1",
        plan.run_dir / "run-2",
        plan.run_dir / "run-3",
    ]
    for call in loadtest_calls:
        assert call["control_plane_url"] == "http://203.0.113.10:30080"
        assert getattr(call["prometheus_client"], "_url") == "http://203.0.113.10:30090"
        control_plane_tag = f"localhost:5000/nanofaas/control-plane:{CURRENT_TAG}-amd64-native"
        assert call["prebuilt_control_plane_image"] == (
            f"localhost:5000/nanofaas/control-plane@{_registry_digest(control_plane_tag)}"
        )
        function_tag = f"localhost:5000/nanofaas/java-word-stats:{CURRENT_TAG}-amd64-native"
        assert call["prebuilt_function_images"] == {
            "word-stats-java": (
                f"localhost:5000/nanofaas/java-word-stats@{_registry_digest(function_tag)}"
            )
        }
    rendered = "\n".join(events).lower()
    assert "arm64" in rendered
    first_ghcr = next(index for index, event in enumerate(events) if "ghcr.io" in event.lower())
    last_smoke_cleanup = max(
        index
        for index, event in enumerate(events)
        if event.startswith("exec:docker rm --force nanofaas-arm64-smoke")
    )
    assert last_smoke_cleanup < first_ghcr
    login = events.index(
        'exec:sh -c exec docker login "$1" --username "$2" --password-stdin < "$3" '
        "nanofaas-release-login ghcr.io miciav "
        "/tmp/nanofaas-release-credentials.fake01/ghcr-token"
    )
    assert last_smoke_cleanup < login < first_ghcr or login == first_ghcr
    assert "exec:rm -rf -- /tmp/nanofaas-release-credentials.fake01" in events
    creates = [
        (index, event.split("--tag ", 1)[1].split(" ")[1:])
        for index, event in enumerate(events)
        if "imagetools create" in event
    ]
    # manifests merge two pinned architecture sources; aliases retag one manifest
    manifests = [index for index, sources in creates if len(sources) == 2]
    aliases = [index for index, sources in creates if len(sources) == 1]
    assert aliases and manifests
    assert max(manifests) < min(aliases)
    for index, sources in creates:
        assert all("@sha256:" in source for source in sources)
    assert plan.credentials is not None
    secret_paths = {
        plan.credentials.ghcr_token,
        plan.credentials.cosign_key,
        plan.credentials.cosign_password,
    }
    transferred = {action[2] for action in provider.actions if action[0] == "transfer"}
    assert transferred.isdisjoint(secret_paths)
    push_actions = [
        action
        for action in provider.actions
        if action[0] == "exec"
        and isinstance(action[2], tuple)
        and action[2][:2] == ("docker", "push")
    ]
    assert push_actions
    assert all(action[4] is None for action in push_actions)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert [payload["phase"] for payload in payloads] == list(release_run.RELEASE_PHASES)
    assert all(payload["outcome"] == "passed" for payload in payloads)


def test_real_regression_failure_stops_the_release_at_the_amd64_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    profile = release_benchmark._performance_profile(plan)
    baseline = release_benchmark.aggregate_runs(
        profile,
        (_summary(100), _summary(100), _summary(100)),
    )
    baseline_record = build_release_record(
        version="0.0.0",
        source_commit="b" * 40,
        image_digests={},
        aggregate=baseline,
        policy=release_benchmark._regression_policy(plan),
    )
    monkeypatch.setattr(
        release_benchmark, "_release_records", lambda _directory: (baseline_record,)
    )
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    with pytest.raises(RuntimeError, match="throughput loss"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    decision = json.loads((plan.run_dir / "regression-decision.json").read_text(encoding="utf-8"))
    assert decision["passed"] is False
    journal = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert journal[-1]["phase"] == "regression-gate"
    assert journal[-1]["outcome"] == "failed"
    rendered = "\n".join(events).lower()
    assert "arm64" not in rendered
    assert "ghcr.io" not in rendered


def test_mutated_benchmark_evidence_is_rejected_before_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def mutate(boundary: str) -> None:
        if boundary == "benchmark-2:after-action":
            (plan.run_dir / "run-2" / "summary.json").write_text(
                json.dumps(_summary(200)), encoding="utf-8"
            )

    with pytest.raises(RuntimeError, match="benchmark-2 evidence changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
            failure_injector=mutate,
        )


def test_mutated_aggregate_cannot_turn_a_real_regression_into_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    profile = release_benchmark._performance_profile(plan)
    baseline = release_benchmark.aggregate_runs(
        profile,
        (_summary(100), _summary(100), _summary(100)),
    )
    baseline_record = build_release_record(
        version="0.0.0",
        source_commit="b" * 40,
        image_digests={},
        aggregate=baseline,
        policy=release_benchmark._regression_policy(plan),
    )
    monkeypatch.setattr(
        release_benchmark, "_release_records", lambda _directory: (baseline_record,)
    )
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def mutate(boundary: str) -> None:
        if boundary == "aggregate:after-action":
            (plan.run_dir / "aggregate.json").write_text(
                json.dumps(asdict(baseline)), encoding="utf-8"
            )

    with pytest.raises(RuntimeError, match="aggregate evidence changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
            failure_injector=mutate,
        )


def test_run_rechecks_the_guarded_commit_immediately_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    checks = 0

    def moving_source(_root: Path) -> release_run.GitState:
        nonlocal checks
        checks += 1
        commit = "a" * 40 if checks == 1 else "b" * 40
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

    journal = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    assert journal[-1]["phase"] == "regression-gate"
    assert journal[-1]["outcome"] == "failed"


def test_registry_digest_change_invalidates_push_and_benchmarks_use_digest_refs(
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
    control_plane = f"localhost:5000/nanofaas/control-plane:{CURRENT_TAG}-amd64-native"
    provider.registry_digests[control_plane] = "sha256:" + "f" * 64
    second_events: list[str] = []
    provider.events = second_events
    _, _, _, second_loadtest, _, calls = _runtime_fakes(plan, second_events)

    decision = release_run.run_amd64_release(
        plan,
        resume=True,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=second_loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    assert len(calls) == 3
    assert all("@sha256:" in str(call["prebuilt_control_plane_image"]) for call in calls)
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    invalidation = next(payload for payload in payloads if payload["kind"] == "invalidation")
    assert invalidation["invalidateFrom"] == "local-registry-push"


def test_registry_mutation_after_push_evidence_fails_before_any_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    control_plane = f"localhost:5000/nanofaas/control-plane:{CURRENT_TAG}-amd64-native"
    events: list[str] = []
    provider = _RegistryMutatesAfterEvidenceProvider(events, control_plane)
    provider, provisioner, builder, loadtest, archive, calls = _runtime_fakes(
        plan, events, provider
    )

    with pytest.raises(RuntimeError, match="registry image changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert calls == []
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    push = next(payload for payload in payloads if payload.get("phase") == "local-registry-push")
    benchmark = payloads[-1]
    assert push["outcome"] == "passed"
    assert benchmark["phase"] == "benchmark-1"
    assert benchmark["outcome"] == "failed"


def test_run_reexports_benchmark_phase_owners() -> None:
    assert release_run._write_aggregate is release_benchmark._write_aggregate
    assert release_run._evaluate_gate is release_benchmark._evaluate_gate
