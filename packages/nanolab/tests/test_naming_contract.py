from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_only_the_nanolab_import_package_exists() -> None:
    assert importlib.util.find_spec("nanolab") is not None
    legacy_package = "controlplane" + "_tool"
    assert importlib.util.find_spec(legacy_package) is None


def test_distribution_and_console_scripts_use_nanolab_name() -> None:
    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert metadata["project"]["name"] == "nanolab"
    assert metadata["project"]["scripts"] == {
        "nanolab": "nanolab.app.main:main",
        "nanolab-package-report": "nanolab.devtools.package_report:main",
        "nanolab-quality": "nanolab.devtools.quality:main",
    }
