"""What the run cost in memory and CPU, per container, while it was running.

Prometheus only scrapes the control plane, so the functions' own footprint was
absent from every run record — which is how two functions differing by an entire
runtime model (a Spring Boot JVM against a GraalVM native binary) sat in the same
experiment described as "the same runtime family". Their resident sets differ by
a factor of four.

Read from the Docker Engine API rather than from each process's own metrics
endpoint, for two reasons: it is the same measurement whatever the runtime, and a
native binary exposes no JVM memory gauges at all. Resident set is also the
number that decides whether a function fits on a node, which a heap gauge does
not — a JVM measured here held 22 MB of heap inside a 190 MiB resident set, the
rest being class metadata, JIT-compiled code and thread stacks.

cAdvisor would be the better source, and on Kubernetes it is the one to use since
the kubelet already exports it. It was tried here first and rejected on evidence:
under Docker Desktop it enumerates only the cgroup roots, reporting nothing for
any individual container. The Engine API is what `docker stats` itself calls, so
this is the same data one layer earlier — exact integers instead of "190MiB"
parsed back out of text meant for people.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DOCKER_SOCKET = "/var/run/docker.sock"


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One container's footprint at one instant."""

    elapsed_seconds: float
    container: str
    memory_bytes: float
    memory_limit_bytes: float
    cpu_percent: float


def working_set_bytes(stats: dict[str, Any]) -> float:
    """Memory in use, page cache excluded.

    Raw `usage` counts the page cache, which makes a container that has merely
    read its own jar look like one that needs the memory. Subtracting the
    reclaimable part is what `docker stats` shows and what Kubernetes decides
    OOM kills on, so it is the honest "how much does this need" number.
    """
    memory = stats.get("memory_stats") or {}
    usage = float(memory.get("usage") or 0.0)
    detail = memory.get("stats") or {}
    # cgroup v2 calls it inactive_file; v1 called it total_inactive_file.
    inactive = float(detail.get("inactive_file") or detail.get("total_inactive_file") or 0.0)
    return max(0.0, usage - inactive)


def cpu_percent(stats: dict[str, Any]) -> float:
    """CPU used since the previous reading, as a percentage of one core x100.

    The Engine reports cumulative nanoseconds plus the same figures from the
    preceding read, so a single request carries its own interval and no state
    has to be kept here. 400% means four cores fully busy.
    """
    cpu = stats.get("cpu_stats") or {}
    previous = stats.get("precpu_stats") or {}
    usage = (cpu.get("cpu_usage") or {}).get("total_usage")
    previous_usage = (previous.get("cpu_usage") or {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    previous_system = previous.get("system_cpu_usage")
    # Checked one at a time: a tuple membership test does not narrow, and the
    # first reading of a container legitimately has no predecessor.
    if usage is None or previous_usage is None or system is None or previous_system is None:
        return 0.0
    delta = float(usage) - float(previous_usage)
    system_delta = float(system) - float(previous_system)
    if delta <= 0 or system_delta <= 0:
        return 0.0
    cores = cpu.get("online_cpus") or len(
        (cpu.get("cpu_usage") or {}).get("percpu_usage") or []
    ) or 1
    return delta / system_delta * float(cores) * 100.0


@dataclass(frozen=True)
class DockerEngineProbe:
    """Reads container stats straight from the Engine API over its unix socket."""

    name_prefix: str = "nanofaas"
    socket_path: str = DOCKER_SOCKET
    timeout_seconds: float = 10.0

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url="http://docker",
            timeout=self.timeout_seconds,
        )

    def container_names(self) -> list[str]:
        with self._client() as client:
            response = client.get("/containers/json")
            response.raise_for_status()
            names = []
            for container in response.json():
                for raw in container.get("Names", []):
                    name = raw.lstrip("/")
                    if name.startswith(self.name_prefix):
                        names.append(name)
            return sorted(names)

    def read(self, elapsed: float) -> list[ResourceSample]:
        try:
            with self._client() as client:
                samples = []
                for name in self.container_names():
                    response = client.get(f"/containers/{name}/stats", params={"stream": "false"})
                    if response.status_code != 200:
                        continue
                    stats = response.json()
                    samples.append(
                        ResourceSample(
                            elapsed_seconds=round(elapsed, 3),
                            container=name,
                            memory_bytes=working_set_bytes(stats),
                            memory_limit_bytes=float(
                                (stats.get("memory_stats") or {}).get("limit") or 0.0
                            ),
                            cpu_percent=round(cpu_percent(stats), 2),
                        )
                    )
                return samples
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"docker engine stats failed: {exc}") from exc


class ResourceWatcher:
    """Samples container memory and CPU on a background thread while load runs.

    Its own thread rather than folded into the concurrency watcher: the Engine
    holds a stats request open for about a second to compute its interval, and
    blocking the governor sampler on that would stretch the very intervals the
    service time is derived from.
    """

    def __init__(self, probe: DockerEngineProbe, poll_interval_seconds: float = 5.0) -> None:
        self._probe = probe
        self._poll_interval = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[ResourceSample] = []
        self._started_at: float | None = None
        self.errors: list[str] = []

    @property
    def samples(self) -> tuple[ResourceSample, ...]:
        return tuple(self._samples)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("ResourceWatcher already started")
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._loop, name="resource-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=self._probe.timeout_seconds + 5)
        self._thread = None

    def _loop(self) -> None:
        self._sample()
        while not self._stop.wait(self._poll_interval):
            self._sample()

    def _sample(self) -> None:
        elapsed = time.monotonic() - (self._started_at or time.monotonic())
        try:
            self._samples.extend(self._probe.read(elapsed))
        except RuntimeError as exc:
            # One failed snapshot costs one reading; a dead watcher costs the
            # rest of the run.
            self.errors.append(str(exc))

    def write_series(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "errors": list(self.errors),
            "samples": [
                {
                    "elapsed_seconds": sample.elapsed_seconds,
                    "container": sample.container,
                    "memory_bytes": sample.memory_bytes,
                    "memory_limit_bytes": sample.memory_limit_bytes,
                    "cpu_percent": sample.cpu_percent,
                }
                for sample in self._samples
            ],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass
class ResourceWatcherGroup:
    """Adapts the resource watcher to the start/stop bracket `RunK6Task` drives,
    writing its series as soon as the load stops so a later failure cannot lose
    it."""

    watcher: ResourceWatcher
    series_path: Path
    inner: Any = field(default=None)

    def start(self) -> None:
        self.watcher.start()
        if self.inner is not None:
            self.inner.start()

    def stop(self) -> None:
        if self.inner is not None:
            self.inner.stop()
        self.watcher.stop()
        self.watcher.write_series(self.series_path)
