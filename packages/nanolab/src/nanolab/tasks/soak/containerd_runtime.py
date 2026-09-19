"""Run the shared soak lifecycle against a systemd CP and containerd functions."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.execution.bindings import CommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec

from nanolab.cli.execution import resolve_loadtest_urls
from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig
from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.soak.artifacts import ArtifactWriter
from nanolab.tasks.soak.containerd import RootlessCollectionTransport
from nanolab.tasks.soak.models import Target
from nanolab.tasks.soak.preflight import effective_cpu_limit
from nanolab.tasks.soak.preparation import PreparationOptions, _payloads
from nanolab.tasks.soak.runtime import (
    create_soak_lifecycle,
    observe_local_configuration,
)
from nanolab.tasks.soak.sources import capture_source_snapshot
from nanolab.tasks.soak.workflow import (
    _publish_run_document,
    write_policy_input,
    write_terminal_receipt,
)


def finalize_containerd_terminal(run_dir: Path, error: BaseException | None) -> Path:
    """Seal the measured verdict only after Sonata and provisioning cleanup finish."""
    marker = run_dir / "measurement-verdict.json"
    status = "INCONCLUSIVE"
    report_path = None
    try:
        if marker.is_file() and not marker.is_symlink():
            with marker.open("rb") as stream:
                body = stream.read(1024 * 1024 + 1)
            if len(body) <= 1024 * 1024:
                data = json.loads(body)
                if (
                    isinstance(data, dict)
                    and data.get("schema") == "nanolab-containerd-measurement-v1"
                ):
                    status = data.get("status", "INCONCLUSIVE")
                    report = data.get("report_path")
                    report_path = Path(report) if isinstance(report, str) else None
    except (OSError, ValueError, TypeError):
        pass
    if status not in {"PASS", "FAIL", "INCONCLUSIVE", "ABORTED"}:
        status = "INCONCLUSIVE"
    if error is not None:
        if not isinstance(error, Exception):
            status = "ABORTED"
        elif status == "PASS":
            status = "INCONCLUSIVE"
    return write_terminal_receipt(
        run_dir,
        status,
        report_path=report_path,
        reason=str(error) if error is not None else None,
    )


class BuildExecutionRecorder:
    """Retain successful build task commands observed at the executor boundary."""

    def __init__(self, delegate: CommandTaskExecutor) -> None:
        """Wrap the same executor used by platform build tasks."""
        self.delegate = delegate
        self.builds: dict[str, list[ObservedBuild]] = {}

    def binding_key(self, role: str) -> str:
        """Keep Sonata task fingerprints bound to the delegated role."""
        return self.delegate.binding_key(role)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False):
        """Record a build command only when its real execution succeeded."""
        result = self.delegate.run(task, dry_run=dry_run)
        if task.role == "stack" and task.summary.startswith("Build "):
            if dry_run or result.status != "passed" or result.return_code != 0:
                raise RuntimeError(
                    f"build task did not execute successfully: {task.summary}"
                )
            self.builds.setdefault(task.summary, []).append(
                ObservedBuild(
                    task.summary, tuple(task.argv), result.status, result.return_code
                )
            )
        return result

    def require_build(self, title: str) -> ObservedBuild:
        """Return the one executed result for a planned task title."""
        commands = self.builds.get(title, ())
        if len(commands) != 1:
            raise ValueError(f"missing or repeated successful build task: {title}")
        return commands[0]


@dataclass(frozen=True)
class ObservedBuild:
    """A command/result pair captured from the actual platform build task."""

    title: str
    argv: tuple[str, ...]
    status: str
    return_code: int


@dataclass(frozen=True)
class ContainerdBuildReceipt:
    """A remote build's actual running artifact and staged source revision."""

    role: str
    image_digest: str
    source_fingerprint: str
    source_revision: str | None
    platform: str
    artifact_kind: str
    artifact_path: str | None
    build_argv: tuple[str, ...]
    build_steps: tuple[tuple[str, ...], ...]
    build_results: tuple[ObservedBuild, ...]
    mode: str
    variant: str
    modules: tuple[str, ...]
    build_options: dict[str, str]


