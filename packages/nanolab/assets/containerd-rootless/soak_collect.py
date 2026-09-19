#!/usr/bin/env python3
"""Read one owned rootless containerd soak process on the stack machine.

This helper is deliberately read-only.  The existing session script resolves
function ownership; this script resolves the live PID and cgroup, then checks
the same identity again after reading.  It never asks Docker for runtime data.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


def command(*argv: str, timeout: float = 5) -> str:
    """Run an external command with bounded execution time."""
    result = subprocess.run(
        argv, text=True, capture_output=True, timeout=timeout, check=True
    )
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("containerd observation exceeds output limit")
    return result.stdout


def bounded(path: Path, limit: int = 1024 * 1024) -> str:
    """Read a file while enforcing the byte limit."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"observation exceeds size limit: {path}")
    return data.decode()


def digest(path: Path) -> str:
    """Hash a file incrementally with SHA256."""
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            sha.update(block)
    return "sha256:" + sha.hexdigest()


def start_ticks(pid: int) -> str:
    """Read the process start time from procfs."""
    stat = bounded(Path("/proc") / str(pid) / "stat", 4096)
    end = stat.rfind(")")
    fields = stat[end + 1 :].split()
    if end < 0 or len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("invalid process start ticks")
    return fields[19]


def task_pid(socket: str, namespace: str, identifier: str) -> int:
    """Find the PID of the owned containerd task."""
    output = command(
        "ctr", "--address", socket, "--namespace", namespace, "tasks", "list"
    )
    rows = [line.split() for line in output.splitlines()]
    matches = [row for row in rows[1:] if len(row) >= 3 and row[0] == identifier]
    if len(matches) != 1 or not matches[0][1].isdigit() or matches[0][2] != "RUNNING":
        raise ValueError("owned containerd task is absent or not running")
    return int(matches[0][1])


def process_environment(pid: int) -> dict[str, str]:
    """Read the environment of the selected process."""
    return dict(
        item.split("=", 1)
        for item in bounded(Path("/proc") / str(pid) / "environ").split("\0")
        if "=" in item
    )


def run_paths(run_id: str) -> tuple[Path, str, str]:
    """Resolve paths and runtime identifiers for a validated run ID."""
    if re.fullmatch(r"[a-z0-9-]{1,48}", run_id) is None:
        raise ValueError("invalid run ID")
    home = Path(os.environ["HOME"])
    state = home / ".local/share/nanolab/containerd-rootless" / run_id
    socket = f"/run/user/{os.getuid()}/containerd/containerd.sock"
    return state, socket, "nanofaas-" + run_id


def host_platform() -> str:
    """Return the supported Linux platform of this host."""
    machine = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if platform.system() != "Linux" or machine is None:
        raise ValueError("unsupported control-plane host architecture")
    return "linux/" + machine


