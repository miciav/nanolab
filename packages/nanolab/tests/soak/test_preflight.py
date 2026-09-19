"""Missing or unlimited resources must not masquerade as declared limits."""

import pytest


def test_process_artifact_requires_actual_sha_and_keeps_docker_image_rule(
    preflight_case, tmp_path
):
    from dataclasses import replace

    from nanolab.config.soak import SoakConfig
    from nanolab.tasks.soak.artifacts import ArtifactWriter
    from nanolab.tasks.soak.preflight import preflight

    config, targets, observations = preflight_case
    raw = config.model_dump(mode="json")
    raw["images"]["control-plane"]["artifact_kind"] = "process"
    config = SoakConfig.model_validate(raw)
    cp = replace(targets[0], image_digest="sha256:" + "b" * 64)
    targets = (cp, targets[1])
    observations["roles"]["control-plane"]["image_digest"] = cp.image_digest
    observations["build_receipts"]["control-plane"]["image_digest"] = cp.image_digest
    writer = ArtifactWriter(tmp_path / "valid", limit_bytes=65536)
    assert all(
        result.status == "PASS"
        for result in preflight(config, targets, observations, writer)
    )
    writer.close()

    observations["roles"]["control-plane"]["image_digest"] = "sha256:unverified"
    writer = ArtifactWriter(tmp_path / "invalid", limit_bytes=65536)
    results = preflight(config, targets, observations, writer)
    assert any(
        result.criterion_id == "control-plane.image" and result.status == "INCONCLUSIVE"
        for result in results
    )
    writer.close()


@pytest.mark.parametrize("actual", [None, 0, -1, float("nan"), float("inf"), 2**63 - 1])
def test_unlimited_or_unknown_memory_cannot_pass(actual):
    from nanolab.tasks.soak.preflight import check_limits

    results = check_limits(
        {"memory_bytes": 1073741824, "cpu": 2}, {"memory_bytes": actual, "cpu": 2}
    )
    assert any(result.status == "INCONCLUSIVE" for result in results)


def test_matching_effective_limits_pass():
    from nanolab.tasks.soak.preflight import check_limits

    results = check_limits(
        {"memory_bytes": 1073741824, "cpu": 2}, {"memory_bytes": 1073741824, "cpu": 2}
    )
    assert len(results) == 2
    assert all(result.status == "PASS" for result in results)


def test_mismatched_cpu_invalidates_protocol():
    from nanolab.tasks.soak.preflight import check_limits

    results = check_limits(
        {"memory_bytes": 1073741824, "cpu": 2}, {"memory_bytes": 1073741824, "cpu": 4}
    )
    assert any(result.status == "INCONCLUSIVE" for result in results)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_expected_limit_is_a_configuration_error(value):
    from nanolab.tasks.soak.preflight import check_limits

    with pytest.raises(ValueError, match="expected CPU and memory limits must be f"):
        check_limits(
            {"memory_bytes": value, "cpu": 2}, {"memory_bytes": 1073741824, "cpu": 2}
        )


def test_cpuset_can_reduce_effective_cpu_quota():
    from nanolab.tasks.soak.preflight import effective_cpu_limit

    assert effective_cpu_limit(400000, 100000, "1,3") == 2
    assert effective_cpu_limit(200000, 100000, "0-7") == 2
    assert effective_cpu_limit(-1, 100000, "0-1") == 2
    assert effective_cpu_limit(-1, 100000, None) is None


@pytest.mark.parametrize("cpuset", ["1-0", "abc", "-1", "1,", "0-1000000000"])
def test_malformed_cpuset_is_not_silently_assumed_valid(cpuset):
    from nanolab.tasks.soak.preflight import effective_cpu_limit

    with pytest.raises(ValueError, match=r"invalid cpuset range|malformed cpuset"):
        effective_cpu_limit(200000, 100000, cpuset)


