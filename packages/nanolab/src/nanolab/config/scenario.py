from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

WorkflowName = Literal["validate", "cli", "loadtest", "offload", "offload-loadtest", "release"]
BackendName = Literal["container", "k8s"]
BuildStrategy = Literal["docker", "buildpack"]
AutoscalingStrategy = Literal["INTERNAL", "HPA"]


# The one key in `resources` that is not a function. The control plane's limits
# are otherwise unreachable from a scenario: nothing sets them, so the chart
# default of one core applies to every load test, and that single core was the
# ceiling a whole investigation spent itself finding.
CONTROL_PLANE_RESOURCES = "control-plane"


class ResourceQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    cpu: float | None = Field(default=None, gt=0, multiple_of=0.001)
    memory_mib: int | None = Field(default=None, alias="memoryMiB", gt=0)


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: ResourceQuantity | None = None
    limits: ResourceQuantity | None = None

    @model_validator(mode="after")
    def requests_within_limits(self) -> "ResourceSpec":
        if self.requests is None or self.limits is None:
            return self
        for field in ("cpu", "memory_mib"):
            request = getattr(self.requests, field)
            limit = getattr(self.limits, field)
            if request is not None and limit is not None and request > limit:
                raise ValueError("resource request must not exceed limit")
        return self


class ReleaseConfig(BaseModel):
    """Release-specific settings embedded in the scenario file."""

    model_config = ConfigDict(extra="forbid")

    version: str
    profile: str = "azure-d8s-v5+d2s-v5-amd64-native-loadtest-v1"
    max_parallelism: int = Field(default=4, gt=0)
    benchmark_runs: int = Field(default=3, ge=1)
    benchmark_scenario: str = "autoscaling-cycle-k8s.yaml"
    throughput_max_loss_percent: float = Field(default=10.0, ge=0)
    p95_max_increase_percent: float = Field(default=15.0, ge=0)
    error_rate_max: float = Field(default=0.30, ge=0, le=1)


