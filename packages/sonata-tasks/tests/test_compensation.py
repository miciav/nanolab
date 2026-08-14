import pytest

from sonata_tasks.compensation import best_effort


def test_best_effort_propagates_programming_errors() -> None:
    error = RuntimeError("acquire failed")

    with pytest.raises(ValueError, match="bad cleanup contract"):
        best_effort(
            error,
            lambda: (_ for _ in ()).throw(ValueError("bad cleanup contract")),
            what="cleanup",
        )


def test_best_effort_notes_operational_cleanup_error_on_the_primary_error() -> None:
    error = RuntimeError("acquire failed")

    best_effort(
        error,
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        what="cleanup",
    )

    assert error.__notes__ == ["Best-effort cleanup after a failed acquire failed: cleanup failed"]
