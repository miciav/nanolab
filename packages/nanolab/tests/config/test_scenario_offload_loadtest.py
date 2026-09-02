from pathlib import Path

import pytest
import yaml

from nanolab.config.environment import EnvironmentConfig
from nanolab.config.scenario import ScenarioConfig

NANOLAB_ROOT = Path(__file__).resolve().parents[2]


def test_offload_loadtest_scenario_parses_without_backend() -> None:
    config = ScenarioConfig(
        workflow="offload-loadtest",
        functions=["word-stats-java", "json-transform-java"],
    )
    assert config.autoscaling is False


def test_offload_loadtest_rejects_autoscaling() -> None:
    with pytest.raises(ValueError, match="autoscaling"):
        ScenarioConfig(
            workflow="offload-loadtest",
            functions=["word-stats-java"],
            autoscaling=True,
        )


@pytest.mark.parametrize(
    ("filename", "provider"),
    [
        ("multipass-offload.yaml", "multipass"),
        ("azure-offload.yaml.example", "azure"),
    ],
)
def test_offload_environment_has_exactly_two_vms(filename: str, provider: str) -> None:
    payload = yaml.safe_load((NANOLAB_ROOT / f"environments/{filename}").read_text())
    environment = EnvironmentConfig.model_validate(payload)

    assert environment.provider == provider
    assert set(environment.roles) == {"stack", "cloud"}
    assert environment.roles["stack"].name != environment.roles["cloud"].name
    if provider == "azure":
        assert environment.azure is not None
        assert environment.azure.operator_source_cidr == "auto"
