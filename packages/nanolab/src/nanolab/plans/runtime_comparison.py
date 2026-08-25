"""The run that puts control-plane BUILDS side by side.

Everything specific to that question lives here rather than in `loadtest`: which
k6 script drives the load, what the generator is told, and the fact that this is
the one profile whose result cannot be read without per-container CPU and memory.

It is a thin plan on purpose. The platform half — build, push, install the chart,
register the functions — and the load half are already assembled by
`build_loadtest_plan`, and duplicating that to avoid a few arguments would leave
two copies of the same wiring to keep in step. What this module does not share
with the load test is knowledge, not code: `loadtest` no longer contains a branch
that knows this experiment exists.

The variants themselves are not built here. A run compares builds that already
exist in the VM-local registry, produced by
`nanolab.images.control_plane_variants`, and each is passed in through
`prebuilt_control_plane_image` — which is also what makes the comparison honest,
since `platform.py` skips its own build entirely when an image is supplied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from sonata_engine import Workflow
from sonata_tasks.deployment import DEFAULT_NAMESPACE, LOCAL_REGISTRY
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.loadtest.models import PrometheusQuery
from sonata_tasks.loadtest.ports import PrometheusClient, RemoteFileFetcher

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.images.control_plane_variants import resolve_variants
from nanolab.plans.functions import resolve_function
from nanolab.plans.loadtest import build_loadtest_plan

SCRIPT_NAME = "runtime-comparison.js"

# The module set every variant is compiled with, and therefore the set the run
# can be asked about. Written out rather than derived from `_additional_modules`:
# that function returns extras for autoscaling and concurrency runs, and this
# profile forbids both, so it would return nothing.
#
# No sync-queue, and that is the whole point of the profile rather than a
# detail. With it, a twelve-cell matrix put all four builds at ~430 requests per
# second and the number was the queue, not the builds: an A/B on one VM with one
# variable changed measured p95 falling 84% and failures 44 points when it was
# switched off (nanofaas#197). A comparison cannot see past a ceiling every
# variant shares. Without it the sync path uses async-queue's per-function
# queue, which is also what every concurrency experiment has always used.
COMPARISON_MODULES: tuple[str, ...] = (
    "k8s-deployment-provider",
    "async-queue",
)

# The script carries its own k6 scenarios, so no --stage flags. An empty tuple
# rather than None: None means "use the profile's defaults", and those defaults
# are VU counts, which this script's arrival-rate executors would reject as a
# description of the load they are meant to schedule.
NO_STAGES: tuple[tuple[str, int], ...] = ()


def is_runtime_comparison(config: ScenarioConfig) -> bool:
    return config.load_profile == "comparison"


def comparison_k6_environment(config: ScenarioConfig) -> Mapping[str, str]:
    """What the generator is told beyond the shared load-test environment.

    The pair is named explicitly because `k6_environment` only volunteers a
    neighbour for co-tenancy runs, and co-tenancy is defined by the presence of a
    governor. This profile drives two functions without one, so it would otherwise
    receive the script's own fallback rather than the functions the scenario asked
    for — and the run would silently load a function that does not exist.
    """
    return {"NANOFAAS_NEIGHBOUR": config.functions[1]}


def container_queries(config: ScenarioConfig) -> tuple[PrometheusQuery, ...]:
    """What each container cost, from cAdvisor, for every pod in the run.

    Enabling the scrape in the chart is not enough — the snapshot only records
    the queries it is given, and the default list asks the control plane's own
    actuator for `jvm_heap_used_bytes`. That metric does not exist on three of
    the four builds being compared, and a heap gauge would be the wrong answer
    anyway: what decides whether a build fits on a node is its resident set,
    which on a JVM also holds class metadata, JIT-compiled code and thread
    stacks. Measured here, a JVM control plane reporting a small heap held
    1002 MiB.

    CPU as a rate over 30s, not a counter: the counter only ever rises, so a
    chart of it says nothing about when the work happened.
    """
    namespace = json.dumps(DEFAULT_NAMESPACE)
    queries = [
        PrometheusQuery(
            "container_memory_bytes@control-plane",
            f'container_memory_working_set_bytes{{namespace={namespace},container="control-plane"}}',
            # Required: this is the one memory reading every build produces, so
            # its absence means the cAdvisor scrape is broken rather than that a
            # build is quiet — which is exactly what the JVM gauges could not
            # distinguish.
            True,
        ),
        PrometheusQuery(
            "container_cpu_cores@control-plane",
            f'rate(container_cpu_usage_seconds_total{{namespace={namespace},container="control-plane"}}[30s])',
        ),
        # Whether the CPU limit was the thing being measured. Usage alone cannot
        # say: a process pinned at its quota and a process with work to spare
        # both report a number below the limit, and only the throttled share
        # tells them apart. Reading a comparison without it, a whole run can be
        # attributed to the build when the answer was the chart's one core.
        # The same reading without the sum. A snapshot entry holds one series, and
        # `_merge_samples` adds every label dimension at each timestamp - so during
        # the rollout each cell begins with, the terminating pod and the starting
        # one are added together and the result exceeds the limit: 1.44 cores
        # under a one-core cap, 3.088 under a two-core one. The mean is barely
        # touched; the peak is nonsense. Kept beside the original rather than
        # replacing it, so the series already archived stay comparable.
        PrometheusQuery(
            "container_cpu_cores_max@control-plane",
            f'max(rate(container_cpu_usage_seconds_total{{namespace={namespace},container="control-plane"}}[30s]))',
        ),
        PrometheusQuery(
            "container_cpu_periods@control-plane",
            f'container_cpu_cfs_periods_total{{namespace={namespace},container="control-plane"}}',
        ),
        PrometheusQuery(
            "container_cpu_throttled_periods@control-plane",
            f'container_cpu_cfs_throttled_periods_total{{namespace={namespace},container="control-plane"}}',
        ),
    ]
    for name in config.functions:
        # Function containers are all named `function` by the deployment builder,
        # so the pod prefix is what separates one function from another.
        selector = f'namespace={namespace},container="function",pod=~"fn-{name}-.*"'
        queries.extend(
            (
                PrometheusQuery(
                    f"container_memory_bytes@{name}",
                    f"container_memory_working_set_bytes{{{selector}}}",
                ),
                PrometheusQuery(
                    f"container_cpu_cores@{name}",
                    f"rate(container_cpu_usage_seconds_total{{{selector}}}[30s])",
                ),
            )
        )
    return tuple(queries)


def _variant_image(config: ScenarioConfig) -> str | None:
    """The image for the build this run measures, taken from the VM-local registry.

    Passing it as `prebuilt_control_plane_image` is what makes the comparison
    honest rather than merely convenient: `platform.py` skips its own build
    entirely when an image is supplied, so a run measures the artefact the matrix
    compiled and cannot silently rebuild a different one under the same name.
    """
    if config.control_plane_variant is None:
        return None
    variant = resolve_variants((config.control_plane_variant,))[0]
    return variant.image(LOCAL_REGISTRY)


def pinned_functions(
    config: ScenarioConfig,
    *,
    repo_root: Path | None,
    tool_root: Path | None,
) -> dict[str, str]:
    """The function images a cell uses, named exactly as the prepare phase built them.

    Resolved rather than invented: these are the same tags `platform.py` would
    have produced for itself. Declaring them as prebuilt changes nothing about
    which image runs and everything about when it is built — the platform half
    skips its build tasks, so no cell compiles anything.

    That matters more than the time it saves. `build_images` gates the functions
    and the control plane together, so a cell that was allowed to build its own
    control-plane variant would rebuild the functions too, twelve times over an
    hour, and base-image drift between the first cell and the last would arrive
    as a difference between variants.
    """
    return {
        function.key: function.image
        for function in (
            resolve_function(config, key, source_root=repo_root, tool_root=tool_root)
            for key in config.functions
        )
    }


def build_runtime_comparison_plan(
    config: ScenarioConfig,
    environment: EnvironmentConfig,
    bindings: RoleBindings,
    *,
    control_plane_url: str,
    prometheus_client: PrometheusClient,
    run_dir: Path,
    remote_run_dir: Path | None = None,
    remote_repo_root: Path | None = None,
    fetcher: RemoteFileFetcher | object | None = None,
    repo_root: Path | None = None,
    tool_root: Path | None = None,
    prebuilt_control_plane_image: str | None = None,
    prebuilt_function_images: Mapping[str, str] | None = None,
) -> Workflow:
    """Compile one variant's run of the comparison into a Sonata workflow."""
    if not is_runtime_comparison(config):
        raise ValueError("runtime-comparison plan requires loadProfile: comparison")
    image = prebuilt_control_plane_image or _variant_image(config)
    if image is None:
        raise ValueError(
            "a comparison run needs a control-plane build to measure: set "
            "controlPlaneVariant, or pass a prebuilt image"
        )
    return build_loadtest_plan(
        config,
        environment,
        bindings,
        control_plane_url=control_plane_url,
        prometheus_client=prometheus_client,
        run_dir=run_dir,
        remote_run_dir=remote_run_dir,
        remote_repo_root=remote_repo_root,
        fetcher=fetcher,
        repo_root=repo_root,
        tool_root=tool_root,
        stages=NO_STAGES,
        prebuilt_control_plane_image=image,
        prebuilt_function_images=(
            prebuilt_function_images
            if prebuilt_function_images is not None
            else pinned_functions(config, repo_root=repo_root, tool_root=tool_root)
        ),
        script_name=SCRIPT_NAME,
        k6_env_overrides=comparison_k6_environment(config),
        # The only profile that needs them, and it cannot be read without them:
        # a natively compiled control plane publishes no JVM memory gauges, so
        # cAdvisor is the one source that prices every build on the same terms.
        container_metrics=True,
        extra_prometheus_queries=container_queries(config),
        observed_modules=COMPARISON_MODULES,
        # No reaching back before the load started: every cell redeploys the
        # control plane, so anything before k6 belongs to the build the previous
        # cell was measuring. That is not hypothetical — two cells reported the
        # same `jvm_gc_pause_seconds` for a JVM build and a native one, and the
        # native one does not publish the metric at all.
        snapshot_lead_seconds=0.0,
        # Not required here, and it is not a lowered standard: the G1 build
        # publishes no JVM gauges at all, because SubstrateVM registers no MXBeans
        # under that collector. The guard moves to the container memory series
        # below, which cAdvisor reports for every build alike — which is the whole
        # reason this profile collects it.
        jvm_metrics_required=False,
        function_concurrency=8,
        function_queue_size=20,
    )
