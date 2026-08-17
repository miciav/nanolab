from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans import loadtest as loadtest_mod
from nanolab.plans.runtime_comparison import (
    NO_STAGES,
    SCRIPT_NAME,
    build_runtime_comparison_plan,
    comparison_k6_environment,
    is_runtime_comparison,
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


def test_recognises_its_own_profile() -> None:
    assert is_runtime_comparison(_config())
    assert not is_runtime_comparison(_config(loadProfile="cycle", functions=PAIR))


def test_names_both_functions_to_the_generator() -> None:
    """`k6_environment` only volunteers a neighbour for co-tenancy, which needs a governor."""
    assert comparison_k6_environment(_config()) == {
        "NANOFAAS_NEIGHBOUR": "word-stats-javascript"
    }


def test_supplies_an_empty_stage_list_rather_than_none() -> None:
    """None would mean "use the defaults", and those defaults are VU counts.

    This script's executors schedule arrival rates, so a --stage flag would not
    merely override the shape — it would describe a different quantity.
    """
    assert NO_STAGES == ()


def test_refuses_a_scenario_that_is_not_a_comparison() -> None:
    with pytest.raises(ValueError, match="loadProfile: comparison"):
        build_runtime_comparison_plan(
            _config(loadProfile="cycle", functions=PAIR),
            environment=None,  # type: ignore[arg-type]
            bindings=None,  # type: ignore[arg-type]
            control_plane_url="http://cp:30080",
            prometheus_client=None,  # type: ignore[arg-type]
            run_dir=None,  # type: ignore[arg-type]
        )


def test_requires_exactly_two_functions() -> None:
    with pytest.raises(ValidationError, match="exactly two functions"):
        _config(functions=["word-stats-java"])


def test_refuses_a_governor_or_an_autoscaler() -> None:
    """Either one moves the limits underneath the builds being compared."""
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(concurrencyControl=True)
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(autoscaling=True)


def test_the_shared_load_test_knows_nothing_about_this_experiment() -> None:
    """The point of the split: no branch in `loadtest` keyed on this profile.

    Every experiment before this one added a case to `load_script_name`,
    `_default_stages` and `k6_environment` at once, which put knowledge of each
    experiment into the code shared by all of them.
    """
    shared = inspect.getsource(loadtest_mod)
    assert '"comparison"' not in shared
    assert SCRIPT_NAME not in shared
