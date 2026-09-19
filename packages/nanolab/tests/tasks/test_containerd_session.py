"""Run the rootless cleanup script against isolated command and home fixtures."""

import json
import os
import shutil
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest
from sonata_engine import TaskInputs
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult

from nanolab.tasks.containerd_rootless import RootlessRun
from nanolab.tasks.resources import ContainerdResourceCheckTask

SESSION = Path(__file__).resolve().parents[2] / "assets/containerd-rootless/session.sh"


@pytest.fixture
def session_home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    binaries = tmp_path / "bin"
    home.mkdir()
    binaries.mkdir()
    scripts = {
        "getent": (
            '#!/bin/sh\nprintf "tester:x:1000:1000::%s:/bin/bash\\n" "$TEST_HOME"\n'
        ),
        "rootlessctl": (
            '#!/bin/sh\nfor last do :; done\ncase " $* " in\n'
            '  *" add-ports "*)\n'
            '    case "$last" in\n'
            "      *:8080:*) echo 42 ;;\n"
            "      *:8081:*) echo 43 ;;\n"
            "      *) echo 41 ;;\n"
            "    esac ;;\n"
            '  *" list-ports "*) cat "$TEST_PORTS" ;;\n'
            '  *" remove-ports "*) printf "port %s\\n" "$last" >> "$TEST_LOG" ;;\n'
            "  *) exit 2 ;;\nesac\n"
        ),
        "nerdctl": (
            '#!/bin/sh\nfor last do :; done\ncase " $* " in\n'
            '  *" ps "*) printf "owned-container\\n" ;;\n'
            '  *" rm "*) printf "container %s\\n" "$last" >> "$TEST_LOG" ;;\n'
            "  *) exit 2 ;;\nesac\n"
        ),
        "systemctl": '#!/bin/sh\nprintf "systemctl %s\\n" "$*" >> "$TEST_LOG"\n',
        "curl": "#!/bin/sh\nexit 0\n",
        "ctr": (
            '#!/bin/sh\nfor last do :; done\ncase " $* " in\n'
            '  *" containers list --quiet "*) cat "$TEST_CTR_DIR/ids" ;;\n'
            '  *" containers info "*)\n'
            '    printf "inspect %s\\n" "$last" >> "$TEST_LOG"\n'
            '    cat "$TEST_CTR_DIR/$last.json" ;;\n'
            "  *) exit 2 ;;\nesac\n"
        ),
    }
    for name, script in scripts.items():
        target = binaries / name
        target.write_text(script)
        target.chmod(0o755)
    ports = tmp_path / "ports"
    ports.write_text(
        "ID PROTO PARENTIP PARENTPORT CHILDPORT\n"
        "41 tcp 127.0.0.1 5000 5000\n"
        "42 tcp 0.0.0.0 8080 8080\n"
        "43 tcp 0.0.0.0 8081 8081\n"
        "44 tcp 0.0.0.0 9090 9090\n"
        "99 tcp 0.0.0.0 9999 9999\n"
    )
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "TEST_HOME": str(home),
        "TEST_PORTS": str(ports),
        "TEST_LOG": str(tmp_path / "commands.log"),
        "TEST_CTR_DIR": str(tmp_path / "ctr"),
    }
    return home, env


