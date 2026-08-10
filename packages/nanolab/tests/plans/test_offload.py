from pathlib import Path
import os

import pytest

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.offload import build_offload_plan
from sonata_tasks.execution.bindings import RoleBindings
from sonata_tasks.tasks.models import TaskResult


NANOFAAS_ROOT = Path(os.environ["NANOFAAS_ROOT"]).resolve()
NANOLAB_ROOT = Path(__file__).resolve().parents[2]


class _RecordingExecutor:
    def run(self, spec, **kwargs):  # noqa: ANN001, ANN003
        return TaskResult(return_code=0, stdout="", stderr="")


def _bindings() -> RoleBindings:
    return RoleBindings(host=_RecordingExecutor(), stack=_RecordingExecutor())


def _scenario() -> ScenarioConfig:
    return ScenarioConfig(workflow="offload", functions=["word-stats-java"])


def test_offload_scenario_rejects_a_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        ScenarioConfig(workflow="offload", backend="container", functions=["word-stats-java"])


def _ids() -> list[str]:
    workflow = build_offload_plan(_scenario(), _bindings(), repo_root=NANOFAAS_ROOT)
    return [task.task_id for task in workflow.compile().tasks]


def test_plan_starts_both_control_planes_before_anything_registers() -> None:
    ids = _ids()

    assert ids.index("005.acquire-cloud-control-plane") < ids.index(
        "006.acquire-edge-control-plane"
    )
    first_register = next(
        index for index, name in enumerate(ids) if "-on-the-" in name and "acquire" in name
    )
    assert ids.index("006.acquire-edge-control-plane") < first_register


def test_plan_ends_with_the_negative_check_then_releases_everything() -> None:
    """The teardown is compiled in, not a cleanup list the caller has to run,
    and the negative check comes before it: deleting the remote copy has to
    happen while the edge still knows the function, or it answers 404."""
    ids = _ids()

    assert ids[-5:] == [
        "012.release-word-stats-java-on-the-edge",
        "013.release-word-stats-java-on-the-cloud",
        "014.release-edge-control-plane",
        "015.release-cloud-control-plane",
        "016.release-local-registry",
    ]
    assert ids.index("011.verify-word-stats-java-has-no-local-fallback") < ids.index(
        "012.release-word-stats-java-on-the-edge"
    )


def test_offload_scenario_file_parses() -> None:
    import yaml

    payload = yaml.safe_load(
        (NANOLAB_ROOT / "scenarios-v2/edge-cloud-offload-contract.yaml").read_text()
    )
    config = ScenarioConfig.model_validate(payload)
    assert config.workflow == "offload"
    assert config.functions == ["word-stats-java"]