@dataclass(frozen=True)
class ContainerdRecipe:
    """Declared build command and platform for one measured role."""

    role: str
    artifact_kind: str
    platform: str
    build_argv: tuple[str, ...]
    build_steps: tuple[tuple[str, ...], ...]
    mode: str
    variant: str
    modules: tuple[str, ...]
    build_options: dict[str, str]


@dataclass(frozen=True)
class PreparedContainerdSoak:
    """Frozen source, workload inputs and running artifact receipts."""

    run_id: str
    config: Any
    evidence_dir: Path
    writer: ArtifactWriter
    snapshot: Any
    recipes: tuple[ContainerdRecipe, ...]
    receipts: tuple[ContainerdBuildReceipt, ...]
    payloads: dict[str, list[dict[str, object]]]
    builds_finished_s: float
    frozen_at_s: float

    @property
    def images(self) -> dict[str, str]:
        """Return actual process and OCI identities used by common workload gates."""
        return {item.role: item.image_digest for item in self.receipts}


@dataclass(frozen=True)
class ContainerdDeployment:
    """Live endpoints and observations of the owned rootless deployment."""

    api_endpoint: str
    metrics_endpoints: dict[str, str | None]
    discover: Callable[[], tuple[Target, ...]]
    observations: Callable[[tuple[Target, ...]], dict[str, Any]]
    diagnostic_inputs: dict[str, Any] | None = None


