"""Shipped soak inputs bind the strict model and criteria-only policy loader.

P24 tests load the checked-in operator policy. Synthetic policies below are used
only to exercise resolver rejection and override boundaries.
"""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios-v2"
P24_ADVANCED = ("memory-soak-sync-container.yaml",)
P24_SOAK = ("memory-soak-sync-candidate-diagnostic-container.yaml",)
P24 = (*P24_ADVANCED, *P24_SOAK)
SHORT = (
    "memory-soak-smoke-container.yaml",
    "memory-soak-prerequisites-container.yaml",
)
ROLES = {"control-plane", "word-stats-java", "word-stats-javascript"}


def read_preset(name):
    return yaml.safe_load((SCENARIOS / name).read_text())


def fixture_criteria():
    criteria = []
    for role in sorted(ROLES):
        criteria.extend(
            [
                {
                    "id": role + ".fixture-budget",
                    "role": role,
                    "metric": "cgroup_memory_usage_bytes",
                    "label_selector": {},
                    "unit": "bytes",
                    "operation": "maximum",
                    "phase": "steady",
                    "window_s": 60,
                    "threshold": 536870912,
                    "rationale": "Synthetic model fixture, not a P24 policy.",
                },
                {
                    "id": role + ".fixture-rss",
                    "role": role,
                    "metric": "process_rss_bytes",
                    "label_selector": {},
                    "unit": "bytes",
                    "operation": "return_to_reference",
                    "phase": "drain",
                    "window_s": 60,
                    "deadline_s": 2100,
                    "absolute_tolerance": 0,
                    "relative_tolerance": 0,
                    "rationale": "Synthetic zero-tolerance parser fixture only.",
                },
            ]
        )
    return criteria


def resolve(data, path):
    from nanolab.cli.soak import resolve_soak_policy

    return resolve_soak_policy(data, path)


def operator_fixture(tmp_path, name):
    data = read_preset(name)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    policy = {"schema": "nanolab-soak-policy-v1", "criteria": fixture_criteria()}
    (tmp_path / "memory-soak-policy.yaml").write_text(yaml.safe_dump(policy))
    return data, path


@pytest.mark.parametrize("name", [*P24, *SHORT])
def test_all_presets_load_through_resolver_and_strict_model(name):
    data, path = read_preset(name), SCENARIOS / name
    config = ScenarioConfig.model_validate(resolve(data, path))
    assert config.workflow == "soak"
    assert config.backend == "container"
    assert config.soak is not None
    soak = config.soak
    assert soak.purpose == ("p24" if name in P24 else "smoke")
    expected_profile = "soak" if name in P24_SOAK else "advanced"
    assert soak.metrics_profile == expected_profile
    assert soak.model_dump(mode="json")["metrics_profile"] == expected_profile
    assert set(soak.roles) == set(soak.images) == ROLES
    assert set(soak.workload.rates) == set(config.functions)
    assert set(config.resources) == ROLES
    ids = [criterion.id for criterion in soak.criteria]
    assert len(ids) == len(set(ids))
    for role, policy in soak.roles.items():
        assert policy.required_metrics and policy.required_capabilities
        assert policy.collection_sources
        assert {"process_rss_bytes", "cgroup_memory_usage_bytes"}.issubset(
            policy.required_metrics
        )
        limits = config.resources[role].limits
        assert limits is not None
        assert limits.cpu == policy.expected_cpu
        assert limits.memory_mib is not None
        assert limits.memory_mib * 1024 * 1024 == policy.memory_limit_bytes
        assert soak.images[role].mode == "build"
        assert soak.images[role].digest is None
        assert soak.images[role].provenance_receipt is None
    if name in P24:
        assert {image.platform for image in soak.images.values()} == {"linux/arm64"}


