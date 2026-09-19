"""Offline full-run receipt gates.

Producer contract (all JSON is nanolab-soak-v1):

* acceptance-manifest.json: run_id, policy_sha256, completed, aborted,
  config/preflight/workload/prerequisites/diagnostics/artifacts references,
  phases {warmup, baseline_drain, baseline, steady, drain: {start_s, end_s}},
  frozen_at_s, builds_finished_s, observation_started_s, observation_ended_s,
  traffic_stopped_s, final_targets, restarts (event objects), natural_drain.
* Reference: {path: run-relative regular file, sha256: file SHA256}; optional
  size_bytes is verified. Nested diagnostic artifacts are receipt-relative.
* config: the complete SoakConfig object. policy_sha256 is fingerprint of its
  normalized model_dump(mode="json"), not the file checksum.
* Native producer receipts are consumed without synthetic wrappers:
  preflight: the existing preflight() output, with configuration_fingerprint,
    configuration, targets, observations. Manifest preflight_started_s and
    preflight_ended_s bind its timing. Saved criterion PASS claims are ignored.
  workload: schema nanolab-soak-v1, kind workload, counters/per_function with
    {value, availability, source}; siblings workload-inputs.json,
    workload-config.json, soak-workload.js, k6-summary.json, generator-process.json.
    Manifest workload_inputs/workload_script independently bind frozen inputs.
    Admission is recomputed from control-plane function_admitted_total samples,
    source prometheus, unit count, labels function=<name>,path=sync.
  prerequisites: schema nanolab-soak-v1, kind prerequisites, actual
    run_prerequisites() output; manifest prerequisite_inputs binds frozen inputs.
    validate_receipt() replays the profile evidence; caller PASS is insufficient.
* Manifest source references emitted snapshot.json, builds maps role to emitted
  build receipt, recipes maps role to serialized BuildRecipe. All source entries
  and build log artifacts are verified offline.
* Orchestration receipts diagnostics/artifacts bind schema, run_id, policy_sha256:
  diagnostics: entries [{role, phase, operation, receipt}]. Referenced
    diagnostic.json uses the existing runtime adapter format and is checked
    against the target, declared operation/helper, the natural checkpoint of the
    phase the entry names and nested artifact checksums.
  artifacts: complete, budget_exhausted, entries (references). Inventory must
    include samples.jsonl, evaluation-input.json, config and other top receipts.

Optional attribution is a bound document with entries [{attribution, equal_work}].
Attribution uses validate_attribution's existing record. equal_work is a reference
to a bound receipt: criterion_id, metric, unit, target, windows (two objects with
  started_s/ended_s, offered/admitted, workload, admission_samples, post_gc and
  observation references). Workload and server samples independently establish
  equal per-function work; totals supplied by the reviewer are cross-checked. Each
  post_gc receipt is a verified diagnostic GC receipt; observation is a persisted
  diagnostic Sample taken after its completed GC. The owner
must have a frozen retention_s entry and a separate maximum criterion for the
same population supplies its frozen budget. Attribution never waives FAIL.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypeGuard

from nanolab.config.soak import Criterion, SoakConfig
from nanolab.tasks.soak.artifacts import (
    describe_artifact,
    fingerprint,
    measure_tree,
    read_records,
)
from nanolab.tasks.soak.diagnostics import validate_attribution
from nanolab.tasks.soak.images import build_key
from nanolab.tasks.soak.models import CriterionResult, Target
from nanolab.tasks.soak.preflight import preflight
from nanolab.tasks.soak.prerequisites import (
    normalize_prerequisite_inputs,
    select_relevant_config,
    validate_receipt,
)
from nanolab.tasks.soak.sources import SourceEntry, SourceSnapshot, verify_snapshot
from nanolab.tasks.soak.workload import allocate_vus, constant_arrival_options

SCHEMA = "nanolab-soak-v1"
# The checkpoints a capture may name, matching the runtime's own list. A
# diagnostic entry carries the checkpoint it was measured from, and both the
# coverage gate and the timing bound are read against it.
_CHECKPOINT_PHASES = ("baseline", "drain")
GATE_IDS = frozenset(
    {
        "frozen-policy",
        "effective-preflight",
        "workload-accounting",
        "workload-correctness",
        "prerequisite-coverage",
        "diagnostic-coverage",
        "artifact-integrity",
        "required-observations",
        "run-continuity",
        "attribution-policy-binding",
        "run-coverage",
        "source-artifact-provenance",
    }
)
_LIMIT = 1024 * 1024


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _finite(value: object) -> TypeGuard[float]:
    """Report whether this is a finite numeric measurement.

    `type(x) is T` rather than isinstance, because bool is an int subclass and
    a boolean is never a measurement. A TypeGuard so that the callers, which
    all go on to compare or convert the value, are narrowed by the check they
    already perform.
    """
    if type(value) is not int and type(value) is not float:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _count(value: object) -> TypeGuard[int]:
    """Report whether this is a non-negative integer count."""
    return type(value) is int and value >= 0


def normalize_phase_windows(windows: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize the lifecycle's explicit baseline-drain phase spelling.

    Window payloads retain start_s/end_s. Supplying both spellings is ambiguous
    and rejected even if their values happen to be identical.
    """
    _require(isinstance(windows, dict), "phase windows must be an object")
    result = {}
    for phase, window in windows.items():
        name = "baseline_drain" if phase == "baseline-drain" else phase
        _require(name not in result, "duplicate phase alias: " + name)
        result[name] = window
    return result


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("nonfinite JSON value: " + value)


def _path(root: Path, name: str) -> Path:
    _require(isinstance(name, str), "artifact path must be a string")
    relative = Path(name)
    _require(
        not root.is_symlink()
        and not relative.is_absolute()
        and bool(relative.parts)
        and ".." not in relative.parts,
        "artifact path must remain inside run",
    )
    current = root
    for part in relative.parts:
        current /= part
        _require(not current.is_symlink(), "symlink evidence is unsupported")
    _require(current.is_file(), "missing regular artifact: " + name)
    return current


def _json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        body = stream.read(_LIMIT + 1)
    _require(len(body) <= _LIMIT, "receipt exceeds JSON size limit")
    value = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    _require(isinstance(value, dict), "receipt must be a JSON object")
    return value


def _reference(root: Path, record: dict[str, Any], limit: int) -> Path:
    _require(isinstance(record, dict), "missing artifact reference")
    name = Path(record["path"])
    if name.is_absolute():
        name = name.relative_to(root.absolute())
    path = _path(root, str(name))
    _require(path.stat().st_size <= limit, "artifact exceeds frozen byte budget")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            _require(size <= limit, "artifact grew beyond frozen byte budget")
            digest.update(chunk)
    _require(record.get("sha256") == digest.hexdigest(), "artifact checksum mismatch")
    if "size_bytes" in record:
        _require(
            _count(record["size_bytes"]) and record["size_bytes"] == size,
            "artifact size mismatch",
        )
    return path