class ContainerdSoakRun(Task):
    """Measure actual rootless processes; Sonata resources own their cleanup."""

    title = "Run containerd soak measurement"
    idempotent = False

    def __init__(
        self,
        scenario: ScenarioConfig,
        run: RootlessRun,
        environment: EnvironmentConfig,
        executor: CommandTaskExecutor,
        *,
        run_dir: Path,
        repo_root: Path,
        functions: tuple[Any, ...] = (),
    ) -> None:
        """Bind scenario, owned runtime and output without running anything."""
        self.scenario = scenario
        self.rootless = run
        self.environment = environment
        self.executor = executor
        self.run_dir = run_dir
        self.repo_root = repo_root
        self.functions = functions

    def _remote(self, *argv: str) -> str:
        result = self.executor.run(
            CommandTaskSpec(
                task_id="",
                summary="Verify staged soak source",
                argv=argv,
                role="stack",
                options=CommandOptions(timeout_seconds=60),
            )
        )
        if result.status != "passed" or result.return_code != 0:
            raise RuntimeError(
                f"remote source verification failed: {result.stderr[:512]}"
            )
        return result.stdout.strip()

    def _verify_remote_source(self, snapshot: Any) -> dict[str, Any]:
        """Hash each staged source input after builds; rsync omits `.git`."""
        if not snapshot.revision or not snapshot.entries:
            raise ValueError("committed source entries are required")
        hashes = []
        for offset in range(0, len(snapshot.entries), 50):
            entries = [
                asdict(entry) for entry in snapshot.entries[offset : offset + 50]
            ]
            body = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            encoded = base64.b64encode(zlib.compress(body)).decode()
            if len(encoded) > 65536:
                raise ValueError(
                    "remote source verification batch exceeds argument budget"
                )
            response = json.loads(
                self._remote(
                    "python3",
                    str(self.rootless.script.with_name("source_verify.py")),
                    str(self.rootless.repo_root),
                    encoded,
                )
            )
            expected = hashlib.sha256(body).hexdigest()
            if response != {"count": len(entries), "sha256": expected}:
                raise ValueError(
                    "staged VM source differs from the frozen feature checkout"
                )
            hashes.append(expected)
        return {
            "schema": "nanolab-containerd-source-v1",
            "revision": snapshot.revision,
            "clean": True,
            "source_fingerprint": snapshot.fingerprint,
            "entry_count": len(snapshot.entries),
            "batch_size": 50,
            "batches": hashes,
            "verification": "remote-content-after-build; rsync excludes .git",
        }

    def _prepare(
        self, transport: RootlessCollectionTransport, targets: tuple[Any, ...]
    ) -> PreparedContainerdSoak:
        policy = self.scenario.soak
        if policy is None:
            raise ValueError("containerd soak policy unavailable")
        evidence = self.run_dir / "evidence"
        writer = ArtifactWriter(evidence, policy.artifact_limit_bytes)
        try:
            writer.write_json("config.json", policy.model_dump(mode="json"))
            payloads = _payloads(policy, PreparationOptions())
            writer.write_json("payloads.json", payloads)
            snapshot = capture_source_snapshot(
                self.repo_root,
                evidence / "source",
                max_bytes=min(policy.artifact_limit_bytes // 2, 1024 * 1024 * 1024),
            )
            if snapshot.dirty:
                raise ValueError("containerd soak requires a committed source snapshot")
            writer.write_json(
                "remote-source.json", self._verify_remote_source(snapshot)
            )
            actual = {role: transport.inspect(role)[1] for role in policy.roles}
            recipes = []
            receipts = []
            if not isinstance(self.executor, BuildExecutionRecorder):
                raise ValueError("containerd soak requires observed build task results")
            functions = {function.name: function for function in self.functions}
            builds = ArtifactWriter(
                evidence / "builds", policy.artifact_limit_bytes // 4
            )
            try:
                for index, (role, spec) in enumerate(policy.images.items()):
                    detail = actual[role]
                    if (
                        detail["artifact_kind"] != spec.artifact_kind
                        or detail.get("platform") != spec.platform
                    ):
                        raise ValueError(
                            f"{role} running artifact kind/platform differs from policy"
                        )
                    if role == "control-plane":
                        results = (self.executor.require_build("Build control plane"),)
                    else:
                        function = functions[role]
                        results = (
                            (
                                self.executor.require_build(
                                    f"Build application artifact: {role}"
                                ),
                            )
                            if function.image_build_argv is not None
                            else ()
                        ) + (self.executor.require_build(f"Build image {role}"),)
                    steps = tuple(result.argv for result in results)
                    command = steps[-1]
                    recipe = ContainerdRecipe(
                        role,
                        spec.artifact_kind,
                        spec.platform,
                        command,
                        steps,
                        spec.mode,
                        spec.variant,
                        tuple(spec.modules),
                        dict(spec.build_options),
                    )
                    receipt = ContainerdBuildReceipt(
                        role,
                        detail["image_digest"],
                        snapshot.fingerprint,
                        snapshot.revision,
                        detail["platform"],
                        spec.artifact_kind,
                        detail.get("artifact_path"),
                        command,
                        steps,
                        results,
                        spec.mode,
                        spec.variant,
                        tuple(spec.modules),
                        dict(spec.build_options),
                    )
                    writer.write_json(f"recipe-{index}.json", asdict(recipe))
                    builds.write_json(
                        f"build-{index}.json",
                        {"schema": "nanolab-containerd-build-v1", **asdict(receipt)},
                    )
                    recipes.append(recipe)
                    receipts.append(receipt)
            finally:
                builds.close()
            return PreparedContainerdSoak(
                "soak-" + self.rootless.run_id,
                policy,
                evidence,
                writer,
                snapshot,
                tuple(recipes),
                tuple(receipts),
                payloads,
                time.monotonic(),
                time.monotonic(),
            )
        except BaseException:
            writer.close()
            raise

    def run(self, inputs: TaskInputs) -> TaskOutcome[Any]:
        """Measure the owned deployment; CLI seals its verdict after cleanup."""
        policy = self.scenario.soak
        if policy is None:
            raise ValueError("containerd soak policy unavailable")
        prepared = None
        holder = None
        try:
            write_policy_input(self.run_dir, self.scenario)
            transport = RootlessCollectionTransport(self.rootless, self.executor)
            targets = tuple(transport.inspect(role)[0] for role in policy.roles)
            for target in targets:
                if target.runtime != policy.roles[target.role].runtime:
                    raise ValueError(
                        f"{target.role} runtime differs from frozen policy"
                    )
            prepared = self._prepare(transport, targets)
            api, _prometheus = resolve_loadtest_urls(
                self.environment, backend="containerd"
            )
            management = api.rsplit(":", 1)[0] + ":8081"

            def discover():
                return tuple(transport.inspect(role)[0] for role in policy.roles)

            def observations(bound_targets):
                effective = observe_local_configuration(
                    prepared, management, api_url=api
                )
                observed: dict[str, Any] = {
                    **effective,
                    "roles": {},
                    "free_bytes": shutil.disk_usage(self.run_dir).free,
                    "remote_source": json.loads(
                        (prepared.evidence_dir / "remote-source.json").read_text()
                    ),
                }
                for target in bound_targets:
                    data = transport.collect(target, None, policy.scrape_timeout_s)
                    _identity, detail = transport.inspect(target.role)
                    config = data["configuration"]
                    cpu = config["cpu_max"]
                    quota = None if cpu[0] == "max" else int(cpu[0])
                    observed["roles"][target.role] = {
                        "image_digest": target.image_digest,
                        "cpu": effective_cpu_limit(
                            quota, int(cpu[1]), config["cpuset"]
                        ),
                        "memory_bytes": config["memory_bytes"],
                        "limit_sources": config["limit_sources"],
                        "runtime": config["runtime"],
                        "runtime_options": config["runtime_options"],
                        "capabilities": config["capabilities"],
                        "collection_sources": config["collection_sources"],
                        "diagnostics": [],
                        "artifact_path": detail.get("artifact_path"),
                        "platform": detail["platform"],
                    }
                    if target.role == "control-plane":
                        observed["roles"][target.role]["modules"] = effective.get(
                            "modules"
                        )
                return observed

            deployment = ContainerdDeployment(
                api_endpoint=api,
                metrics_endpoints={
                    role: management + "/actuator/prometheus"
                    if role == "control-plane"
                    else "http://127.0.0.1:8080/metrics"
                    for role in policy.roles
                },
                discover=discover,
                observations=observations,
                diagnostic_inputs=None,
            )
            holder = create_soak_lifecycle(
                prepared,
                deployment=deployment,
                run_dir=self.run_dir,
                transport=transport,
                defer_terminal=True,
            )
            result = holder.run(inputs)
            evaluation = holder.state.evaluation
            _publish_run_document(
                self.run_dir,
                "measurement-verdict.json",
                {
                    "schema": "nanolab-containerd-measurement-v1",
                    "status": evaluation.get("status")
                    if isinstance(evaluation, dict)
                    else "INCONCLUSIVE",
                    "report_path": str(holder.state.report)
                    if holder.state.report
                    else None,
                },
            )
            return result
        except BaseException as error:
            if not (self.run_dir / "measurement-verdict.json").exists():
                evaluation = getattr(getattr(holder, "state", None), "evaluation", None)
                _publish_run_document(
                    self.run_dir,
                    "measurement-verdict.json",
                    {
                        "schema": "nanolab-containerd-measurement-v1",
                        "status": (
                            "ABORTED"
                            if not isinstance(error, Exception)
                            else evaluation.get("status")
                            if isinstance(evaluation, dict)
                            else "INCONCLUSIVE"
                        ),
                        "report_path": str(holder.state.report)
                        if holder is not None and holder.state.report
                        else None,
                    },
                )
            raise
        finally:
            if prepared is not None:
                prepared.writer.close()
