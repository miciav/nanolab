"""What the repo sync ships, checked against rsync itself.

Asserting that a pattern is in the tuple would only restate the tuple. What can
actually be wrong is whether rsync AGREES: `docs/experiments/*/raw/` and
`experiments/control-plane-staging/versions/*/snapshot/` have wildcards in the
middle, and a pattern that silently matches nothing looks exactly like a pattern
that works until a 2.1 GB directory goes over the wire.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sonata_tasks.vm.multipass import REPO_SYNC_EXCLUDE_PATTERNS


def _tree(root: Path) -> None:
    """A miniature of the checkout, with one file per thing worth deciding on."""
    files = {
        # Kept: this is what the VM actually builds from.
        "platform/control-plane/src/main/java/App.java": "keep",
        "settings.gradle.kts": "keep",
        "docs/experiments/baseline/README.md": "keep",
        "experiments/control-plane-staging/versions/v1/version.yaml": "keep",
        # Dropped: regenerated on the VM, or irrelevant to building.
        "platform/control-plane/build/libs/control-plane.jar": "drop",
        "build/reports/index.html": "drop",
        "functions/java/word-stats/build/classes/Main.class": "drop",
        "docs/experiments/baseline/raw/A1/metrics/snapshot.json.gz": "drop",
        "experiments/control-plane-staging/versions/v1/snapshot/binary": "drop",
        "runtimes/watchdog/target/debug/watchdog": "drop",
        "tools/controlplane/.venv/bin/python": "drop",
    }
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_the_sync_ships_sources_and_not_build_output(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    _tree(source)

    result = subprocess.run(
        [
            "rsync", "-an", "--out-format=%n", "--delete", "--delete-excluded",
            *(f"--exclude={pattern}" for pattern in REPO_SYNC_EXCLUDE_PATTERNS),
            f"{source}/", str(tmp_path / "dest") + "/",
        ],
        capture_output=True, text=True, check=True,
    )
    shipped = {line for line in result.stdout.splitlines() if not line.endswith("/")}

    assert "platform/control-plane/src/main/java/App.java" in shipped
    assert "settings.gradle.kts" in shipped
    assert "docs/experiments/baseline/README.md" in shipped
    # The metadata beside a staging snapshot is tracked and stays.
    assert "experiments/control-plane-staging/versions/v1/version.yaml" in shipped

    for dropped in (
        "platform/control-plane/build/libs/control-plane.jar",
        "build/reports/index.html",
        "docs/experiments/baseline/raw/A1/metrics/snapshot.json.gz",
        "experiments/control-plane-staging/versions/v1/snapshot/binary",
        "runtimes/watchdog/target/debug/watchdog",
        "tools/controlplane/.venv/bin/python",
    ):
        assert dropped not in shipped, f"the sync would still ship {dropped}"
