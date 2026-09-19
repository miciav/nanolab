"""Compare the intended experiment with actual process and container observations."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, replace
from typing import Any, Protocol, TypeGuard

from nanolab.config.soak import SoakConfig
from nanolab.tasks.soak.artifacts import fingerprint
from nanolab.tasks.soak.models import CriterionResult, Target


class _ArtifactSink(Protocol):
    """Minimal artifact destination required by preflight evaluation."""

    def write_json(self, name: str, value: dict[str, Any]) -> object: ...


def _positive(value: object) -> TypeGuard[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def applies_declared_options(
    effective: object, declared: list[str] | tuple[str, ...]
) -> bool:
    """Report whether every declared option is in effect, in declared order.

    The observation is the complete launch option list, which always carries
    the image's own flags too, so this is a subsequence and not an equality.
    Order is kept because a later JVM flag overrides an earlier one. An
    unavailable observation proves nothing and never passes.
    """
    if not isinstance(effective, list):
        return False
    remaining = iter(effective)
    return all(option in remaining for option in declared)


def _result(name: str, valid: bool, reason: str) -> CriterionResult:
    return CriterionResult(
        name, "PASS" if valid else "INCONCLUSIVE", reason, ("preflight.json",)
    )


def check_limits(
    expected: dict[str, float], actual: dict[str, float | None]
) -> tuple[CriterionResult, ...]:
    """Reject absent, unlimited or mismatched effective resource limits."""
    if set(expected) != {"cpu", "memory_bytes"} or any(
        not _positive(value) for value in expected.values()
    ):
        raise ValueError("expected CPU and memory limits must be finite and positive")
    results = []
    for key, value in expected.items():
        observed = actual.get(key)
        finite = _positive(observed)
        if (
            key == "memory_bytes"
            and isinstance(observed, (int, float))
            and observed >= 2**60
        ):
            finite = False
        results.append(
            _result(
                f"limits.{key}",
                finite and observed == value,
                f"declared={value!r}, observed={observed!r}",
            )
        )
    return tuple(results)


def effective_cpu_limit(
    quota_us: int | None, period_us: int | None, cpuset: str | None
) -> float | None:
    """Combine a quota and effective cpuset without expanding arbitrary CPU ranges."""
    quota = None
    if quota_us is not None and quota_us > 0:
        if not _positive(period_us):
            raise ValueError("positive CPU quota requires a positive period")
        quota = quota_us / period_us
    count = None
    if cpuset is not None and cpuset.strip():
        if len(cpuset) > 4096:
            raise ValueError("cpuset exceeds parsing budget")
        intervals = []
        for token in cpuset.split(","):
            if re.fullmatch(r"[0-9]+(?:-[0-9]+)?", token) is None:
                raise ValueError("malformed cpuset")
            bounds = token.split("-")
            start, end = int(bounds[0]), int(bounds[-1])
            if end < start or end >= 1048576:
                raise ValueError("invalid cpuset range")
            intervals.append((start, end))
        total, last_end = 0, -1
        for start, end in sorted(intervals):
            total += max(0, end - max(start, last_end + 1) + 1)
            last_end = max(last_end, end)
        count = float(total)
    if quota is None:
        return count
    return quota if count is None else min(quota, count)


def preflight(
    config: SoakConfig,
    targets: tuple[Target, ...],
    observations: dict[str, Any],
    writer: _ArtifactSink,
) -> tuple[CriterionResult, ...]:
    """Persist readiness checks without claiming the soak or prerequisites passed."""
    results = []
    counts = Counter(target.role for target in targets)
    results.append(
        _result(
            "targets",
            set(counts) == set(config.roles)
            and all(count == 1 for count in counts.values()),
            "one identified process is required for every configured role",
        )
    )
    observed_roles = observations.get("roles", {})
    receipts = observations.get("build_receipts", {})
    source = observations.get("snapshot_fingerprint")
    results.append(
        _result(
            "source",
            isinstance(source, str) and bool(source),
            "source snapshot identity must be available",
        )
    )
    for target in targets:
        policy = config.roles.get(target.role)
        if policy is None:
            continue
        actual = observed_roles.get(target.role, {})
        receipt = receipts.get(target.role, {})
        prefix = target.role + "."
        results.extend(
            replace(item, criterion_id=prefix + item.criterion_id)
            for item in check_limits(
                {"cpu": policy.expected_cpu, "memory_bytes": policy.memory_limit_bytes},
                actual,
            )
        )
        sources = actual.get("limit_sources", {})
        results.append(
            _result(
                prefix + "limit_sources",
                bool(sources.get("cpu")) and bool(sources.get("memory_bytes")),
                "effective limit sources must be recorded",
            )
        )
        image = config.images[target.role]
        digest_valid = (
            re.fullmatch(
                r"sha256:[0-9a-f]{64}"
                if image.artifact_kind == "process"
                else r"[^\s@]+@sha256:[0-9a-f]{64}",
                target.image_digest,
            )
            is not None
        )
        results.append(
            _result(
                prefix + "image",
                digest_valid
                and target.image_digest
                == actual.get("image_digest")
                == receipt.get("image_digest")
                and (image.mode != "prebuilt" or image.digest == target.image_digest),
                "running image must match the frozen build or prebuilt digest",
            )
        )
        results.append(
            _result(
                prefix + "source",
                bool(source) and receipt.get("source_fingerprint") == source,
                "all application images must match the selected source snapshot",
            )
        )
        results.append(
            _result(
                prefix + "platform",
                receipt.get("platform") == image.platform,
                "build platform must match the selected platform",
            )
        )
        results.append(
            _result(
                prefix + "runtime",
                actual.get("runtime") == target.runtime == policy.runtime,
                "effective runtime must match the selected role runtime",
            )
        )
        results.append(
            _result(
                prefix + "runtime_options",
                applies_declared_options(
                    actual.get("runtime_options"), policy.runtime_options
                ),
                "declared runtime options must be in effect, in their declared order",
            )
        )
        for key, required in (
            ("metrics", policy.required_metrics),
            ("capabilities", policy.required_capabilities),
            ("collection_sources", policy.collection_sources),
            # Every checkpoint's declarations are checked against the helper here.
            # A reading the pinned helper cannot dispatch is otherwise found at
            # capture time, after the measured phases have already run.
            (
                "diagnostics",
                [
                    *config.diagnostics.operations.get(target.role, []),
                    *config.diagnostics.baseline_operations.get(target.role, []),
                ],
            ),
        ):
            values = actual.get(key)
            valid = isinstance(values, list) and set(required).issubset(values)
            results.append(
                _result(
                    prefix + key, valid, f"required={required!r}, observed={values!r}"
                )
            )
        if target.role == "control-plane":
            modules = actual.get("modules")
            results.append(
                _result(
                    prefix + "modules",
                    isinstance(modules, list) and set(modules) == set(image.modules),
                    "effective module selection must match the image recipe",
                )
            )
        gc_evidence = config.diagnostics.gc_completion_evidence.get(target.role)
        if gc_evidence is not None:
            results.append(
                _result(
                    prefix + "gc_evidence",
                    actual.get("gc_completion_evidence") == gc_evidence,
                    "the requested collection needs an observable completion source",
                )
            )
    results.append(
        _result(
            "retention",
            observations.get("retention_s") == config.retention_s,
            "effective retention must match the checkpoint schedule",
        )
    )
    free_bytes = observations.get("free_bytes")
    results.append(
        _result(
            "disk",
            _positive(free_bytes) and free_bytes >= config.artifact_limit_bytes,
            "free disk must cover the declared evidence budget",
        )
    )
    generator = observations.get("generator", {})
    capacity = generator.get("max_vus")
    results.append(
        _result(
            "generator",
            generator.get("available") is True
            and _positive(capacity)
            and capacity >= config.workload.max_vus,
            "generator must be available with its declared VU capacity",
        )
    )
    resolved = config.model_dump(mode="json")
    writer.write_json(
        "preflight.json",
        {
            "schema": "nanolab-soak-v1",
            "scope": "preflight-only",
            "configuration_fingerprint": fingerprint(resolved),
            "configuration": resolved,
            "targets": [asdict(target) for target in targets],
            "observations": observations,
            "criteria": [asdict(result) for result in results],
        },
    )
    return tuple(results)
