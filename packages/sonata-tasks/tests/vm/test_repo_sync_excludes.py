"""What the repo sync ships, checked by running rsync rather than re-reading the tuple.

The sync used to carry a hand-written copy of .gitignore, and the copy drifted:
it listed `target/`, `out/` and `dist/` but not `build/`, so 2.77 GB went over
the wire where 44 MB was needed. It now asks rsync to read the repo's own
.gitignore files. That is only true if rsync actually honours them, which is
what this checks - on a real tree, with the real command.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sonata_tasks.vm.multipass import repo_rsync_command


# One file per decision worth making, and the .gitignore that should decide it.
KEEP = (
    "settings.gradle.kts",
    "platform/control-plane/src/main/java/App.java",
    "docs/experiments/baseline/README.md",
    "experiments/control-plane-staging/versions/v1/version.yaml",
)
DROP = (
    # Gitignored: the VM regenerates all of it.
    "platform/control-plane/build/libs/control-plane.jar",
    "build/reports/index.html",
    "runtimes/watchdog/target/debug/watchdog",
    "tools/controlplane/.venv/bin/python",
    "experiments/control-plane-staging/versions/v1/snapshot/target/debug/binary",
    # Tracked on purpose, but useless to a VM that compiles: excluded explicitly.
    "docs/experiments/baseline/raw/A1/metrics/snapshot.json.gz",
    # A level deeper: an archived campaign. rsync's `*` stops at a slash, so
    # the first version of this exclusion missed exactly this shape and shipped
    # 28 MB of it.
    "docs/experiments/archive/dispatch-bottleneck/raw/A/snapshot.json.gz",
)
GITIGNORE = "build/\ntarget/\n.venv/\n"


def _tree(root: Path) -> None:
    (root / ".gitignore").write_text(GITIGNORE)
    for name in KEEP + DROP:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_the_sync_ships_sources_and_not_what_git_ignores(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    _tree(source)

    command = repo_rsync_command(
        source=source, user="u", host="h", destination="/remote/nanofaas"
    )
    # Same argv the provisioning uses, minus the remote: run it dry and locally
    # so the assertion is about the filters and nothing else.
    local = [*command[:-1], "--dry-run", "--out-format=%n", f"{tmp_path / 'dest'}/"]
    result = subprocess.run(local, capture_output=True, text=True, check=True)
    shipped = {line for line in result.stdout.splitlines() if not line.endswith("/")}

    for kept in KEEP:
        assert kept in shipped, f"the sync would no longer ship {kept}"
    for dropped in DROP:
        assert dropped not in shipped, f"the sync would still ship {dropped}"


def test_the_command_reads_gitignore_per_directory() -> None:
    command = repo_rsync_command(
        source=Path("/src"), user="u", host="h", destination="/remote"
    )
    # A dir-merge rule, not a single top-level exclude: nested .gitignore files
    # are most of what a monorepo relies on.
    assert "--filter" in command
    assert command[command.index("--filter") + 1].startswith(":-")
