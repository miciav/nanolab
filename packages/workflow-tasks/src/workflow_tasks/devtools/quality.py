from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MEMBER_ROOT = Path(__file__).resolve().parents[3]

CHECKS = (
    ("ruff", ["ruff", "check", "."]),
    ("basedpyright", ["basedpyright"]),
    ("import-linter", ["lint-imports", "--config", ".importlinter", "--no-cache"]),
)


def main() -> None:
    failures: list[str] = []
    for name, command in CHECKS:
        completed = subprocess.run(command, check=False, cwd=MEMBER_ROOT)
        if completed.returncode != 0:
            failures.append(name)
    if failures:
        raise SystemExit(f"Quality checks failed: {', '.join(failures)}")
    sys.stdout.write("Quality checks passed\n")
