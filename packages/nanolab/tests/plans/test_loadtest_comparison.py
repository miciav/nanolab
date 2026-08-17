from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import (
    _default_stages,
    k6_environment,
    load_script_name,
)

PAIR = ["word-stats-java", "word-stats-javascript"]


def _config(**overrides) -> ScenarioConfig:
    payload = {
        "workflow": "loadtest",
        "backend": "k8s",
        "loadProfile": "comparison",
        "functions": PAIR,
    }
    payload.update(overrides)
    return ScenarioConfig.model_validate(payload)


def test_comparison_selects_its_own_script() -> None:
    """It drives two functions without a governor, which no other branch covers."""
    assert load_script_name(_config()) == "runtime-comparison.js"


def test_comparison_supplies_no_stages() -> None:
    """--stage carries VU counts; this script's executors schedule arrival rates."""
    assert _default_stages(_config()) == ()


def test_comparison_names_both_functions_to_the_generator() -> None:
    env = k6_environment(_config(), "http://cp:30080", PAIR[0])

    assert env["NANOFAAS_FUNCTION"] == "word-stats-java"
    assert env["NANOFAAS_NEIGHBOUR"] == "word-stats-javascript"


def test_comparison_sets_no_latency_threshold() -> None:
    """Whether a build sheds requests at the peak is the result, not a failure."""
    env = k6_environment(_config(), "http://cp:30080", PAIR[0])

    assert "K6_MAX_P95_MS" not in env


def test_comparison_requires_exactly_two_functions() -> None:
    with pytest.raises(ValidationError, match="exactly two functions"):
        _config(functions=["word-stats-java"])


def test_comparison_refuses_a_governor_or_an_autoscaler() -> None:
    """Either one moves the limits underneath the builds being compared."""
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(concurrencyControl=True)
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(autoscaling=True)
