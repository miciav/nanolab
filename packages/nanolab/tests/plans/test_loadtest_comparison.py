from __future__ import annotations

import pytest
from pydantic import ValidationError

from pathlib import Path

from sonata_tasks.platform import PlatformFunction

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import (
    _build_platform_request,
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


def _helm_values(container_metrics: bool) -> tuple[str, ...]:
    request = _build_platform_request(
        backend="k8s",
        build="docker",
        functions=(
            PlatformFunction(
                name="word-stats-java", image="i:e2e", payload="{}", build_argv=("x",)
            ),
        ),
        additional_modules=(),
        prebuilt=False,
        prebuilt_control_plane_image=None,
        root=Path("/repo"),
        remote_repo_root=None,
        hpa=False,
        container_metrics=container_metrics,
    )
    return request.helm_values


def test_only_a_run_that_reads_container_metrics_deploys_the_scrape() -> None:
    """cAdvisor needs a kubelet scrape and RBAC; runs that ignore it should not carry that."""
    assert any(
        "prometheus.containerMetrics.enabled=true" in value
        for value in _helm_values(container_metrics=True)
    )
    assert not any(
        "containerMetrics" in value for value in _helm_values(container_metrics=False)
    )


def test_comparison_requires_exactly_two_functions() -> None:
    with pytest.raises(ValidationError, match="exactly two functions"):
        _config(functions=["word-stats-java"])


def test_comparison_refuses_a_governor_or_an_autoscaler() -> None:
    """Either one moves the limits underneath the builds being compared."""
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(concurrencyControl=True)
    with pytest.raises(ValidationError, match="compares control-plane builds"):
        _config(autoscaling=True)
