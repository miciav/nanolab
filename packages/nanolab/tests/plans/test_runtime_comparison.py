from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError
from sonata_tasks.platform import PlatformFunction

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans import loadtest as loadtest_mod
from nanolab.plans import runtime_comparison as comparison_mod
from nanolab.plans.loadtest import _resolve_functions
from nanolab.plans.runtime_comparison import (
    NO_STAGES,
    MIXED_SCRIPT_NAME,
    SCRIPT_NAME,
    script_for,
    _variant_image,
    container_queries,
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


def test_the_mixed_profile_is_this_plan_with_a_different_generator() -> None:
    """Same modules, same fixed pair, same absence of a governor.

    Only the script changes, and it has to actually change: routed to the
    comparison generator, a mixed run would send 100% synchronous traffic under a
    scenario name promising otherwise, and every table drawn from it would be
    answering the old question.
    """
    mixed = _config(loadProfile="mixed")

    assert is_runtime_comparison(mixed)
    assert script_for(mixed) == MIXED_SCRIPT_NAME
    assert script_for(_config()) == SCRIPT_NAME
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


def test_the_variant_names_an_image_in_the_vm_local_registry() -> None:
    """k3s pulls from the VM registry; a locally tagged image is invisible to containerd."""
    assert (
        _variant_image(_config(controlPlaneVariant="native-o3-g1"))
        == "127.0.0.1:5000/nanofaas/control-plane:native-o3-g1"
    )
    assert _variant_image(_config()) is None


def test_an_unknown_variant_is_rejected_with_the_alternatives() -> None:
    with pytest.raises(ValueError, match="Available: jvm"):
        _variant_image(_config(controlPlaneVariant="native-o4"))


def test_a_variant_belongs_only_to_the_comparison_profile() -> None:
    with pytest.raises(ValidationError, match="no other profile builds them"):
        _config(loadProfile="cycle", controlPlaneVariant="jvm")


def test_a_run_must_say_which_build_it_measures() -> None:
    with pytest.raises(ValueError, match="needs a control-plane build to measure"):
        build_runtime_comparison_plan(
            _config(),
            environment=None,  # type: ignore[arg-type]
            bindings=None,  # type: ignore[arg-type]
            control_plane_url="http://cp:30080",
            prometheus_client=None,  # type: ignore[arg-type]
            run_dir=None,  # type: ignore[arg-type]
        )


def test_container_cost_is_queried_for_the_control_plane_and_every_function() -> None:
    """Enabling the scrape is not enough: the snapshot records only what it asks for.

    The default query list asks the control plane's actuator for
    `jvm_heap_used_bytes`, which three of the four builds do not publish.
    """
    names = [q.name for q in container_queries(_config().functions)]

    assert "container_memory_bytes@control-plane" in names
    assert "container_memory_bytes@word-stats-java" in names
    assert "container_memory_bytes@word-stats-javascript" in names
    # Cores used says what the control plane got; these say what it was denied:
    # a process pinned at its cgroup quota and one with work to spare both report
    # a number below the limit, and only the throttled share separates them.
    assert "container_cpu_periods@control-plane" in names
    assert "container_cpu_throttled_periods@control-plane" in names
    assert "container_cpu_throttled_seconds@control-plane" in names
    # And the same rate without the sum, since a snapshot entry holds one series
    # and _merge_samples adds the terminating pod to the starting one.
    assert "container_cpu_cores_max@control-plane" in names
    assert len(names) == 10


def test_cpu_is_a_rate_not_a_counter() -> None:
    """A counter only rises, so charting it says nothing about when the work happened."""
    cpu = next(q for q in container_queries(_config().functions) if q.name == "container_cpu_cores@control-plane")

    assert cpu.expr.startswith("rate(container_cpu_usage_seconds_total")
    assert "[30s]" in cpu.expr


def test_functions_are_separated_by_pod_prefix() -> None:
    """Every function container is named `function`; only the pod tells them apart."""
    java = next(
        q for q in container_queries(_config().functions) if q.name == "container_memory_bytes@word-stats-java"
    )

    assert 'container="function"' in java.expr
    assert 'pod=~"fn-word-stats-java-.*"' in java.expr


def test_the_shared_load_test_knows_nothing_about_this_experiment() -> None:
    """The point of the split: no branch in `loadtest` keyed on this profile.

    Every experiment before this one added a case to `load_script_name`,
    `_default_stages` and `k6_environment` at once, which put knowledge of each
    experiment into the code shared by all of them.
    """
    shared = inspect.getsource(loadtest_mod)
    assert '"comparison"' not in shared
    assert SCRIPT_NAME not in shared


def test_the_helm_chart_is_an_absolute_path_on_a_remote_provider() -> None:
    """Relative, it is resolved against the executor's working directory.

    That directory is the checkout only on multipass; on Azure it is the home
    directory, and helm read the first segment of "deploy/helm/nanofaas" as a
    chart repository name: "Error: repo deploy not found".
    """
    from pathlib import Path as _Path

    import yaml

    from nanolab.config.environment import EnvironmentConfig
    from nanolab.plans.loadtest import _build_platform_request

    environment = EnvironmentConfig.model_validate(
        yaml.safe_load(
            _Path("packages/nanolab/environments/azure-comparison.yaml.example").read_text()
        )
    )
    from nanolab.cli.vm_provider import vm_request_for_role
    from sonata_tasks.components.bootstrap import remote_project_dir

    root = _Path(remote_project_dir(vm_request_for_role(environment, "stack")))
    request = _build_platform_request(
        backend="k8s",
        build="docker",
        functions=(
            PlatformFunction(
                name="word-stats-java", image="i:e2e", payload="{}", build_argv=("x",)
            ),
        ),
        additional_modules=(),
        prebuilt=True,
        prebuilt_control_plane_image="cp:jvm",
        root=_Path("/repo"),
        remote_repo_root=root,
        hpa=False,
    )

    assert request.helm_chart.startswith("/")
    assert request.helm_chart.endswith("/deploy/helm/nanofaas")


def test_the_comparison_does_not_require_the_heap_gauge() -> None:
    """The G1 build publishes no heap pools: SubstrateVM registers no heap
    MemoryPoolMXBean under it.

    Measured across the matrix — the JVM build and both serial-collector native
    builds report jvm_memory_used_bytes, and G1 reports nothing. Requiring it
    failed the first G1 cell with "returned no data", which was the truth rather
    than a fault.

    process_cpu_usage is not in that boat and no longer travels with it: every
    build measured publishes it, G1 included, because nanoFaaS registers
    ProcessorMetrics for native images on purpose. Dropping it here only hid
    broken actuator scrapes.
    """
    from nanolab.metrics.catalogue import queries_for

    required = {
        q.name
        for q in queries_for("word-stats-java", modules=(), heap_metrics_required=False)
        if q.required
    }

    assert "jvm_heap_used_bytes" not in required
    assert "process_cpu_usage" in required
    # A single-build run keeps the guard: there the absence does mean a broken scrape.
    default = {q.name for q in queries_for("word-stats-java", modules=()) if q.required}
    assert "jvm_heap_used_bytes" in default


def test_the_container_memory_series_is_the_guard_instead() -> None:
    """cAdvisor reports it for every build alike, so its absence means a broken scrape."""
    required = [q.name for q in container_queries(_config().functions) if q.required]

    assert required == ["container_memory_bytes@control-plane"]


def test_comparison_pins_two_slots_and_twenty_queue_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def capture(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(comparison_mod, "build_loadtest_plan", capture)

    result = build_runtime_comparison_plan(
        _config(controlPlaneVariant="native-o3-g1"),
        environment=None,  # type: ignore[arg-type]
        bindings=None,  # type: ignore[arg-type]
        control_plane_url="http://cp:30080",
        prometheus_client=None,  # type: ignore[arg-type]
        run_dir=tmp_path,
    )

    assert result is not None
    assert captured["function_concurrency"] == 2
    assert captured["function_queue_size"] == 20


def test_fixed_function_limits_reach_the_registration_shape(nanofaas_root: Path) -> None:
    functions, _ = _resolve_functions(
        _config(),
        nanofaas_root,
        None,
        None,
        None,
        None,
        function_concurrency=8,
        function_queue_size=20,
    )

    assert {(function.concurrency, function.queue_size) for function in functions} == {(8, 20)}


def test_a_two_function_comparison_records_both_functions_refusals() -> None:
    """One cell reported 30% of HTTP requests failed and 0.3% refused.

    The two are not in contradiction: function_queue_rejected_total was
    collected for the primary function only, so the second function's refusals -
    a third of the offered load - were never recorded anywhere. The gate used to
    be concurrency control, which a build comparison never enables.
    """
    from nanolab.plans.loadtest import _default_prometheus_queries, neighbour_name

    config = _config()
    assert len(config.functions) == 2
    names = [
        q.name
        for q in _default_prometheus_queries(
            config.functions[0], neighbour_name(config), modules=("async-queue",)
        )
    ]

    assert "function_queue_rejected_total" in names
    assert f"function_queue_rejected_total@{config.functions[1]}" in names