@pytest.fixture
def preflight_case():
    from nanolab.config.soak import SoakConfig
    from nanolab.tasks.soak.models import Target

    roles = ("control-plane", "fn")
    metrics = ["process_rss_bytes", "cgroup_memory_usage_bytes"]
    config = SoakConfig.model_validate(
        {
            "purpose": "smoke",
            "phases": {
                "warmup_s": 5,
                "baseline_drain_s": 5,
                "baseline_window_s": 3,
                "steady_s": 5,
                "drain_s": 5,
                "cleanup_margin_s": 1,
            },
            "retention_s": {"outcomes": 1},
            "roles": {
                role: {
                    "runtime": "jvm",
                    "expected_cpu": 2,
                    "memory_limit_bytes": 1073741824,
                    "required_metrics": metrics,
                    "required_capabilities": ["rss", "cgroup"],
                    "runtime_options": ["-Xmx512m"],
                    "collection_sources": ["procfs", "cgroup"],
                }
                for role in roles
            },
            "images": {
                role: {
                    "variant": "jvm",
                    "platform": "linux/amd64",
                    "modules": ["async-queue"] if role == "control-plane" else [],
                }
                for role in roles
            },
            "criteria": [
                criterion
                for role in roles
                for criterion in (
                    {
                        "id": role + ".max",
                        "role": role,
                        "metric": "cgroup_memory_usage_bytes",
                        "unit": "bytes",
                        "operation": "maximum",
                        "phase": "steady",
                        "window_s": 5,
                        "threshold": 1073741824,
                        "rationale": "Test fixture budget",
                    },
                    {
                        "id": role + ".rss",
                        "role": role,
                        "metric": "process_rss_bytes",
                        "unit": "bytes",
                        "operation": "return_to_reference",
                        "phase": "drain",
                        "deadline_s": 5,
                        "window_s": 3,
                        "absolute_tolerance": 1024,
                        "relative_tolerance": 0.01,
                        "rationale": "Test fixture recovery policy",
                    },
                )
            ],
            "diagnostics": {
                "operations": {},
                "timeout_s": 1,
                "max_dumps": 0,
                "max_dump_bytes": 0,
            },
            "prerequisites": {
                "mode": "run",
                "required_coverage": [],
                "relevant_config_keys": {},
            },
            "sample_interval_s": 1,
            "scrape_timeout_s": 1,
            "max_observation_gap_s": 3,
            "artifact_limit_bytes": 1048576,
            "cancellation_timeout_s": 5,
            "workload": {
                "rates": {"fn": 1},
                "preallocated_vus": 2,
                "max_vus": 2,
                "max_error_ratio": 0,
                "max_dropped_iterations": 0,
            },
        }
    )
    targets = tuple(
        Target(
            role,
            role + "-container",
            index + 10,
            "started",
            f"registry.test/{role}@sha256:" + "a" * 64,
            "jvm",
        )
        for index, role in enumerate(roles)
    )
    observations = {
        "snapshot_fingerprint": "source",
        "free_bytes": 2097152,
        "retention_s": {"outcomes": 1},
        "generator": {"available": True, "max_vus": 2},
        "roles": {
            target.role: {
                "cpu": 2,
                "memory_bytes": 1073741824,
                "limit_sources": {
                    "cpu": "cgroup/cpu.max",
                    "memory_bytes": "cgroup/memory.max",
                },
                "image_digest": target.image_digest,
                "runtime": "jvm",
                "runtime_options": ["-Xmx512m"],
                "modules": ["async-queue"] if target.role == "control-plane" else [],
                "metrics": metrics,
                "capabilities": ["rss", "cgroup"],
                "collection_sources": ["procfs", "cgroup"],
                "diagnostics": [],
            }
            for target in targets
        },
        "build_receipts": {
            target.role: {
                "image_digest": target.image_digest,
                "source_fingerprint": "source",
                "platform": "linux/amd64",
            }
            for target in targets
        },
    }
    return config, targets, observations