def verify_containerd_builds(
    root: Path,
    manifest: dict[str, Any],
    config: SoakConfig,
    targets: dict[str, dict[str, Any]],
    source: SourceSnapshot,
    observed: dict[str, Any],
) -> str:
    """Bind staged source, process binary and running OCI tasks to receipts."""

    def repository(reference: str) -> str:
        name = reference.split("@", 1)[0]
        return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name

    remote = _json(
        _reference(root, manifest["remote_source"], config.artifact_limit_bytes)
    )
    batches = [
        hashlib.sha256(
            json.dumps(
                [asdict(entry) for entry in source.entries[offset : offset + 50]],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for offset in range(0, len(source.entries), 50)
    ]
    _require(
        remote == observed.get("remote_source")
        and remote.get("schema") == "nanolab-containerd-source-v1"
        and remote.get("revision") == source.revision
        and remote.get("source_fingerprint") == source.fingerprint
        and remote.get("clean") is True
        and source.dirty is False
        and remote.get("entry_count") == len(source.entries)
        and remote.get("batch_size") == 50
        and remote.get("batches") == batches
        and remote.get("verification")
        == "remote-content-after-build; rsync excludes .git",
        "staged source differs from frozen local checkout",
    )
    for role, spec in config.images.items():
        build = _json(
            _reference(root, manifest["builds"][role], config.artifact_limit_bytes)
        )
        recipe = _json(
            _reference(root, manifest["recipes"][role], config.artifact_limit_bytes)
        )
        digest = targets[role]["image_digest"]
        if spec.artifact_kind == "process":
            _require(
                role == "control-plane"
                and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
                "process artifact digest must be a SHA256 of the running file",
            )
        _require(
            build.get("schema") == "nanolab-containerd-build-v1"
            and build.get("role") == role
            and build.get("image_digest") == digest
            and build.get("source_fingerprint") == source.fingerprint
            and build.get("source_revision") == source.revision
            and build.get("platform") == spec.platform
            and build.get("platform") == observed["roles"][role].get("platform")
            and build.get("artifact_kind") == spec.artifact_kind
            and build.get("artifact_path")
            == observed["roles"][role].get("artifact_path")
            and all(
                observed["build_receipts"][role].get(key) == value
                for key, value in build.items()
                if key != "schema"
            ),
            "running artifact/source/build receipt differs",
        )
        command = build.get("build_argv")
        steps = build.get("build_steps")
        executions = build.get("build_results")
        if not isinstance(command, list) or not isinstance(steps, list):
            raise ValueError("containerd build command differs from frozen recipe")
        _require(
            isinstance(command, list)
            and bool(command)
            and all(isinstance(arg, str) and arg for arg in command)
            and isinstance(steps, list)
            and bool(steps)
            and all(
                isinstance(step, list)
                and bool(step)
                and all(isinstance(arg, str) and arg for arg in step)
                for step in steps
            )
            and steps[-1] == command
            and recipe
            == {
                "role": role,
                "artifact_kind": spec.artifact_kind,
                "platform": spec.platform,
                "build_argv": command,
                "build_steps": steps,
                "mode": spec.mode,
                "variant": spec.variant,
                "modules": spec.modules,
                "build_options": spec.build_options,
            },
            "containerd build command differs from frozen recipe",
        )
        _require(
            all(
                build.get(key) == recipe[key]
                for key in (
                    "build_steps",
                    "mode",
                    "variant",
                    "modules",
                    "build_options",
                )
            )
            and len(steps)
            == (2 if spec.variant == "jvm" and role != "control-plane" else 1),
            "containerd build steps or requested image recipe differ",
        )
        expected_titles = (
            [f"Build application artifact: {role}", f"Build image {role}"]
            if role != "control-plane" and spec.variant == "jvm"
            else [
                "Build control plane"
                if role == "control-plane"
                else f"Build image {role}"
            ]
        )
        _require(
            isinstance(executions, list)
            and executions
            == [
                {"title": title, "argv": step, "status": "passed", "return_code": 0}
                for title, step in zip(expected_titles, steps, strict=True)
            ],
            "containerd build task results differ from executed steps",
        )
        if role != "control-plane" and spec.variant == "jvm":
            _require(
                len(steps[0]) >= 2
                and steps[0][0] == "./gradlew"
                and steps[0][1].startswith(":functions:java:")
                and steps[0][1].endswith(":bootJar"),
                "Java application artifact build is missing",
            )
        if spec.artifact_kind == "process":
            _require(
                isinstance(build.get("artifact_path"), str)
                and Path(build["artifact_path"]).is_absolute()
                and "-PcontrolPlaneModules=" + ",".join(spec.modules) in command,
                "process artifact path/modules differ from policy",
            )
        else:
            _require(
                re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", digest) is not None,
                "function OCI digest missing",
            )
            tag_index = command.index("-t") if "-t" in command else -1
            _require(
                role != "control-plane"
                and tag_index >= 0
                and tag_index + 1 < len(command)
                and repository(command[tag_index + 1]) == repository(digest),
                "function build tag differs from running image repository",
            )
    return "staged source, systemd process artifact and running OCI functions verified"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _counter(counter: Any, metric: str, metrics: dict[str, Any]) -> int:
    _require(
        isinstance(counter, dict)
        and counter.get("availability") == "observed"
        and _count(counter.get("value"))
        and counter.get("source") == metric,
        "required counter unavailable or source differs: " + metric,
    )
    observed = metrics.get(metric, {}).get("values", {}).get("count")
    _require(
        _finite(observed)
        and observed >= 0
        and int(observed) == observed
        and counter["value"] == observed,
        "counter differs from raw k6 summary: " + metric,
    )
    return counter["value"]


def _workload_evidence(
    root: Path,
    manifest: dict[str, Any],
    config: SoakConfig,
    targets: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, int]], dict[str, Any]]:
    """Verify producer artifacts and independently reconcile all client counters."""
    path = _reference(root, manifest["workload"], config.artifact_limit_bytes)
    item = _json(path)
    _require(
        item.get("schema") == SCHEMA and item.get("kind") == "workload",
        "unsupported workload producer schema/kind",
    )
    inputs = _json(
        _reference(root, manifest["workload_inputs"], config.artifact_limit_bytes)
    )
    frozen_script = _reference(
        root, manifest["workload_script"], config.artifact_limit_bytes
    )
    emitted = _json(_path(path.parent, "workload-inputs.json"))
    provenance = item["provenance"]
    _require(
        emitted == inputs and _sha(inputs) == provenance.get("config_sha256"),
        "workload input fingerprint mismatch",
    )
    images = {r: t["image_digest"] for r, t in targets.items()}
    _require(
        inputs.get("image_digests") == images == provenance.get("image_digests")
        and inputs.get("function_rates")
        == config.workload.rates
        == provenance.get("function_rates")
        and inputs.get("config") == config.model_dump(mode="json"),
        "workload full configuration/images/rates differ from frozen policy",
    )
    script = _path(path.parent, "soak-workload.js")
    _require(
        describe_artifact(script)["sha256"]
        == describe_artifact(frozen_script)["sha256"]
        == provenance.get("script_sha256"),
        "workload script identity mismatch",
    )
    payloads = inputs["payloads"]
    _require(
        set(payloads) == set(config.workload.rates)
        and {n: _sha(v) for n, v in payloads.items()}
        == provenance.get("payload_sha256"),
        "workload payload identity mismatch",
    )
    effective = _json(_path(path.parent, "workload-config.json"))
    _require(
        _sha(effective) == provenance.get("effective_config_sha256")
        and effective.get("base_url") == inputs.get("base_url"),
        "effective workload input differs",
    )
    functions = effective["functions"]
    _require(
        len(functions) == len(config.workload.rates)
        and {f["name"] for f in functions} == set(config.workload.rates)
        and len({f["scenario"] for f in functions}) == len(functions),
        "effective function/scenario coverage differs",
    )
    summary = _json(_path(path.parent, "k6-summary.json"))
    metrics = summary["metrics"]
    _require(
        set(item["per_function"]) == set(config.workload.rates),
        "per-function counters missing",
    )
    allocation = allocate_vus(
        config.workload.rates, config.workload.preallocated_vus, config.workload.max_vus
    )
    _require(
        item.get("vu_policy")
        == {
            "scope": "global",
            "preallocated_vus": config.workload.preallocated_vus,
            "max_vus": config.workload.max_vus,
            "allocation": allocation,
        },
        "producer VU policy differs from frozen global allocation",
    )
    values: dict[str, dict[str, int]] = {}
    for function in functions:
        name, options = function["name"], function["options"]
        expected_options = constant_arrival_options(
            config.workload.rates[name],
            config.phases.steady_s,
            allocation[name]["preAllocatedVUs"],
            allocation[name]["maxVUs"],
        )
        _require(
            function["payloads"] == payloads[name]
            and all(
                options.get(key) == value for key, value in expected_options.items()
            ),
            "effective constant workload differs from frozen policy",
        )
        row = {}
        for key in ("offered", "success", "error", "retry", "replay", "dropped"):
            metric = "dropped_iterations" if key == "dropped" else "soak_" + key
            row[key] = _counter(
                item["per_function"][name].get(key),
                metric + "{scenario:" + function["scenario"] + "}",
                metrics,
            )
        _require(
            row["offered"] == row["success"] + row["error"],
            "per-function offered != success + error: " + name,
        )
        values[name] = row
    _require(
        sum(f["options"]["preAllocatedVUs"] for f in functions)
        == config.workload.preallocated_vus
        and sum(f["options"]["maxVUs"] for f in functions) == config.workload.max_vus,
        "per-function VU allocation exceeds or differs from global frozen budget",
    )
    aggregate = {}
    for key in ("offered", "success", "error", "retry", "replay", "dropped"):
        metric = "dropped_iterations" if key == "dropped" else "soak_" + key
        aggregate[key] = _counter(item["counters"].get(key), metric, metrics)
        _require(
            aggregate[key] == sum(row[key] for row in values.values()),
            "aggregate counter differs from per-function sum: " + key,
        )
    _require(
        aggregate["offered"] == aggregate["success"] + aggregate["error"],
        "aggregate offered != success + error",
    )
    return item, values, summary


