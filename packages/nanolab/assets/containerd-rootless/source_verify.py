#!/usr/bin/env python3
"""Verify rsynced feature-source inputs after builds, without a remote `.git`."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import zlib
from pathlib import Path


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def verify(root: Path, encoded: str) -> dict[str, object]:
    """Return the actual remote entry digest only when every input matches."""
    if not root.is_absolute() or not root.is_dir() or len(encoded) > 65536:
        raise ValueError("invalid source root or batch budget")
    raw = zlib.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) > 1024 * 1024:
        raise ValueError("source batch exceeds byte budget")
    entries = json.loads(raw)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        raise ValueError("source batch has invalid entry count")
    observed = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise TypeError("invalid source entry")
        relative = Path(entry["path"])
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("source path escapes staged checkout")
        path = root
        for component in relative.parts[:-1]:
            path = path / component
            if path.is_symlink():
                raise ValueError("source parent is a symbolic link")
        path = path / relative.parts[-1]
        if (
            entry.get("kind") == "deleted"
            and not path.exists()
            and not path.is_symlink()
        ):
            actual = {
                "path": entry["path"],
                "kind": "deleted",
                "mode": 0,
                "size_bytes": 0,
                "sha256": "",
                "link_target": None,
            }
        elif path.is_symlink():
            target = os.readlink(path)  # noqa: PTH115 -- hash the raw, unnormalized link target
            encoded_target = os.fsencode(target)
            actual = {
                "path": entry["path"],
                "kind": "symlink",
                "mode": stat.S_IMODE(path.lstat().st_mode),
                "size_bytes": len(encoded_target),
                "sha256": hashlib.sha256(encoded_target).hexdigest(),
                "link_target": target,
            }
        elif path.is_file():
            actual = {
                "path": entry["path"],
                "kind": "file",
                "mode": stat.S_IMODE(path.stat().st_mode),
                "size_bytes": path.stat().st_size,
                "sha256": _digest(path),
                "link_target": None,
            }
        else:
            raise ValueError(f"staged source input missing: {entry['path']}")
        if actual != entry:
            raise ValueError(f"staged source input changed: {entry['path']}")
        observed.append(actual)
    body = json.dumps(observed, sort_keys=True, separators=(",", ":")).encode()
    return {"count": len(observed), "sha256": hashlib.sha256(body).hexdigest()}


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(sys.argv[1]), sys.argv[2])))
    except (OSError, ValueError, TypeError, IndexError, zlib.error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
