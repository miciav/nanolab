"""Group declarations must become executable, independently supervised profiles."""

import pytest

from nanolab.config.soak import PrerequisitePolicy


def test_five_groups_produce_eight_profiles_with_their_config_dependencies():
    groups = [
        "sync",
        "error-timeout-cancellation",
        "async-late-callback",
        "idempotent-replay",
        "function-name-churn",
    ]
    policy = PrerequisitePolicy(
        required_coverage=groups,
        relevant_config_keys={group: ["roles", "retention_s"] for group in groups},
    )
    assert set(policy.required_coverage) == {
        "sync",
        "error",
        "timeout",
        "cancellation",
        "async",
        "late-callback",
        "idempotent-replay",
        "function-name-churn",
    }
    assert set(policy.relevant_config_keys) == set(policy.required_coverage)
    assert policy.relevant_config_keys["timeout"] == ["roles", "retention_s"]
    # Revalidation must not expand or otherwise change a frozen policy.
    assert PrerequisitePolicy.model_validate(policy.model_dump()) == policy


def test_overlapping_group_and_atomic_profile_cannot_lose_a_recipe():
    with pytest.raises(ValueError, match="overlap"):
        PrerequisitePolicy(
            required_coverage=["error-timeout-cancellation", "timeout"],
            relevant_config_keys={
                "error-timeout-cancellation": ["roles"],
                "timeout": ["images"],
            },
        )
