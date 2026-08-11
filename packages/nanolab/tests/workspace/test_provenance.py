from __future__ import annotations

from pathlib import Path
import subprocess

from nanolab.workspace.provenance import source_fingerprint


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "t"),
    ):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    (root / "app.txt").write_text("one")
    subprocess.run(("git", "add", "-A"), cwd=root, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-qm", "init"), cwd=root, check=True, capture_output=True)
    return root


def test_the_same_source_keeps_the_same_tag(tmp_path: Path) -> None:
    """Rebuilding unchanged source must not churn the image name: no change,
    nothing for Kubernetes to roll."""
    root = _repo(tmp_path / "repo")

    assert source_fingerprint(root) == source_fingerprint(root)


def test_an_edit_changes_the_tag(tmp_path: Path) -> None:
    """The whole point: an edited control plane cannot reach the registry under
    a name the cluster already runs."""
    root = _repo(tmp_path / "repo")
    before = source_fingerprint(root)

    (root / "app.txt").write_text("two")

    assert source_fingerprint(root) != before


def test_an_untracked_file_changes_the_tag(tmp_path: Path) -> None:
    """A new file that was never committed still ends up in the image."""
    root = _repo(tmp_path / "repo")
    before = source_fingerprint(root)

    (root / "extra.txt").write_text("new")

    assert source_fingerprint(root) != before


def test_it_falls_back_to_the_plain_tag_outside_a_repository(tmp_path: Path) -> None:
    """No git, no fingerprint — a random tag would rebuild the world every run."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert source_fingerprint(plain) is None


def test_the_fingerprint_survives_as_an_image_tag(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")

    fingerprint = source_fingerprint(root)

    assert fingerprint is not None
    assert all(character.isalnum() for character in fingerprint)
