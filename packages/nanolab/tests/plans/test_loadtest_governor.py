"""The concurrency-governor loadtest: what the scenario must set up for the run
to be able to prove anything.

Every check here is about isolating the governor. If the autoscaler is compiled
in, or the replica count is free to move, then `function_effective_concurrency`
— which is replicas x per-replica target — changes for reasons the run cannot
attribute, and a green result means nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.loadtest import (
    _CONCURRENCY_CEILING,
    compose_control_plane_modules,
    _additional_modules,
    _concurrency_control_setup,
    _default_stages,
    build_loadtest_plan,
    is_co_tenancy,
    k6_environment,
    load_script_name,
    waits_for_parking,
)
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.platform import Backend

from .test_loadtest import NoopPrometheus, RecordingExecutor

PLAIN_SCENARIO = ScenarioConfig.model_validate(
    {"workflow": "loadtest", "backend": "container", "functions": ["word-stats-java"]}
)

AUTOSCALING_SCENARIO = ScenarioConfig.model_validate(
    {
        "workflow": "loadtest",
        "backend": "container",
        "autoscaling": True,
        "functions": ["word-stats-java"],
    }
)

CONCURRENCY_SCENARIO = ScenarioConfig.model_validate(
    {
        "workflow": "loadtest",
        "backend": "container",
        "concurrencyControl": True,
        "functions": ["word-stats-java"],
    }
)


def test_scenario_rejects_running_the_governor_alongside_the_autoscaler() -> None:
    with pytest.raises(ValueError, match="cannot run together with autoscaling"):
        ScenarioConfig.model_validate(
            {
                "workflow": "loadtest",
                "backend": "container",
                "concurrencyControl": True,
                "autoscaling": True,
                "functions": ["word-stats-java"],
            }
        )


def test_scenario_rejects_the_governor_outside_the_loadtest_workflow() -> None:
    with pytest.raises(ValueError, match="only supported by the loadtest workflow"):
        ScenarioConfig.model_validate(
            {
                "workflow": "validate",
                "backend": "container",
                "concurrencyControl": True,
                "functions": ["word-stats-java"],
            }
        )


def test_the_autoscaler_is_not_compiled_in() -> None:
    modules = _additional_modules(autoscaling=False, hpa=False, concurrency_control=True)

    assert "autoscaler" not in modules
    assert "concurrency-control" in modules
    # The governor only decides; FunctionQueueState.tryAcquireSlot is what
    # enforces the decision, and it lives in the queue module.
    assert "async-queue" in modules
    # sync-queue's admission control would start rejecting under the heavy
    # phase, which is load the function then never sees.
    assert "sync-queue" not in modules


def test_replicas_are_pinned_so_the_reading_is_attributable() -> None:
    scaling = _concurrency_control_setup(CONCURRENCY_SCENARIO)

    assert scaling is not None
    assert scaling["strategy"] == "NONE"
    assert scaling["minReplicas"] == scaling["maxReplicas"] == 1


def test_the_governor_is_adaptive_and_has_room_under_the_ceiling() -> None:
    scaling = _concurrency_control_setup(CONCURRENCY_SCENARIO)

    assert scaling is not None
    control = scaling["concurrencyControl"]
    assert isinstance(control, dict)
    assert control["mode"] == "ADAPTIVE_PER_POD"
    # The effective limit is clamped to the function's `concurrency`, so the
    # ceiling has to sit at or above the highest per-replica target the governor
    # may pick, or the trajectory is flat however the function behaves.
    assert control["maxTargetInFlightPerPod"] <= _CONCURRENCY_CEILING
    assert control["minTargetInFlightPerPod"] < control["maxTargetInFlightPerPod"]


def test_setup_is_inert_for_scenarios_that_did_not_ask_for_it() -> None:
    plain = ScenarioConfig.model_validate(
        {"workflow": "loadtest", "backend": "container", "functions": ["word-stats-java"]}
    )

    assert _concurrency_control_setup(plain) is None


def test_the_load_profile_goes_light_heavy_light() -> None:
    stages = _default_stages(CONCURRENCY_SCENARIO)
    targets = [target for _, target in stages]

    peak = max(targets)
    peak_at = targets.index(peak)
    # Light before and after the peak: the run has to show the limit recovering,
    # not just falling, or a governor stuck at its minimum passes.
    assert targets[0] < peak
    assert targets[-1] < peak
    assert peak_at not in (0, len(targets) - 1)


def test_the_default_profiles_of_the_other_modes_are_untouched() -> None:
    plain = ScenarioConfig.model_validate(
        {"workflow": "loadtest", "backend": "container", "functions": ["word-stats-java"]}
    )
    autoscaling = ScenarioConfig.model_validate(
        {
            "workflow": "loadtest",
            "backend": "container",
            "autoscaling": True,
            "functions": ["word-stats-java"],
        }
    )

    assert _default_stages(plain) == (("15s", 1), ("30s", 3))
    assert _default_stages(autoscaling) == (
        ("10s", 10),
        ("20s", 20),
        ("90s", 20),
        ("10s", 0),
    )


def test_the_container_image_is_built_with_only_the_needed_modules() -> None:
    modules = compose_control_plane_modules(
        _additional_modules(autoscaling=False, hpa=False, concurrency_control=True)
    )

    assert set(modules.split(",")) == {
        "container-deployment-provider",
        "concurrency-control",
        "async-queue",
    }


def test_scenarios_that_ask_for_nothing_keep_the_historical_module_set() -> None:
    assert compose_control_plane_modules(()) == (
        "container-deployment-provider,autoscaler,async-queue,sync-queue"
    )


def test_the_plan_still_compiles_for_the_governor_scenario(
    tmp_path: Path, nanofaas_root: Path
) -> None:
    workflow = build_loadtest_plan(
        CONCURRENCY_SCENARIO,
        EnvironmentConfig(provider="local"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        control_plane_url="http://127.0.0.1:8080",
        prometheus_client=NoopPrometheus(),
        run_dir=tmp_path,
        repo_root=nanofaas_root,
    )

    assert workflow is not None


@pytest.mark.parametrize(
    ("scenario", "backend", "expected"),
    [
        (CONCURRENCY_SCENARIO, "container", False),
        (CONCURRENCY_SCENARIO, "k8s", False),
        (AUTOSCALING_SCENARIO, "container", False),
        (AUTOSCALING_SCENARIO, "k8s", True),
        (PLAIN_SCENARIO, "k8s", False),
    ],
)
def test_only_an_autoscaling_kubernetes_run_waits_for_parking(
    scenario: ScenarioConfig, backend: Backend, expected: bool
) -> None:
    """The park-at-zero wait shells out to kubectl and waits for an autoscaler.

    It was gated on the replica floor alone, which is 0 whenever HPA is off, so
    it ran on runs with no autoscaler and on the container backend, where there
    is no cluster to ask. The first real run of the governor scenario spent 182s
    waiting for a parking that could never happen, then failed on sudo.

    This checks the decision, not the wiring: the preflight is assembled inside a
    composite that a recording executor never reaches, so a plan-level assertion
    passes whether the gate is right or wrong.
    """
    assert waits_for_parking(scenario, backend, replica_floor=0) is expected


def test_the_governor_run_offers_real_concurrency() -> None:
    """Little's law decides whether the experiment is valid at all.

    A closed-loop VU keeps S/(S+Z) of a request in flight, so the script's
    default 50ms think time against a 2.5ms function offers roughly one
    concurrent request however many VUs are added. A measured run confirmed it:
    180s at 25 VUs held a mean of 1.0 in flight against a limit of 8, so the
    function was never concurrent and the governor had nothing to react to.
    """
    env = k6_environment(CONCURRENCY_SCENARIO, "http://cp:8080", "word-stats-java")

    assert env["K6_THINK_SECONDS"] == "0"


def test_other_runs_keep_the_scripts_own_think_time() -> None:
    for scenario in (AUTOSCALING_SCENARIO, PLAIN_SCENARIO):
        assert "K6_THINK_SECONDS" not in k6_environment(scenario, "http://cp:8080", "fn")


def test_the_governor_run_uses_the_single_function_load_script() -> None:
    """`autoscaling.js` is the script that hammers one function with the given
    stages; the default one carries a 100ms think time that held offered
    concurrency near 2 against a limit of 8, so the run measured nothing."""
    assert load_script_name(CONCURRENCY_SCENARIO) == "autoscaling.js"
    assert load_script_name(AUTOSCALING_SCENARIO) == "autoscaling.js"
    assert load_script_name(PLAIN_SCENARIO) == "two-vm-function-invoke.js"


CO_TENANCY_SCENARIO = ScenarioConfig.model_validate(
    {
        "workflow": "loadtest",
        "backend": "container",
        "concurrencyControl": True,
        "functions": ["word-stats-java", "word-stats-java-lite"],
    }
)


def test_a_second_function_is_what_makes_a_run_co_tenant() -> None:
    """Read from the function list rather than a flag: declaring a neighbour is
    the intent, and a flag that had to agree with the list would be one more
    thing able to contradict it."""
    assert is_co_tenancy(CO_TENANCY_SCENARIO)
    assert not is_co_tenancy(CONCURRENCY_SCENARIO)
    assert not is_co_tenancy(PLAIN_SCENARIO)


def test_co_tenancy_drives_its_own_staggered_script() -> None:
    """The neighbour has to arrive partway through, which one global stage list
    cannot express, so the phases live in the script as k6 scenarios."""
    assert load_script_name(CO_TENANCY_SCENARIO) == "co-tenancy.js"
    assert _default_stages(CO_TENANCY_SCENARIO) == ()


def test_the_load_generator_is_told_which_function_is_the_neighbour() -> None:
    env = k6_environment(CO_TENANCY_SCENARIO, "http://cp:8080", "word-stats-java")

    assert env["NANOFAAS_FUNCTION"] == "word-stats-java"
    assert env["NANOFAAS_NEIGHBOUR"] == "word-stats-java-lite"
    assert env["K6_THINK_SECONDS"] == "0"
