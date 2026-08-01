# ruff: noqa: F403, F405
from __future__ import annotations

from ._release_support import *

def test_plan_is_amd64_only_and_uses_a_named_bounded_buildx_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    assert plan.identity.source_commit == "a" * 40
    assert plan.version == CURRENT_VERSION
    assert plan.image_plan.cells
    assert {cell.architecture for cell in plan.image_plan.cells} == {"amd64"}
    assert plan.phase_names == release_run.RELEASE_PHASES
    assert plan.builder.name == BUILDER_NAME
    assert plan.builder.max_parallelism == 4
    assert "max-parallelism = 4" in plan.buildkit_config.read_text(encoding="utf-8")
    rendered = plan.render()
    assert "docker-container" in rendered
    assert f"{len(plan.image_plan.cells)} AMD64 + dynamic ARM64" in rendered
    assert "digest-pinned QEMU after regression-gate" in rendered
    assert "ghcr.io" not in rendered
    assert "fixture-token" not in repr(plan)
    assert "fixture-key" not in repr(plan)
    assert "fixture-password" not in repr(plan)

def test_plan_rejects_dirty_source_before_creating_release_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="a" * 40, clean=False),
    )
    credentials = release_run.CredentialFiles(
        ghcr_token=_secret(tmp_path / "ghcr-token", "token"),
        cosign_key=_secret(tmp_path / "cosign.key", "key"),
        cosign_password=_secret(tmp_path / "cosign.password", "password"),
    )

    with pytest.raises(ValueError, match="clean Git tree"):
        release_run.build_amd64_release_plan(
            repo_root=NANOFAAS_ROOT,
            version=CURRENT_VERSION,
            environment_path=_environment(tmp_path),
            release_config_path=NANOLAB_ROOT / "release.yaml",
            run_dir=tmp_path / "run",
            credentials=credentials,
        )

    assert not (tmp_path / "run").exists()

def test_plan_requires_all_private_credential_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="a" * 40, clean=True),
    )
    token = _secret(tmp_path / "ghcr-token", "token")
    key = _secret(tmp_path / "cosign.key", "key")

    with pytest.raises(ValueError, match="cosign password"):
        release_run.CredentialFiles(
            ghcr_token=token,
            cosign_key=key,
            cosign_password=None,
        ).validate()

def test_credentials_reject_path_traversal_into_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ignored = repo / "build" / "release-secrets"
    ignored.mkdir(parents=True)
    credentials = release_run.CredentialFiles(
        ghcr_token=_secret(ignored / "ghcr-token", "token"),
        cosign_key=_secret(tmp_path / "cosign.key", "key"),
        cosign_password=_secret(tmp_path / "cosign.password", "password"),
    )
    credentials = replace(
        credentials,
        ghcr_token=repo / "build" / ".." / "build" / "release-secrets" / "ghcr-token",
    )

    with pytest.raises(ValueError, match="outside the repository"):
        credentials.validate(repo_root=repo)

def test_credentials_reject_parent_symlink_into_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ignored = repo / "build" / "release-secrets"
    ignored.mkdir(parents=True)
    _secret(ignored / "ghcr-token", "token")
    alias = tmp_path / "outside-looking-alias"
    alias.symlink_to(ignored, target_is_directory=True)
    credentials = release_run.CredentialFiles(
        ghcr_token=alias / "ghcr-token",
        cosign_key=_secret(tmp_path / "cosign.key", "key"),
        cosign_password=_secret(tmp_path / "cosign.password", "password"),
    )

    with pytest.raises(ValueError, match="outside the repository"):
        credentials.validate(repo_root=repo)

def test_plan_rejects_requested_version_that_is_not_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="a" * 40, clean=True),
    )
    credentials = release_run.CredentialFiles(
        ghcr_token=_secret(tmp_path / "ghcr-token", "token"),
        cosign_key=_secret(tmp_path / "cosign.key", "key"),
        cosign_password=_secret(tmp_path / "cosign.password", "password"),
    )

    with pytest.raises(ValueError, match="prepared project version"):
        release_run.build_amd64_release_plan(
            repo_root=NANOFAAS_ROOT,
            version=MISMATCH_VERSION,
            environment_path=_environment(tmp_path),
            release_config_path=NANOLAB_ROOT / "release.yaml",
            run_dir=tmp_path / "run",
            credentials=credentials,
        )

