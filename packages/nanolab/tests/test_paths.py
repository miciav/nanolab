import os
from pathlib import Path

import pytest

from nanolab.workspace.paths import ToolPaths, default_tool_paths


def test_tool_paths_preserve_separate_source_and_tool_roots() -> None:
    paths = ToolPaths.from_roots(Path("/nanofaas"), Path("/nanolab"))

    assert paths.nanofaas_root == Path("/nanofaas")
    assert paths.tool_root == Path("/nanolab")
    assert paths.profiles_dir == Path("/nanolab/profiles")
    assert paths.runs_dir == Path("/nanolab/runs")
    assert paths.scenarios_dir == Path("/nanolab/scenarios")
    assert paths.scenario_payloads_dir == Path("/nanolab/scenarios/payloads")


def test_default_tool_paths_requires_nanofaas_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NANOFAAS_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="NANOFAAS_ROOT"):
        default_tool_paths()


def test_default_tool_paths_accepts_valid_nanofaas_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nanofaas_root = tmp_path / "nanofaas"
    nanofaas_root.mkdir()
    (nanofaas_root / "build.gradle").touch()
    (nanofaas_root / "settings.gradle").touch()
    monkeypatch.setenv("NANOFAAS_ROOT", os.fspath(nanofaas_root))

    paths = default_tool_paths()

    assert paths.nanofaas_root == nanofaas_root.resolve()
    assert paths.tool_root == Path(__file__).resolve().parents[1]


def test_default_tool_paths_rejects_invalid_nanofaas_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "not-nanofaas"
    checkout.mkdir()
    monkeypatch.setenv("NANOFAAS_ROOT", os.fspath(checkout))

    with pytest.raises(RuntimeError, match="build.gradle, settings.gradle"):
        default_tool_paths()
