from __future__ import annotations

from sonata_tasks.command import CommandTask

from nanolab.comparison.prepare import (
    function_build_operations,
    leftover_cleanup_operations,
    pinned_function_images,
    prepare_operations,
)
from nanolab.images.control_plane_variants import resolve_variants
from nanolab.plans.functions import ResolvedFunction

REGISTRY = "127.0.0.1:5000"
MODULES = "k8s-deployment-provider,async-queue,sync-queue"

JAVA = ResolvedFunction(
    key="word-stats-java",
    name="word-stats-java",
    image=f"{REGISTRY}/nanofaas/word-stats-java:e2e",
    build_argv=("./gradlew", ":functions:java:word-stats:bootJar"),
    payload="{}",
    image_build_argv=("docker", "build", "-t", f"{REGISTRY}/nanofaas/word-stats-java:e2e", "."),
)
JS = ResolvedFunction(
    key="word-stats-javascript",
    name="word-stats-javascript",
    image=f"{REGISTRY}/nanofaas/word-stats-javascript:e2e",
    build_argv=("docker", "build", "-t", f"{REGISTRY}/nanofaas/word-stats-javascript:e2e", "."),
    payload="{}",
)


def test_a_function_with_an_artifact_step_builds_it_before_the_image() -> None:
    ops = function_build_operations([JAVA])

    assert [op.operation_id for op in ops] == [
        "prepare.function.word-stats-java.artifact",
        "prepare.function.word-stats-java.image",
        "prepare.function.word-stats-java.push",
    ]


def test_a_function_without_one_goes_straight_to_the_image() -> None:
    """JavaScript has no compile step; emitting an empty one would run the docker build twice."""
    ops = function_build_operations([JS])

    assert [op.operation_id for op in ops] == [
        "prepare.function.word-stats-javascript.image",
        "prepare.function.word-stats-javascript.push",
    ]
    assert ops[0].argv == JS.build_argv


def test_functions_are_built_before_the_native_images() -> None:
    """Learning the checkout does not compile after 40 minutes of native-image work is bad."""
    ops = prepare_operations(
        functions=[JAVA, JS],
        variants=resolve_variants(("native-o3",)),
        registry=REGISTRY,
        modules=MODULES,
    )
    ids = [op.operation_id for op in ops]

    assert ids.index("prepare.function.word-stats-java.image") < ids.index(
        "variant.native-o3.image"
    )


def test_everything_prepared_runs_on_the_vm() -> None:
    for op in prepare_operations(
        functions=[JAVA, JS],
        variants=resolve_variants(("jvm", "native-o3")),
        registry=REGISTRY,
        modules=MODULES,
    ):
        assert op.execution_target == "vm", op.operation_id


def test_pinned_images_are_keyed_by_catalogue_key() -> None:
    """`_resolve_with_prebuilt_images` looks up `key`; a map keyed by `name`
    reports every entry it holds as missing."""
    assert pinned_function_images([JAVA, JS]) == {
        "word-stats-java": JAVA.image,
        "word-stats-javascript": JS.image,
    }


def test_native_builds_can_be_told_their_memory_budget() -> None:
    """Unbounded, native-image sizes its heap from the whole machine.

    On a 12GB VM already holding k3s, a control plane and Prometheus it was
    OOM-killed after 9m50s, reporting only "exit 137".
    """
    ops = prepare_operations(
        functions=[],
        variants=resolve_variants(("native-o3",)),
        registry=REGISTRY,
        modules=MODULES,
        build_memory="6g",
        parallelism=2,
    )

    assert ops[0].env["NATIVE_BUILD_MEMORY"] == "6g"
    assert ops[0].env["NATIVE_PARALLELISM"] == "2"


def test_the_budget_is_absent_when_not_asked_for() -> None:
    """A bound helps on a shared VM and only costs wall-clock on a dedicated one."""
    ops = prepare_operations(
        functions=[],
        variants=resolve_variants(("native-o3",)),
        registry=REGISTRY,
        modules=MODULES,
    )

    assert "NATIVE_BUILD_MEMORY" not in ops[0].env


def test_the_jvm_variant_ignores_a_native_budget() -> None:
    """javac is not native-image; passing it the flag would only confuse a reader."""
    ops = prepare_operations(
        functions=[],
        variants=resolve_variants(("jvm",)),
        registry=REGISTRY,
        modules=MODULES,
        build_memory="6g",
    )

    assert all("NATIVE_BUILD_MEMORY" not in op.env for op in ops)