def test_plan_resolves_relative_release_inputs_against_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(NANOLAB_ROOT)
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="a" * 40, clean=True),
    )
    credentials = release_run.CredentialFiles(
        ghcr_token=_secret(tmp_path / "ghcr-token", "token"),
        cosign_key=_secret(tmp_path / "cosign.key", "key"),
        cosign_password=_secret(tmp_path / "cosign.password", "password"),
    )

    plan = release_run.build_amd64_release_plan(
        repo_root=NANOFAAS_ROOT,
        version=CURRENT_VERSION,
        environment_path=_environment(tmp_path),
        release_config_path=Path("release.yaml"),
        run_dir=tmp_path / "run",
        credentials=credentials,
        # finalization must never write into the real repository docs in tests
        performance_root=tmp_path / "performance-docs",
    )

    assert plan.settings.scenario == (NANOLAB_ROOT / "scenarios-v2/loadtest.yaml")

def test_release_settings_allow_tool_config_outside_nanofaas_source(tmp_path: Path) -> None:
    path, config = _release_config(tmp_path)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    settings = release_run._release_settings(Path("/separate/nanofaas"), path)

    assert settings.scenario == tmp_path / "scenario.yaml"

def test_release_settings_normalize_equivalent_scenario_paths(tmp_path: Path) -> None:
    path, config = _release_config(tmp_path)
    config["benchmark"]["scenario"] = "nested/../scenario.yaml"  # type: ignore[index]
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    settings = release_run._release_settings(Path("/separate/nanofaas"), path)

    assert settings.scenario_name == "scenario.yaml"

def test_release_settings_reject_scenario_parent_escape(tmp_path: Path) -> None:
    config_root = tmp_path / "nanolab"
    config_root.mkdir()
    path, config = _release_config(config_root)
    outside = tmp_path / "outside.yaml"
    outside.write_text("workflow: loadtest\nfunctions: [word-stats-java]\n")
    config["benchmark"]["scenario"] = "../outside.yaml"  # type: ignore[index]
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="configuration-relative"):
        release_run._release_settings(Path("/separate/nanofaas"), path)

def test_release_settings_reject_scenario_symlink_escape(tmp_path: Path) -> None:
    config_root = tmp_path / "nanolab"
    config_root.mkdir()
    path, config = _release_config(config_root)
    outside = tmp_path / "outside.yaml"
    outside.write_text("workflow: loadtest\nfunctions: [word-stats-java]\n")
    (config_root / "scenario.yaml").unlink()
    (config_root / "scenario.yaml").symlink_to(outside)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="configuration-relative"):
        release_run._release_settings(Path("/separate/nanofaas"), path)

@pytest.mark.parametrize("runs", (3.0, True, "3"))
def test_release_settings_require_exact_integer_run_count(runs: object, tmp_path: Path) -> None:
    path, config = _release_config(tmp_path)
    config["benchmark"]["runs"] = runs  # type: ignore[index]
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match="runs.*integer"):
        release_run._release_settings(tmp_path, path)

@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("throughputMaxLossPercent", float("nan"), "finite nonnegative"),
        ("p95MaxIncreasePercent", float("inf"), "finite nonnegative"),
        ("throughputMaxLossPercent", -1, "finite nonnegative"),
        ("errorRateMax", float("nan"), "between 0 and 1"),
        ("errorRateMax", -0.1, "between 0 and 1"),
        ("errorRateMax", 1.1, "between 0 and 1"),
    ),
)
def test_release_settings_reject_nonfinite_or_out_of_range_thresholds(
    field: str, value: object, error: str, tmp_path: Path
) -> None:
    path, config = _release_config(tmp_path)
    config["benchmark"]["regression"][field] = value  # type: ignore[index]
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        release_run._release_settings(tmp_path, path)

def test_provider_exec_retries_on_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release_run.time, "sleep", lambda _s: None)
    # dropped mid-command (-1), then a connect-time exception, then success
    provider = _FlakyProvider([-1, ConnectionError("reset"), 0])

    release_run._provider_exec(provider, object(), ("docker", "push", "img"))

    assert provider.calls == 3

def test_provider_exec_does_not_retry_a_real_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_run.time, "sleep", lambda _s: None)
    # a genuine command failure (exit 1) must surface immediately, no retry
    provider = _FlakyProvider([1, 0])

    with pytest.raises(RuntimeError):
        release_run._provider_exec(provider, object(), ("false",))

    assert provider.calls == 1

