"""Explicit, pre-run contracts for single-version memory observations."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
PositiveNumber = Annotated[float, Field(gt=0)]
NonNegativeNumber = Annotated[float, Field(ge=0)]
Text = Annotated[str, Field(min_length=1)]
PREREQUISITE_GROUPS = {
    "error-timeout-cancellation": ("error", "timeout", "cancellation"),
    "async-late-callback": ("async", "late-callback"),
}
MetricOperation = Literal[
    "maximum", "return_to_reference", "growth_review", "expected_zero"
]
CriterionPhase = Literal["baseline", "steady", "drain", "diagnostic"]
DiagnosticOperation = Literal[
    "gc",
    "histogram",
    "heap_dump",
    "jfr",
    "native_memory",
    "native_memory_baseline",
    "native_memory_diff",
]


class _StrictModel(BaseModel):
    """Forbid silent configuration loss and coerced or non-finite values."""

    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, str_strip_whitespace=True
    )


def _unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")


def validate_schedule(
    steady_s: int,
    drain_s: int,
    retention_s: tuple[int, ...],
    cleanup_margin_s: int,
) -> None:
    """Require three complete retention cycles and an additional cleanup margin."""
    values = (steady_s, drain_s, cleanup_margin_s, *retention_s)
    if not retention_s or any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("schedule requires positive integer durations and retention")
    longest = max(retention_s)
    if steady_s < 3 * longest:
        raise ValueError("steady must cover three retention cycles")
    if drain_s < longest + cleanup_margin_s:
        raise ValueError("drain must include retention and cleanup margin")


class PhaseConfig(_StrictModel):
    """Keep preparation, natural observation and diagnostic durations separate."""

    warmup_s: PositiveInt
    baseline_drain_s: PositiveInt
    baseline_window_s: PositiveInt
    steady_s: PositiveInt
    drain_s: PositiveInt
    cleanup_margin_s: PositiveInt


class ImageBuildSpec(_StrictModel):
    """Build a requested recipe unless immutable prebuilt use is explicit."""

    artifact_kind: Literal["oci-image", "process"] = "oci-image"
    mode: Literal["build", "prebuilt"] = "build"
    variant: Text
    platform: Annotated[str, Field(pattern=r"^linux/(amd64|arm64)(/v8)?$")]
    modules: list[Text] = Field(default_factory=list)
    build_options: dict[Text, str] = Field(default_factory=dict)
    digest: Text | None = None
    provenance_receipt: Text | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        """Reject tag fallback and contradictory build/prebuilt inputs."""
        _unique(self.modules, "modules")
        if self.mode == "build":
            if self.digest is not None or self.provenance_receipt is not None:
                raise ValueError(
                    "build mode cannot consume a prebuilt digest or receipt"
                )
        else:
            if (
                self.digest is None
                or re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", self.digest) is None
            ):
                raise ValueError(
                    "prebuilt requires an image reference with a SHA-256 digest"
                )
            if self.provenance_receipt is None:
                raise ValueError("prebuilt requires a provenance receipt")
            if self.build_options:
                raise ValueError("prebuilt cannot apply build options")
        return self


class RolePolicy(_StrictModel):
    """Declare effective limits and evidence required for one observed process role."""

    runtime: Literal["jvm", "native", "node"]
    expected_cpu: PositiveNumber
    memory_limit_bytes: PositiveInt
    required_metrics: list[Text] = Field(min_length=1)
    required_capabilities: list[Text] = Field(min_length=1)
    runtime_options: list[Text]
    collection_sources: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Require separate resident/cgroup memory and unambiguous source lists."""
        for name in ("required_metrics", "required_capabilities", "collection_sources"):
            _unique(getattr(self, name), name)
        required = {"process_rss_bytes", "cgroup_memory_usage_bytes"}
        if not required.issubset(self.required_metrics):
            raise ValueError(
                "each role requires separate RSS and cgroup memory metrics"
            )
        return self


