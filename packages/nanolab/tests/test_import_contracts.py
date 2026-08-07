from __future__ import annotations

import ast
from pathlib import Path
import subprocess


def test_controlplane_import_contracts_pass() -> None:
    tool_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=tool_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_product_has_no_dead_workflow_engine_routing() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "nanolab"
    product = ast.parse((source_root / "cli" / "product.py").read_text(encoding="utf-8"))
    tui = ast.parse((source_root / "tui" / "app.py").read_text(encoding="utf-8"))

    product_functions = {
        node.name for node in ast.walk(product) if isinstance(node, ast.FunctionDef)
    }
    tui_imports = {
        alias.name
        for node in ast.walk(tui)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert product_functions.isdisjoint({"uses_sonata", "_render", "_slice"})
    assert "uses_sonata" not in tui_imports