def test_provider_exec_gives_up_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_run.time, "sleep", lambda _s: None)
    provider = _FlakyProvider([-1, -1, -1, -1])

    with pytest.raises(RuntimeError):
        release_run._provider_exec(provider, object(), ("docker", "push", "img"))

    assert provider.calls == 4

def test_provider_transfer_to_retries_on_dropped_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_run.time, "sleep", lambda _s: None)
    provider = _FlakyTransferProvider([-1, 0])
    source = tmp_path / "source.tar"
    source.write_bytes(b"x")

    release_run._provider_transfer_to(
        provider, object(), source=source, destination="/srv/source.tar", action="upload"
    )

    assert provider.calls == 2

@pytest.mark.parametrize(
    ("failed_phase", "expect_manifests"),
    (
        ("publish-architectures", False),
        ("publish-manifests", False),
        ("publish-aliases", True),
    ),
)
def test_injected_publish_phase_failures_stop_downstream_publication(
    failed_phase: str,
    expect_manifests: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def fail(boundary: str) -> None:
        if boundary == failed_phase:
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
    creates = [event for event in events if "imagetools create" in event]
    if failed_phase == "publish-architectures":
        assert not any("skopeo copy" in event for event in events)
        assert not creates
    if failed_phase == "publish-manifests":
        assert any("skopeo copy" in event for event in events)
        assert not creates
    if failed_phase == "publish-aliases":
        assert creates
        alias_creates = [
            event for event in creates if len(event.split("--tag ", 1)[1].split(" ")) == 2
        ]
        assert not alias_creates
    # credentials directory is always cleaned, even on failure
    assert "exec:rm -rf -- /tmp/nanofaas-release-credentials.fake01" in events

@pytest.mark.parametrize(
    ("vm_name", "field", "actual"),
    (
        ("nanofaas-azure-release", "location", "eastus"),
        ("nanofaas-azure-release", "vm_size", "Standard_D4s_v5"),
        ("nanofaas-azure-release", "disk_size_gb", 64),
        (
            "nanofaas-azure-release",
            "image_urn",
            "Canonical:ubuntu-24_04-lts:server:24.04.202505281",
        ),
        ("nanofaas-azure-release-loadgen", "vm_size", "Standard_B2s"),
    ),
)
def test_post_provision_vm_fact_mismatch_stops_before_source_tests_or_builds(
    vm_name: str,
    field: str,
    actual: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)
    provider.fact_overrides[vm_name] = {field: actual}

    with pytest.raises(RuntimeError, match="Azure release VM facts mismatch"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda _environment, _root: provider,
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert events.index("provision:enter") < events.index(f"facts:{vm_name}")
    assert not any(
        event.startswith("release-builder:")
        or "./gradlew test" in event
        or "docker buildx" in event
        for event in events
    )

def test_existing_journal_requires_explicit_resume_before_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)
    kwargs = {
        "provider_factory": lambda _environment, _root: provider,
        "provisioner": provisioner,
        "builder_provisioner": builder,
        "loadtest_builder": loadtest,
        "archive_builder": archive,
    }
    release_run.run_amd64_release(plan, **kwargs)
    events.clear()

    with pytest.raises(ValueError, match="--resume"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("must reject before provider creation")
            ),
            provisioner=provisioner,
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert events == []

def test_resume_requires_existing_journal_before_provider_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="--resume requires an existing"):
        release_run.run_amd64_release(
            plan,
            resume=True,
            provider_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("must reject before provider creation")
            ),
        )

def test_run_rejects_repository_credentials_before_provider_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    repo = tmp_path / "source-repo"
    secret_dir = repo / "build" / "release-secrets"
    secret_dir.mkdir(parents=True)
    assert plan.credentials is not None
    unsafe = replace(
        plan,
        repo_root=repo,
        credentials=replace(
            plan.credentials,
            ghcr_token=_secret(secret_dir / "ghcr-token", "token"),
        ),
    )

    with pytest.raises(ValueError, match="outside the repository"):
        release_run.run_amd64_release(
            unsafe,
            provider_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("must reject before provider creation")
            ),
        )

def test_release_run_lock_rejects_a_second_coordinator_before_provider_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)

    with release_run._release_run_lock(release_run._release_lock_path(plan)):
        with pytest.raises(RuntimeError, match="already in progress"):
            release_run.run_amd64_release(
                plan,
                provider_factory=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("must reject before provider creation")
                ),
            )

