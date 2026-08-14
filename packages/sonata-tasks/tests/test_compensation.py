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
