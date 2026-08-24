from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "run-matrix.sh"


def _executable(path: Path, body: str) -> None:
    _ = path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _run_matrix(
    tmp_path: Path, *, fail_all: bool = False
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    script = tmp_path / "run-matrix.sh"
    source = SCRIPT.read_text().replace(
        "cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1",
        'cd "$NANOLAB_ROOT" || exit 1',
    )
    _ = script.write_text(source)
    script.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "events.log"
    _executable(
        bin_dir / "caffeinate",
        "\n".join(
            (
                'echo caffeinate-start >> "$EVENT_LOG"',
                "shift",
                '"$@"',
                "rc=$?",
                'echo caffeinate-end >> "$EVENT_LOG"',
                "exit $rc",
            )
        ),
    )
    _executable(bin_dir / "az", 'printf "%s\\n" 198.51.100.42\n')
    _executable(bin_dir / "ssh", 'echo "ssh $*" >> "$EVENT_LOG"\n')
    _executable(
        tmp_path / "nanolab.sh",
        "\n".join(
            (
                'echo "compare $*" >> "$EVENT_LOG"',
                '[ "${FAIL_ALL:-0}" = 1 ] && exit 23',
                "while [ $# -gt 0 ]; do",
                '  [ "$1" = --run-dir ] && { run_dir=$2; break; }',
                "  shift",
                "done",
                "for repetition in 1 2 3; do",
                '  mkdir -p "$run_dir/jvm-c2/run-$repetition"',
                '  : > "$run_dir/jvm-c2/run-$repetition/k6-summary.json"',
                "done",
            )
        ),
    )
    _executable(tmp_path / "teardown.sh", 'echo teardown >> "$EVENT_LOG"\n')

    env = os.environ | {
        "EVENT_LOG": str(log),
        "MATRIX_ID": "current",
        "NANOLAB_ROOT": str(tmp_path),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    if fail_all:
        env["FAIL_ALL"] = "1"
    result = subprocess.run(
        [str(bin_dir / "caffeinate"), "-dimsu", str(script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log.read_text().splitlines()


def test_matrix_resolves_the_current_stack_ip(tmp_path: Path) -> None:
    result, events = _run_matrix(tmp_path)

    assert result.returncode == 0
    ssh_event = next(event for event in events if event.startswith("ssh "))
    assert "azureuser@198.51.100.42" in ssh_event


def test_failed_compare_propagates_and_does_not_teardown_historical_runs(
    tmp_path: Path,
) -> None:
    for repetition in range(1, 13):
        historical = tmp_path / f"packages/nanolab/runs/azure-matrix-old/run-{repetition}"
        historical.mkdir(parents=True)
        (historical / "k6-summary.json").touch()

    result, events = _run_matrix(tmp_path, fail_all=True)

    assert result.returncode == 23
    assert "teardown" not in events


def test_current_twelve_cells_teardown_despite_historical_runs(tmp_path: Path) -> None:
    historical = tmp_path / "packages/nanolab/runs/azure-matrix-old/run-1"
    historical.mkdir(parents=True)
    (historical / "k6-summary.json").touch()

    result, events = _run_matrix(tmp_path)

    assert result.returncode == 0
    assert "teardown" in events


def test_caffeinate_covers_synchronous_teardown(tmp_path: Path) -> None:
    result, events = _run_matrix(tmp_path)

    assert result.returncode == 0
    assert events[0] == "caffeinate-start"
    assert events[-2:] == ["teardown", "caffeinate-end"]
