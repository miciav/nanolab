from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MEMBER_ROOT = Path(__file__).resolve().parents[3]

ENTRYPOINT_IMPORT_MODULES = (
    "nanolab.app.main",
    "nanolab.cli.product",
    "nanolab.tui.app",
)


_GRIMP_CHECK = """
import grimp

graph = grimp.build_graph("nanolab", "sonata_tasks", "tui_toolkit")
violations = []

for member in ("sonata_tasks", "tui_toolkit"):
    chain = graph.find_shortest_chain(importer=member, imported="nanolab")
    if chain:
        violations.append(f"{member} -> nanolab: {' -> '.join(chain)}")

if violations:
    for v in violations:
        print(f"VIOLATION: {v}")
    raise SystemExit(1)

print("Cross-project coupling: OK")
"""

CHECKS = (
    ("ruff", ["ruff", "check", "."]),
    ("basedpyright", ["basedpyright"]),
    ("import-linter", ["lint-imports", "--config", ".importlinter", "--no-cache"]),
    (
        "entrypoint-imports",
        [
            sys.executable,
            "-c",
            (
                "import importlib; "
                f"[importlib.import_module(name) for name in {ENTRYPOINT_IMPORT_MODULES!r}]"
            ),
        ],
    ),
    ("cross-project-coupling", [sys.executable, "-c", _GRIMP_CHECK]),
)


def main() -> None:
    failures: list[str] = []
    for name, command in CHECKS:
        completed = subprocess.run(command, check=False, cwd=MEMBER_ROOT)
        if completed.returncode != 0:
            failures.append(name)

    if failures:
        joined = ", ".join(failures)
        raise SystemExit(f"Quality checks failed: {joined}")

    sys.stdout.write("Quality checks passed\n")