def test_a_matrix_reconciles_what_an_interrupted_run_left_behind() -> None:
    """One cluster serves every cell, so a killed run leaves its functions registered.

    The next matrix then dies on its first cell with a 409 having done nothing
    wrong — which is exactly what happened, twice.
    """
    ops = prepare_operations(
        functions=[JAVA, JS],
        variants=resolve_variants(("jvm",)),
        registry=REGISTRY,
        modules=MODULES,
    )

    assert ops[0].operation_id == "prepare.cleanup.leftover_functions"
    script = ops[0].argv[2]
    assert "word-stats-java" in script and "word-stats-javascript" in script
    assert "-X DELETE" in script


def test_the_cleanup_cannot_stop_a_run() -> None:
    """A fresh VM has no control plane and the functions are usually absent.

    Both are ordinary; neither may fail the matrix before it starts.
    """
    script = leftover_cleanup_operations(["word-stats-java"])[0].argv[2]

    assert script.endswith("|| true")
    assert " -m 5 " in script, "an unreachable control plane must not hang the run"


class _RecordingExecutor:
    def __init__(self) -> None:
        self.seen: list[tuple[str, ...]] = []

    def binding_key(self, role: str) -> str:
        return f"test-recording:{role}"

    def run(self, spec, dry_run: bool = False):  # noqa: ANN001 - structural stand-in
        from sonata_tasks.tasks.models import TaskResult

        self.seen.append(tuple(spec.argv))
        return TaskResult(task_id="", status="passed", return_code=0, stdout="", stderr="")


def test_prepare_is_a_workflow_of_the_operations_in_order() -> None:
    """It ran outside the engine for no better reason than being written later.

    Command output routing is bound to the workflow sink, so the phase was silent,
    and a reader had to learn two execution models to follow one command.
    """
    from nanolab.comparison.prepare import prepare_workflow

    ops = prepare_operations(
        functions=[JAVA],
        variants=resolve_variants(("jvm",)),
        registry=REGISTRY,
        modules=MODULES,
    )
    workflow = prepare_workflow(ops, executor=_RecordingExecutor())
    compiled = workflow.compile().tasks

    assert [task.task.title for task in compiled] == [op.summary for op in ops]


def test_every_prepare_task_targets_the_vm_role() -> None:
    """A build that ran on the host would produce an image for the wrong architecture."""
    from nanolab.comparison.prepare import prepare_workflow

    ops = prepare_operations(
        functions=[JAVA],
        variants=resolve_variants(("native-o3",)),
        registry=REGISTRY,
        modules=MODULES,
    )
    for compiled in prepare_workflow(ops, executor=_RecordingExecutor()).compile().tasks:
        assert getattr(compiled.task, "role", None) == "stack"


def test_prepare_tasks_run_inside_the_checkout() -> None:
    """The commands are `./gradlew` and docker builds against paths in the repo.

    The executor's default working directory is the checkout only on multipass;
    on Azure and Proxmox it is the home directory one level above. Inheriting it
    worked for nine multipass matrices and failed two seconds into the first
    Azure one with `./gradlew: No such file or directory`.
    """
    from nanolab.comparison.prepare import prepare_workflow

    ops = prepare_operations(
        functions=[JAVA],
        variants=resolve_variants(("jvm",)),
        registry=REGISTRY,
        modules=MODULES,
    )
    workflow = prepare_workflow(
        ops, executor=_RecordingExecutor(), remote_dir="/home/azureuser/nanofaas"
    )

    for compiled in workflow.compile().tasks:
        assert isinstance(compiled.task, CommandTask)
        assert compiled.task.options.remote_dir == "/home/azureuser/nanofaas"


def test_a_cell_is_retried_once_and_not_more() -> None:
    """A dropped connection killed two k6 runs out of eight on Azure.

    Re-running a cell is safe: it has produced no summary yet, so the second
    attempt yields a whole valid cell rather than a mixture. But the SDK already
    keepalives the transport, so a connection that dies twice running is not a
    blip — the matrix should stop rather than grind through the rest producing
    nothing.
    """
    from nanolab.cli import comparison

    assert comparison.CELL_ATTEMPTS == 2