def test_release_lock_collides_across_different_run_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _plan(first_root, monkeypatch)
    second = _plan(second_root, monkeypatch)
    assert first.run_dir != second.run_dir
    assert release_run._release_lock_path(first) == release_run._release_lock_path(second)
    assert not release_run._release_lock_path(first).is_relative_to(first.run_dir)
    assert not release_run._release_lock_path(second).is_relative_to(second.run_dir)

    with release_run._release_run_lock(release_run._release_lock_path(first)):
        with pytest.raises(RuntimeError, match="already in progress"):
            release_run.run_amd64_release(
                second,
                provider_factory=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("must reject before provider creation")
                ),
            )

def test_release_lock_collides_across_versions_for_shared_azure_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = replace(_plan(tmp_path, monkeypatch), version="0.18.0")
    second = replace(first, version="0.19.0")

    assert release_run._release_lock_path(first) == release_run._release_lock_path(second)

def test_release_lock_differs_for_distinct_azure_resource_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _plan(tmp_path, monkeypatch)
    other_environment = first.environment.model_copy(deep=True)
    assert other_environment.azure is not None
    other_environment.azure.resource_group = "other-release-rg"
    second = replace(first, environment=other_environment)

    assert release_run._release_lock_path(first) != release_run._release_lock_path(second)

def test_release_lock_normalizes_case_insensitive_azure_resource_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _plan(tmp_path, monkeypatch)
    case_variant_environment = first.environment.model_copy(deep=True)
    assert case_variant_environment.azure is not None
    case_variant_environment.azure.resource_group = "NANOFAAS-RG"
    case_variant_environment.roles["stack"].name = "NANOFAAS-AZURE-RELEASE"
    case_variant_environment.roles["loadgen"].name = (
        "NANOFAAS-AZURE-RELEASE-LOADGEN"
    )
    second = replace(first, environment=case_variant_environment)

    assert release_run._release_lock_path(first) == release_run._release_lock_path(second)

def test_release_lock_ignores_location_for_the_same_azure_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _plan(tmp_path, monkeypatch)
    other_location_environment = first.environment.model_copy(deep=True)
    assert other_location_environment.azure is not None
    other_location_environment.azure.location = "eastus"
    second = replace(first, environment=other_location_environment)

    assert release_run._release_lock_path(first) == release_run._release_lock_path(second)

def test_run_rechecks_the_guarded_commit_before_provider_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    monkeypatch.setattr(
        release_run,
        "git_state",
        lambda _root: release_run.GitState(commit="b" * 40, clean=True),
    )

    with pytest.raises(ValueError, match="source commit changed"):
        release_run.run_amd64_release(
            plan,
            provider_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("must reject before provider creation")
            ),
        )

def test_verified_resume_reuses_every_amd64_phase(
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
    assert calls == []
    assert not any(event.startswith("teardown:") for event in second_events)
    assert second_events[:9] == [
        "provision:enter",
        "facts:nanofaas-azure-release",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-loadgen",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-arm",
        "restrict:nanofaas-azure-release",
        "release-builder:nanofaas-azure-release",
        "release-builder:nanofaas-azure-release-arm",
    ]
    assert not any("buildx" in event or "docker push" in event for event in second_events)

def test_resume_restricts_legacy_wildcard_ingress_before_bootstrap_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    first_events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(
        plan, first_events
    )
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )
    events: list[str] = []
    provider.events = events
    provider.restrictions.clear()
    legacy = {"wildcard": True}
    original_restrict = provider.restrict_inbound_sources

    def restrict(request, *, ports, source_cidrs, priority_base=1010):
        original_restrict(
            request, ports=ports, source_cidrs=source_cidrs, priority_base=priority_base
        )
        legacy["wildcard"] = False

    monkeypatch.setattr(provider, "restrict_inbound_sources", restrict)

    decision = release_run.run_amd64_release(
        plan,
        resume=True,
        provider_factory=lambda _environment, _root: provider,
        provisioner=_provisioner_with_recorded_rsync(
            plan, provider, lambda: legacy["wildcard"]
        ),
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )

    assert decision.passed is True
    assert "rsync:stack:wildcard=False" in events
    assert "rsync:loadgen:wildcard=False" in events
    assert plan.environment.azure is not None
    operator_source = plan.environment.azure.operator_source_cidr
    assert operator_source is not None
    assert [sources for _request, _ports, sources in provider.restrictions] == [
        (operator_source,),
        ("198.51.100.42/32", operator_source),
        ("203.0.113.10/32",),
    ]