@pytest.mark.parametrize(
    ("name", "expected_profile"),
    [
        (P24_ADVANCED[0], "advanced"),
        (P24_SOAK[0], "soak"),
    ],
)
def test_p24_invocations_bind_profile_specific_owner_requirements(
    name, expected_profile
):
    from nanolab.tasks.soak.prerequisites import _required_populations

    data, path = read_preset(name), SCENARIOS / name
    soak = ScenarioConfig.model_validate(resolve(data, path)).soak
    assert soak is not None and soak.metrics_profile == expected_profile
    required = _required_populations(
        dict.fromkeys(soak.images, "sha256:" + "a" * 64),
        frozenset(soak.prerequisites.required_coverage),
        soak.metrics_profile,
    )
    if expected_profile == "advanced":
        assert required == dict.fromkeys(soak.images, frozenset())
    else:
        assert required == {
            "control-plane": frozenset(
                {
                    "execution_records",
                    "outcomes",
                    "logical_executions",
                    "canonical_input_bytes",
                    "physical_input_copy_bytes",
                    "expiry_queue_depth",
                    "pending_acquisitions",
                    "replica_snapshots",
                }
            ),
            "word-stats-java": frozenset({"live_executions"}),
            "word-stats-javascript": frozenset(
                {
                    "live_executions",
                    "input_bytes",
                    "output_bytes",
                }
            ),
        }


def test_checked_in_p24_policy_enforces_zero_tolerance_without_metric_series():
    policy = yaml.safe_load((SCENARIOS / "memory-soak-policy.yaml").read_text())

    assert set(policy) == {"schema", "criteria"}
    assert policy["schema"] == "nanolab-soak-policy-v1"
    assert len(policy["criteria"]) == 2 * len(ROLES)
    assert all(c["metric"] != "metric_series" for c in policy["criteria"])
    for role in ROLES:
        criteria = [c for c in policy["criteria"] if c["role"] == role]
        budget = next(c for c in criteria if c["metric"] == "cgroup_memory_usage_bytes")
        rss = next(c for c in criteria if c["metric"] == "process_rss_bytes")
        assert budget["operation"] == "maximum"
        assert budget["phase"] == "steady"
        assert rss["operation"] == "return_to_reference"
        assert rss["phase"] == "drain"
        assert rss["deadline_s"] == 2100
        assert rss["absolute_tolerance"] == 0
        assert rss["relative_tolerance"] == 0


@pytest.mark.parametrize("name", P24)
def test_p24_requires_real_operator_file_even_with_environment_fallback(
    tmp_path, monkeypatch, name
):
    data = read_preset(name)
    unrelated = tmp_path / "unrelated.yaml"
    unrelated.write_text(
        yaml.safe_dump(
            {"schema": "nanolab-soak-policy-v1", "criteria": fixture_criteria()}
        )
    )
    monkeypatch.setenv("NANOFAAS_SOAK_POLICY", str(unrelated))
    with pytest.raises((ValueError, OSError)):
        resolve(data, tmp_path / name)
    without_directive = deepcopy(data)
    without_directive.pop("soakPolicyFile")
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(without_directive)


@pytest.mark.parametrize("name", P24)
def test_p24_schedule_keeps_full_load_and_cleanup_margin(name):
    data, path = read_preset(name), SCENARIOS / name
    soak = ScenarioConfig.model_validate(resolve(data, path)).soak
    assert soak is not None
    assert soak.phases.steady_s == 5400
    assert soak.phases.drain_s == 2100
    assert soak.phases.baseline_drain_s == 2100
    assert soak.phases.cleanup_margin_s == 300
    assert sorted(soak.retention_s.values()) == [30, 300, 1800]
    assert soak.prerequisites.mode == "run"
    assert soak.prerequisites.required_coverage
    assert not soak.prerequisites.receipts


def test_native_p24_preset_is_not_shipped():
    assert not (SCENARIOS / "memory-soak-sync-native.yaml").exists()