def _admitted(
    root: Path,
    manifest: dict[str, Any],
    config: SoakConfig,
    targets: dict[str, dict[str, Any]],
    sample_ref: dict[str, Any] | None = None,
) -> tuple[dict[str, int], dict[str, float]]:
    """Return admitted counts per function, and how much of the window they miss.

    Only control-plane admission counters establish admitted work. The second
    mapping is the span of the phase lying outside the two boundary readings
    the delta was taken between, which the caller needs to reconcile it.
    """
    low: float = manifest["phases"]["steady"]["start_s"]
    high: float = manifest["phases"]["steady"]["end_s"]
    histories: dict[str, tuple[float, float, float, float, int] | None] = dict.fromkeys(
        config.workload.rates
    )
    stream = (
        _path(root, "samples.jsonl")
        if sample_ref is None
        else _reference(root, sample_ref, config.artifact_limit_bytes)
    )
    for row in read_records(stream):
        if (
            row.get("metric") != "function_admitted_total"
            or row.get("phase") != "steady"
        ):
            continue
        labels = dict(row["labels"])
        name = labels.get("function")
        if name not in histories or labels.get("path") != "sync":
            continue
        value = row.get("value")
        _require(
            row.get("schema") == SCHEMA
            and labels == {"function": name, "path": "sync"}
            and row.get("target") == targets["control-plane"]
            and row.get("source") == "prometheus"
            and row.get("unit") == "count"
            and row.get("availability") == "observed"
            and _finite(value)
            and value >= 0
            and int(value) == value,
            "admission observations lack authoritative process/source identity",
        )
        stamp = row["scheduled_s"]
        _require(
            _finite(stamp)
            and _finite(row.get("started_s"))
            and _finite(row.get("ended_s"))
            and low <= stamp <= row["started_s"] <= row["ended_s"] <= high,
            "invalid admission counter timing",
        )
        history = histories[name]
        if history:
            _require(
                history[2] < stamp
                and stamp - history[2] <= config.max_observation_gap_s
                and history[3] <= row["value"],
                "admission counter reset/duplicate/gap",
            )
            histories[name] = (
                history[0],
                history[1],
                stamp,
                row["value"],
                history[4] + 1,
            )
        else:
            histories[name] = (stamp, row["value"], stamp, row["value"], 1)
    result: dict[str, int] = {}
    uncounted: dict[str, float] = {}
    for name, history in histories.items():
        _require(
            history is not None
            and history[4] >= 2
            and brackets_window(
                first=history[0],
                last=history[2],
                low=low,
                high=high,
                slack=config.sample_interval_s,
            ),
            "authoritative admission boundary observations unavailable: " + name,
        )
        assert history is not None  # nosec B101 - validated invariant/type narrowing
        result[name] = int(history[3] - history[1])
        # The counter is read at ticks inside the window, so this much of it
        # lies outside the two readings the delta was taken between.
        uncounted[name] = (history[0] - low) + (high - history[2])
    return result, uncounted


def admission_is_consistent(
    *, admitted: int, success: int, offered: int, rate: float, uncounted_s: float
) -> bool:
    """Reconcile the server's admission count against the work offered.

    The counter is sampled at ticks inside the phase, never at its edges, so
    the delta cannot include requests admitted in the slivers before the first
    reading and after the last. Allow exactly that much at the declared rate
    and no more: with the readings tight against the edges nothing is
    forgiven, and a real shortfall of admitted work still fails.
    """
    edge = math.ceil(rate * uncounted_s)
    return offered > 0 and success - edge <= admitted <= offered


def _released_workload(
    root: Path, manifest: dict[str, Any], config: SoakConfig, item: dict[str, Any]
) -> None:
    _require(
        item.get("completed") is True
        and type(item.get("exit_code")) is int
        and item["exit_code"] in (0, 99)
        and all(
            item.get(key) is False for key in ("cancelled", "forced_stop", "timed_out")
        )
        and item.get("errors") == [],
        "generator incomplete, interrupted or failed",
    )
    workload_root = _reference(
        root, manifest["workload"], config.artifact_limit_bytes
    ).parent
    state = _json(_path(workload_root, "generator-process.json"))
    _require(
        state.get("schema") == SCHEMA
        and state.get("kind") == "owned-process"
        and state.get("reaped") is True
        and state.get("forced_stop") is False
        and state.get("cancelled") is False
        and state.get("timed_out") is False
        and state.get("quota_exceeded") is False
        and state.get("summary_complete") is True
        and state.get("returncode") == item["exit_code"]
        and state.get("ended_s") == item["generator_end_s"]
        and state.get("errors") == []
        and item.get("cleanup_complete") is True
        and item.get("quota_exceeded") is False,
        "owned generator process release/quota unverified",
    )
    for key, name in (("log_bytes", "k6.log"), ("summary_bytes", "k6-summary.json")):
        _require(
            _count(state.get(key))
            and state[key]
            == item.get(key)
            == _path(workload_root, name).stat().st_size,
            "owned output byte accounting differs from artifacts",
        )
    _require(
        _count(item.get("artifact_limit_bytes"))
        and 0 < item["artifact_limit_bytes"] <= config.artifact_limit_bytes
        and _count(item.get("output_limit_bytes"))
        and state["log_bytes"] + state["summary_bytes"]
        <= item["output_limit_bytes"]
        < item["artifact_limit_bytes"],
        "generator output quota differs from frozen artifact budget",
    )


def _bound(document: dict[str, Any], manifest: dict[str, Any]) -> None:
    _require(
        document.get("schema") == SCHEMA
        and document.get("run_id") == manifest["run_id"]
        and document.get("policy_sha256") == manifest["policy_sha256"],
        "receipt schema/run/frozen-policy binding mismatch",
    )


class _OfflineSink:
    """Consume recomputed preflight serialization without changing saved evidence."""

    def write_json(self, name: str, value: dict[str, Any]) -> None:
        pass


