from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
import shlex
import threading
import time
from typing import Protocol
from urllib.parse import quote

import httpx
from sonata_tasks.loadtest.ports import RemoteFileFetcher
from sonata_tasks.tasks.executors import VmCommandRunner


class Sampling(Protocol):
    """Something that samples for as long as the load runs.

    All the load task does is bracket the run, so this is all it should have to
    be given — asking for the reading too would exclude a sampler that only
    records elsewhere.
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...


class InitialReplicaCheck(Protocol):
    """Whatever asserts the run starts where it should.

    A protocol so the load task depends on the assertion, not on the one class
    that happens to make it today.
    """

    def run(self) -> None: ...


class AutoscalingResult(Protocol):
    """Somewhere to read what the autoscaling verification concluded.

    The summary writer reads `.result` and nothing else, so asking for the whole
    verifier made callers cast a shim into a class it never was.
    """

    @property
    def result(self) -> AutoscalingSummary: ...


class Watcher(Protocol):
    """Somewhere to read the highest replica count someone observed.

    Independent of Sampling on purpose: the verifier only reads, and requiring
    start/stop here would demand of it a lifecycle it never drives. `errors` is
    absent for the same reason — it is read through getattr.
    """

    @property
    def max_observed(self) -> int: ...


class ReplicaStatusProbe(Protocol):
    def ready_replicas(self) -> int: ...
    def desired_replicas(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ReplicaSample:
    """One reading of a deployment's replica counts while load was running."""

    elapsed_seconds: float
    desired: int
    ready: int


def rises_from_zero(samples: Sequence[ReplicaSample]) -> int:
    """How many times the deployment came up from zero during the load.

    A healthy scale-to-zero run rises exactly once: parked at zero, up when the
    load arrives, down again after it. Rising twice means the autoscaler let go
    of the function while traffic was still flowing and had to fetch it back —
    which a peak replica count cannot express, because both runs peak the same.

    Counted on `desired` rather than `ready`: desired is what the autoscaler
    decided, and a pod that is merely slow to become ready is not the autoscaler
    changing its mind.
    """
    rises = 0
    previous = None
    for sample in samples:
        if previous is not None and previous == 0 and sample.desired > 0:
            rises += 1
        previous = sample.desired
    return rises


@dataclass(frozen=True)
class AutoscalingSummary:
    deployment_name: str
    max_replicas_observed: int
    final_desired_replicas: int
    # The trajectory, not just its peak. `max()` over the samples answers "did it
    # ever scale up" and nothing else; an autoscaler that oscillates to zero
    # mid-load produces the same peak as one that holds steady, so the defect was
    # invisible until the series itself was kept.
    replica_samples: tuple[ReplicaSample, ...] = ()
    rises_from_zero: int = 0


@dataclass(frozen=True)
class ReplicaProbe:
    """Reads deployment replica counts over the VM command runner.

    Errors are surfaced, not masked: a missing deployment and an unreachable
    cluster must be distinguishable from "0 replicas" when diagnosing a run.
    """

    runner: VmCommandRunner
    namespace: str
    deployment_name: str
    remote_dir: str

    def ready_replicas(self) -> int:
        return self._replica_count("{.status.readyReplicas}")

    def desired_replicas(self) -> int:
        return self._replica_count("{.spec.replicas}")

    def _replica_count(self, jsonpath: str) -> int:
        deployment = shlex.quote(self.deployment_name)
        namespace = shlex.quote(self.namespace)
        output = shlex.quote(f"jsonpath={jsonpath}")
        result = self.runner.run_vm_command(
            (
                "bash",
                "-lc",
                # sudo: on k3s VMs /etc/rancher/k3s/k3s.yaml is root-readable only.
                f"sudo kubectl get deployment {deployment} -n {namespace} -o {output}",
            ),
            env={},
            remote_dir=self.remote_dir,
            dry_run=False,
        )
        if result.return_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if "NotFound" in detail:
                raise RuntimeError(
                    f"deployment {self.deployment_name!r} not found in namespace {self.namespace!r}: {detail}"
                )
            raise RuntimeError(detail or f"kubectl replica query failed (exit {result.return_code})")
        raw = (result.stdout or "").strip()
        if not raw:
            # jsonpath yields empty output when the field is absent (e.g. readyReplicas at 0).
            return 0
        try:
            return int(raw)
        except ValueError as exc:
            raise RuntimeError(f"invalid replica count: {result.stdout!r}") from exc


