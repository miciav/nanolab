# CLI Workflow Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dare al workflow `cli` un bersaglio che funziona: con `backend: container` il workflow avvia da sé un control plane locale su Docker, ci gira contro e lo spegne, senza VM, senza k8s e senza argomenti aggiuntivi.

**Architecture:** Il control plane locale diventa una `Resource` sonata: il compiler la acquisisce prima del primo consumer e la rilascia dopo l'ultimo, anche in caso di fallimento. `sonata-tasks` guadagna un helper generico per processi gestiti (il gemello sonata di `managed_process_resource`, che non è riusabile perché restituisce un tipo del motore legacy che i contratti di import vietano). Il comando java concreto e l'URL di salute restano in `nanolab`, dove già stanno per il workflow `validate`.

**Tech Stack:** Python 3.12, uv workspace, pytest, Ruff, basedpyright, import-linter, sonata-engine.

**Spec:** `docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md`

## Global Constraints

- **Solo il percorso sonata.** I workflow legacy (`validate`, `loadtest`, `offload`, `offload-loadtest`) non si toccano. `preflight_control_plane` resta invariato per loro.
- **`backend: k8s` resta com'è oggi:** richiede un `--control-plane-url` esplicito. Il provisioning di VM e piattaforma è fuori scope, e così l'attesa di readiness che serve solo lì.
- **Nessuna primitiva nuova di sonata.** L'acquire del control plane non produce valori: la porta è fissa e nota. Se durante l'implementazione sembra servire una `Resource` che produce un valore, FERMATI e segnala — significa che il design è sbagliato, non che sonata va esteso.
- **Il contratto di import è vincolante:** `sonata_tasks` non può importare `workflow_tasks.core`, `workflow_tasks.workflows`, `workflow_tasks.workflow`, `nanolab`, `tui_toolkit` — nemmeno indirettamente. In particolare **non** può importare `workflow_tasks.components.container`, che tira dentro `workflow_tasks.core.resource_task`.
- **Identità dei task al compiler.** Nessun task dichiara un `task_id`.
- **Messaggi di commit: NIENTE trailer `Co-Authored-By`** (requisito utente, sovrascrive il default dell'harness).
- **Staging esplicito.** Prima di ogni commit esegui `git status --short` e aggiungi solo i file elencati dal task. Mai `git add -A`.
- **Branch dedicata.** Crea `codex/cli-workflow-target` da `main` e resta lì. Non committare su `main`.
- **Comandi** (da `~/Downloads/nanolab`; `NANOFAAS_ROOT` è obbligatoria per la suite nanolab, altrimenti 12 file falliscono in collection — è preesistente):

  ```bash
  uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
  NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
  uv run --all-packages --all-groups pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
  uv run --all-packages --all-groups ruff check packages
  uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
  uv run --all-packages --all-groups basedpyright --project packages/nanolab
  uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
  uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
  ```

- **Baseline di partenza:** sonata-tasks 27 test (99.04%), nanolab 667 test, workflow-tasks 93.14% (gate 90%).

## Fatti verificati che il piano dà per acquisiti

Non ri-verificarli, sono già stati controllati nel codice:

- `ContainerLocalDeploymentProvider` chiama `endpointProbe.awaitReady(...)` subito dopo `runContainer`, quindi con `container-local` **`fn apply` ritorna solo quando la function risponde**. Nessuna attesa da implementare.
- Il control plane espone l'API su `server.port` e l'actuator su `management.server.port`: sono porte diverse, sempre.
- `nanolab/plans/validate.py::_container_control_plane` è il precedente funzionante da imitare: jar, argv, porte 18080/18081, health su `http://127.0.0.1:18081/actuator/health`.
- `_resolve_function(config, key)` restituisce un oggetto con `name`, `image`, `payload`, `resources` e **`build_argv`** — il comando per costruire l'immagine della function in locale.
- Il comando per il jar del control plane con il modulo container:
  `./gradlew :control-plane:bootJar -PcontrolPlaneModules=container-deployment-provider --no-daemon`

## File Structure

| File | Responsabilità | Cambio |
|---|---|---|
| `packages/sonata-tasks/src/sonata_tasks/process.py` | `managed_process_resource`: avvia un processo, attende la readiness, ne garantisce la terminazione — come `Resource` sonata | **create** |
| `packages/sonata-tasks/src/sonata_tasks/cli.py` | `CliFunction.build_argv`; `build_cli_workflow` accetta risorse esterne e task di build immagine | modify |
| `packages/sonata-tasks/src/sonata_tasks/__init__.py` | export | modify |
| `packages/sonata-tasks/tests/test_process.py` | ciclo di vita del processo gestito | **create** |
| `packages/sonata-tasks/tests/test_cli.py` | topologia con risorsa esterna e build immagine | modify |
| `packages/nanolab/src/nanolab/plans/cli.py` | onora `config.backend`; costruisce il control plane locale | modify |
| `packages/nanolab/src/nanolab/cli/product.py` | il preflight non si applica più al percorso sonata | modify |
| `packages/nanolab/scenarios-v2/cli-container.yaml` | scenario con control plane locale | **create** |
| `packages/nanolab/tests/plans/test_cli.py` | topologia per entrambi i backend | modify |
| `packages/nanolab/tests/cli/test_command_surface.py` | il preflight non blocca più `cli` | modify |
| `.github/workflows/ci.yml` | genera anche il piano del nuovo scenario | modify |

---

### Task 1: La risorsa "processo gestito" per sonata

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/process.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Test: `packages/sonata-tasks/tests/test_process.py`

**Interfaces:**
- Consumes: `Resource` da `sonata_engine`.
- Produces:
  ```python
  managed_process_resource(
      *,
      title: str,
      argv: tuple[str, ...],
      ready: Callable[[], bool],
      cwd: Path | None = None,
      spawn: Callable[..., Any] = subprocess.Popen,
      readiness_attempts: int = 90,
      readiness_interval: float = 1.0,
      sleep: Callable[[float], None] = time.sleep,
  ) -> Resource
  ```

**Perché non si riusa quello legacy:** `workflow_tasks.components.container.managed_process_resource` restituisce un `ResourceTask` da `workflow_tasks.core`, che il contratto `no_legacy_workflows` vieta a `sonata_tasks` anche per catene indirette. Questo è il gemello, non una copia: restituisce una `Resource` sonata.

- [ ] **Step 1: Scrivere i test che falliscono**

Crea `packages/sonata-tasks/tests/test_process.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest
from sonata_engine import Resource

from sonata_tasks.process import managed_process_resource


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def _spawner(process: FakeProcess, seen: list[dict[str, object]]):
    def spawn(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        return process

    return spawn


def test_it_builds_a_sonata_resource() -> None:
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(FakeProcess(), [])
    )

    assert isinstance(resource, Resource)
    assert resource.title == "Acquire thing"
    assert resource.release_title == "Release thing"


def test_acquire_spawns_with_argv_and_cwd() -> None:
    seen: list[dict[str, object]] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("java", "-jar", "app.jar"),
        cwd=Path("/repo"),
        ready=lambda: True,
        spawn=_spawner(FakeProcess(), seen),
    )

    resource.acquire()

    assert seen[0]["argv"] == ("java", "-jar", "app.jar")
    assert seen[0]["cwd"] == Path("/repo")


def test_acquire_waits_until_ready() -> None:
    attempts = iter([False, False, True])
    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: next(attempts),
        spawn=_spawner(FakeProcess(), []),
        readiness_interval=0.5,
        sleep=slept.append,
    )

    resource.acquire()

    assert slept == [0.5, 0.5]


def test_acquire_gives_up_and_stops_the_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(process, []),
        readiness_attempts=3,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="never became ready"):
        resource.acquire()

    # A failed acquire is never released by the engine, so it must clean up itself.
    assert process.terminated is True


def test_release_terminates_a_live_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(process, [])
    )

    resource.acquire()
    resource.release()

    assert (process.terminated, process.killed) == (True, False)


def test_release_kills_a_process_that_ignores_terminate() -> None:
    import subprocess

    class Stubborn(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self._waits = 0

        def terminate(self) -> None:
            self.terminated = True  # stays alive on purpose

        def wait(self, timeout: float | None = None) -> int:
            self._waits += 1
            if self._waits == 1:
                raise subprocess.TimeoutExpired(cmd="run", timeout=timeout or 0)
            return 0

    process = Stubborn()
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(process, [])
    )

    resource.acquire()
    resource.release()

    assert process.killed is True


def test_release_is_a_no_op_when_nothing_was_started() -> None:
    resource = managed_process_resource(
        title="Acquire thing", argv=("run",), ready=lambda: True, spawn=_spawner(FakeProcess(), [])
    )

    resource.release()  # must not raise
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_process.py -q
```
Expected: FAIL con `ModuleNotFoundError: No module named 'sonata_tasks.process'`.

- [ ] **Step 3: Implementare**

Crea `packages/sonata-tasks/src/sonata_tasks/process.py`:

```python
from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sonata_engine import Resource


def managed_process_resource(
    *,
    title: str,
    argv: tuple[str, ...],
    ready: Callable[[], bool],
    cwd: Path | None = None,
    spawn: Callable[..., Any] = subprocess.Popen,
    readiness_attempts: int = 90,
    readiness_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Resource:
    """A long-running local process as a Sonata resource.

    The acquire hands back a process that is already answering `ready()`, so
    consumers never have to poll for it themselves. The release always stops it,
    and the compiler runs that release even when a consumer fails.

    This is the Sonata twin of `workflow_tasks.components.container.
    managed_process_resource`, not a reuse of it: that one returns the legacy
    engine's `ResourceTask`, which `sonata_tasks` is forbidden to import.
    """
    process: Any | None = None

    def stop() -> None:
        nonlocal process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def acquire() -> None:
        nonlocal process
        process = spawn(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        for attempt in range(readiness_attempts):
            if ready():
                return
            if attempt < readiness_attempts - 1:
                sleep(readiness_interval)
        # The engine never releases an acquire that did not pass, so a process
        # that never came up has to be cleaned up here or it leaks.
        stop()
        raise RuntimeError(f"{title} never became ready")

    # infrastructure stays False on purpose: `keep_infrastructure` skips the release
    # of infrastructure resources, and a spawned child process that outlives the run
    # is a leak, not something a user asked to keep. A process always gets stopped.
    return Resource(title=title, acquire=acquire, release=stop)
```

Aggiorna `packages/sonata-tasks/src/sonata_tasks/__init__.py` aggiungendo l'import e la voce in `__all__`, mantenendo l'ordine alfabetico:

```python
from sonata_tasks.process import managed_process_resource
```

- [ ] **Step 4: Eseguire i test e verificare che passino**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: PASS, 34 test (27 preesistenti + 7 nuovi), copertura sopra il 90%.

- [ ] **Step 5: Controlli statici e contratti**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```
Expected: PASS. Se il contratto si rompe, hai importato qualcosa dal motore legacy: non allargare il contratto, cambia l'import.

- [ ] **Step 6: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add packages/sonata-tasks/src/sonata_tasks/process.py \
        packages/sonata-tasks/src/sonata_tasks/__init__.py \
        packages/sonata-tasks/tests/test_process.py
git commit -m "Add a managed process resource for Sonata"
```

---

### Task 2: Il workflow accetta risorse esterne e costruisce le immagini

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/cli.py`
- Test: `packages/sonata-tasks/tests/test_cli.py`

**Interfaces:**
- Consumes: `managed_process_resource` (Task 1) — non usata qui, ma è la risorsa che il chiamante passerà.
- Produces:
  - `CliFunction` guadagna `build_argv: tuple[str, ...] | None = None`
  - `build_cli_workflow(request, bindings, *, workflow_id="cli", cwd=None, requires: tuple[Resource, ...] = ())`

Quando `requires` non è vuota, **ogni** task del workflow la dichiara fra le proprie risorse, così il compiler la acquisisce prima del primo e la rilascia dopo l'ultimo. Quando una `CliFunction` ha `build_argv`, il workflow emette un task di build dell'immagine prima che quella function venga registrata.

- [ ] **Step 1: Scrivere i test che falliscono**

Aggiungi in fondo a `packages/sonata-tasks/tests/test_cli.py`:

```python
def test_an_external_resource_wraps_the_whole_workflow() -> None:
    executor = ScriptedExecutor()
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda: events.append("start"),
        release=lambda: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        requires=(control_plane,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.acquire-local-control-plane",
        "002.build-nanofaas-cli",
        "003.acquire-word-stats-java",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.release-local-control-plane",
    ]

    workflow.run()
    assert events == ["start", "stop"]


def test_the_external_resource_is_released_when_a_task_fails() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="failed", return_code=1, stderr="boom"
            )
        }
    )
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda: events.append("start"),
        release=lambda: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(executor),
        requires=(control_plane,),
    )

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    assert events == ["start", "stop"]