class Criterion(_StrictModel):
    """Specify a numerical policy before observing any candidate results."""

    id: Text
    role: Text
    metric: Text
    label_selector: dict[Text, str] = Field(default_factory=dict)
    unit: Text
    operation: MetricOperation
    phase: CriterionPhase
    window_s: PositiveNumber
    deadline_s: PositiveNumber | None = None
    threshold: NonNegativeNumber | None = None
    absolute_tolerance: NonNegativeNumber | None = None
    relative_tolerance: Annotated[float, Field(ge=0, le=1)] | None = None
    rationale: Text

    @model_validator(mode="after")
    def validate_operation(self) -> Self:
        """Do not let an unused threshold or missing tolerance weaken a criterion."""
        if self.deadline_s is not None and self.window_s > self.deadline_s:
            raise ValueError("criterion window cannot extend before its phase begins")
        if (
            self.metric in {"process_rss_bytes", "cgroup_memory_usage_bytes"}
            and self.unit != "bytes"
        ):
            raise ValueError("memory observations must use bytes")
        if self.operation == "return_to_reference":
            if self.phase != "drain" or self.deadline_s is None:
                raise ValueError(
                    "return_to_reference requires a natural drain deadline"
                )
            if self.absolute_tolerance is None or self.relative_tolerance is None:
                raise ValueError("return_to_reference requires both tolerances")
            if self.threshold is not None:
                raise ValueError("return_to_reference does not consume threshold")
        else:
            if (
                self.absolute_tolerance is not None
                or self.relative_tolerance is not None
            ):
                raise ValueError("tolerances belong only to return_to_reference")
            if self.operation == "expected_zero":
                if self.phase != "drain" or self.deadline_s is None:
                    raise ValueError("expected_zero requires a drain deadline")
                if self.threshold is not None:
                    raise ValueError("expected_zero does not consume threshold")
            elif self.threshold is None:
                raise ValueError("maximum and growth_review require a threshold")
        return self


