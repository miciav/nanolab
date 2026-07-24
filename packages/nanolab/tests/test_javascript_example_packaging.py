import os
from pathlib import Path


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()


def test_javascript_example_images_copy_local_sdk_dependency_target() -> None:
    dockerfiles = (
        NANOFAAS_ROOT / "functions" / "javascript" / "word-stats" / "Dockerfile",
        NANOFAAS_ROOT / "functions" / "javascript" / "json-transform" / "Dockerfile",
        NANOFAAS_ROOT / "functions" / "javascript" / "roman-numeral" / "Dockerfile",
    )

    for dockerfile in dockerfiles:
        text = dockerfile.read_text(encoding="utf-8")
        assert "COPY --from=build /src/sdks/javascript /sdks/javascript" in text, (
            f"{dockerfile} must copy the local function SDK into the final image because "
            "npm installs nanofaas-function-sdk as a symlinked file dependency."
        )