def _diagnostic(
    root: Path,
    ref: dict[str, Any],
    config: SoakConfig,
    target: dict[str, Any],
    operation: str,
    phase: str,
    low: float,
    high: float | None = None,
) -> dict[str, Any]:
    path = _reference(root, ref, config.artifact_limit_bytes)
    item = _json(path)
    _require(
        item.get("schema") == SCHEMA and item.get("target") == target,
        "diagnostic target/schema mismatch",
    )
    _require(
        item.get("operation") == operation
        and item.get("status") == "PASS"
        and item.get("availability") == "observed"
        and item.get("command_completed") is True
        and type(item.get("exit_code")) is int
        and item["exit_code"] == 0,
        "diagnostic completion unavailable",
    )
    started, ended = item.get("started_s"), item.get("ended_s")
    _require(
        _finite(started)
        and _finite(ended)
        and low <= started <= ended
        and ended - started <= config.diagnostics.timeout_s
        and (high is None or ended <= high),
        "invalid diagnostic timing",
    )
    role = target["role"]
    helper = config.diagnostics.helper_images.get(role)
    executable = config.diagnostics.executables.get(role)
    _require(
        helper is not None or executable is not None,
        "diagnostic helper/executable identity not frozen",
    )
    if helper is not None:
        _require(item.get("helper_digest") == helper, "diagnostic helper changed")
    if executable is not None:
        argv = item.get("command", {}).get("argv", [])
        _require(argv[: len(executable)] == executable, "diagnostic executable changed")
    natural_path = _reference(
        root, item["natural_checkpoint"], config.artifact_limit_bytes
    )
    natural = _json(natural_path)
    _require(
        natural.get("schema") == SCHEMA
        and natural.get("kind") == "natural_checkpoint"
        and natural.get("target") == target
        # The checkpoint this capture was measured from, not a fixed one: a
        # reading at the baseline window and one at drain are different
        # readings, and each must be bound to the window it came from.
        and natural.get("phase") == phase
        and natural.get("completed") is True
        and _finite(natural.get("ended_s"))
        and natural["ended_s"] <= started,
        "natural checkpoint binding/completion missing",
    )
    _require(
        isinstance(natural.get("artifacts"), list) and bool(natural["artifacts"]),
        "natural checkpoint evidence missing",
    )
    for ref in natural["artifacts"]:
        _reference(natural_path.parent, ref, config.artifact_limit_bytes)
    _reference(root, item["capability_artifact"], config.artifact_limit_bytes)
    perturbation = item["perturbation"]
    _require(
        perturbation.get("exclude_from_natural_windows") is True
        and _finite(perturbation.get("started_s"))
        and _finite(perturbation.get("ended_s"))
        and started <= perturbation["started_s"] <= perturbation["ended_s"] <= ended,
        "diagnostic perturbation timing/completion missing",
    )
    artifacts = item.get("artifacts")
    _require(
        isinstance(artifacts, list) and 1 <= len(artifacts) <= 32,
        "diagnostic artifacts missing",
    )
    assert isinstance(artifacts, list)  # nosec B101 - validated invariant/type narrowing
    paths = [
        _reference(path.parent, ref, config.artifact_limit_bytes) for ref in artifacts
    ]
    if operation == "gc":
        _require(
            item.get("full_gc_verified") is True
            and _count(item.get("before_count"))
            and _count(item.get("after_count"))
            and item["after_count"] > item["before_count"],
            "post-GC checkpoint lacks observed collection",
        )
        events = [_json(p) for p in paths if p.suffix == ".json"]
        matching = [
            event
            for event in events
            if event.get("kind") == "full_gc_completed"
            and event.get("schema") == SCHEMA
            and event.get("target") == target
            and event.get("request_id") == item.get("request_id")
            and event.get("source")
            == config.diagnostics.gc_completion_evidence.get(role)
            and _finite(event.get("started_s"))
            and _finite(event.get("ended_s"))
            and started <= event["started_s"] <= event["ended_s"] <= ended
        ]
        _require(len(matching) == 1, "matching full GC event missing or ambiguous")
        item["_gc_ended_s"] = matching[0]["ended_s"]
    return item


def exemption_is_allowed(receipt: dict[str, Any], config: SoakConfig) -> bool:
    """Decide whether an exemption may stand in for prerequisite profiles.

    Only when the frozen policy asked for none and declared itself a smoke.
    A policy that requires coverage is never satisfied by the receipt shape
    a run with no requirements emits.
    """
    return (
        not config.prerequisites.required_coverage
        and config.purpose == "smoke"
        and receipt.get("coverage") == []
        and receipt.get("purpose") == config.purpose
        and receipt.get("p24_qualified") is False
    )


def phases_are_contiguous(
    *, start: float, previous: float, sample_interval_s: float
) -> bool:
    """Report whether the next phase began without losing an observation.

    Each boundary is its own clock read, so consecutive phases are never
    bit-identical; requiring that could not pass a real run. Bounding the gap
    below one sampling interval is the property that matters: no scheduled
    sample can fall into time that belongs to no phase.
    """
    return 0 <= start - previous < sample_interval_s


def brackets_window(
    *, first: float, last: float, low: float, high: float, slack: float
) -> bool:
    """Report whether the boundary observations account for the whole window.

    The observer's ticks run on their own origin, so none of them lands on a
    phase boundary; requiring one could never pass. A tick does fall in every
    interval, so both ends sit inside the window and strictly less than one
    interval from its edges. Strictly: a missing observation puts an end
    exactly one interval away, which this must still refuse.
    """
    return low <= first < low + slack and high - slack < last <= high


def required_sample_count(duration_s: float, sample_interval_s: float) -> int:
    """Return the fewest samples a correctly sampled window must contain.

    Sampling ticks are not aligned to phase boundaries, so a window of length D
    sampled every I holds floor(D/I) or one more depending on where it starts.
    Only the floor is guaranteed; requiring the ceiling failed runs that had
    missed nothing. Gap checks, not this count, are what catch real gaps.
    """
    return max(2, math.floor(duration_s / sample_interval_s))


