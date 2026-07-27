from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from nanolab.release import versioning
from nanolab.release.versioning import (
    normalize_version,
    prepare_version,
    read_project_version,
    verify_version_consistency,
)


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
CURATED_FILES = (
    Path("build.gradle"),
    Path("deploy/helm/nanofaas/Chart.yaml"),
    Path("deploy/helm/nanofaas/values.yaml"),
    Path("deploy/k8s/control-plane-deployment.yaml"),
    Path("runtimes/watchdog/Cargo.toml"),
    Path("runtimes/watchdog/Cargo.lock"),
    Path("sdks/python/pyproject.toml"),
    Path("sdks/python/uv.lock"),
    Path("functions/python/roman-numeral/uv.lock"),
    Path("tools/fn-init/src/fn_init/main.py"),
    Path("clients/cli/src/test/java/it/unimib/datai/nanofaas/cli/commands/RootCommandTest.java"),
)
LOCKFILE_COMMANDS = (
    (("cargo", "check"), Path("runtimes/watchdog"), Path("runtimes/watchdog/Cargo.lock")),
    (("uv", "lock"), Path("sdks/python"), Path("sdks/python/uv.lock")),
    (
        ("uv", "lock"),
        Path("functions/python/roman-numeral"),
        Path("functions/python/roman-numeral/uv.lock"),
    ),
)