def test_a_function_with_build_argv_gets_an_image_build_task() -> None:
    executor = ScriptedExecutor()
    function = replace(FUNCTION, build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"))
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-image-word-stats-java",
        "003.acquire-word-stats-java",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
    ]


def test_the_image_build_runs_before_the_function_is_registered() -> None:
    executor = ScriptedExecutor()
    function = replace(FUNCTION, build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"))
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    workflow.run()

    titles = executor.titles
    assert titles.index("Build image word-stats-java") < titles.index("Apply word-stats-java")


def test_without_build_argv_nothing_extra_is_emitted() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    assert "002.build-image-word-stats-java" not in [
        task.task_id for task in workflow.compile().tasks
    ]
```

Aggiungi `Resource` agli import del file di test:

```python
from sonata_engine import Resource, Selection
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_cli.py -q
```
Expected: FAIL — `build_cli_workflow()` non accetta `requires`, e `CliFunction` non ha `build_argv`.

- [ ] **Step 3: Aggiungere `build_argv` a `CliFunction`**

In `packages/sonata-tasks/src/sonata_tasks/cli.py`, nella dataclass `CliFunction`, aggiungi il campo in fondo:

```python
    build_argv: tuple[str, ...] | None = None
```

- [ ] **Step 4: Far accettare a `build_cli_workflow` le risorse esterne e i build**

Sostituisci la firma e il corpo di `build_cli_workflow`:

```python
def build_cli_workflow(
    request: CliWorkflowRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "cli",
    cwd: Path | None = None,
    requires: tuple[Resource, ...] = (),
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove.

    `requires` are resources every task depends on -- typically the control plane
    itself. Declaring them on every task lets the compiler acquire them before the
    first one and release them after the last, failure included, without this
    builder arranging any of it.
    """
    executor = RoleBoundCommandTaskExecutor(bindings)
    workflow = Workflow(workflow_id=workflow_id)
    workflow.add(
        CommandTask(
            title="Build nanofaas-cli",
            argv=("./gradlew", ":nanofaas-cli:installDist", "--no-daemon"),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        ),
        requires=requires,
    )
    for function in request.functions:
        if function.build_argv is not None:
            workflow.add(
                CommandTask(
                    title=f"Build image {function.name}",
                    argv=function.build_argv,
                    executor=executor,
                    role=request.cli_role,
                    cwd=cwd,
                ),
                requires=requires,
            )
    resources = tuple(
        _function_resource(request, function, executor, cwd) for function in request.functions
    )
    workflow.add(
        CommandTask(
            title="List functions",
            argv=_cli_argv(request, "fn", "list"),
            executor=executor,
            role=request.cli_role,
            cwd=cwd,
        ),
        requires=(*requires, *resources),
    )
    for function, resource in zip(request.functions, resources):
        workflow.add(
            CommandTask(
                title=f"Invoke {function.name}",
                argv=_cli_argv(request, "invoke", function.name, "--data", function.payload),
                executor=executor,
                role=request.cli_role,
                cwd=cwd,
                verify=_verify_invocation,
            ),
            requires=(*requires, resource),
        )
    return workflow
```

Aggiungi `Resource` all'import da `sonata_engine` in cima al file, se non c'è già:

```python
from sonata_engine import Resource, Workflow
```

- [ ] **Step 5: Eseguire i test e verificare che passino**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: PASS, 39 test, copertura sopra il 90%.

- [ ] **Step 6: Controlli statici**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add packages/sonata-tasks/src/sonata_tasks/cli.py packages/sonata-tasks/tests/test_cli.py
git commit -m "Let the CLI workflow depend on external resources and build images"
```

---

### Task 3: `backend` conta, e il control plane locale si avvia da solo

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/cli.py`
- Create: `packages/nanolab/scenarios-v2/cli-container.yaml`
- Test: `packages/nanolab/tests/plans/test_cli.py`

**Interfaces:**
- Consumes: `managed_process_resource` (Task 1), `build_cli_workflow(..., requires=...)` e `CliFunction.build_argv` (Task 2).
- Produces: `build_cli_plan` invariata nella firma, ma il suo comportamento dipende ora da `config.backend`.

- [ ] **Step 1: Creare lo scenario**

Crea `packages/nanolab/scenarios-v2/cli-container.yaml`:

```yaml
workflow: cli
backend: container
build: docker
functions:
  - word-stats-java
```

- [ ] **Step 2: Scrivere i test che falliscono**

Aggiungi in fondo a `packages/nanolab/tests/plans/test_cli.py`:

```python
def test_container_backend_wraps_the_workflow_in_a_local_control_plane() -> None:
    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.acquire-local-control-plane",
        "002.build-nanofaas-cli",
        "003.build-image-word-stats-java",
        "004.acquire-word-stats-java",
        "005.list-functions",
        "006.invoke-word-stats-java",
        "007.release-word-stats-java",
        "008.release-local-control-plane",
    ]


def test_container_backend_targets_the_local_control_plane_port() -> None:
    executor = RecordingExecutor()

    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    )
    invoke = next(
        task for task in plan.compile().tasks if task.task_id.endswith(".invoke-word-stats-java")
    )

    assert "http://127.0.0.1:18080" in " ".join(invoke.task.argv)


def test_k8s_backend_keeps_the_explicit_endpoint_and_starts_nothing() -> None:
    plan = build_cli_plan(
        _scenario(backend="k8s"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        endpoint="http://stack.example:30080",
    )
    task_ids = [task.task_id for task in plan.compile().tasks]

    assert not any("local-control-plane" in task_id for task_id in task_ids)
    assert not any("build-image" in task_id for task_id in task_ids)


def test_pool_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="cli workflow supports"):
        build_cli_plan(
            _scenario(backend="pool"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )
```

Il helper `_scenario` esistente accetta già `**overrides`, quindi `_scenario(backend="container")` funziona senza modifiche.

- [ ] **Step 3: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
```
Expected: FAIL — `build_cli_plan` ignora ancora `config.backend`.

- [ ] **Step 4: Implementare**

Sostituisci interamente `packages/nanolab/src/nanolab/plans/cli.py`:

```python
import json
import urllib.error
import urllib.request
from pathlib import Path

from sonata_engine import Resource, Workflow
from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.process import managed_process_resource
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.execution.roles import ExecutionRole

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import _resolve_function

LOCAL_ENDPOINT = "http://127.0.0.1:18080"
LOCAL_HEALTH_URL = "http://127.0.0.1:18081/actuator/health"


def _local_control_plane(repo_root: Path) -> Resource:
    """The control plane running on this machine, deploying functions with Docker.

    The health probe deliberately targets the management port: the actuator never
    lives on the API port, which is what made the old `cli` preflight impossible
    to satisfy.
    """

    def ready() -> bool:
        try:
            with urllib.request.urlopen(LOCAL_HEALTH_URL, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    return managed_process_resource(
        title="Acquire local control plane",
        argv=(
            "java",
            "-jar",
            str(repo_root / "platform/control-plane/build/libs/app.jar"),
            "--server.port=18080",
            "--management.server.port=18081",
            "--sync-queue.enabled=false",
            "--nanofaas.deployment.default-backend=container-local",
            "--nanofaas.container-local.runtime-adapter=docker",
            "--nanofaas.container-local.bind-host=127.0.0.1",
        ),
        cwd=repo_root,
        ready=ready,
    )


def build_cli_plan(
    config: ScenarioConfig,
    bindings: RoleBindings,
    *,
    cli_role: ExecutionRole = "host",
    endpoint: str = "http://127.0.0.1:8080",
    namespace: str = "nanofaas-e2e",
    repo_root: Path | None = None,
) -> Workflow:
    if config.workflow != "cli":
        raise ValueError("CLI plan requires a cli scenario")
    if config.backend not in ("container", "k8s"):
        raise ValueError(f"cli workflow supports container or k8s, not {config.backend!r}")
    root = repo_root or Path.cwd()
    local = config.backend == "container"
    functions = tuple(
        CliFunction(
            name=resolved.name,
            image=resolved.image,
            payload=json.dumps(json.loads(resolved.payload)["input"], separators=(",", ":")),
            resources=resolved.resources,
            build_argv=resolved.build_argv if local else None,
        )
        for key in config.functions
        for resolved in (_resolve_function(config, key),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role=cli_role,
        endpoint=LOCAL_ENDPOINT if local else endpoint,
        namespace=namespace,
    )
    requires = (_local_control_plane(root),) if local else ()
    return build_cli_workflow(request, bindings, cwd=root, requires=requires)
```

- [ ] **Step 5: Eseguire i test e verificare che passino**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
```
Expected: PASS.

Il control plane non viene mai avviato in questi test: si asserisce solo sulla topologia compilata, e `compile()` non esegue nulla. Il test che chiama `.run()` usa `backend="k8s"`, che non ha risorse esterne.

- [ ] **Step 6: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add packages/nanolab/src/nanolab/plans/cli.py \
        packages/nanolab/scenarios-v2/cli-container.yaml \
        packages/nanolab/tests/plans/test_cli.py
git commit -m "Honor the scenario backend in the cli plan"
```

---

### Task 4: Togliere il preflight dal percorso sonata

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Test: `packages/nanolab/tests/cli/test_command_surface.py`

**Interfaces:**
- Consumes: `uses_sonata` (già presente in `product.py`).
- Produces: nessuna nuova API.

**Perché.** Con il control plane espresso come `Resource`, la readiness è garantita dall'acquire — chi acquisisce una risorsa è responsabile di consegnarla pronta. Il preflight per `cli` diventa ridondante, e nella sua forma attuale è comunque insoddisfacibile: sonda `/actuator/health` sulla porta dell'API, mentre l'actuator sta sempre su una porta diversa.

`preflight_control_plane` **non si cancella**: la funzione resta e continua a servire il resto. Cambia solo chi la chiama.

**Deviazione consapevole dallo spec.** D3 dice che per `backend: k8s` deve restare un controllo di raggiungibilità, corretto sulla porta di management. Questo task invece lo toglie da tutto il percorso sonata, k8s compreso. La ragione: con `backend: k8s` l'URL è esplicito e il primo task del workflow è già una chiamata CLI verso quell'URL, quindi un preflight duplicherebbe lo stesso fallimento pochi secondi prima e con un messaggio peggiore. Aggiungere invece un secondo endpoint di management da configurare per il solo caso k8s è configurazione nuova per valore marginale. Se preferisci mantenere il controllo, va aggiunto qui insieme al test contro un control plane vero che lo spec richiede.

- [ ] **Step 1: Scrivere il test che fallisce**

Aggiungi in `packages/nanolab/tests/cli/test_command_surface.py`:

```python
def test_run_does_not_preflight_a_sonata_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    preflight = MagicMock()
    workflow = MagicMock()
    monkeypatch.setattr(product_module, "preflight_control_plane", preflight)
    monkeypatch.setattr(product_module, "_workflow", MagicMock(return_value=workflow))

    result = CliRunner().invoke(app, ["run", "scenarios-v2/cli-container.yaml"])

    assert result.exit_code == 0, result.output
    preflight.assert_not_called()
    workflow.run.assert_called_once()
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml \
    packages/nanolab/tests/cli/test_command_surface.py -k preflight -q
```
Expected: FAIL — il preflight viene ancora chiamato.

- [ ] **Step 3: Implementare**

In `packages/nanolab/src/nanolab/cli/product.py`, in `run_command`, sostituisci il blocco:

```python
                    try:
                        preflight_control_plane(
                            scenario_config,
                            environment_config,
                            base_url=effective_control_plane_url,
                        )
                    except PreflightError as exc:
                        typer.echo(f"Error: {exc}", err=True)
                        raise typer.Exit(1) from None
```

con:

```python
                    # Sonata workflows express the control plane as a resource, so
                    # its acquire already delivers it ready. The legacy preflight
                    # also probes /actuator/health on the API port, which the
                    # actuator never listens on -- it could not pass regardless.
                    if not uses_sonata(scenario_config):
                        try:
                            preflight_control_plane(
                                scenario_config,
                                environment_config,
                                base_url=effective_control_plane_url,
                            )
                        except PreflightError as exc:
                            typer.echo(f"Error: {exc}", err=True)
                            raise typer.Exit(1) from None
```

- [ ] **Step 4: Eseguire la suite nanolab**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
```
Expected: PASS. I test preesistenti che verificano il preflight su uno scenario `cli` vanno aggiornati: `preflight_control_plane` non si applica più a `cli`. Non reintrodurre la chiamata per farli passare — aggiorna il test o spostalo su uno scenario legacy.

- [ ] **Step 5: Controlli statici**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add packages/nanolab/src/nanolab/cli/product.py \
        packages/nanolab/tests/cli/test_command_surface.py
git commit -m "Stop preflighting scenarios whose control plane is a resource"
```

---

### Task 5: Il gate di CI e la validazione a freddo

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: tutto quanto sopra.
- Produces: nessuna API.

- [ ] **Step 1: Aggiungere il nuovo scenario alla generazione dei piani**

In `.github/workflows/ci.yml`, nello step `Generate representative plans`, aggiungi in fondo:

```yaml
          uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-container.yaml
```

- [ ] **Step 2: Verificare il piano in locale**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli-container.yaml
```
Expected: otto righe, da `001.acquire-local-control-plane` a `008.release-local-control-plane`. `plan` non avvia niente.

- [ ] **Step 3: Costruire il jar del control plane**

Serve una sola volta, ed è il presupposto che l'acquire dà per soddisfatto.

Run:
```bash
cd ~/Downloads/mcFaas
./gradlew :control-plane:bootJar -PcontrolPlaneModules=container-deployment-provider --no-daemon
ls -la platform/control-plane/build/libs/app.jar
```
Expected: il jar esiste.

- [ ] **Step 4: La validazione a freddo — è il criterio di chiusura**

Con Docker attivo e **niente in ascolto** su 18080/18081, senza argomenti aggiuntivi:

```bash
cd ~/Downloads/nanolab
curl -sS -m 2 http://127.0.0.1:18080/v1/functions && echo "ERRORE: qualcosa e' gia' in ascolto" && exit 1
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab run packages/nanolab/scenarios-v2/cli-container.yaml
echo "EXIT=$?"
```

Expected: EXIT=0, otto fasi passate. Verifica poi che non resti niente acceso:

```bash
curl -sS -m 2 http://127.0.0.1:18080/v1/functions 2>&1 | head -1   # deve fallire: il processo e' stato fermato
docker ps --filter "name=nanofaas-" --format '{{.Names}}'          # deve essere vuoto
```

**Non usare pipe sul comando `nanolab run`.** Una `| tail` maschera l'exit code e un fallimento passa per successo — è successo davvero durante la validazione dell'incremento precedente.

- [ ] **Step 5: Verificare che il fallimento pulisca**

Con il jar spostato temporaneamente, l'acquire deve fallire e non lasciare processi:

```bash
cd ~/Downloads/mcFaas && mv platform/control-plane/build/libs/app.jar /tmp/app.jar.bak
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab run packages/nanolab/scenarios-v2/cli-container.yaml
echo "EXIT=$?"
pgrep -f "control-plane/build/libs/app.jar" || echo "nessun processo residuo: corretto"
mv /tmp/app.jar.bak ~/Downloads/mcFaas/platform/control-plane/build/libs/app.jar
```
Expected: exit diverso da 0, messaggio leggibile, nessun processo residuo.

- [ ] **Step 6: Verifica finale**

Run tutti i comandi elencati in Global Constraints. Expected: tutti PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add .github/workflows/ci.yml
git commit -m "Generate the cli-container plan in CI"
```