@dataclass(frozen=True)
class HttpReplicaProbe:
    """Read provider-neutral replica status from the nanoFaaS API."""

    endpoint: str
    function_name: str
    timeout_seconds: float = 4.0

    def ready_replicas(self) -> int:
        return self._status()[1]

    def desired_replicas(self) -> int:
        return self._status()[0]

    def _status(self) -> tuple[int, int]:
        name = quote(self.function_name, safe="")
        url = f"{self.endpoint.rstrip('/')}/v1/functions/{name}/replicas"
        try:
            response = httpx.get(url, timeout=self.timeout_seconds)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"replica status request failed for {self.function_name!r}: {exc}"
            ) from exc
        if response.status_code != 200:
            detail = response.text.strip()
            raise RuntimeError(
                detail
                or f"replica status request failed for {self.function_name!r} "
                f"(HTTP {response.status_code})"
            )
        try:
            data = response.json()
            desired = int(data["desiredReplicas"])
            ready = int(data["readyReplicas"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid replica status response for {self.function_name!r}"
            ) from exc
        if desired < 0 or ready < 0:
            raise RuntimeError(
                f"invalid replica status response for {self.function_name!r}"
            )
        return desired, ready


class ReplicaWatcher:
    """Samples deployment replicas on a background thread while load runs.

    Scale-up must be observed DURING the k6 run: checking afterwards only sees
    residual state and races the autoscaler's downscale cooldown.
    """

    def __init__(
        self,
        probe: ReplicaStatusProbe,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._probe = probe
        self._poll_interval = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[ReplicaSample] = []
        self._started_at: float | None = None
        self.errors: list[str] = []

    @property
    def samples(self) -> tuple[ReplicaSample, ...]:
        """Every reading taken, in order.

        The list is what makes the shape of a run knowable after the fact. It
        is appended from the sampling thread and read from the caller's, which
        CPython's GIL makes safe for `list.append` plus `tuple()`.
        """
        return tuple(self._samples)

    @property
    def max_observed(self) -> int:
        return max(
            (max(sample.desired, sample.ready) for sample in self._samples),
            default=0,
        )

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ReplicaWatcher already started")
        self._stop.clear()
        self._started_at = time.monotonic()
        # Taken here, synchronously, rather than left to the thread's first turn:
        # starting a thread is asynchronous, so by the time it is scheduled the
        # load may already have woken a function that was parked at zero. A real
        # run lost exactly that — the function was verified at zero moments
        # before, and the series still opened at 1, leaving `rises_from_zero`
        # unable to see the rise it existed to count.
        self._sample()
        self._thread = threading.Thread(target=self._loop, name="replica-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None

    def _sample(self) -> None:
        try:
            ready = self._probe.ready_replicas()
            desired = self._probe.desired_replicas()
            self._samples.append(
                ReplicaSample(
                    elapsed_seconds=round(
                        time.monotonic() - (self._started_at or time.monotonic()), 3
                    ),
                    desired=desired,
                    ready=ready,
                )
            )
        except RuntimeError as exc:
            # A transient probe failure must not kill the watcher mid-load;
            # errors are kept for diagnostics.
            self.errors.append(str(exc))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._poll_interval)
            if self._stop.is_set():
                return
            self._sample()


@dataclass(frozen=True)
class VerifyInitialAutoscalingReplicas:
    """Require an autoscaling run to begin at its configured replica floor."""

    probe: ReplicaStatusProbe
    expected_replicas: int = 0

    def run(self) -> None:
        desired = self.probe.desired_replicas()
        ready = self.probe.ready_replicas()
        if desired != self.expected_replicas or ready != self.expected_replicas:
            raise RuntimeError(
                "initial autoscaling replicas: "
                f"expected desired={self.expected_replicas} and ready={self.expected_replicas}, "
                f"got desired={desired} and ready={ready}"
            )


@dataclass
class VerifyAutoscalingReplicas:
    task_id: str
    title: str
    runner: VmCommandRunner
    namespace: str
    deployment_name: str
    remote_dir: str
    scale_up_polls: int = 24
    scale_down_initial_delay_seconds: int = 90
    scale_down_polls: int = 24
    poll_interval_seconds: int = 5
    watcher: Watcher | None = None
    probe: ReplicaStatusProbe | None = None
    expected_final_replicas: int = 0
    _result: AutoscalingSummary | None = field(default=None, init=False, repr=False)

    @property
    def result(self) -> AutoscalingSummary:
        if self._result is None:
            raise RuntimeError("VerifyAutoscalingReplicas.run() has not been called")
        return self._result

    def _complete(self, max_replicas: int, final_desired: int) -> AutoscalingSummary:
        # getattr, like `errors` above: a sampler that reports only a peak is
        # still a valid Watcher, and requiring the series of every double would
        # buy nothing the summary cannot express as "no samples".
        samples = tuple(getattr(self.watcher, "samples", ()) or ())
        self._result = AutoscalingSummary(
            deployment_name=self.deployment_name,
            max_replicas_observed=max_replicas,
            final_desired_replicas=final_desired,
            replica_samples=samples,
            rises_from_zero=rises_from_zero(samples),
        )
        return self._result

    def _resolved_probe(self) -> ReplicaStatusProbe:
        return self.probe or ReplicaProbe(
            runner=self.runner,
            namespace=self.namespace,
            deployment_name=self.deployment_name,
            remote_dir=self.remote_dir,
        )

    def _ready_replicas(self) -> int:
        return self._resolved_probe().ready_replicas()

    def _desired_replicas(self) -> int:
        return self._resolved_probe().desired_replicas()

    def run(self) -> AutoscalingSummary:
        max_replicas = self.watcher.max_observed if self.watcher is not None else 0
        if max_replicas <= 1:
            # Fallback: no watcher (or it observed nothing) — poll residual state.
            for _ in range(self.scale_up_polls):
                time.sleep(self.poll_interval_seconds)
                ready = self._ready_replicas()
                desired = self._desired_replicas()
                max_replicas = max(max_replicas, ready, desired)
                if max_replicas > 1:
                    break

        if max_replicas <= 1:
            message = f"Scale-up not observed: max replicas stayed at {max_replicas}"
            watcher_errors = list(getattr(self.watcher, "errors", []) or [])
            if watcher_errors:
                message += f" (watcher probe errors: {watcher_errors[-1]!r}, {len(watcher_errors)} total)"
            raise RuntimeError(message)

        time.sleep(self.scale_down_initial_delay_seconds)
        final_desired = self._desired_replicas()
        if final_desired == self.expected_final_replicas:
            return self._complete(max_replicas, final_desired)
        for _ in range(self.scale_down_polls):
            time.sleep(self.poll_interval_seconds)
            final_desired = self._desired_replicas()
            if final_desired == self.expected_final_replicas:
                return self._complete(max_replicas, final_desired)

        raise RuntimeError(
            f"Scale-down to {self.expected_final_replicas} not observed: "
            f"desired replicas = {final_desired}"
        )


@dataclass
class FetchAutoscalingSummary:
    """Copies the autoscaling k6 summary from the VM into the local run dir."""

    task_id: str
    title: str
    fetcher: RemoteFileFetcher
    remote_path: str
    local_path: Path

    def run(self) -> None:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.fetcher.fetch_from(self.remote_path, self.local_path)