def _bump_minor(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def _bump_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


# Read from the checkout rather than pinned here. These tests copy the live
# nanoFaaS tree, so a hard-coded version makes every release break them: eleven
# failed the moment 0.17.1 was prepared, none of them pointing at a real defect.
CURRENT_VERSION = read_project_version(NANOFAAS_ROOT)
NEXT_VERSION = _bump_minor(CURRENT_VERSION)
NEXT_PATCH_VERSION = _bump_patch(CURRENT_VERSION)
# Safe forever: project versions only ever increase, so a fixed older one stays older.
OLDER_VERSION = "0.16.9"

@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    for relative_path in CURATED_FILES:
        destination = tmp_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(NANOFAAS_ROOT / relative_path, destination)
    sentinel = tmp_path / "docs" / "release-sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("must remain unchanged\n", encoding="utf-8")
    return tmp_path


def lockfile_runner(
    repo_root: Path, calls: list[tuple[tuple[str, ...], Path]]
) -> Callable[[tuple[str, ...], Path], None]:
    lockfiles = {cwd: lockfile for _, cwd, lockfile in LOCKFILE_COMMANDS}

    def run(command: tuple[str, ...], cwd: Path) -> None:
        calls.append((command, cwd.relative_to(repo_root)))
        lockfile = repo_root / lockfiles[cwd.relative_to(repo_root)]
        assert CURRENT_VERSION in lockfile.read_text(encoding="utf-8")
        lockfile.write_text(
            # Same whole-version match production uses: a plain replace would
            # rewrite any dependency pinned at a version starting with ours.
            versioning._version_pattern(CURRENT_VERSION).sub(
                NEXT_VERSION, lockfile.read_text(encoding="utf-8")
            ),
            encoding="utf-8",
        )

    return run


def install_lockfile_runner(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    calls: list[tuple[tuple[str, ...], Path]],
) -> None:
    monkeypatch.setattr(versioning, "_run_command", lockfile_runner(repo_root, calls))


def test_normalize_version_returns_plain_and_image_tag() -> None:
    assert normalize_version("v0.18.0") == ("0.18.0", "v0.18.0")
    assert normalize_version("0.18.0") == ("0.18.0", "v0.18.0")


@pytest.mark.parametrize("value", ("", "v0.18", "0.18.0-rc1", "v0.18.0.1", "v01.18.0"))
def test_normalize_version_rejects_invalid_versions(value: str) -> None:
    with pytest.raises(ValueError, match="version"):
        normalize_version(value)


def test_read_project_version_reads_root_gradle_version(source_tree: Path) -> None:
    assert read_project_version(source_tree) == CURRENT_VERSION


def test_verify_version_consistency_returns_current_version(source_tree: Path) -> None:
    assert verify_version_consistency(source_tree) == CURRENT_VERSION


def test_verify_version_consistency_rejects_mismatched_source_tree(source_tree: Path) -> None:
    chart = source_tree / "deploy/helm/nanofaas/Chart.yaml"
    chart.write_text(
        chart.read_text(encoding="utf-8").replace(
            f"version: {CURRENT_VERSION}", f"version: {OLDER_VERSION}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Chart.yaml"):
        verify_version_consistency(source_tree)


def test_prepare_version_updates_each_curated_location_without_reformatting(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {
        relative_path: (source_tree / relative_path).read_text(encoding="utf-8")
        for relative_path in CURATED_FILES
    }

    install_lockfile_runner(monkeypatch, source_tree, [])
    changed = prepare_version(source_tree, f"v{NEXT_VERSION}")

    assert changed == tuple(source_tree / relative_path for relative_path in CURATED_FILES)
    for relative_path, original in before.items():
        updated = (source_tree / relative_path).read_text(encoding="utf-8")
        assert updated == versioning._version_pattern(CURRENT_VERSION).sub(NEXT_VERSION, original)
    assert verify_version_consistency(source_tree) == NEXT_VERSION


def test_prepare_version_ignores_dependency_pins_that_start_with_the_new_version(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency pinned at a longer version starting with ours must not count.

    The real case: releasing 0.17.1 found `ring 0.17.14` in the watchdog
    lockfile beside our own pin, so a substring search reported two hits where
    one was expected and aborted the release.
    """
    lockfile = source_tree / "runtimes" / "watchdog" / "Cargo.lock"
    # A pin the target version is a strict prefix of, whatever the target is today.
    colliding_pin = f'\n[[package]]\nname = "colliding-dep"\nversion = "{NEXT_PATCH_VERSION}4"\n'
    lockfile.write_text(lockfile.read_text(encoding="utf-8") + colliding_pin, encoding="utf-8")

    def bump_lockfiles(_command: tuple[str, ...], cwd: Path) -> None:
        for _, relative_cwd, relative_lockfile in LOCKFILE_COMMANDS:
            if cwd.relative_to(source_tree) != relative_cwd:
                continue
            path = source_tree / relative_lockfile
            path.write_text(
                versioning._version_pattern(CURRENT_VERSION).sub(
                    NEXT_PATCH_VERSION, path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(versioning, "_run_command", bump_lockfiles)

    prepare_version(source_tree, NEXT_PATCH_VERSION)

    assert verify_version_consistency(source_tree) == NEXT_PATCH_VERSION
    assert colliding_pin in lockfile.read_text(encoding="utf-8")


def test_prepare_version_regenerates_lockfiles_after_primary_edits(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], Path]] = []
    install_lockfile_runner(monkeypatch, source_tree, calls)

    prepare_version(source_tree, NEXT_VERSION)

    assert calls == [(command, cwd) for command, cwd, _ in LOCKFILE_COMMANDS]


def test_prepare_version_leaves_non_curated_sentinel_unchanged(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = source_tree / "docs" / "release-sentinel.txt"
    before = sentinel.read_bytes()

    install_lockfile_runner(monkeypatch, source_tree, [])
    prepare_version(source_tree, NEXT_VERSION)

    assert sentinel.read_bytes() == before


def test_prepare_version_rejects_runner_changes_outside_curated_files(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], Path]] = []
    runner = lockfile_runner(source_tree, calls)
    sentinel = source_tree / "docs" / "release-sentinel.txt"

    def run(command: tuple[str, ...], cwd: Path) -> None:
        runner(command, cwd)
        sentinel.write_text("unexpected runner output\n", encoding="utf-8")

    monkeypatch.setattr(versioning, "_run_command", run)

    with pytest.raises(ValueError, match="outside curated") as error:
        prepare_version(source_tree, NEXT_VERSION)

    assert error.value.__cause__ is None


def test_prepare_version_rejects_runner_scope_changes_when_runner_fails(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = source_tree / "docs" / "release-sentinel.txt"

    def run(command: tuple[str, ...], cwd: Path) -> None:
        sentinel.write_text("unexpected runner output\n", encoding="utf-8")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(versioning, "_run_command", run)

    with pytest.raises(ValueError, match="outside curated") as error:
        prepare_version(source_tree, NEXT_VERSION)

    assert isinstance(error.value.__cause__, subprocess.CalledProcessError)


def test_prepare_version_preserves_runner_error_without_scope_changes(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: tuple[str, ...], cwd: Path) -> None:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(versioning, "_run_command", run)

    with pytest.raises(subprocess.CalledProcessError):
        prepare_version(source_tree, NEXT_VERSION)


def test_prepare_version_rejects_scope_changes_when_final_consistency_check_fails(
    source_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = source_tree / "docs" / "release-sentinel.txt"
    checks = 0
    original_verify = versioning.verify_version_consistency
    install_lockfile_runner(monkeypatch, source_tree, [])

    def verify(repo_root: Path) -> str:
        nonlocal checks
        checks += 1
        if checks == 1:
            return original_verify(repo_root)
        sentinel.write_text("unexpected verification output\n", encoding="utf-8")
        raise RuntimeError("simulated final consistency failure")

    monkeypatch.setattr(versioning, "verify_version_consistency", verify)

    with pytest.raises(ValueError, match="outside curated") as error:
        prepare_version(source_tree, NEXT_VERSION)

    assert isinstance(error.value.__cause__, RuntimeError)


def test_prepare_version_exposes_only_the_required_public_parameters() -> None:
    assert tuple(inspect.signature(prepare_version).parameters) == ("repo_root", "requested")


@pytest.mark.parametrize(
    "requested", (CURRENT_VERSION, f"v{CURRENT_VERSION}", OLDER_VERSION, "not-a-version")
)
def test_prepare_version_rejects_invalid_or_nonincrementing_versions(
    source_tree: Path,
    requested: str,
) -> None:
    with pytest.raises(ValueError):
        prepare_version(source_tree, requested)


def test_prepare_version_rejects_unexpected_replacement_count_without_writing(
    source_tree: Path,
) -> None:
    values = source_tree / "deploy/helm/nanofaas/values.yaml"
    original = values.read_text(encoding="utf-8")
    values.write_text(original + f"\n# stale image: v{CURRENT_VERSION}\n", encoding="utf-8")
    before = {
        relative_path: (source_tree / relative_path).read_bytes() for relative_path in CURATED_FILES
    }

    with pytest.raises(ValueError, match="replacement count"):
        prepare_version(source_tree, NEXT_VERSION)

    assert {
        relative_path: (source_tree / relative_path).read_bytes() for relative_path in CURATED_FILES
    } == before