# SOJOURN decides from what the caller experiences — queue wait plus service — where the other
# two decide from service time alone. Comparing it against ADAPTIVE_PER_POD isolates the signal,
# since both govern one function at a time and neither divides a budget.
ConcurrencyMode = Literal["ADAPTIVE_PER_POD", "BUDGETED", "SOJOURN"]
# `burst` is the two-function profile: variable closed-loop load calibrated to
# fill the queue without automatically overflowing it, so queue depth, queue
# wait and rejections are readings about the controller rather than about how
# hard the generator was told to push.
# `openloop` schedules arrivals by the clock instead of holding one request per VU.
# Under `burst` the number in the system is pinned by the generator, so queue depth is
# VUs minus limit and no controller can move it by more than a few percent — which is
# why two controllers reading different signals produced end-to-end p95 within 2% of
# each other. Only with open arrivals does the wait become something a limit changes.
# `comparison` is the profile for telling control-plane BUILDS apart rather than
# controllers: open arrivals shaped as warm/climb/hold/spike/recover/climb/spike/drain,
# driving two functions at once. A flat rate is the one regime where the builds are
# hardest to distinguish — a JIT reaches its peak and stays there, and a native image
# starts at its own — so the differences live in the transitions.
LoadProfile = Literal["cycle", "saturation", "burst", "openloop", "comparison", "mixed"]
# Which control-plane build to run. `native` uses a GraalVM image compiled
# beforehand by `scripts/native-java-image.sh control-plane`, so the run does not
# rebuild it — measured at rest, the JVM build held 212 MiB against the native
# build's 31 MiB, and started in 1.59s against 0.14s.
ControlPlaneRuntime = Literal["jvm", "native"]


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: WorkflowName
    backend: BackendName | None = None
    build: BuildStrategy = "docker"
    functions: list[str] = Field(min_length=1)
    resources: dict[str, ResourceSpec] = Field(default_factory=dict)
    handler_envelope: bool = Field(
        default=False,
        alias="handlerEnvelope",
        validation_alias=AliasChoices("handlerEnvelope", "handler_envelope"),
    )
    async_load: bool = Field(
        default=False,
        alias="asyncLoad",
        validation_alias=AliasChoices("asyncLoad", "async_load"),
    )
    persistent_recovery: bool = Field(default=False, alias="persistentRecovery")
    autoscaling: bool = False
    # The concurrency governor is not the autoscaler: it holds the replica count
    # still and moves the per-replica in-flight limit instead. Running both would
    # make a change in effective concurrency unattributable, since that value is
    # replicas x per-replica target.
    concurrency_control: bool = Field(default=False, alias="concurrencyControl")
    # Which controller the run exercises. Selectable because the question the harness
    # exists to answer is comparative: whether one mode holds latency or sheds fewer
    # requests than another is not answerable from a run of either alone.
    concurrency_mode: ConcurrencyMode = Field(
        default="ADAPTIVE_PER_POD", alias="concurrencyMode"
    )
    # `cycle` exercises the controller's trajectory; `saturation` offers more than the queue can
    # hold, which is the only condition under which a request is ever rejected. Whether one
    # controller sheds fewer than another cannot be asked of a run where neither shed any.
    load_profile: LoadProfile = Field(default="cycle", alias="loadProfile")
    # La composizione del carico misto. Con entrambe a zero il generatore misto e'
    # il generatore puramente sincrono, che e' come si guida un braccio di
    # confronto: uno script solo, il mix come parametri, cosi' ogni braccio e'
    # misurato dallo stesso codice.
    async_share: float | None = Field(default=None, alias="asyncShare", ge=0.0, le=1.0)
    idem_share: float | None = Field(default=None, alias="idemShare", ge=0.0, le=1.0)
    control_plane_runtime: ControlPlaneRuntime = Field(
        default="jvm", alias="controlPlaneRuntime"
    )
    autoscaling_strategy: AutoscalingStrategy = Field(default="INTERNAL", alias="autoscalingStrategy")
    hpa_scale_to_zero: bool = Field(default=False, alias="hpaScaleToZero")
    # Which control-plane build this run measures. A plain string rather than a
    # Literal: the catalogue of builds lives with the code that compiles them, and
    # importing it here would make the scenario schema depend on the image layer.
    # The name is checked against that catalogue by the plan, which can also say
    # what the alternatives are.
    control_plane_variant: str | None = Field(
        default=None, alias="controlPlaneVariant"
    )
    # The CPU budget of the control plane under test. Unset means the chart's
    # own default, which is 1 CPU: every comparison so far ran against that
    # without saying so, and `process_cpu_usage` sat at 1.00 in all fourteen
    # cells - the process pinned to the whole of the single core it was allowed.
    # Multiplies every stage of the comparison profile. 1.0 is the shape every
    # matrix so far has used; the CPU model of 2026-08-23 puts the control
    # plane's own ceiling near 5.7x and the concurrency limit near 4.2x, so the
    # interesting range starts well above the increments intuition suggests.
    load_scale: float = Field(default=1.0, alias="loadScale", gt=0)
    # The VU pool, declared rather than derived. A VU is held for a whole
    # iteration, so the pool a run needs is its rate times its latency - and past
    # the knee that latency is what the run is trying to find out. Deriving it
    # from a latency budget censored the 2x and 3x arms of 2026-08-23 at 98% and
    # 100% of their pool, which makes "VUs used" a floor rather than a demand.
    load_vus: int | None = Field(default=None, alias="loadVus", gt=0)
    release: ReleaseConfig | None = None

    @model_validator(mode="after")
    def validate_workflow(self) -> "ScenarioConfig":
        if self.workflow in ("validate", "cli") and self.backend is None:
            raise ValueError(f"backend is required for {self.workflow} workflow")
        if self.workflow == "offload" and self.backend is not None:
            raise ValueError("offload workflow does not take a backend")
        if set(self.resources) - set(self.functions) - {CONTROL_PLANE_RESOURCES}:
            raise ValueError(
                "resources must refer to selected functions, or to "
                f"{CONTROL_PLANE_RESOURCES!r}"
            )
        if self.autoscaling and self.workflow != "loadtest":
            raise ValueError("autoscaling is only supported by the loadtest workflow")
        if self.concurrency_control and self.workflow != "loadtest":
            raise ValueError("concurrencyControl is only supported by the loadtest workflow")
        if self.concurrency_mode != "ADAPTIVE_PER_POD" and not self.concurrency_control:
            raise ValueError("concurrencyMode requires concurrencyControl=true")
        if self.concurrency_control and self.autoscaling:
            raise ValueError("concurrencyControl cannot run together with autoscaling")
        if self.load_profile != "mixed" and (self.async_share is not None or self.idem_share is not None):
            raise ValueError("asyncShare and idemShare belong to loadProfile: mixed")
        if (self.async_share or 0.0) + (self.idem_share or 0.0) > 1.0:
            raise ValueError("asyncShare + idemShare cannot exceed 1.0")
        if self.load_profile in ("comparison", "mixed"):
            # The script drives a named pair, and holds the functions fixed so the
            # control-plane build is the only thing that varies between runs.
            if len(self.functions) != 2:
                raise ValueError("the comparison profile drives exactly two functions")
            if self.concurrency_control or self.autoscaling:
                raise ValueError(
                    "the comparison profile compares control-plane builds; a governor "
                    "or an autoscaler would move the limits underneath that comparison"
                )
        elif self.control_plane_variant is not None:
            raise ValueError(
                "controlPlaneVariant selects one of the builds the comparison profile "
                "compares; no other profile builds them"
            )
        if self.autoscaling_strategy == "HPA" and not self.autoscaling:
            raise ValueError("HPA autoscaling requires autoscaling=true")
        if self.autoscaling_strategy == "HPA" and self.backend == "container":
            raise ValueError("HPA autoscaling requires the k8s backend")
        if self.hpa_scale_to_zero and self.autoscaling_strategy != "HPA":
            raise ValueError("HPA scale-to-zero requires autoscalingStrategy=HPA")
        if self.async_load and (self.workflow != "validate" or self.backend != "container"):
            raise ValueError("async load requires the validate workflow with the container backend")
        if self.persistent_recovery and self.workflow != "validate":
            raise ValueError("persistentRecovery is only supported by the validate workflow")
        if self.workflow == "release" and self.release is None:
            raise ValueError("release workflow requires a 'release' config block")
        return self
