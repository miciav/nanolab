"""HTTP prerequisite profiles for the strict POSIX prerequisite receipt runner.

Integration API::

    runner = make_live_runner(inputs=frozen_inputs, factory=fresh_platform)
    receipt = await run_prerequisites(
        inputs=frozen_inputs, required_coverage=expand_coverage(coverage),
        runner=runner, writer=writer,
        timeout_s=required_body_budget(frozen_inputs),
    )

``fresh_platform(coverage_id, lifetime_id, frozen_inputs)`` returns an async
context manager yielding ``LivePlatform``. Acquisition, HTTP client construction,
identity/config inspection and confirmed release happen inside the existing
worker. The factory owns a NEW platform for each lifetime, built from the SAME
already frozen images/settings, including default owner TTLs 30/300/1800 seconds.
It must not return the measured soak platform. Failed acquisition compensation
and recovery after a killed worker remain the platform owner's responsibility.

``observe_config`` must inspect effective settings and return the same shape as
``inputs['relevant_config']``; echoing requested settings is not observation.
Each config names ``function`` and its SDK ``role``. Optional ``request`` is the
actual invocation envelope; otherwise the hashed payload artifact is wrapped in
``{'input': payload}``. Fault profiles need real configured fault/delay handlers;
the standard Java warm-echo handler only echoes and cannot prove these paths.

No callback 204, client timeout, logical execution count or missing metric is
interpreted as physical settlement. Current images lack several authoritative
populations; preflight reports these before exercise. Instrumented images can
provide frozen ``population_metrics`` bindings (role -> population -> nonempty
list of {metric, labels?} selectors). These select actual HTTP scrape samples,
never supplied values. Bindings must describe the named physical population;
they are not an escape hatch to relabel logical records or exposition size.

For error responses with HTTP 200, freeze ``expected_error_code`` in the error
profile: the response and terminal poll must agree on that code and execution ID.
``population_semantics[role]['metric_series'] = 'prometheus-exposition-series'``
explicitly selects exposition cardinality (all name/label identities), not registry
size. Optional physical replay counters require an explicit metric binding and frozen
``population_semantics[role]['physical_executions'] = 'handler-starts'``. The
request counter ``runtime_invocations_total`` is not such an instrument; no
request-to-handler equivalence proof is implemented by this adapter.

The existing validator requires sampling after each settlement ``retention_s``.
This adapter does not shorten that interval or synthesize timestamps. A bounded
retained-state shortcut would require a separately reviewed validator contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

from nanolab.config.soak import PREREQUISITE_GROUPS
from nanolab.tasks.soak.artifacts import describe_artifact, fingerprint
from nanolab.tasks.soak.preflight import applies_declared_options
from nanolab.tasks.soak.prerequisites import SUPPORTED_COVERAGE, ProfileRunner
from nanolab.tasks.soak.probes import parse_exposition

_MAX_RESPONSE = 1024 * 1024
_MAX_EVIDENCE = 256 * 1024
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_TERMINAL = {"success", "error", "timeout"}

# Deliberately incomplete: only source-backed observations have defaults.
# Java runtime_in_flight is a servlet request gauge, NOT physical handler work.
# CP execution_in_flight_records is logical state, NOT physical handler work.
# Pool pending acquisitions is NOT all pending HTTP; exposition series is NOT
# registry population. None of these are aliases for the required populations.
_DEFAULT_METRICS = {
    "control-plane": {
        "execution_records": ("execution_in_flight_records",),
        "outcomes": ("execution_store_size",),
        "idempotency_entries": ("idempotency_keys_held",),
        "logical_executions": ("invocation_execution_reservations",),
        "canonical_input_bytes": ("invocation_canonical_input_bytes",),
        "physical_input_copy_bytes": ("invocation_physical_input_copy_bytes",),
        "waiters": ("execution_waiters_retained",),
        "expiry_queue_depth": ("execution_expiry_queue_depth",),
        "pending_acquisitions": ("nanofaas_http_pool_pending_acquisitions",),
        "replica_snapshots": ("replica_snapshot_entries",),
        "retired_owners": ("function_capacity_retired_generations",),
    },
    "java": {
        "live_executions": ("runtime_active_handlers",),
        "callbacks": ("runtime_pending_callbacks",),
        "callback_bytes": ("runtime_pending_callback_bytes",),
    },
    "javascript": {
        "live_executions": ("runtime_active_handlers",),
        "input_bytes": ("runtime_input_bytes",),
        "output_bytes": ("runtime_output_bytes",),
        "callbacks": ("runtime_pending_callbacks",),
        "callback_bytes": ("runtime_pending_callback_bytes",),
        "serialized_callback_bytes": ("runtime_serialized_callback_bytes",),
    },
}


class UnsupportedPreflightError(RuntimeError):
    """An authoritative observation is unavailable; this cannot qualify P24."""


@dataclass
class LivePlatform:
    """One fresh, owned platform; all observers must query that live lifetime.

    ``client`` is owned/closed by the factory and has the control-plane base URL.
    ``metrics_urls`` identifies a distinct actual process per application role.
    Supported runtime kinds are ``control-plane``, ``java`` and ``javascript``;
    unsupported kinds need explicit bindings backed by real instrumentation.
    ``record`` optionally persists raw events in a worker-owned bounded writer.
    """

    lifetime_id: str
    client: httpx.AsyncClient
    metrics_urls: dict[str, str]
    observe_identities: Callable[[], Awaitable[dict[str, str]]]
    observe_config: Callable[[], Awaitable[dict[str, Any]]]
    runtime_kinds: dict[str, str]
    record: Callable[[dict[str, Any]], None] | None = None


FreshPlatformFactory = Callable[
    [str, str, dict[str, Any]], AbstractAsyncContextManager[LivePlatform]
]


def expand_coverage(coverage: Iterable[str]) -> frozenset[str]:
    """Expand workflow group labels into the validator's exact atomic IDs."""
    result: set[str] = set()
    for name in coverage:
        result.update(PREREQUISITE_GROUPS.get(name, (name,)))
    unknown = result - SUPPORTED_COVERAGE
    if unknown:
        raise UnsupportedPreflightError(
            "unsupported coverage: " + ", ".join(sorted(unknown))
        )
    return frozenset(result)


