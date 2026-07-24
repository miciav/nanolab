from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELLCRAFT_REVISION = "b6547835aac88d058810c7b9a592ad442c54ac99"


def _metadata() -> dict[str, object]:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def test_shellcraft_dependency_is_pinned_to_the_locked_revision() -> None:
    dependencies = _metadata()["dependencies"]

    assert (
        f"shellcraft @ git+https://github.com/miciav/shellcraft.git@{SHELLCRAFT_REVISION}"
        in dependencies
    )


def test_description_does_not_claim_the_package_has_no_external_dependencies() -> None:
    description = _metadata()["description"]

    assert "No external dependencies" not in description
