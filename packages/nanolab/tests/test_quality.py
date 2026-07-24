from __future__ import annotations

import sys

from nanolab.devtools import quality


def test_quality_gate_includes_console_entrypoint_import_smoke() -> None:
    entrypoint_check = dict(quality.CHECKS)["entrypoint-imports"]

    assert entrypoint_check[0] == sys.executable
    assert "nanolab.app.main" in entrypoint_check[-1]
    assert "nanolab.cli.product" in entrypoint_check[-1]
    assert "nanolab.tui.app" in entrypoint_check[-1]