def _positive(value: object, name: str) -> float:
    # Spelled as two comparisons rather than `in (...)` so the check narrows.
    if type(value) is not int and type(value) is not float:
        raise UnsupportedPreflightError(f"{name} must be finite and positive")
    if not math.isfinite(value) or value <= 0:
        raise UnsupportedPreflightError(f"{name} must be finite and positive")
    return float(value)


def required_body_budget(inputs: dict[str, Any]) -> float:
    """Allow real exercise plus the longest frozen retention, without changing it."""
    delays = [
        p["retention_s"]
        for role in inputs["settlement"].values()
        for p in role.values()
    ]
    if any(
        type(d) not in (int, float) or not math.isfinite(d) or d < 0 for d in delays
    ):
        raise UnsupportedPreflightError("invalid frozen settlement retention")
    exercise = max(
        _positive(c.get("exercise_timeout_s", 30), "exercise_timeout_s")
        for c in inputs["relevant_config"].values()
    )
    # Identity/config observers and each distinct retention scrape also need time.
    request = max(
        _positive(c.get("request_timeout_s", 10), "request_timeout_s")
        for c in inputs["relevant_config"].values()
    )
    return (
        max(delays, default=0)
        + exercise
        + (len(set(delays)) + 1) * (len(inputs["images"]) + 2) * request
        + 1
    )


