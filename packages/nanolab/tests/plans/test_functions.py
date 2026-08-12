from __future__ import annotations

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.functions import ResolvedFunction, resolve_function, sonata_function


def _scenario() -> ScenarioConfig:
    return ScenarioConfig.model_validate(
        {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
    )


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
