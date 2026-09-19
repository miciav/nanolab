import json
import subprocess
from dataclasses import replace

import pytest

from nanolab.tasks.soak.models import Target

DIGEST = "repo/cp@sha256:" + "a" * 64
TARGET = Target("cp", "b" * 64, 42, "started", DIGEST, "jvm")
REQUIRED = {
    "process_rss_bytes": "bytes",
    "process_pss_bytes": "bytes",
    "cgroup_memory_usage_bytes": "bytes",
    "heap_bytes": "bytes",
    "registry_meter_count": "count",
}


def observation():
    identity = {
        "container_id": TARGET.container_id,
        "process_id": 42,
        "process_started_at": "started",
        "image_digests": [DIGEST],
        "running": True,
    }
    return {
        "before": identity.copy(),
        "after": identity.copy(),
        "procfs": {"status": "VmRSS: 20 kB\n", "smaps_rollup": "Pss: 10 kB\n"},
        "stats": {
            "memory_stats": {
                "usage": 50000,
                "limit": 100000,
                "stats": {"inactive_file": 1000, "pgfault": 3},
            }
        },
        "exposition": 'heap_bytes{area="heap"} 100\nheap_bytes{area="nonheap"} 30\n',
        "errors": {},
    }


def probe_for(data=None, fail=None):
    from nanolab.tasks.soak.adapters import RoleBinding, RoleBoundProbe

    class Transport:
        def collect(self, target, endpoint, timeout_s):
            assert timeout_s == 0.25
            assert endpoint == "http://localhost:8080/metrics"
            if fail:
                raise fail
            return data if data is not None else observation()

    return RoleBoundProbe(
        (RoleBinding(TARGET, "http://localhost:8080/metrics", REQUIRED),),
        Transport(),
        timeout_s=0.25,
    )


def test_distinct_sources_and_labels():
    rows = probe_for().sample(TARGET, "drain", 100)
    assert [(r.labels, r.value) for r in rows if r.metric == "heap_bytes"] == [
        ((("area", "heap"),), 100),
        ((("area", "nonheap"),), 30),
    ]
    values = {r.metric: r.value for r in rows if not r.labels}
    assert values["process_rss_bytes"] == 20480
    assert values["process_pss_bytes"] == 10240
    assert values["cgroup_memory_usage_bytes"] == 50000
    assert values["docker_working_set_estimate_bytes"] == 49000
    assert values["registry_meter_count"] is None
    assert next(r for r in rows if r.labels == (("field", "pgfault"),)).unit == "raw"


def test_containerd_cgroup_v2_samples_keep_their_real_source():
    data = observation()
    del data["stats"]
    data["cgroup"] = {
        "memory_current": 50000,
        "memory_max": 100000,
        "memory_stat": {"inactive_file": 1000, "pgfault": 3},
    }
    rows = probe_for(data).sample(TARGET, "steady", 1)
    memory = [row for row in rows if row.metric == "cgroup_memory_usage_bytes"]
    assert len(memory) == 1
    assert memory[0].value == 50000
    assert memory[0].source == "cgroup-v2/memory.current"
    assert not any("docker-engine" in row.source for row in rows)
    assert not any(row.metric == "docker_working_set_estimate_bytes" for row in rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("container_id", "other"),
        ("process_id", 43),
        ("process_started_at", "restarted"),
        ("image_digests", ["wrong"]),
        ("running", False),
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_restart_or_wrong_identity_invalidates_entire_scrape(field, value, when):
    data = observation()
    data[when][field] = value
    rows = probe_for(data).sample(TARGET, "steady", 10)
    assert {r.metric for r in rows} == set(REQUIRED)
    assert all(r.value is None and r.availability == "unavailable" for r in rows)
    assert all("identity" in r.reason for r in rows)  # pyright: ignore[reportOperatorIssue]


@pytest.mark.parametrize("source", ["procfs", "stats", "exposition"])
def test_source_failure_is_a_gap_without_losing_other_sources(source):
    data = observation()
    del data[source]
    data["errors"][source] = "permission denied"
    rows = probe_for(data).sample(TARGET, "drain", 5)
    assert any(r.availability == "unavailable" for r in rows)
    assert any(r.availability == "observed" for r in rows)


@pytest.mark.parametrize("error", [TimeoutError("deadline"), OSError("unreachable")])
def test_transport_failure_is_explicit(error):
    rows = probe_for(fail=error).sample(TARGET, "drain", 0)
    assert len(rows) == len(REQUIRED)
    assert all(r.value is None and r.reason for r in rows)


def test_missing_pss_does_not_erase_rss():
    data = observation()
    data["procfs"]["smaps_rollup"] = None
    rows = {r.metric: r for r in probe_for(data).sample(TARGET, "steady", 0)}
    assert rows["process_rss_bytes"].value == 20480
    assert rows["process_pss_bytes"].availability == "unavailable"


def test_malformed_exposition_cannot_supply_partial_success():
    data = observation()
    data["exposition"] += "bad NaN\n"
    rows = probe_for(data).sample(TARGET, "steady", 0)
    assert all(
        r.availability == "unavailable" for r in rows if r.metric == "heap_bytes"
    )


def test_exposition_cannot_spoof_procfs():
    data = observation()
    data["exposition"] += "process_rss_bytes 0\n"
    rows = probe_for(data).sample(TARGET, "steady", 0)
    assert [r.value for r in rows if r.metric == "process_rss_bytes"] == [20480]


def test_all_roles_are_bound_independently():
    from nanolab.tasks.soak.adapters import RoleBinding, RoleBoundProbe

    roles = tuple(
        replace(TARGET, role=role) for role in ("cp", "java", "javascript", "proxy")
    )
    bindings = tuple(RoleBinding(target, None, REQUIRED) for target in roles)
    probe = RoleBoundProbe(bindings, object(), timeout_s=1)  # pyright: ignore[reportArgumentType]
    assert probe.targets() == roles
    with pytest.raises(ValueError, match="target is not bound to this probe"):
        probe.sample(replace(TARGET, role="unknown"), "steady", 0)


def test_child_transport_has_timeout_and_no_shell(monkeypatch):
    from nanolab.tasks.soak.adapters import SubprocessTransport

    def run(argv, **kwargs):
        assert argv[1:3] == ["-m", "nanolab.tasks.soak.collector"]
        assert kwargs["timeout"] == 0.2
        assert not kwargs.get("shell", False)
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(observation()).encode(), b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert SubprocessTransport().collect(TARGET, None, 0.2) == observation()


def test_child_timeout_becomes_timeout_error(monkeypatch):
    from nanolab.tasks.soak.adapters import SubprocessTransport

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TimeoutError, match="process collection deadline exceeded"):
        SubprocessTransport().collect(TARGET, None, 0.2)


def test_cgroup_invalid_value_does_not_become_zero():
    data = observation()
    data["stats"]["memory_stats"]["usage"] = -1
    rows = probe_for(data).sample(TARGET, "steady", 0)
    assert (
        next(r for r in rows if r.metric == "cgroup_memory_usage_bytes").value is None
    )
