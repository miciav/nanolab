"""Role-bound process probes, isolated from legacy loadtest collection."""

import json
import math
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit

from nanolab.tasks.soak.models import Phase, Sample, Target
from nanolab.tasks.soak.probes import parse_exposition, parse_procfs_memory

_PROCESS_METRICS = frozenset(("process_rss_bytes", "process_pss_bytes"))
_CGROUP_METRICS = frozenset(
    (
        "cgroup_memory_usage_bytes",
        "cgroup_memory_limit_bytes",
        "docker_working_set_estimate_bytes",
        "cgroup_memory_stat",
    )
)
_MAX_OUTPUT = 4 * 1024 * 1024


def _duration(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("timeout must be positive and finite")


@dataclass(frozen=True)
class RoleBinding:
    """One role's target, exposition endpoint and the metrics it must report."""

    target: Target
    endpoint: str | None
    required_metrics: Mapping[str, str]

    def __post_init__(self) -> None:
        """Reject a binding that cannot identify one bounded target."""
        if not self.required_metrics or len(self.required_metrics) > 1000:
            raise ValueError("invalid required metric count")
        if any(not key or not value for key, value in self.required_metrics.items()):
            raise ValueError("metric names and units are required")
        if self.endpoint is not None:
            parsed = urlsplit(self.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("metrics endpoint must use HTTP(S)")
        object.__setattr__(
            self, "required_metrics", MappingProxyType(dict(self.required_metrics))
        )


class CollectionTransport(Protocol):
    """Collect one target's raw observations within a bounded deadline."""

    def collect(
        self, target: Target, endpoint: str | None, timeout_s: float
    ) -> dict[str, Any]:
        """Return the raw procfs, cgroup and exposition readings for a target."""
        ...


class SubprocessTransport:
    """Bound the whole collection, including DNS and slow response bodies.

    The trusted helper never spawns children and caps its output. subprocess.run
    kills and waits for it on timeout. No Docker CLI or shell is executed.
    """

    def __init__(self, docker_socket: str = "/var/run/docker.sock"):
        """Bind the read-only Docker socket the helper is allowed to query."""
        self._socket = docker_socket

    def collect(
        self, target: Target, endpoint: str | None, timeout_s: float
    ) -> dict[str, Any]:
        """Run one bounded collection in a dedicated child and return its reply."""
        _duration(timeout_s)
        request = json.dumps(
            {
                "target": asdict(target),
                "endpoint": endpoint,
                "docker_socket": self._socket,
                "timeout_s": timeout_s,
            }
        )
        try:
            result = subprocess.run(
                [sys.executable, "-m", "nanolab.tasks.soak.collector", request],
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("process collection deadline exceeded") from error
        if result.returncode:
            raise OSError(
                "collector failed: "
                + result.stderr[:1024].decode("utf-8", errors="replace")
            )
        if len(result.stdout) > _MAX_OUTPUT:
            raise ValueError("collector output limit exceeded")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("collector response must be an object")
        return data


def _identity_matches(value: object, target: Target) -> bool:
    return (
        isinstance(value, dict)
        and value.get("container_id") == target.container_id
        and type(value.get("process_id")) is int
        and value["process_id"] == target.process_id
        and value.get("process_started_at") == target.process_started_at
        and value.get("running") is True
        and isinstance(value.get("image_digests"), list)
        and target.image_digest in value["image_digests"]
    )


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("missing or invalid cgroup value")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("nonfinite or negative cgroup value")
    return number


class RoleBoundProbe:
    """Sample each role only through its own binding, never another's."""

    def __init__(
        self,
        bindings: tuple[RoleBinding, ...],
        transport: CollectionTransport,
        *,
        timeout_s: float,
    ):
        """Bind one transport and deadline to a set of distinct role targets."""
        _duration(timeout_s)
        if not bindings or len(bindings) > 128:
            raise ValueError("invalid role count")
        if len({binding.target.role for binding in bindings}) != len(bindings):
            raise ValueError("duplicate role binding")
        self._bindings = {binding.target.role: binding for binding in bindings}
        self._transport = transport
        self._timeout = timeout_s

    def targets(self) -> tuple[Target, ...]:
        """Return every bound target, one per configured role."""
        return tuple(binding.target for binding in self._bindings.values())

    def sample(
        self, target: Target, phase: Phase, scheduled_s: float
    ) -> tuple[Sample, ...]:
        """Collect one target's samples, verifying its identity either side."""
        binding = self._bindings.get(target.role)
        if binding is None or binding.target != target:
            raise ValueError("target is not bound to this probe")
        started = time.monotonic()
        # Keep only the current scrape; no historical label set is retained.
        values: list[tuple[str, tuple[tuple[str, str], ...], str, float, str]] = []
        errors: dict[str, str] = {}
        try:
            data = self._transport.collect(target, binding.endpoint, self._timeout)
            if not _identity_matches(
                data.get("before"), target
            ) or not _identity_matches(data.get("after"), target):
                raise ValueError(
                    "container/process/image identity changed or unavailable"
                )
            raw_errors = data.get("errors", {})
            if isinstance(raw_errors, dict):
                errors.update(
                    {str(key): str(value)[:1024] for key, value in raw_errors.items()}
                )
        except (OSError, ValueError, TypeError, OverflowError) as error:
            return self._missing(
                binding, phase, scheduled_s, started, str(error)[:1024]
            )

        try:
            proc = data.get("procfs", {})
            memory = parse_procfs_memory(proc.get("status"), proc.get("smaps_rollup"))
            for metric, value in memory.items():
                if value is not None:
                    values.append((metric, (), "bytes", float(value), "procfs"))
        except (AttributeError, ValueError, TypeError, OverflowError) as error:
            errors["procfs"] = str(error)[:1024]

        try:
            cgroup = data.get("cgroup")
            if cgroup is not None:
                usage = _number(cgroup.get("memory_current"))
                limit = _number(cgroup.get("memory_max"))
                raw = cgroup.get("memory_stat", {})
                source = "cgroup-v2"
                values.extend(
                    (
                        (
                            "cgroup_memory_usage_bytes",
                            (),
                            "bytes",
                            usage,
                            source + "/memory.current",
                        ),
                        (
                            "cgroup_memory_limit_bytes",
                            (),
                            "bytes",
                            limit,
                            source + "/memory.max",
                        ),
                    )
                )
            else:
                memory_stats = data.get("stats", {}).get("memory_stats", {})
                usage = _number(memory_stats.get("usage"))
                limit = _number(memory_stats.get("limit"))
                raw = memory_stats.get("stats", {})
                source = "docker-engine"
                values.extend(
                    (
                        (
                            "cgroup_memory_usage_bytes",
                            (),
                            "bytes",
                            usage,
                            source + "/memory_stats",
                        ),
                        (
                            "cgroup_memory_limit_bytes",
                            (),
                            "bytes",
                            limit,
                            source + "/memory_stats",
                        ),
                    )
                )
            if not isinstance(raw, dict) or len(raw) > 1000:
                raise ValueError("invalid raw cgroup statistics")
            for field, raw_value in raw.items():
                values.append(
                    (
                        "cgroup_memory_stat",
                        (("field", str(field)),),
                        "raw",
                        _number(raw_value),
                        source
                        + (
                            "/memory.stat"
                            if cgroup is not None
                            else "/memory_stats.stats"
                        ),
                    )
                )
            inactive = raw.get("total_inactive_file", raw.get("inactive_file"))
            if cgroup is None and inactive is not None:
                values.append(
                    (
                        "docker_working_set_estimate_bytes",
                        (),
                        "bytes",
                        max(0.0, usage - _number(inactive)),
                        "docker-engine/derived",
                    )
                )
        except (AttributeError, ValueError, TypeError, OverflowError) as error:
            errors["stats"] = str(error)[:1024]

        try:
            text = data.get("exposition")
            if not isinstance(text, str):
                raise ValueError("exposition unavailable")
            for metric, labels, value in parse_exposition(text):
                if metric not in _PROCESS_METRICS and metric not in _CGROUP_METRICS:
                    # Unknown units remain explicit; never infer a gauge's unit.
                    values.append(
                        (
                            metric,
                            labels,
                            binding.required_metrics.get(metric, "unknown"),
                            value,
                            "prometheus",
                        )
                    )
        except (ValueError, TypeError, OverflowError) as error:
            errors["exposition"] = str(error)[:1024]

        ended = time.monotonic()
        if ended - started > self._timeout:
            return self._missing(
                binding, phase, scheduled_s, started, "collection exceeded deadline"
            )
        if len(values) + len(binding.required_metrics) > 10000:
            return self._missing(
                binding, phase, scheduled_s, started, "combined sample limit exceeded"
            )
        samples = [
            Sample(
                target,
                phase,
                scheduled_s,
                started,
                ended,
                metric,
                labels,
                unit,
                value,
                "observed",
                source,
                None,
            )
            for metric, labels, unit, value, source in values
        ]
        present = {row.metric for row in samples}
        for metric, unit in binding.required_metrics.items():
            if metric not in present:
                source = (
                    "procfs"
                    if metric in _PROCESS_METRICS
                    else "stats"
                    if metric in _CGROUP_METRICS
                    else "exposition"
                )
                samples.append(
                    Sample(
                        target,
                        phase,
                        scheduled_s,
                        started,
                        ended,
                        metric,
                        (),
                        unit,
                        None,
                        "unavailable",
                        source,
                        errors.get(source, "required metric absent from source"),
                    )
                )
        return tuple(samples)

    def _missing(
        self,
        binding: RoleBinding,
        phase: Phase,
        scheduled_s: float,
        started: float,
        reason: str,
    ) -> tuple[Sample, ...]:
        ended = time.monotonic()
        return tuple(
            Sample(
                binding.target,
                phase,
                scheduled_s,
                started,
                ended,
                metric,
                (),
                unit,
                None,
                "unavailable",
                "collection",
                reason,
            )
            for metric, unit in binding.required_metrics.items()
        )