def make_live_runner(
    *, inputs: dict[str, Any], factory: FreshPlatformFactory
) -> ProfileRunner:
    """Bind immutable inputs to fresh contexts; keep receipts in prerequisites.py."""
    frozen = deepcopy(inputs)
    expected = fingerprint(frozen)
    expand_coverage(frozen["relevant_config"])

    @asynccontextmanager
    async def runner(coverage_id: str, lifetime_id: str):
        if coverage_id not in frozen["relevant_config"]:
            raise UnsupportedPreflightError(
                "profile absent from frozen relevant configuration"
            )
        if not lifetime_id or _NAME.fullmatch(lifetime_id) is None:
            raise UnsupportedPreflightError("invalid owned lifetime identity")
        supplied = deepcopy(frozen)
        async with factory(coverage_id, lifetime_id, supplied) as live:
            if fingerprint(supplied) != expected:
                raise UnsupportedPreflightError("factory changed frozen inputs")
            if live.lifetime_id != lifetime_id:
                raise UnsupportedPreflightError(
                    "factory returned another platform lifetime"
                )
            session = LiveProfileSession(live, frozen, coverage_id)
            await session.preflight()
            yield session

    return runner


def _matches_effective_configuration(observed: object, expected: dict) -> bool:
    if not isinstance(observed, dict):
        return False
    candidate = deepcopy(observed)
    try:
        for coverage, recipe in expected.items():
            roles = recipe.get("effective_config", {}).get("roles", {})
            for role, settings in roles.items():
                declared = settings.get("runtime_options")
                if declared is None:
                    continue
                actual = candidate[coverage]["effective_config"]["roles"][role]
                if not applies_declared_options(
                    actual.get("runtime_options"), declared
                ):
                    return False
                actual["runtime_options"] = deepcopy(declared)
    except (AttributeError, KeyError, TypeError):
        return False
    return candidate == expected


