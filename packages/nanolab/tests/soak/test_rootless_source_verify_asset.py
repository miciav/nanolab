"""Verify the deployed checkout without assuming rsync transferred `.git`."""

import base64
import json
import stat
import subprocess
import sys
import zlib
from hashlib import sha256
from pathlib import Path

ASSET = Path(__file__).parents[2] / "assets/containerd-rootless/source_verify.py"


def _check(root, entries):
    payload = base64.b64encode(zlib.compress(json.dumps(entries).encode())).decode()
    return subprocess.run(
        [sys.executable, str(ASSET), str(root), payload],
        text=True,
        capture_output=True,
        check=False,
    )


def test_verified_source_detects_changed_or_missing_remote_inputs(tmp_path):
    source = tmp_path / "src.txt"
    source.write_text("feature source")
    entries = [
        {
            "path": "src.txt",
            "kind": "file",
            "mode": stat.S_IMODE(source.stat().st_mode),
            "size_bytes": source.stat().st_size,
            "sha256": sha256(source.read_bytes()).hexdigest(),
            "link_target": None,
        }
    ]
    assert json.loads(_check(tmp_path, entries).stdout)["count"] == 1
    source.write_text("other source")
    assert _check(tmp_path, entries).returncode != 0
    source.unlink()
    assert _check(tmp_path, entries).returncode != 0