def test_complete_effective_preflight_is_persisted(preflight_case, tmp_path):
    import json

    from nanolab.tasks.soak.artifacts import ArtifactWriter
    from nanolab.tasks.soak.preflight import preflight

    config, targets, observations = preflight_case
    writer = ArtifactWriter(tmp_path, limit_bytes=65536)
    results = preflight(config, targets, observations, writer)
    assert results
    assert all(result.status == "PASS" for result in results)
    saved = json.loads((tmp_path / "preflight.json").read_text())
    assert saved["observations"]["roles"]["control-plane"]["memory_bytes"] == 1073741824
    assert saved["scope"] == "preflight-only"
    writer.close()


@pytest.mark.parametrize(
    "missing",
    [
        "metrics",
        "capabilities",
        "limit_sources",
        "image_digest",
        "runtime_options",
        "collection_sources",
    ],
)
def test_missing_required_source_blocks_preflight(preflight_case, tmp_path, missing):
    from nanolab.tasks.soak.artifacts import ArtifactWriter
    from nanolab.tasks.soak.preflight import preflight

    config, targets, observations = preflight_case
    del observations["roles"]["fn"][missing]
    writer = ArtifactWriter(tmp_path, limit_bytes=65536)
    assert any(
        result.status == "INCONCLUSIVE"
        for result in preflight(config, targets, observations, writer)
    )
    writer.close()


@pytest.mark.parametrize(
    "change",
    [
        "image",
        "source",
        "retention",
        "modules",
        "missing_target",
        "duplicate_target",
        "disk",
        "generator",
    ],
)
def test_preflight_does_not_accept_a_different_experiment(
    preflight_case, tmp_path, change
):
    from nanolab.tasks.soak.artifacts import ArtifactWriter
    from nanolab.tasks.soak.preflight import preflight

    config, targets, observations = preflight_case
    if change == "image":
        observations["roles"]["fn"]["image_digest"] = "fn:latest"
    elif change == "source":
        observations["build_receipts"]["fn"]["source_fingerprint"] = "other-source"
    elif change == "retention":
        observations["retention_s"] = {"outcomes": 1800}
    elif change == "modules":
        observations["roles"]["control-plane"]["modules"] = []
    elif change == "missing_target":
        targets = targets[:1]
    elif change == "duplicate_target":
        targets = (*targets, targets[0])
    elif change == "disk":
        observations["free_bytes"] = 1
    else:
        observations["generator"]["max_vus"] = 1
    writer = ArtifactWriter(tmp_path, limit_bytes=65536)
    assert any(
        result.status == "INCONCLUSIVE"
        for result in preflight(config, targets, observations, writer)
    )
    writer.close()


def test_declared_runtime_options_must_be_applied_in_order_not_be_the_whole_list():
    """runtime_options is what the soak injects, not everything the JVM runs with.

    The observation reads the complete effective launch options, which always
    include the image's own flags, so equality could never hold. What must
    hold is that every declared option is actually in effect, in the declared
    order.
    """
    from nanolab.tasks.soak.preflight import applies_declared_options

    effective = [
        "-XX:+UseSerialGC",
        "-XX:TieredStopAtLevel=1",
        "-XX:MaxRAMPercentage=70",
        "-Xss256k",
    ]

    assert applies_declared_options(effective, [])
    assert applies_declared_options(effective, ["-Xss256k"])
    assert applies_declared_options(effective, ["-XX:+UseSerialGC", "-Xss256k"])
    # Declared but absent.
    assert not applies_declared_options(effective, ["-Xmx512m"])
    # Present but in the wrong order: for JVM flags the last one wins.
    assert not applies_declared_options(effective, ["-Xss256k", "-XX:+UseSerialGC"])
    # No observation at all is not a pass.
    assert not applies_declared_options(None, ["-Xss256k"])
    assert not applies_declared_options(None, [])
