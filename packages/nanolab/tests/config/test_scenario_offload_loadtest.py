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


def test_multipass_offload_environment_has_three_roles() -> None:
    payload = yaml.safe_load(
        (NANOLAB_ROOT / "environments/multipass-offload.yaml").read_text()
    )
    environment = EnvironmentConfig.model_validate(payload)
    assert set(environment.roles) == {"stack", "cloud", "loadgen"}