def _run(
    action: str, home: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(SESSION), action, "run123", str(home), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _container(
    directory: Path,
    identifier: str,
    *,
    function: str,
    replica: str = "1",
    backend: str = "containerd",
    managed: str = "true",
) -> None:
    (directory / f"{identifier}.json").write_text(
        json.dumps(
            {
                "ID": identifier,
                "Labels": {
                    "io.nanofaas.backend": backend,
                    "io.nanofaas.managed": managed,
                    "io.nanofaas.function": function,
                    "io.nanofaas.replica": replica,
                },
                "Spec": {"linux": {"resources": {}}},
            }
        )
    )


def test_normal_release_removes_only_owned_ports_and_unit(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    state = home / ".local/share/nanolab/containerd-rootless/run123"
    state.mkdir(parents=True)
    unit = home / ".config/systemd/user/nanofaas-run123.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("owned")
    for name, port in {
        "registry": 41,
        "api": 42,
        "management": 43,
        "prometheus": 44,
    }.items():
        (state / f"port-{name}").write_text(f"{port}\n")
    for name in (
        "registry-owned",
        "prometheus-owned",
        "control-plane.env",
        "prometheus.yml",
    ):
        (state / name).touch()

    for action in ("prometheus-stop", "control-stop", "registry-stop"):
        _run(action, home, env)

    assert not unit.exists()
    assert not any(state.iterdir())
    log = Path(env["TEST_LOG"]).read_text()
    assert [line for line in log.splitlines() if line.startswith("port ")] == [
        "port 44",
        "port 43",
        "port 42",
        "port 41",
    ]
    assert "port 99" not in log


def test_partial_control_acquisition_releases_existing_api_port(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    state = home / ".local/share/nanolab/containerd-rootless/run123"
    state.mkdir(parents=True)
    (state / "port-api").write_text("42\n")
    (state / "control-plane.env").touch()
    unit = home / ".config/systemd/user/nanofaas-run123.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("partially started")

    _run("control-stop", home, env)

    assert not unit.exists()
    assert not (state / "control-plane.env").exists()
    assert not (state / "port-api").exists()
    assert [
        line
        for line in Path(env["TEST_LOG"]).read_text().splitlines()
        if line.startswith("port ")
    ] == ["port 42"]


def test_inspect_owned_resolves_hashed_id_and_ignores_foreign_container(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    directory = Path(env["TEST_CTR_DIR"])
    directory.mkdir()
    owned = "nanofaas-word-stats-java-0123456789-r1"
    foreign = ("wrong-backend", "unmanaged", "wrong-function", "wrong-replica")
    (directory / "ids").write_text("".join(f"{name}\n" for name in (*foreign, owned)))
    _container(
        directory, foreign[0], function="word-stats-java", backend="container-local"
    )
    _container(directory, foreign[1], function="word-stats-java", managed="false")
    _container(directory, foreign[2], function="another-function")
    _container(directory, foreign[3], function="word-stats-java", replica="2")
    _container(directory, owned, function="word-stats-java")

    result = _run("inspect-owned", home, env, "word-stats-java", "1")

    assert json.loads(result.stdout)["ID"] == owned
    assert f"inspect {owned}" in Path(env["TEST_LOG"]).read_text()


def test_resource_task_inspects_the_id_resolved_from_owned_labels(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    directory = Path(env["TEST_CTR_DIR"])
    directory.mkdir()
    owned = "nanofaas-word-stats-java-0123456789-r1"
    (directory / "ids").write_text(f"{owned}\n")
    _container(directory, owned, function="word-stats-java")

    class ShellExecutor:
        def binding_key(self, role: str) -> str:
            return f"session:{role}"

        def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
            result = subprocess.run(
                task.argv, env=env, capture_output=True, text=True, check=False
            )
            return TaskResult(
                task_id=task.task_id,
                status="passed" if result.returncode == 0 else "failed",
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

    task = ContainerdResourceCheckTask(
        function="word-stats-java",
        replica=1,
        resources=None,
        run=RootlessRun("run123", home, SESSION),
        executor=ShellExecutor(),
        role="stack",
    )

    task.run(TaskInputs.empty())

    assert Path(env["TEST_LOG"]).read_text().splitlines() == [f"inspect {owned}"]


def test_control_restart_keeps_run_owned_registry_and_provider_state(
    session_home: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home, env = session_home
    runner = tmp_path / "runner"
    runner.mkdir()
    script = runner / "session.sh"
    script.write_bytes(SESSION.read_bytes())
    (runner / "provision.sh").write_text("#!/bin/sh\nexit 0\n")
    repo = tmp_path / "repo"
    template = repo / "deploy/containerd-rootless/nanofaas.service"
    template.parent.mkdir(parents=True)
    template.write_text(
        "[Service]\nEnvironmentFile=@NANOLAB_ENV_FILE@\n"
        "ExecStart=@NANOFAAS_ROOT@/start-control-plane.sh\n"
    )

    for action in ("control-start", "control-restart"):
        result = subprocess.run(
            ["bash", str(script), action, "run123", str(repo)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    state = home / ".local/share/nanolab/containerd-rootless/run123"
    values = dict(
        line.split("=", 1)
        for line in (state / "control-plane.env").read_text().splitlines()
    )
    assert values["NANOFAAS_REGISTRY_PATH"] == str(state / "functions.json")
    assert values["NANOFAAS_CONTAINERD_STATEDIRECTORY"] == str(state / "containerd")
    assert values["NANOFAAS_CONTAINERD_CNICACHEDIRECTORY"] == str(state / "cni-cache")
    assert (state / "containerd").is_dir()
    assert (state / "cni-cache").is_dir()


def test_control_restart_retries_connection_reset_until_ready(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    unit = home / ".config/systemd/user/nanofaas-run123.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("owned")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    attempts = []

    def serve() -> None:
        try:
            for attempt in range(2):
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    attempts.append(attempt)
                    if attempt == 0:
                        connection.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_LINGER,
                            struct.pack("ii", 1, 0),
                        )
                    else:
                        connection.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                        )
        except TimeoutError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    binary = Path(env["PATH"].split(":", 1)[0]) / "curl"
    binary.write_text(
        '#!/bin/bash\nargs=("$@")\nargs[-1]=$TEST_CURL_URL\n'
        'exec "$TEST_REAL_CURL" "${args[@]}"\n'
    )
    binary.chmod(0o755)
    env["TEST_REAL_CURL"] = shutil.which("curl") or "curl"
    env["TEST_CURL_URL"] = f"http://127.0.0.1:{listener.getsockname()[1]}/ready"
    try:
        result = subprocess.run(
            ["bash", str(SESSION), "control-restart", "run123", str(home)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        thread.join(timeout=4)
        listener.close()

    assert result.returncode == 0, result.stderr
    assert attempts == [0, 1]


def test_soak_control_start_sets_actual_artifact_limits_and_cleans_owned_dropin(
    session_home: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    home, env = session_home
    runner = tmp_path / "runner"
    runner.mkdir()
    script = runner / "session.sh"
    script.write_bytes(SESSION.read_bytes())
    (runner / "provision.sh").write_text("#!/bin/sh\nexit 0\n")
    repo = tmp_path / "repo"
    template = repo / "deploy/containerd-rootless/nanofaas.service"
    template.parent.mkdir(parents=True)
    template.write_text(
        "[Service]\nEnvironmentFile=@NANOLAB_ENV_FILE@\nExecStart=@NANOFAAS_ROOT@/start-control-plane.sh\n"
    )
    artifact = repo / "platform/control-plane/build/libs/app.jar"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"actual jar")
    result = subprocess.run(
        [
            "bash",
            str(script),
            "control-start",
            "run123",
            str(repo),
            "0",
            "",
            "jvm",
            str(artifact),
            "2",
            "1073741824",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    state = home / ".local/share/nanolab/containerd-rootless/run123"
    values = dict(
        line.split("=", 1)
        for line in (state / "control-plane.env").read_text().splitlines()
    )
    assert values["NANOFAAS_CONTROL_PLANE_MODE"] == "jvm"
    assert values["NANOFAAS_CONTROL_PLANE_ARTIFACT"] == str(artifact)
    dropin = home / ".config/systemd/user/nanofaas-run123.service.d/limits.conf"
    assert "CPUQuota=200%" in dropin.read_text()
    assert "MemoryMax=1073741824" in dropin.read_text()
    _run("control-stop", home, env)
    assert not dropin.exists()
    assert not (state / "soak-limits-owned").exists()


@pytest.mark.parametrize("matches", [0, 2])
def test_inspect_owned_rejects_missing_or_ambiguous_matches(
    session_home: tuple[Path, dict[str, str]], matches: int
) -> None:
    home, env = session_home
    directory = Path(env["TEST_CTR_DIR"])
    directory.mkdir()
    identifiers = [
        f"nanofaas-word-stats-java-{index:010d}-r1" for index in range(matches)
    ]
    (directory / "ids").write_text(
        "".join(f"{identifier}\n" for identifier in identifiers)
    )
    for identifier in identifiers:
        _container(directory, identifier, function="word-stats-java")

    result = subprocess.run(
        [
            "bash",
            str(SESSION),
            "inspect-owned",
            "run123",
            str(home),
            "word-stats-java",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "expected exactly one" in result.stderr


def test_session_pins_native_snapshotter_for_nerdctl(
    session_home: tuple[Path, dict[str, str]],
) -> None:
    home, env = session_home
    binary = Path(env["PATH"].split(":", 1)[0]) / "nerdctl"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "${CONTAINERD_SNAPSHOTTER:-unset}"\n')
    binary.chmod(0o755)

    result = _run("managed-ids", home, env, "word-stats-java")

    assert result.stdout.strip() == "native"
