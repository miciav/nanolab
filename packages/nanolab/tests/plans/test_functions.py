from __future__ import annotations

from pathlib import Path

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import (
    ResolvedFunction,
    resolve_function,
    resolve_function_payloads,
    sonata_function,
)


def _scenario() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
    )


def _checkout_with_function(tmp_path: Path, *, with_payloads: bool) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "build.gradle").touch()
    (checkout / "settings.gradle").touch()
    function_dir = checkout / "functions/java/word-stats"
    function_dir.mkdir(parents=True)
    (function_dir / "function.yaml").write_text(
        "name: word-stats-java\ncatalog:\n  defaultImage: registry.example/ws\n",
        encoding="utf-8",
    )
    if with_payloads:
        payloads = function_dir / "payloads"
        payloads.mkdir()
        (payloads / "happy-path.json").write_text(
            '{"description":"d","input":{"text":"a b","topN":1},"expected":{"wordCount":2}}',
            encoding="utf-8",
        )
        (payloads / "missing-input.json").write_text(
            '{"description":"d","input":{},"expected":{"error":"missing"}}',
            encoding="utf-8",
        )
    return checkout


def test_resolve_function_returns_the_shared_shape() -> None:
    resolved = resolve_function(_scenario(), "word-stats-java")

    assert isinstance(resolved, ResolvedFunction)
    assert resolved.key == "word-stats-java"
    assert resolved.image
    assert resolved.build_argv


def test_sonata_function_carries_the_name_and_image_across() -> None:
    resolved = resolve_function(_scenario(), "word-stats-java")

    task_shape = sonata_function(resolved)

    assert task_shape.name == resolved.name
    assert task_shape.image == resolved.image


def test_resolve_function_payloads_reads_the_function_directory(tmp_path: Path) -> None:
    result = resolve_function_payloads(
        "word-stats-java", source_root=_checkout_with_function(tmp_path, with_payloads=True)
    )

    assert [payload.name for payload in result] == ["happy-path", "missing-input"]
    assert result[0].input == {"text": "a b", "topN": 1}
    assert result[0].expected == {"wordCount": 2}


def test_resolve_function_payloads_returns_nothing_without_a_payload_directory(
    tmp_path: Path,
) -> None:
    result = resolve_function_payloads(
        "word-stats-java", source_root=_checkout_with_function(tmp_path, with_payloads=False)
    )

    assert result == ()
