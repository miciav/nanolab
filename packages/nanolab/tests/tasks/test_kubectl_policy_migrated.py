from sonata_engine import TaskInputs
from sonata_tasks.testing import RecordingExecutor

from nanolab.tasks.kubectl import k8s_function_resources_absent


def test_it_waits_until_a_deleted_function_has_no_deployment_or_service() -> None:
    executor = RecordingExecutor()

    _ = k8s_function_resources_absent(
        function="word-stats",
        namespace="nf",
        executor=executor,
        role="stack",
    ).run(TaskInputs.empty())

    script = executor.seen[0].argv[-1]
    assert executor.seen[0].argv[:2] == ("bash", "-lc")
    assert "deployment/fn-word-stats" in script
    assert "service/fn-word-stats" in script
    assert "kubectl -n nf get" in script