def inspect(run_id: str, role: str, repo_root: Path, script: Path) -> dict:
    """Inspect the process or container owned by this run."""
    state, socket, namespace = run_paths(run_id)
    if role == "control-plane":
        env_file = state / "control-plane.env"
        values = dict(
            line.split("=", 1)
            for line in bounded(env_file, 65536).splitlines()
            if "=" in line
        )
        artifact = Path(values["NANOFAAS_CONTROL_PLANE_ARTIFACT"])
        mode = values["NANOFAAS_CONTROL_PLANE_MODE"]
        if not artifact.is_absolute() or mode not in {"jvm", "native"}:
            raise ValueError("owned control-plane artifact declaration is invalid")
        pid_text = command(
            "systemctl",
            "--user",
            "show",
            "--property=MainPID",
            "--value",
            f"nanofaas-{run_id}.service",
        ).strip()
        if not pid_text.isdigit() or int(pid_text) <= 0:
            raise ValueError("owned control-plane service is not running")
        pid = int(pid_text)
        argv = bounded(Path("/proc") / str(pid) / "cmdline", 65536).split("\0")
        if mode == "jvm" and str(artifact) not in argv:
            raise ValueError("running JVM did not launch the declared artifact")
        if mode == "native" and digest(Path("/proc") / str(pid) / "exe") != digest(
            artifact
        ):
            raise ValueError("running native executable differs from owned artifact")
        return {
            "role": role,
            "container_id": "systemd:" + run_id,
            "process_id": pid,
            "process_started_at": start_ticks(pid),
            "image_digest": digest(artifact),
            "runtime": mode,
            "platform": host_platform(),
            "artifact_path": str(artifact),
            "artifact_kind": "process",
        }
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", role) is None:
        raise ValueError("invalid function role")
    payload = json.loads(
        command(
            "bash",
            str(script),
            "inspect-owned",
            run_id,
            str(repo_root),
            role,
            "1",
        )
    )
    identifier = payload.get("ID", payload.get("id"))
    image = payload.get("Image", payload.get("image"))
    if (
        not isinstance(identifier, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", identifier) is None
    ):
        raise ValueError("containerd returned no immutable owned ID")
    if not isinstance(image, str) or not image:
        raise ValueError("owned container has no image reference")
    pid = task_pid(socket, namespace, identifier)
    image_info = json.loads(
        command("nerdctl", "--namespace", namespace, "image", "inspect", image)
    )
    if not isinstance(image_info, list) or len(image_info) != 1:
        raise ValueError("containerd image inspection is ambiguous")
    digests = image_info[0].get("RepoDigests", [])
    matches = [item for item in digests if isinstance(item, str) and "@sha256:" in item]
    if len(matches) != 1:
        raise ValueError("owned function image digest is absent or ambiguous")
    architecture = image_info[0].get("Architecture")
    operating_system = image_info[0].get("Os")
    if operating_system != "linux" or architecture not in {"arm64", "amd64"}:
        raise ValueError("owned function image platform is unavailable")
    executable = (Path("/proc") / str(pid) / "exe").readlink().name
    runtime = (
        "jvm" if executable == "java" else "node" if executable == "node" else "native"
    )
    return {
        "role": role,
        "container_id": identifier,
        "process_id": pid,
        "process_started_at": start_ticks(pid),
        "image_digest": matches[0],
        "runtime": runtime,
        "platform": f"linux/{architecture}",
        "artifact_kind": "oci-image",
    }


def cgroup(pid: int) -> tuple[Path, dict]:
    """Read the unified cgroup path and resource configuration."""
    lines = bounded(Path("/proc") / str(pid) / "cgroup", 65536).splitlines()
    paths = [line.removeprefix("0::") for line in lines if line.startswith("0::")]
    if len(paths) != 1 or ".." in Path(paths[0]).parts:
        raise ValueError("process has no unambiguous cgroup v2 path")
    root = Path("/sys/fs/cgroup") / paths[0].lstrip("/")
    memory_max = bounded(root / "memory.max", 128).strip()
    raw_stat = bounded(root / "memory.stat", 65536)
    stats = {
        key: int(value)
        for key, value in (line.split() for line in raw_stat.splitlines())
    }
    return root, {
        "memory_current": int(bounded(root / "memory.current", 128)),
        "memory_max": None if memory_max == "max" else int(memory_max),
        "memory_stat": stats,
    }


def exposition(target: dict, timeout: float) -> str:
    """Collect the target metrics with a bounded timeout."""
    if target["role"] == "control-plane":
        url = "http://127.0.0.1:8081/actuator/prometheus"
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(url), timeout=timeout) as response:
            if response.status != 200:
                raise ValueError("management exposition unavailable")
            body = response.read(1024 * 1024 + 1)
    else:
        body = subprocess.run(
            (
                "nsenter",
                "--target",
                str(target["process_id"]),
                "--user",
                "--preserve-credentials",
                "--net",
                "--",
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                str(timeout),
                "http://127.0.0.1:8080/metrics",
            ),
            capture_output=True,
            timeout=timeout + 1,
            check=True,
        ).stdout
    if len(body) > 1024 * 1024:
        raise ValueError("exposition exceeds collection limit")
    return body.decode()


def sample(
    run_id: str,
    role: str,
    repo_root: Path,
    script: Path,
    expected: dict,
    timeout: float,
) -> dict:
    """Collect a sample bound to the same process identity."""
    before = inspect(run_id, role, repo_root, script)
    if before != expected:
        raise ValueError("owned process changed before collection")
    pid = before["process_id"]
    proc = Path("/proc") / str(pid)
    result = {
        "before": {
            **before,
            "running": True,
            "image_digests": [before["image_digest"]],
        },
        "procfs": {
            "status": bounded(proc / "status"),
            "smaps_rollup": bounded(proc / "smaps_rollup"),
        },
        "errors": {},
    }
    root, result["cgroup"] = cgroup(pid)
    cpu = bounded(root / "cpu.max", 128).split()
    memory = result["cgroup"]["memory_max"]
    result["configuration"] = {
        "cpu_max": cpu,
        "cpuset": bounded(root / "cpuset.cpus.effective", 4096).strip(),
        "memory_bytes": memory,
        "limit_sources": {
            "cpu": str(root / "cpu.max"),
            "memory_bytes": str(root / "memory.max"),
        },
        "runtime": before["runtime"],
        "runtime_options": process_environment(pid).get("JAVA_TOOL_OPTIONS", "").split()
        if before["runtime"] == "jvm"
        else process_environment(pid).get("NODE_OPTIONS", "").split(),
        "capabilities": [
            "procfs",
            "cgroup-v2",
            "systemd" if role == "control-plane" else "containerd",
        ],
        "collection_sources": ["procfs", "cgroup-v2"],
    }
    try:
        result["exposition"] = exposition(before, timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["errors"]["exposition"] = f"{type(error).__name__}: {error}"
    after = inspect(run_id, role, repo_root, script)
    if after != before:
        raise ValueError("owned process changed during collection")
    result["after"] = {
        **after,
        "running": True,
        "image_digests": [after["image_digest"]],
    }
    return result


def main(argv: list[str]) -> int:
    """Dispatch the requested inspection or sampling command."""
    action, run_id, role, repo_text, script_text, *rest = argv
    root = Path(repo_text)
    script = Path(script_text)
    if not root.is_absolute() or not script.is_absolute():
        raise ValueError("repository and session paths must be absolute")
    if action == "inspect" and not rest:
        value = inspect(run_id, role, root, script)
    elif action == "sample" and len(rest) == 2:
        value = sample(run_id, role, root, script, json.loads(rest[0]), float(rest[1]))
    else:
        raise ValueError("invalid collector arguments")
    print(json.dumps(value, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
