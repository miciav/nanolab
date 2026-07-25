from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_launcher_script_exists_at_repo_root() -> None:
    script = REPO_ROOT / "nanolab.sh"
    assert script.is_file()


def test_launcher_script_fails_fast_when_uv_is_missing() -> None:
    script = (REPO_ROOT / "nanolab.sh").read_text(encoding="utf-8")
    assert "command -v uv" in script
    assert "uv not found" in script.lower()


def test_launcher_script_runs_the_nanolab_package() -> None:
    script = (REPO_ROOT / "nanolab.sh").read_text(encoding="utf-8")
    assert "uv run --locked --package nanolab nanolab" in script
