from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from workflow_tasks.devtools import quality


def test_quality_gate_runs_every_check_from_member_root(monkeypatch) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(command: list[str], *, check: bool, cwd: Path | None = None) -> object:
        assert check is False
        calls.append((command, cwd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(quality.subprocess, "run", fake_run)

    quality.main()

    assert calls
    assert all(cwd == quality.MEMBER_ROOT for _, cwd in calls)


def test_quality_gate_uses_explicit_member_import_linter_config() -> None:
    import_linter_check = dict(quality.CHECKS)["import-linter"]

    assert import_linter_check == ["lint-imports", "--config", ".importlinter", "--no-cache"]