@pytest.mark.parametrize("name", SHORT)
def test_short_presets_are_inline_smoke_and_cannot_be_relabelled_p24(name):
    data = read_preset(name)
    assert "soakPolicyFile" not in data
    config = ScenarioConfig.model_validate(resolve(data, SCENARIOS / name))
    soak = config.soak
    assert soak is not None
    assert soak.purpose == "smoke"
    assert soak.phases.steady_s < 5400
    assert soak.phases.drain_s < 2100
    for criterion in soak.criteria:
        if criterion.metric == "process_rss_bytes":
            assert criterion.operation == "growth_review"
            assert criterion.threshold == 0
    data["soak"]["purpose"] = "p24"
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(data)


def test_prerequisite_smoke_declares_coverage_without_claiming_saved_receipts():
    data = read_preset(SHORT[1])
    soak = ScenarioConfig.model_validate(resolve(data, SCENARIOS / SHORT[1])).soak
    assert soak is not None
    assert soak.purpose == "smoke"
    assert set(soak.prerequisites.required_coverage) == {
        "sync",
        "error",
        "timeout",
        "cancellation",
        "async",
        "late-callback",
        "idempotent-replay",
        "function-name-churn",
    }
    assert soak.prerequisites.mode == "run"
    assert soak.prerequisites.receipts == {}


def test_relative_policy_replaces_only_criteria_before_validation(
    tmp_path, monkeypatch
):
    data, path = operator_fixture(tmp_path, P24[0])
    original = deepcopy(data)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    resolved = resolve(data, path)
    expected = deepcopy(original)
    expected.pop("soakPolicyFile")
    expected["soak"]["criteria"] = fixture_criteria()
    assert resolved == expected
    validated = ScenarioConfig.model_validate(resolved).soak
    assert validated is not None and validated.criteria


def test_explicit_absolute_operator_policy_path(tmp_path):
    data, path = operator_fixture(tmp_path, P24[0])
    data["soakPolicyFile"] = str(tmp_path / "memory-soak-policy.yaml")
    resolved = resolve(data, path)
    assert "soakPolicyFile" not in resolved
    validated = ScenarioConfig.model_validate(resolved).soak
    assert validated is not None and validated.criteria


@pytest.mark.parametrize(
    "extra", [{"resources": {}}, {"soak": {}}, {"images": {}}, {"retention_s": {}}]
)
def test_policy_cannot_override_noncriteria_inputs(tmp_path, extra):
    data, path = operator_fixture(tmp_path, P24[0])
    policy = {
        "schema": "nanolab-soak-policy-v1",
        "criteria": fixture_criteria(),
        **extra,
    }
    (tmp_path / "memory-soak-policy.yaml").write_text(yaml.safe_dump(policy))
    with pytest.raises(ValueError, match=r"soak policy must contain schema"):
        resolve(data, path)


@pytest.mark.parametrize("schema", [None, "nanolab-soak-policy-v2"])
def test_policy_requires_exact_versioned_schema(tmp_path, schema):
    data, path = operator_fixture(tmp_path, P24[0])
    policy = {"criteria": fixture_criteria()}
    if schema is not None:
        policy["schema"] = schema
    (tmp_path / "memory-soak-policy.yaml").write_text(yaml.safe_dump(policy))
    with pytest.raises(ValueError, match=r"soak policy must contain schema"):
        resolve(data, path)


def test_duplicate_operator_criterion_ids_fail_strict_validation(tmp_path):
    data, path = operator_fixture(tmp_path, P24[0])
    criteria = fixture_criteria()
    criteria.append(deepcopy(criteria[0]))
    (tmp_path / "memory-soak-policy.yaml").write_text(
        yaml.safe_dump({"schema": "nanolab-soak-policy-v1", "criteria": criteria})
    )
    with pytest.raises(ValidationError, match="validation error for ScenarioConfig"):
        ScenarioConfig.model_validate(resolve(data, path))