class DiagnosticPolicy(_StrictModel):
    """Bound diagnostic work and declare how collection completion is observed.

    Two checkpoints declare operations: the baseline window, and the final
    drain. The second is where a difference is measured from the first, so the
    same reading declared in both is the point of the split rather than a
    duplication. Their budgets are shared and run-wide.
    """

    operations: dict[Text, list[DiagnosticOperation]]
    baseline_operations: dict[Text, list[DiagnosticOperation]] = Field(
        default_factory=dict
    )
    timeout_s: PositiveNumber
    max_dumps: Annotated[int, Field(ge=0)]
    max_dump_bytes: Annotated[int, Field(ge=0)]
    helper_images: dict[Text, Text] = Field(default_factory=dict)
    executables: dict[Text, list[Text]] = Field(default_factory=dict)
    gc_completion_evidence: dict[Text, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_operations(self) -> Self:
        """Require named completion evidence: a command exiting proves nothing.

        Uniqueness is per checkpoint: which checkpoint declares an operation
        changes nothing about what proves it completed.
        """
        for checkpoint in (self.operations, self.baseline_operations):
            for role, operations in checkpoint.items():
                _unique(list(operations), "diagnostic operations")
                if "gc" in operations and role not in self.gc_completion_evidence:
                    raise ValueError(
                        "gc requires explicit completion evidence for its role"
                    )
                if "heap_dump" in operations and (
                    self.max_dumps == 0 or self.max_dump_bytes == 0
                ):
                    raise ValueError("heap_dump requires a positive dump budget")
        for argv in self.executables.values():
            if not argv:
                raise ValueError("diagnostic executable argv cannot be empty")
        return self


class PrerequisitePolicy(_StrictModel):
    """Execute required checks or validate explicitly supplied evidence receipts."""

    mode: Literal["run", "receipts"] = "run"
    required_coverage: list[Text]
    relevant_config_keys: dict[Text, list[Text]]
    receipts: dict[Text, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        """Tie every coverage claim to declared configuration dependencies."""
        _unique(self.required_coverage, "required coverage")
        coverage = set(self.required_coverage)
        if set(self.relevant_config_keys) != coverage:
            raise ValueError("every prerequisite needs relevant configuration keys")
        for keys in self.relevant_config_keys.values():
            if not keys:
                raise ValueError("prerequisite configuration keys cannot be empty")
            _unique(keys, "prerequisite configuration keys")
        if self.mode == "receipts" and set(self.receipts) != coverage:
            raise ValueError("receipt mode requires a receipt for every prerequisite")
        if self.mode == "run" and self.receipts:
            raise ValueError("run mode does not consume saved receipts")
        expanded = {}
        for name in self.required_coverage:
            members = PREREQUISITE_GROUPS.get(name, (name,))
            if self.mode == "receipts" and len(members) > 1:
                raise ValueError("saved receipts require atomic prerequisite coverage")
            for member in members:
                if member in expanded:
                    raise ValueError("prerequisite coverage groups overlap")
                expanded[member] = list(self.relevant_config_keys[name])
        self.required_coverage = list(expanded)
        self.relevant_config_keys = expanded
        return self


class WorkloadConfig(_StrictModel):
    """Fix per-function arrival rates and generator validity limits."""

    rates: dict[Text, PositiveNumber] = Field(min_length=1)
    preallocated_vus: PositiveInt
    max_vus: PositiveInt
    max_error_ratio: Annotated[float, Field(ge=0, le=1)]
    max_dropped_iterations: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_capacity(self) -> Self:
        """Do not declare more initially allocated VUs than the generator can own."""
        if self.preallocated_vus > self.max_vus:
            raise ValueError("preallocated_vus cannot exceed max_vus")
        return self


class SoakConfig(_StrictModel):
    """A complete single-version protocol, independent of comparison profiles."""

    # "diagnostic" is an internal purpose: a protocol that deploys and observes
    # but reaches no verdict, so it carries no acceptance criteria and no
    # retention gates. Only heap analysis constructs one, and it never enters
    # evaluation, reporting or the soak lifecycle. A soak scenario file cannot
    # select it (see validate_functions), and "p24" and "smoke" still require
    # criteria and retention exactly as before.
    purpose: Literal["p24", "smoke", "diagnostic"]
    metrics_profile: Literal["advanced", "soak"] = "advanced"
    phases: PhaseConfig
    retention_s: dict[Text, PositiveInt] = Field(default_factory=dict)
    roles: dict[Text, RolePolicy] = Field(min_length=1)
    images: dict[Text, ImageBuildSpec] = Field(min_length=1)
    criteria: list[Criterion] = Field(default_factory=list)
    diagnostics: DiagnosticPolicy
    prerequisites: PrerequisitePolicy
    sample_interval_s: PositiveNumber
    scrape_timeout_s: PositiveNumber
    max_observation_gap_s: PositiveNumber
    artifact_limit_bytes: PositiveInt
    cancellation_timeout_s: PositiveNumber
    workload: WorkloadConfig

    @model_validator(mode="after")
    def validate_protocol(self) -> Self:
        """Reject incomplete timing, roles and policy before any side effect."""
        if self.purpose != "diagnostic" and not (self.criteria and self.retention_s):
            raise ValueError(
                f"{self.purpose} requires acceptance criteria and retention policy"
            )
        if self.purpose == "p24":
            validate_schedule(
                self.phases.steady_s,
                self.phases.drain_s,
                tuple(self.retention_s.values()),
                self.phases.cleanup_margin_s,
            )
            if self.phases.baseline_drain_s < (
                max(self.retention_s.values()) + self.phases.cleanup_margin_s
            ):
                raise ValueError(
                    "baseline drain must include retention and cleanup margin"
                )
            if not self.prerequisites.required_coverage:
                raise ValueError("p24 requires explicit prerequisite coverage")
        if (
            max(self.sample_interval_s, self.scrape_timeout_s)
            > self.max_observation_gap_s
        ):
            raise ValueError(
                "sampling interval and timeout must fit the observation gap"
            )
        if self.phases.baseline_window_s < 2 * self.sample_interval_s:
            raise ValueError(
                "baseline window must contain at least three scheduled samples"
            )
        if self.diagnostics.max_dump_bytes >= self.artifact_limit_bytes:
            raise ValueError("artifact budget must leave room beyond diagnostic dumps")
        self._validate_roles()
        # A diagnostic protocol declares no criteria, so the per-role cgroup
        # budget and RSS residual rules below have nothing to bind to.
        if self.purpose != "diagnostic":
            self._validate_criteria()
        return self

    def _validate_roles(self) -> None:
        expected = {"control-plane", *self.workload.rates}
        if "control-plane" in self.workload.rates or "proxy" in self.workload.rates:
            raise ValueError(
                "workload rates must name functions, not infrastructure roles"
            )
        if not expected.issubset(self.roles) or set(self.roles) - expected - {"proxy"}:
            raise ValueError(
                "roles must cover control-plane, workload functions and optional proxy"
            )
        if set(self.images) != set(self.roles):
            raise ValueError("images must cover exactly the observed application roles")
        for mapping in (
            self.diagnostics.operations,
            self.diagnostics.helper_images,
            self.diagnostics.executables,
            self.diagnostics.gc_completion_evidence,
        ):
            if set(mapping) - set(self.roles):
                raise ValueError("diagnostics refer to an unknown role")

    def _validate_criteria(self) -> None:
        _unique([criterion.id for criterion in self.criteria], "criterion IDs")
        phase_lengths = {
            "baseline": self.phases.baseline_window_s,
            "steady": self.phases.steady_s,
            "drain": self.phases.drain_s,
            "diagnostic": self.diagnostics.timeout_s,
        }
        for criterion in self.criteria:
            role = self.roles.get(criterion.role)
            if role is None:
                raise ValueError("criterion refers to an unknown role")
            if criterion.metric not in role.required_metrics:
                raise ValueError(
                    "criterion metric must be declared required for its role"
                )
            end = (
                criterion.deadline_s
                if criterion.deadline_s is not None
                else criterion.window_s
            )
            if end > phase_lengths[criterion.phase]:
                raise ValueError("criterion window/deadline extends beyond its phase")
        for name, role in self.roles.items():
            own = [criterion for criterion in self.criteria if criterion.role == name]
            budgets = [
                criterion
                for criterion in own
                if criterion.metric == "cgroup_memory_usage_bytes"
                and criterion.operation == "maximum"
                and criterion.phase == "steady"
                and not criterion.label_selector
            ]
            if not budgets or any(
                criterion.threshold is None
                or criterion.threshold > role.memory_limit_bytes
                for criterion in budgets
            ):
                raise ValueError(
                    "each role needs a cgroup budget within its memory limit"
                )
            if not any(
                criterion.metric == "process_rss_bytes"
                and criterion.operation in {"return_to_reference", "growth_review"}
                and criterion.phase == "drain"
                and not criterion.label_selector
                for criterion in own
            ):
                raise ValueError("each role needs an explicit RSS residual policy")

    def validate_functions(self, functions: list[str]) -> None:
        """Bind the protocol to selected functions, with no two-function rule.

        This is the public soak scenario's entry point, so it is also where the
        internal "diagnostic" purpose is refused: a scenario file must never be
        able to run a soak with no acceptance criteria and no retention gates.
        """
        if self.purpose == "diagnostic":
            raise ValueError("a soak scenario must declare a p24 or smoke purpose")
        _unique(functions, "functions")
        if any(not name or name.strip() != name for name in functions):
            raise ValueError(
                "function names must be nonempty and whitespace-free at edges"
            )
        if set(functions) != set(self.workload.rates):
            raise ValueError("workload rates must cover exactly the selected functions")