def test_resume_nsg_restriction_failure_stops_before_any_bootstrap_rsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    first_events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(
        plan, first_events
    )
    release_run.run_amd64_release(
        plan,
        provider_factory=lambda _environment, _root: provider,
        provisioner=provisioner,
        builder_provisioner=builder,
        loadtest_builder=loadtest,
        archive_builder=archive,
    )
    events: list[str] = []
    provider.events = events
    provider.actions.clear()
    provider.restrictions.clear()
    restriction_calls = 0
    original_restrict = provider.restrict_inbound_sources

    def fail_final_restriction(request, *, ports, source_cidrs, priority_base=1010):
        nonlocal restriction_calls
        restriction_calls += 1
        events.append(f"restrict:attempt:{restriction_calls}")
        if restriction_calls == 2:
            raise RuntimeError("cannot apply final benchmark ingress")
        original_restrict(
            request, ports=ports, source_cidrs=source_cidrs, priority_base=priority_base
        )

    monkeypatch.setattr(
        provider, "restrict_inbound_sources", fail_final_restriction
    )

    with pytest.raises(RuntimeError, match="cannot apply final benchmark ingress"):
        release_run.run_amd64_release(
            plan,
            resume=True,
            provider_factory=lambda _environment, _root: provider,
            provisioner=_provisioner_with_recorded_rsync(
                plan, provider, lambda: True
            ),
            builder_provisioner=builder,
            loadtest_builder=loadtest,
            archive_builder=archive,
        )

    assert restriction_calls == 2
    assert not any(event.startswith("rsync:") for event in events)
    assert provider.actions == []
    assert not any(event.startswith("release-builder:") for event in events)

def test_resume_provisions_before_verification_and_invalidates_from_changed_evidence(
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
    (plan.run_dir / "run-2" / "summary.json").write_text(
        json.dumps(_summary(999)), encoding="utf-8"
    )
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
    assert second_events[:9] == [
        "provision:enter",
        "facts:nanofaas-azure-release",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-loadgen",
        "restrict:nanofaas-azure-release",
        "facts:nanofaas-azure-release-arm",
        "restrict:nanofaas-azure-release",
        "release-builder:nanofaas-azure-release",
        "release-builder:nanofaas-azure-release-arm",
    ]
    assert [call["run_dir"] for call in calls] == [
        plan.run_dir / "run-2",
        plan.run_dir / "run-3",
    ]
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(plan.state_directory.glob("*.json"))
    ]
    invalidation = next(payload for payload in payloads if payload["kind"] == "invalidation")
    assert invalidation["invalidateFrom"] == "benchmark-2"
    assert invalidation["affectedPhases"] == [
        "benchmark-2",
        "benchmark-3",
        "aggregate",
        "regression-gate",
        "arm64-build",
        "arm64-smoke",
        "publish-architectures",
        "publish-manifests",
        "publish-aliases",
        "attest",
        "finalize",
    ]

@pytest.mark.parametrize("failed_phase", release_run.AMD64_PHASES)
def test_each_phase_failure_stops_before_arm_or_publication_and_is_journaled(
    failed_phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def fail(phase: str) -> None:
        if phase == failed_phase:
            raise RuntimeError(f"injected:{phase}")

    with pytest.raises(RuntimeError, match=f"injected:{failed_phase}"):
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
    assert "arm64" not in rendered
    assert "ghcr.io" not in rendered

@pytest.mark.parametrize("failed_phase", release_run.AMD64_PHASES)
def test_each_post_action_failure_is_journaled_before_later_phases(
    failed_phase: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, monkeypatch)
    events: list[str] = []
    provider, provisioner, builder, loadtest, archive, _ = _runtime_fakes(plan, events)

    def fail(boundary: str) -> None:
        if boundary == f"{failed_phase}:after-action":
            raise RuntimeError(f"injected-after:{failed_phase}")

    with pytest.raises(RuntimeError, match=f"injected-after:{failed_phase}"):
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
    later = release_run.AMD64_PHASES[release_run.AMD64_PHASES.index(failed_phase) + 1 :]
    assert not any(payload.get("phase") in later for payload in payloads)
    rendered = "\n".join(events).lower()
    assert "arm64" not in rendered
    assert "ghcr.io" not in rendered