def evaluate_acceptance(
    root: Path,
    projection: dict[str, Any] | None,
    numerical: tuple[CriterionResult, ...],
    attribution_path: Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[CriterionResult, ...]:
    """Return receipt gates and explicitly resolved growth-review criteria.

    Missing or malformed evidence produces INCONCLUSIVE. Independent observed
    correctness failures and restart events survive other missing evidence.
    No network, runtime command, or modification of source receipts is performed.
    """
    results: list[CriterionResult] = []
    try:
        manifest = _json(_path(root, "acceptance-manifest.json"))
        _require(manifest.get("schema") == SCHEMA, "unsupported acceptance schema")
        _require(
            isinstance(manifest.get("run_id"), str) and bool(manifest["run_id"]),
            "run identity missing",
        )
        _require(
            isinstance(manifest.get("policy_sha256"), str)
            and len(manifest["policy_sha256"]) == 64,
            "frozen policy hash missing",
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        return (
            CriterionResult(
                "run-coverage",
                "INCONCLUSIVE",
                str(error)[:2048],
                ("acceptance-manifest.json",),
            ),
        )

    def gate(name: str, check: Any) -> None:
        try:
            result = check()
            if isinstance(result, CriterionResult):
                results.append(replace(result, criterion_id=name))
            else:
                results.append(
                    CriterionResult(
                        name, "PASS", str(result), ("acceptance-manifest.json",)
                    )
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            sqlite3.Error,
        ) as error:
            results.append(
                CriterionResult(
                    name,
                    "INCONCLUSIVE",
                    str(error)[:2048],
                    ("acceptance-manifest.json",),
                )
            )

    config: SoakConfig | None = None
    normalized: dict[str, Any] = {}
    targets: dict[str, dict[str, Any]] = {}

    def receipt(name: str) -> dict[str, Any]:
        limit = config.artifact_limit_bytes if config else _LIMIT
        document = _json(_reference(root, manifest[name], limit))
        if name == "preflight":
            _require(
                document.get("schema") == SCHEMA
                and document.get("scope") == "preflight-only",
                "unsupported preflight receipt",
            )
        elif name in {"workload", "prerequisites"}:
            # A run that requires no prerequisites emits an explicit exemption
            # instead of an empty profile set. The gate below still refuses it
            # unless the frozen policy actually declares no coverage.
            kinds = (
                {name, "prerequisite-exemption"} if name == "prerequisites" else {name}
            )
            _require(
                document.get("schema") == SCHEMA and document.get("kind") in kinds,
                "unsupported native producer schema/kind",
            )
        else:
            _bound(document, manifest)
        return document

    def policy() -> str:
        nonlocal config, normalized, targets
        raw = _json(_reference(root, manifest["config"], _LIMIT))
        config = SoakConfig.model_validate(raw)
        normalized = config.model_dump(mode="json")
        _require(
            fingerprint(normalized) == manifest["policy_sha256"],
            "frozen policy hash mismatch",
        )
        _require(projection is not None, "numerical projection unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert projection is not None  # nosec B101 - validated invariant/type narrowing
        _require(
            projection["purpose"] == config.purpose
            and projection["sample_interval_s"] == config.sample_interval_s
            and projection["max_observation_gap_s"] == config.max_observation_gap_s,
            "numerical projection changes frozen policy",
        )
        _require(
            [
                Criterion.model_validate(c).model_dump(mode="json")
                for c in projection["criteria"]
            ]
            == normalized["criteria"],
            "numerical criteria differ from frozen policy",
        )
        _require(
            not ({c.id for c in config.criteria} & GATE_IDS),
            "criterion ID collides with acceptance gate",
        )
        targets = {target["role"]: target for target in projection["targets"]}
        _require(set(targets) == set(config.roles), "projection omits configured roles")
        for role, target in targets.items():
            _require(
                target["runtime"] == config.roles[role].runtime,
                "target runtime differs from policy",
            )
        return "complete SoakConfig and numerical projection match frozen fingerprint"

    gate("frozen-policy", policy)

    def provenance() -> str:
        _require(config is not None and targets, "frozen source policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        snapshot_path = _reference(
            root, manifest["source"], config.artifact_limit_bytes
        )
        snapshot = _json(snapshot_path)
        _require(
            snapshot.get("schema") == SCHEMA
            and snapshot.get("status") == "captured"
            and type(snapshot.get("dirty")) is bool,
            "unsupported source snapshot",
        )
        tree = Path(snapshot["root"])
        tree.relative_to(root.absolute())
        _require(not tree.is_symlink(), "symlink source tree unsupported")
        source_manifest = _path(snapshot_path.parent, "source-manifest.jsonl")
        entries = []
        for entry in read_records(source_manifest):
            _require(len(entries) < 100000, "source manifest entry budget exceeded")
            entries.append(SourceEntry(**entry))
        _require(len(entries) == snapshot["entry_count"], "source entry count mismatch")
        source = SourceSnapshot(
            tree,
            snapshot["fingerprint"],
            snapshot["revision"],
            snapshot["dirty"],
            tuple(entries),
            source_manifest,
            snapshot["manifest_sha256"],
        )
        verify_snapshot(source)
        _require(
            set(manifest["builds"]) == set(config.roles) == set(manifest["recipes"]),
            "source/build/recipe role coverage differs",
        )
        observed = receipt("preflight")["observations"]
        _require(
            observed.get("snapshot_fingerprint") == source.fingerprint,
            "preflight source snapshot differs",
        )
        if manifest.get("backend") == "containerd":
            return verify_containerd_builds(
                root, manifest, config, targets, source, observed
            )
        for role, spec in config.images.items():
            build = _json(
                _reference(root, manifest["builds"][role], config.artifact_limit_bytes)
            )
            recipe = _json(
                _reference(root, manifest["recipes"][role], config.artifact_limit_bytes)
            )
            _require(
                build.get("schema") == SCHEMA
                and build.get("role") == role
                and build.get("image_digest") == targets[role]["image_digest"]
                and build.get("source_fingerprint") == source.fingerprint
                and build.get("platform") == spec.platform,
                "build identity/source/platform differs",
            )
            # Build observation rewrites the recipe before building it, so
            # build.recipe_fingerprint identifies the instrumented recipe. The
            # receipt binds it back to the requested one; only an uninstrumented
            # build has the two the same.
            requested = build.get("original_recipe_fingerprint") or build.get(
                "recipe_fingerprint"
            )
            _require(
                recipe.get("role") == role
                and recipe.get("mode") == spec.mode
                and recipe.get("variant") == spec.variant
                and recipe.get("platform") == spec.platform
                and recipe.get("recipe_fingerprint") == requested,
                "requested image recipe differs from built recipe",
            )
            _require(
                build.get("build_fingerprint")
                == build_key(
                    source.fingerprint, build["recipe_fingerprint"], spec.platform
                ),
                "build key differs from frozen source/recipe/platform",
            )
            if spec.mode == "prebuilt":
                _require(
                    recipe.get("image") == spec.digest == build["image_digest"]
                    and recipe["recipe_fingerprint"]
                    == fingerprint(spec.model_dump(mode="json")),
                    "prebuilt image/recipe provenance differs",
                )
            else:
                rendered = recipe["bake"]["target"]
                _require(
                    isinstance(rendered, dict) and len(rendered) == 1,
                    "ambiguous recipe build target",
                )
                definition = next(iter(rendered.values()))
                identity = {
                    "role": role,
                    "variant": spec.variant,
                    "platform": spec.platform,
                    "prerequisite": recipe["prerequisite_argv"],
                    "build": {k: v for k, v in definition.items() if k != "tags"},
                }
                _require(
                    fingerprint(identity) == recipe["recipe_fingerprint"],
                    "recipe fingerprint mismatch",
                )
                _require(
                    definition.get("platforms") == [spec.platform]
                    and all(
                        definition.get("args", {}).get(k) == v
                        for k, v in spec.build_options.items()
                    ),
                    "effective recipe platform/options differ",
                )
                if role == "control-plane":
                    arguments = (
                        " ".join(recipe.get("prerequisite_argv") or [])
                        + " "
                        + definition.get("args", {}).get("GRADLE_ARGS", "")
                    )
                    _require(
                        "-PcontrolPlaneModules=" + (",".join(spec.modules) or "none")
                        in arguments,
                        "effective recipe modules differ",
                    )
            toolchains, bases, logs = (
                build.get("toolchains"),
                build.get("base_images"),
                build.get("logs"),
            )
            _require(
                isinstance(toolchains, list)
                and toolchains
                and all(
                    len(p) == 2 and all(isinstance(v, str) and v for v in p)
                    for p in toolchains
                ),
                "actual build toolchains missing",
            )
            _require(
                isinstance(bases, list)
                and bases
                and all(
                    len(p) == 2 and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", p[1])
                    for p in bases
                ),
                "actual base image digests missing",
            )
            _require(
                isinstance(logs, list) and logs,
                "build logs/attestation evidence missing",
            )
            assert isinstance(logs, list)  # nosec B101 - validated invariant/type narrowing
            for ref in logs:
                _reference(root, ref, config.artifact_limit_bytes)
            actual = observed["build_receipts"][role]
            _require(
                all(actual.get(k) == v for k, v in build.items() if k != "schema"),
                "preflight build receipt differs from artifact",
            )
        return (
            "source snapshot, requested recipes, build outputs, "
            "toolchains, bases and logs verified"
        )

    gate("source-artifact-provenance", provenance)

    def continuity() -> CriterionResult | str:
        _require(type(manifest.get("aborted")) is bool, "abort state unavailable")
        restarts = manifest.get("restarts")
        _require(isinstance(restarts, list), "restart evidence unavailable")
        if restarts or (
            targets
            and manifest.get("final_targets") is not None
            and manifest["final_targets"] != list(targets.values())
        ):
            return CriterionResult(
                "", "FAIL", "observed process restart or final identity change", ()
            )
        _require(
            manifest.get("final_targets") == list(targets.values()) and bool(targets),
            "final process identities unavailable",
        )
        _require(
            config is not None and projection is not None, "frozen protocol unavailable"
        )
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        assert projection is not None  # nosec B101 - validated invariant/type narrowing
        phases = normalize_phase_windows(manifest["phases"])
        manifest["phases"] = phases
        previous = None
        # The natural phases run for their declared duration; the checkpoint in
        # between runs for as long as its captures take, which the diagnostic
        # timeout bounds. Holding it to a sample interval instead would make the
        # gate pass or fail on how long a capture happened to take.
        natural_slack = config.cancellation_timeout_s + config.max_observation_gap_s
        for phase, duration, slack in (
            ("warmup", config.phases.warmup_s, natural_slack),
            ("baseline_drain", config.phases.baseline_drain_s, natural_slack),
            ("baseline", config.phases.baseline_window_s, natural_slack),
            (
                "baseline_diagnostics",
                0,
                config.diagnostics.timeout_s,
            ),
            ("steady", config.phases.steady_s, natural_slack),
            ("drain", config.phases.drain_s, natural_slack),
        ):
            start, end = phases[phase]["start_s"], phases[phase]["end_s"]
            _require(
                _finite(start)
                and _finite(end)
                and duration <= end - start <= duration + slack
                and (
                    previous is None
                    or phases_are_contiguous(
                        start=start,
                        previous=previous,
                        sample_interval_s=config.sample_interval_s,
                    )
                ),
                "phase timing differs from frozen schedule",
            )
            previous = end
        _require(
            {p: phases[p] for p in ("baseline", "steady", "drain")}
            == projection["windows"],
            "projection natural windows differ from actual phases",
        )
        for key in (
            "frozen_at_s",
            "builds_finished_s",
            "observation_started_s",
            "observation_ended_s",
            "traffic_stopped_s",
        ):
            _require(_finite(manifest.get(key)), "missing finite timing: " + key)
        # Each of these is its own clock read, so they are compared in order
        # with a one-interval tolerance rather than exactly: a strict or exact
        # comparison between two reads of the same instant cannot hold, and
        # one sampling interval is still too small to hide a lost observation.
        slack = config.sample_interval_s
        _require(
            # The image set is frozen once the builds that produced it finish.
            manifest["builds_finished_s"] <= manifest["frozen_at_s"] + slack
            and manifest["frozen_at_s"] <= phases["warmup"]["start_s"] + slack
            and manifest["observation_started_s"] <= phases["warmup"]["start_s"] + slack
            and manifest["observation_ended_s"] >= phases["drain"]["end_s"] - slack
            and abs(manifest["traffic_stopped_s"] - phases["steady"]["end_s"]) <= slack
            and manifest.get("natural_drain") is True,
            "freeze/build/observer/natural-drain timing invalid",
        )
        _require(manifest.get("completed") is True, "protocol did not complete")
        return "continuous frozen schedule and final process identities verified"

    gate("run-continuity", continuity)

    def effective() -> str:
        _require(config is not None and targets, "frozen policy/targets unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        item = receipt("preflight")
        _require(
            item.get("targets") == list(targets.values()),
            "preflight process identity mismatch",
        )
        _require(
            item.get("configuration_fingerprint") == manifest["policy_sha256"]
            and item.get("configuration") == normalized,
            "preflight configuration differs from frozen policy",
        )
        _require(
            _finite(manifest.get("preflight_started_s"))
            and _finite(manifest.get("preflight_ended_s"))
            and manifest["frozen_at_s"]
            <= manifest["preflight_started_s"]
            <= manifest["preflight_ended_s"]
            <= manifest["phases"]["warmup"]["start_s"],
            "preflight did not precede warmup",
        )
        checks = preflight(
            config,
            tuple(Target(**t) for t in targets.values()),
            item["observations"],
            _OfflineSink(),
        )
        missing = [
            c.criterion_id + ": " + c.reason for c in checks if c.status != "PASS"
        ]
        _require(not missing, "; ".join(missing))
        return (
            "effective limits, runtime, images, retention, "
            "capabilities and generator verified"
        )

    gate("effective-preflight", effective)

    def correctness() -> CriterionResult | str:
        _require(
            config is not None and targets, "frozen correctness inputs unavailable"
        )
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        _item, functions, summary = _workload_evidence(root, manifest, config, targets)
        # The known frozen generator counts success only after HTTP/body checks.
        # Independently require the raw named check observations, never infer an
        # actual wrong result from an undifferentiated transport error count.
        checks = summary.get("root_group", {}).get("checks")
        _require(isinstance(checks, list), "raw k6 correctness checks unavailable")
        selected = [
            c for c in checks if c.get("name") == "has expected success response"
        ]
        _require(
            len(selected) == 1
            and _count(selected[0].get("passes"))
            and _count(selected[0].get("fails")),
            "expected-response checks missing or ambiguous",
        )
        if selected[0]["fails"] > 0:
            return CriterionResult(
                "",
                "FAIL",
                "observed HTTP/success-output correctness check failure",
                (manifest["workload"]["path"],),
            )
        successes = sum(row["success"] for row in functions.values())
        _require(
            successes > 0 and selected[0]["passes"] == successes,
            "success responses lack complete raw correctness checks",
        )
        return "saved expected-output checks cover every successful response"

    gate("workload-correctness", correctness)

    def workload() -> str:
        _require(config is not None, "workload policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        item, functions, _summary = _workload_evidence(root, manifest, config, targets)
        steady = manifest["phases"]["steady"]
        # The generator records its own start/end; the lifecycle records the
        # phase boundaries. They bracket the same interval but are separate
        # clock reads, so they agree to within a sampling interval, not exactly.
        # The declared duration stays an exact match.
        slack = config.sample_interval_s
        _require(
            _finite(item.get("started_s"))
            and _finite(item.get("generator_end_s"))
            and abs(item["started_s"] - steady["start_s"]) <= slack
            and abs(item["generator_end_s"] - steady["end_s"]) <= slack
            and item.get("duration_s") == config.phases.steady_s,
            "workload does not cover actual steady interval",
        )
        _released_workload(root, manifest, config, item)
        admission, uncounted = _admitted(root, manifest, config, targets)
        for name, rate in config.workload.rates.items():
            row = functions[name]
            _require(
                row["success"] + row["error"] == row["offered"]
                and admission_is_consistent(
                    admitted=admission[name],
                    success=row["success"],
                    offered=row["offered"],
                    rate=rate,
                    uncounted_s=uncounted[name],
                ),
                "workload/admission counts inconsistent",
            )
            _require(
                row["retry"] == row["replay"] == 0,
                "historical SYNC workload contains retries/replays",
            )
            _require(
                row["dropped"] == 0, "generator dropped iterations invalidate workload"
            )
            demand = rate * config.phases.steady_s
            _require(
                _finite(demand)
                and abs(row["offered"] + row["dropped"] - demand) <= 1.0 + 1e-9,
                "offered plus dropped work differs from scheduled demand "
                "beyond one arrival boundary",
            )
            _require(
                row["error"] / row["offered"] <= config.workload.max_error_ratio,
                "workload error ratio exceeds policy",
            )
        return (
            "aggregate/per-function conservation and authoritative server "
            "admission verified; retries describe clients only"
        )

    gate("workload-accounting", workload)

    def prerequisites() -> CriterionResult | str:
        _require(config is not None and targets, "prerequisite policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        item = receipt("prerequisites")
        if item.get("kind") == "prerequisite-exemption":
            _require(
                exemption_is_allowed(item, config),
                "prerequisite exemption is allowed only by explicit smoke policy",
            )
            return "smoke explicitly requires no prerequisite profiles"
        expected = normalize_prerequisite_inputs(
            _json(
                _reference(
                    root,
                    manifest["prerequisite_inputs"],
                    config.artifact_limit_bytes,
                )
            )
        )
        _require(
            item.get("inputs") == expected,
            "prerequisite input differs from independently frozen artifact",
        )
        _require(
            expected.get("images")
            == {r: t["image_digest"] for r, t in targets.items()},
            "prerequisite application image identity mismatch",
        )
        coverage = frozenset(config.prerequisites.required_coverage)
        _require(
            set(expected["relevant_config"]) == coverage,
            "prerequisite configuration coverage differs",
        )
        for name in coverage:
            relevant = expected["relevant_config"][name]
            for key in config.prerequisites.relevant_config_keys[name]:
                _require(
                    relevant.get(key) == select_relevant_config(normalized, key),
                    "prerequisite relevant configuration differs: " + key,
                )
        for key in ("payload", "script"):
            _reference(root, expected[key], config.artifact_limit_bytes)
        for profile in item["profiles"]:
            _reference(root, profile["evidence"], config.artifact_limit_bytes)
            _require(
                _finite(profile.get("ended_s"))
                and profile["ended_s"] <= manifest["phases"]["warmup"]["start_s"],
                "prerequisite profile did not finish before measured warmup",
            )
        if not coverage:
            _require(
                config.purpose == "smoke"
                and item.get("coverage") == []
                and item.get("profiles") == [],
                "empty prerequisite coverage is allowed only by explicit smoke policy",
            )
            return "smoke explicitly requires no prerequisite profiles"
        return validate_receipt(item, fingerprint(expected), coverage)

    gate("prerequisite-coverage", prerequisites)

    def diagnostics() -> str:
        _require(config is not None and targets, "diagnostic policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        entries = receipt("diagnostics")["entries"]
        # Coverage is per checkpoint. The same reading declared at both is how a
        # difference is measured, so the pair is no longer the unit and one
        # capture per declared triple is what the length check still means.
        expected = {
            (role, phase, op)
            for phase, declared in (
                ("baseline", config.diagnostics.baseline_operations),
                ("drain", config.diagnostics.operations),
            )
            for role, ops in declared.items()
            for op in ops
        }
        _require(
            isinstance(entries, list) and len(entries) <= 1000,
            "invalid diagnostic entries",
        )
        _require(
            {(e["role"], e.get("phase"), e["operation"]) for e in entries} == expected
            and len(entries) == len(expected),
            "required diagnostic coverage missing or duplicate",
        )
        dumps, dump_bytes = 0, 0
        for entry in entries:
            _require(
                entry.get("phase") in _CHECKPOINT_PHASES,
                "invalid diagnostic checkpoint phase",
            )
            item = _diagnostic(
                root,
                entry["receipt"],
                config,
                targets[entry["role"]],
                entry["operation"],
                entry["phase"],
                manifest["phases"][entry["phase"]]["end_s"],
            )
            if entry["operation"] == "heap_dump":
                dumps += 1
                dump_bytes += sum(a["size_bytes"] for a in item["artifacts"])
        _require(
            dumps <= config.diagnostics.max_dumps
            and dump_bytes <= config.diagnostics.max_dump_bytes,
            "diagnostic dump budget exceeded",
        )
        return (
            "runtime-specific final diagnostic operations completed after natural drain"
        )

    gate("diagnostic-coverage", diagnostics)

    def observations() -> str:
        _require(
            config is not None and connection is not None and projection is not None,
            "indexed observations unavailable",
        )
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        assert connection is not None  # nosec B101 - validated invariant/type narrowing
        assert projection is not None  # nosec B101 - validated invariant/type narrowing
        for role, role_policy in config.roles.items():
            for metric in role_policy.required_metrics:
                labels = connection.execute(
                    "SELECT DISTINCT labels FROM samples WHERE role=? AND metric=?",
                    (role, metric),
                ).fetchall()
                _require(bool(labels), f"required metric absent: {role}/{metric}")
                for (label,) in labels:
                    for phase, window in projection["windows"].items():
                        low, high = window["start_s"], window["end_s"]
                        previous, count, units = low, 0, set()
                        for stamp, available, unit in connection.execute(
                            "SELECT scheduled, available, unit FROM samples "
                            "WHERE role=? AND metric=? AND labels=? "
                            "AND phase=? AND scheduled BETWEEN ? AND ? "
                            "ORDER BY scheduled",
                            (role, metric, label, phase, low, high),
                        ):
                            _require(
                                available == 1
                                and stamp - previous <= config.max_observation_gap_s,
                                "required observation unavailable/gapped: "
                                f"{role}/{metric}/{phase}",
                            )
                            previous, count = stamp, count + 1
                            units.add(unit)
                        _require(
                            count
                            >= required_sample_count(
                                high - low, config.sample_interval_s
                            )
                            and high - previous <= config.max_observation_gap_s
                            and len(units) == 1
                            and bool(next(iter(units))),
                            f"incomplete required series: {role}/{metric}/{phase}",
                        )
        return "every required role/metric/label series covers every natural phase"

    gate("required-observations", observations)

    def artifacts() -> str:
        _require(config is not None, "artifact policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        item = receipt("artifacts")
        _require(
            item.get("complete") is True and item.get("budget_exhausted") is False,
            "artifact persistence incomplete or exhausted",
        )
        entries = item["entries"]
        _require(
            isinstance(entries, list) and 1 <= len(entries) <= 10000,
            "invalid artifact inventory",
        )
        seen, total = set(), 0
        for ref in entries:
            path = _reference(root, ref, config.artifact_limit_bytes)
            _require(path not in seen, "duplicate artifact inventory entry")
            seen.add(path)
            total += path.stat().st_size
        required = {root / "samples.jsonl", root / "evaluation-input.json"}
        required.update(
            root / manifest[name]["path"]
            for name in (
                "config",
                "preflight",
                "workload",
                "prerequisites",
                "diagnostics",
            )
        )
        required.update(
            root / manifest[name]["path"]
            for name in (
                "source",
                "workload_inputs",
                "workload_script",
                "prerequisite_inputs",
            )
        )
        required.update(
            _reference(root, ref, config.artifact_limit_bytes)
            for ref in (*manifest["builds"].values(), *manifest["recipes"].values())
        )
        workload_root = _reference(
            root, manifest["workload"], config.artifact_limit_bytes
        ).parent
        required.update(
            workload_root / name
            for name in (
                "workload-inputs.json",
                "workload-config.json",
                "soak-workload.js",
                "k6-summary.json",
                "generator-process.json",
                "k6.log",
            )
        )
        _require(required.issubset(seen), "inventory omits required evidence")
        _require(total <= config.artifact_limit_bytes, "total artifact budget exceeded")
        # The inventory sum only bounds what the inventory knows about.
        # Measure the tree so a file written past it cannot hide.
        _require(
            measure_tree(root) <= config.artifact_limit_bytes,
            "measured artifact tree exceeds the total budget",
        )
        return "required artifact byte counts and checksums verified"

    gate("artifact-integrity", artifacts)

    resolutions: list[CriterionResult] = []

    def attribution() -> str:
        _require(config is not None, "attribution policy unavailable")
        # Narrowed by the _require above; the check itself is not the assert.
        assert config is not None  # nosec B101 - validated invariant/type narrowing
        pending = {c.id: c for c in config.criteria if c.operation == "growth_review"}
        triggered = {
            r.criterion_id
            for r in numerical
            if r.criterion_id in pending
            and r.status == "INCONCLUSIVE"
            and r.reason.startswith("growth exceeds review threshold")
        }
        if attribution_path is None:
            _require(
                not triggered,
                "suspicious residual requires bound ownership "
                "and equal-work attribution",
            )
            return "no unresolved numerical growth attribution requested"
        _require(not attribution_path.is_symlink(), "symlink attribution unsupported")
        document = _json(attribution_path)
        _bound(document, manifest)
        entries = document["entries"]
        _require(
            isinstance(entries, list) and len(entries) <= 1000,
            "invalid attribution entries",
        )
        seen = set()
        for entry in entries:
            record = entry["attribution"]
            name = record["criterion_id"]
            _require(
                name in pending and name not in seen,
                "unknown or duplicate attribution criterion",
            )
            seen.add(name)
            criterion = pending[name]
            result = validate_attribution(record, root)
            _require(result.status == "PASS", result.reason)
            policy_ref = record["policy_artifact"]
            _require(
                policy_ref["path"] == manifest["config"]["path"]
                and policy_ref["sha256"] == manifest["config"]["sha256"],
                "attribution policy differs from frozen policy",
            )
            _require(
                record["owner"] in config.retention_s
                and record["expected_lifetime_s"]
                <= config.retention_s[record["owner"]],
                "attribution owner/lifetime lacks frozen retention policy",
            )
            budgets = [
                c.threshold
                for c in config.criteria
                if c.role == criterion.role
                and c.metric == criterion.metric
                and c.label_selector == criterion.label_selector
                and c.operation == "maximum"
            ]
            _require(
                bool(budgets)
                and record["budget_bytes"] <= min(b for b in budgets if b is not None),
                "attributed population lacks a matching frozen maximum budget",
            )
            work = _json(
                _reference(root, entry["equal_work"], config.artifact_limit_bytes)
            )
            _bound(work, manifest)
            _require(
                work.get("criterion_id") == name
                and work.get("metric") == criterion.metric
                and work.get("unit") == criterion.unit
                and work.get("target") == targets[criterion.role],
                "equal-work population/process binding mismatch",
            )
            windows = work["windows"]
            _require(
                isinstance(windows, list) and len(windows) == 2,
                "two equal-work checkpoints required",
            )
            values, durations, offered, admitted, signatures = [], [], [], [], []
            previous = -math.inf
            for window in windows:
                low, high = window["started_s"], window["ended_s"]
                _require(
                    _finite(low) and _finite(high) and previous <= low < high,
                    "invalid equal-work window timing",
                )
                previous = high
                _require(
                    _count(window.get("offered"))
                    and _count(window.get("admitted"))
                    and 0 < window["admitted"] <= window["offered"],
                    "equal-work counts unavailable",
                )
                scoped = {**manifest, "workload": window["workload"]}
                traffic, counters, _ = _workload_evidence(root, scoped, config, targets)
                _released_workload(root, scoped, config, traffic)
                _require(
                    _finite(traffic.get("started_s"))
                    and _finite(traffic.get("generator_end_s"))
                    and low <= traffic["started_s"] < traffic["generator_end_s"] <= high
                    and traffic.get("duration_s") == config.phases.steady_s,
                    "equal-work traffic timing differs from checkpoint window",
                )
                scoped["phases"] = {
                    "steady": {
                        "start_s": traffic["started_s"],
                        "end_s": traffic["generator_end_s"],
                    }
                }
                server, server_uncounted = _admitted(
                    root, scoped, config, targets, window["admission_samples"]
                )
                _require(
                    sum(c["offered"] for c in counters.values()) == window["offered"]
                    and sum(server.values()) == window["admitted"],
                    "reviewer totals differ from actual offered/admitted work",
                )
                for function, counts in counters.items():
                    _require(
                        counts["retry"] == counts["replay"] == counts["dropped"] == 0
                        and admission_is_consistent(
                            admitted=server[function],
                            success=counts["success"],
                            offered=counts["offered"],
                            rate=config.workload.rates[function],
                            uncounted_s=server_uncounted[function],
                        )
                        and counts["error"] / counts["offered"]
                        <= config.workload.max_error_ratio,
                        "equal-work workload invalid",
                    )
                signatures.append(
                    {n: (c["offered"], server[n]) for n, c in counters.items()}
                )
                checkpoint = _diagnostic(
                    root,
                    window["post_gc"],
                    config,
                    targets[criterion.role],
                    "gc",
                    # The diagnostic protocol's own checkpoint is its final one.
                    "drain",
                    low,
                    high,
                )
                _require(
                    traffic["generator_end_s"] <= checkpoint["started_s"],
                    "post-GC checkpoint precedes equal-work traffic stop",
                )
                observation_path = _reference(
                    root, window["observation"], config.artifact_limit_bytes
                )
                checkpoint_path = _reference(
                    root, window["post_gc"], config.artifact_limit_bytes
                )
                _require(
                    any(
                        _reference(
                            checkpoint_path.parent, ref, config.artifact_limit_bytes
                        )
                        == observation_path
                        for ref in checkpoint["artifacts"]
                    ),
                    "post-GC sample is not a captured diagnostic artifact",
                )
                sample = _json(
                    _reference(root, window["observation"], config.artifact_limit_bytes)
                )
                _require(
                    sample.get("schema") == SCHEMA
                    and sample.get("phase") == "diagnostic"
                    and sample.get("target") == targets[criterion.role]
                    and sample.get("metric") == criterion.metric
                    and sample.get("unit") == criterion.unit
                    and dict(sample.get("labels", [])) == criterion.label_selector
                    and sample.get("availability") == "observed"
                    and sample.get("source") in {"prometheus", "jcmd", "procfs"}
                    and _finite(sample.get("value"))
                    and sample["value"] >= 0
                    and _finite(sample.get("started_s"))
                    and _finite(sample.get("ended_s"))
                    and checkpoint["_gc_ended_s"]
                    <= sample["started_s"]
                    <= sample["ended_s"]
                    <= high,
                    "observed post-GC population sample missing or unbound",
                )
                values.append(sample["value"])
                durations.append(high - low)
                offered.append(window["offered"])
                admitted.append(window["admitted"])
            _require(
                durations[0] == durations[1]
                and offered[0] == offered[1]
                and admitted[0] == admitted[1]
                and signatures[0] == signatures[1],
                "growth checkpoints do not represent equal work",
            )
            _require(
                values[1] - values[0] <= criterion.threshold
                and values[1] <= record["budget_bytes"],
                "post-GC growth or retained population exceeds frozen policy",
            )
            if name in triggered:
                resolutions.append(
                    CriterionResult(
                        name,
                        "PASS",
                        "growth review resolved with bound equal-work GC "
                        "and ownership evidence",
                        result.evidence,
                    )
                )
        _require(triggered.issubset(seen), "required attribution remains open")
        return (
            "ownership, retention, numerical budget and equal-work "
            "checkpoints bound to frozen policy"
        )

    gate("attribution-policy-binding", attribution)
    if results[-1].status == "PASS":
        results.extend(resolutions)
    aborted = manifest.get("aborted") is True
    complete = all(r.status == "PASS" for r in results)
    results.append(
        CriterionResult(
            "run-coverage",
            "ABORTED" if aborted else "PASS" if complete else "INCONCLUSIVE",
            "external interruption"
            if aborted
            else "full-run receipt gates verified"
            if complete
            else "one or more required run acceptance gates remain unsatisfied",
            ("acceptance-manifest.json",),
        )
    )
    return tuple(results)