class LiveProfileSession:
    """Perform bounded HTTP operations and obtain independent physical samples."""

    def __init__(self, platform: LivePlatform, inputs: dict[str, Any], coverage: str):
        """Keep only bounded evidence for this single owned profile."""
        self.platform = platform
        self.inputs = deepcopy(inputs)
        self.coverage = coverage
        if coverage not in SUPPORTED_COVERAGE:
            raise UnsupportedPreflightError(f"unsupported coverage: {coverage}")
        self.config = self.inputs["relevant_config"][coverage]
        self.request_timeout = _positive(
            self.config.get("request_timeout_s", 10), "request_timeout_s"
        )
        self.exercise_timeout = _positive(
            self.config.get("exercise_timeout_s", 30), "exercise_timeout_s"
        )
        self.poll_interval = _positive(
            self.config.get("poll_interval_s", 0.05), "poll_interval_s"
        )
        self.events: list[dict[str, Any]] = []
        self.event_bytes = 0

    def _record(self, kind: str, **values: Any) -> None:
        event = {"kind": kind, "observed_s": monotonic(), **values}
        self.event_bytes += len(json.dumps(event, allow_nan=False).encode())
        if self.event_bytes > _MAX_EVIDENCE:
            raise UnsupportedPreflightError(
                "runtime evidence exceeds bounded profile capacity"
            )
        self.events.append(event)
        if self.platform.record is not None:
            self.platform.record({"lifetime_id": self.platform.lifetime_id, **event})

    async def identities(self) -> dict[str, str]:
        """Reinspect immutable image identities, never return requested identities."""
        async with asyncio.timeout(self.request_timeout):
            observed = await self.platform.observe_identities()
        self._record("images", images=observed)
        if observed != self.inputs["images"]:
            raise UnsupportedPreflightError(
                "effective images differ from frozen images"
            )
        return observed

    async def _configuration(self) -> None:
        async with asyncio.timeout(self.request_timeout):
            observed = await self.platform.observe_config()
        self._record("configuration", relevant_config=observed)
        if not _matches_effective_configuration(
            observed, self.inputs["relevant_config"]
        ):
            raise UnsupportedPreflightError(
                "effective configuration differs from frozen settings"
            )

    async def _http(self, method: str, path: str, **kwargs: Any):
        try:
            async with asyncio.timeout(self.request_timeout):
                async with self.platform.client.stream(
                    method,
                    path,
                    follow_redirects=False,
                    timeout=self.request_timeout,
                    **kwargs,
                ) as response:
                    chunks = bytearray()
                    async for part in response.aiter_bytes():
                        chunks.extend(part)
                        if len(chunks) > _MAX_RESPONSE:
                            raise UnsupportedPreflightError(
                                "HTTP response exceeds profile byte limit"
                            )
                    content = bytes(chunks)
                    headers = dict(response.headers)
                    status = response.status_code
        except (httpx.HTTPError, TimeoutError) as error:
            raise UnsupportedPreflightError(
                f"HTTP transport observation unavailable: {error}"
            ) from error
        self._record(
            "http",
            method=method,
            url=str(self.platform.client.base_url.join(path)),
            http_status=status,
            body_sha256=hashlib.sha256(content).hexdigest(),
        )
        return status, headers, content

    async def _json(self, method: str, path: str, **kwargs: Any):
        status, headers, content = await self._http(method, path, **kwargs)
        try:
            body = json.loads(content) if content else None
        except ValueError as error:
            raise UnsupportedPreflightError(
                "HTTP response is not a JSON observation"
            ) from error
        self._record(
            "http_json", method=method, path=path, http_status=status, body=body
        )
        return status, headers, body

    def _selectors(self, role: str, population: str) -> list[dict[str, Any]]:
        explicit = self.inputs.get("population_metrics", {}).get(role, {})
        if population in explicit:
            selectors = explicit[population]
        else:
            kind = self.platform.runtime_kinds.get(role)
            names = _DEFAULT_METRICS.get(kind or "", {}).get(population)
            selectors = [{"metric": name} for name in names] if names else []
        if not isinstance(selectors, list) or not selectors:
            raise UnsupportedPreflightError(
                f"{role}/{population}: no authoritative metric binding"
            )
        for selector in selectors:
            if (
                not isinstance(selector, dict)
                or set(selector) - {"metric", "labels"}
                or not isinstance(selector.get("metric"), str)
                or not selector["metric"]
                or not isinstance(selector.get("labels", {}), dict)
            ):
                raise UnsupportedPreflightError(
                    f"{role}/{population}: invalid metric selector"
                )
        if population == "physical_executions":
            semantics = self.inputs.get("population_semantics", {}).get(role, {})
            if semantics.get(population) != "handler-starts":
                raise UnsupportedPreflightError(
                    f"{role}/{population}: frozen handler-starts semantics required"
                )
            if any(s["metric"] == "runtime_invocations_total" for s in selectors):
                raise UnsupportedPreflightError(
                    f"{role}/{population}: runtime_invocations_total counts requests; "
                    "physical handler equivalence is not established"
                )
        return selectors

    async def _scrape(self, role: str):
        url = self.platform.metrics_urls.get(role)
        if not url:
            raise UnsupportedPreflightError(f"{role}: metrics endpoint unavailable")
        status, _, content = await self._http("GET", url)
        if status != 200:
            raise UnsupportedPreflightError(f"{role}: metrics HTTP {status}")
        try:
            return parse_exposition(content.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise UnsupportedPreflightError(
                f"{role}: invalid metrics: {error}"
            ) from error

    def _value(self, role: str, population: str, rows) -> float:
        if population == "metric_series":
            semantics = self.inputs.get("population_semantics", {}).get(role, {})
            if semantics.get(population) != "prometheus-exposition-series":
                raise UnsupportedPreflightError(
                    f"{role}/{population}: explicit frozen exposition semantics "
                    f"required"
                )
            if population in self.inputs.get("population_metrics", {}).get(role, {}):
                raise UnsupportedPreflightError(
                    f"{role}/{population}: exposition cardinality cannot use metric "
                    f"selectors"
                )
            if not rows:
                raise UnsupportedPreflightError(
                    f"{role}/{population}: empty metrics exposition"
                )
            identities = [
                {"metric": name, "labels": dict(labels)} for name, labels, _ in rows
            ]
            value = float(len(identities))
            self._record(
                "population",
                role=role,
                population=population,
                value=value,
                semantics="prometheus-exposition-series",
                series=identities,
                source=self.platform.metrics_urls[role],
            )
            return value
        selected = []
        identities = set()
        for selector in self._selectors(role, population):
            matches = [
                (name, labels, value)
                for name, labels, value in rows
                if name == selector["metric"]
                and all(
                    dict(labels).get(k) == v
                    for k, v in selector.get("labels", {}).items()
                )
            ]
            if not matches:
                raise UnsupportedPreflightError(
                    f"{role}/{population}: metric absent: {selector['metric']}"
                )
            for name, labels, value in matches:
                if value < 0 or (name, labels) in identities:
                    raise UnsupportedPreflightError(
                        f"{role}/{population}: negative or overlapping samples"
                    )
                identities.add((name, labels))
                selected.append(
                    {"metric": name, "labels": dict(labels), "value": value}
                )
        value = sum(sample["value"] for sample in selected)
        if not math.isfinite(value):
            raise UnsupportedPreflightError(f"{role}/{population}: nonfinite aggregate")
        self._record(
            "population",
            role=role,
            population=population,
            value=value,
            samples=selected,
            source=self.platform.metrics_urls[role],
        )
        return value

    async def _population(self, role: str, population: str) -> float:
        return self._value(role, population, await self._scrape(role))

    async def populations(self) -> dict[str, dict[str, float]]:
        """Scrape every required role; absent telemetry is an explicit gap."""
        await self.identities()
        await self._configuration()
        result = {}
        gaps = []
        for role, policies in self.inputs["settlement"].items():
            result[role] = {}
            try:
                rows = await self._scrape(role)
            except UnsupportedPreflightError as error:
                gaps.append(str(error))
                continue
            for population in policies:
                try:
                    result[role][population] = self._value(role, population, rows)
                except UnsupportedPreflightError as error:
                    gaps.append(str(error))
        if gaps:
            raise UnsupportedPreflightError(
                "unsupported prerequisite populations: " + "; ".join(gaps)
            )
        return result

    async def preflight(self) -> None:
        """Reject observation/config gaps before starting behavioral traffic."""
        roles = set(self.inputs["images"])
        if set(self.platform.metrics_urls) != roles or len(
            set(self.platform.metrics_urls.values())
        ) != len(roles):
            raise UnsupportedPreflightError(
                "metrics endpoints must identify every distinct application role"
            )
        self._function()
        self._request_body()
        await self.populations()
        if self.coverage == "cancellation":
            await self._population("control-plane", "waiters")
        if self.coverage == "late-callback":
            self._callback_result()
        if self.coverage == "function-name-churn":
            self._function_spec()

    def _function(self) -> str:
        name = self.config.get("function")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise UnsupportedPreflightError("profile needs a real frozen function name")
        return name

    def _role(self) -> str:
        role = self.config.get("role")
        if role == "control-plane" or role not in self.inputs["images"]:
            raise UnsupportedPreflightError("profile requires its actual SDK role")
        return role

    def _request_body(self):
        if "request" in self.config:
            body = deepcopy(self.config["request"])
        else:
            descriptor = self.inputs["payload"]
            path = Path(descriptor["path"])
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise UnsupportedPreflightError(
                    "payload requires an absolute regular artifact"
                )
            if describe_artifact(path) != descriptor:
                raise UnsupportedPreflightError(
                    "payload artifact differs from frozen identity"
                )
            with path.open("rb") as stream:
                content = stream.read(_MAX_RESPONSE + 1)
            if len(content) > _MAX_RESPONSE:
                raise UnsupportedPreflightError("payload exceeds profile byte limit")
            body = {"input": json.loads(content)}
        if not isinstance(body, dict) or "input" not in body:
            raise UnsupportedPreflightError(
                "request must be an invocation envelope with input"
            )
        return body

    async def _invoke(self, *, enqueue=False, key=None, name=None):
        headers = dict(self.config.get("headers", {}))
        if any(k.lower() == "idempotency-key" for k in headers):
            raise UnsupportedPreflightError(
                "idempotency key is owned by the replay profile"
            )
        if key is not None:
            headers["Idempotency-Key"] = key
        function = name or self._function()
        operation = "enqueue" if enqueue else "invoke"
        return await self._json(
            "POST",
            f"/v1/functions/{quote(function, safe='')}:{operation}",
            json=self._request_body(),
            headers=headers,
        )

    @staticmethod
    def _execution_id(headers, body) -> str:
        identity = body.get("executionId") if isinstance(body, dict) else None
        header = headers.get("x-execution-id")
        if identity and header and identity != header:
            raise UnsupportedPreflightError("response execution identities disagree")
        identity = identity or header
        if not isinstance(identity, str) or not identity:
            raise UnsupportedPreflightError("execution identity unavailable")
        return identity

    async def _terminal(self, identity: str):
        while True:
            status, _, body = await self._json(
                "GET", "/v1/executions/" + quote(identity, safe="")
            )
            if (
                status != 200
                or not isinstance(body, dict)
                or body.get("executionId") != identity
            ):
                raise UnsupportedPreflightError(
                    "execution status observation unavailable or mismatched"
                )
            if body.get("status") in _TERMINAL:
                return body
            if body.get("status") not in {"queued", "running"}:
                raise UnsupportedPreflightError("unknown execution state")
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _outcome(body: dict[str, Any]) -> str:
        # ATTEMPT_TIMEOUT is produced by the actual dispatch deadline path.
        if (
            body.get("status") == "error"
            and (body.get("error") or {}).get("code") == "ATTEMPT_TIMEOUT"
        ):
            return "TIMEOUT"
        return str(body.get("status", "unknown")).upper()

    def _callback_result(self) -> dict[str, Any]:
        result = self.config.get("callback_result")
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or "output" not in result
        ):
            raise UnsupportedPreflightError(
                "late callback needs a frozen successful InvocationResult"
            )
        return deepcopy(result)

    async def _late_callback(self, identity, terminal):
        before = monotonic()
        status, _, _ = await self._http(
            "POST",
            "/v1/internal/executions/" + quote(identity, safe="") + ":complete",
            json=self._callback_result(),
        )
        after = await self._terminal(identity)
        ignored = (
            status == 204 and self._outcome(terminal) == "TIMEOUT" and after == terminal
        )
        return {
            "outcome": self._outcome(terminal),
            "callback_status": "ignored" if ignored else "changed",
            "callback_after_timeout": self._outcome(terminal) == "TIMEOUT"
            and before <= monotonic(),
            "callback_http_status": status,
            "terminal_before": terminal,
            "terminal_after": after,
        }

    async def _replay(self):
        ids, outputs = [], []
        key = "p24-" + self.platform.lifetime_id
        for _ in range(2):
            status, headers, body = await self._invoke(key=key)
            identity = self._execution_id(headers, body)
            terminal = await self._terminal(identity)
            if status != 200 or terminal["status"] != "success":
                raise UnsupportedPreflightError(
                    "replay did not return a successful real execution"
                )
            ids.append(identity)
            outputs.append(body.get("output") if isinstance(body, dict) else None)
        return {
            "execution_ids": ids,
            "outputs": outputs,
        }

    async def _cancel(self):
        baseline = await self._population("control-plane", "waiters")
        task = asyncio.create_task(self._invoke())
        try:
            while True:
                active = await self._population("control-plane", "waiters")
                if task.done():
                    await task
                    raise UnsupportedPreflightError(
                        "invocation completed before cancellation"
                    )
                if active == baseline + 1:
                    break
                await asyncio.sleep(self.poll_interval)
            task.cancel()
            cancelled = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
            while True:
                released = await self._population("control-plane", "waiters")
                if released == baseline:
                    break
                await asyncio.sleep(self.poll_interval)
            return {
                "outcome": "CANCELLED" if cancelled else "COMPLETED",
                "cancellation_scope": "sync-waiter",
                "waiter_counts": [baseline, active, released],
            }
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _function_spec(self):
        spec = deepcopy(self.config.get("function_spec"))
        if (
            not isinstance(spec, dict)
            or spec.get("image") != self.inputs["images"][self._role()]
        ):
            raise UnsupportedPreflightError(
                "churn function spec must use its frozen SDK image"
            )
        count = self.config.get("churn_count", 2)
        if type(count) is not int or not 2 <= count <= 100:
            raise UnsupportedPreflightError("churn_count must be between 2 and 100")
        return spec, count

    async def _churn(self):
        spec, count = self._function_spec()
        created, removed = [], []
        for index in range(count):
            token = hashlib.sha256(self.platform.lifetime_id.encode()).hexdigest()[:20]
            name = f"p24-{token}-{index}"
            spec["name"] = name
            status, _, _ = await self._json("POST", "/v1/functions", json=spec)
            if status not in {200, 201, 202}:
                raise UnsupportedPreflightError(
                    f"churn registration failed: HTTP {status}"
                )
            path = "/v1/functions/" + name
            status, _, body = await self._json("GET", path)
            if status != 200 or not isinstance(body, dict) or body.get("name") != name:
                raise UnsupportedPreflightError("created function identity unconfirmed")
            created.append(name)
            status, _, result = await self._invoke(name=name)
            if (
                status != 200
                or not isinstance(result, dict)
                or result.get("output") != self.config["expected_output"]
            ):
                raise UnsupportedPreflightError(
                    "churn function did not execute expected workload"
                )
            status, _, _ = await self._http("DELETE", path)
            if status not in {200, 202, 204}:
                raise UnsupportedPreflightError(f"churn deletion failed: HTTP {status}")
            status, _, _ = await self._http("GET", path)
            if status != 404:
                raise UnsupportedPreflightError("churn function removal unconfirmed")
            removed.append(name)
        return {"created_names": created, "removed_names": removed}

    async def exercise(self, coverage: str) -> dict[str, Any]:
        """Return raw observations; only the existing validator creates assertions."""
        if coverage != self.coverage:
            raise UnsupportedPreflightError(
                "exercise coverage differs from owned lifetime"
            )
        async with asyncio.timeout(self.exercise_timeout):
            observed: dict[str, Any]
            if coverage == "idempotent-replay":
                observed = await self._replay()
            elif coverage == "cancellation":
                observed = await self._cancel()
            elif coverage == "function-name-churn":
                observed = await self._churn()
            else:
                enqueue = coverage in {"async", "late-callback", "timeout"}
                status, headers, body = await self._invoke(enqueue=enqueue)
                if coverage == "sync":
                    observed = {
                        "http_status": status,
                        "output": body.get("output")
                        if isinstance(body, dict)
                        else None,
                    }
                else:
                    identity = self._execution_id(headers, body)
                    terminal = await self._terminal(identity)
                    if coverage == "async":
                        observed = {
                            "enqueue_status": status,
                            "execution_status": terminal["status"].upper(),
                            "output": terminal.get("output"),
                        }
                    elif coverage == "late-callback":
                        observed = await self._late_callback(identity, terminal)
                    else:
                        observed = {
                            "http_status": status,
                            "outcome": self._outcome(terminal),
                            "execution": terminal,
                            "response": body,
                        }
            observed["runtime_evidence"] = {
                "lifetime_id": self.platform.lifetime_id,
                "input_fingerprint": fingerprint(self.inputs),
                "events": deepcopy(self.events),
            }
            return observed
