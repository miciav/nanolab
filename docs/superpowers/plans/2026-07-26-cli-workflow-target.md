# CLI Workflow Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dare al workflow `cli` un bersaglio che funziona: con `backend: container` il workflow avvia da sé un control plane locale su Docker, ci gira contro e lo spegne, senza VM, senza k8s e senza argomenti aggiuntivi.

**Architecture:** Il control plane locale diventa una `Resource` sonata: il compiler la acquisisce appena prima del primo task che lo usa e la rilascia dopo l'ultimo, anche in caso di fallimento. Prima dell'acquire il workflow costruisce CLI, jar del control plane e immagini delle function, così un checkout pulito non richiede preparazione manuale. `sonata-tasks` guadagna un helper generico per processi gestiti; comando Gradle, comando Java e URL di salute restano in `nanolab`.

**Tech Stack:** Python 3.12, uv workspace, pytest, Ruff, basedpyright, import-linter, sonata-engine.

**Spec:** `docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md`

## Global Constraints

- **Solo il percorso sonata.** I workflow legacy (`validate`, `loadtest`, `offload`, `offload-loadtest`) non si toccano. La guardia `preflight_control_plane`, non usata da quei workflow, viene rimossa insieme ai suoi soli chiamanti `cli`.
- **`backend: k8s` richiede davvero un `--control-plane-url` esplicito.** Nessun default implicito a localhost: l'assenza dell'opzione fallisce prima di costruire il workflow. Il provisioning di VM e piattaforma è fuori scope, e così l'attesa di readiness che serve solo lì.
- **Nessun preflight HTTP separato per `cli`.** Nel caso `container` la readiness appartiene all'acquire della risorsa; nel caso `k8s` la prima chiamata CLI verso l'URL esplicito è la verifica di raggiungibilità. Questa è la scelta minima: niente secondo URL di management e niente richiesta duplicata. Il Task 4 aggiorna la spec D3 per registrarla.
- **Stesso comportamento da CLI e TUI.** La TUI usa lo scenario locale funzionante e non esegue una guardia diversa dal comando `run`.
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
| `packages/nanolab/src/nanolab/cli/product.py` | conserva l'assenza dell'URL e rimuove il preflight duplicato | modify |
| `packages/nanolab/src/nanolab/tui/app.py` | usa lo scenario container e non preflighta separatamente | modify |
| `packages/nanolab/src/nanolab/cli/preflight.py` | guardia CLI rimasta senza chiamanti | **delete** |
| `packages/nanolab/scenarios-v2/cli-container.yaml` | scenario con control plane locale | **create** |
| `packages/nanolab/tests/plans/test_cli.py` | topologia per entrambi i backend | modify |
| `packages/nanolab/tests/cli/test_command_surface.py` | il preflight non blocca più `cli` | modify |
| `packages/nanolab/tests/cli/test_preflight.py` | test della guardia rimasta senza chiamanti | **delete** |
| `packages/nanolab/tests/test_tui_app.py` | percorso TUI locale senza preflight | modify |
| `docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md` | registra la decisione finale su D3 e il gate reale | modify |
| `.github/workflows/ci.yml` | genera il piano ed esegue lo smoke test reale | modify |

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


def test_acquire_fails_immediately_when_the_process_exits() -> None:
    class Exited(FakeProcess):
        def poll(self) -> int | None:
            return 23

    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(Exited(), []),
        sleep=slept.append,
    )

    with pytest.raises(RuntimeError, match="exited with code 23"):
        resource.acquire()

    assert slept == []


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
        current = spawn(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if current is None:  # pragma: no cover - invalid injected spawn contract
            raise RuntimeError(f"{title} failed to start")
        process = current
        for attempt in range(readiness_attempts):
            exit_code = current.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"{title} exited with code {exit_code} before becoming ready"
                )
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
Expected: PASS, 35 test (27 preesistenti + 8 nuovi), copertura sopra il 90%.

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

### Task 2: Il workflow costruisce gli artefatti prima delle risorse esterne

**Files:**
- Modify: `packages/sonata-tasks/src/sonata_tasks/cli.py`
- Test: `packages/sonata-tasks/tests/test_cli.py`

**Interfaces:**
- Consumes: `managed_process_resource` (Task 1) — non usata qui, ma è la risorsa che il chiamante passerà.
- Produces:
  - `CliFunction` guadagna `build_argv: tuple[str, ...] | None = None`
  - `build_cli_workflow(request, bindings, *, workflow_id="cli", cwd=None, control_plane_build_argv=None, requires=())`

`control_plane_build_argv` è solo il comando opzionale da eseguire: il valore concreto resta in `nanolab`. `requires` si applica esclusivamente ai task che parlano con il control plane, non ai build. Il compiler acquisisce così il control plane dopo la costruzione di CLI, jar e immagini e lo rilascia dopo l'ultimo consumer. Quando una `CliFunction` ha `build_argv`, il workflow emette il build dell'immagine prima che quella function venga registrata.

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
        "001.build-nanofaas-cli",
        "002.acquire-local-control-plane",
        "003.acquire-word-stats-java",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.release-local-control-plane",
    ]

    workflow.run()
    assert events == ["start", "stop"]


def test_build_only_selection_does_not_acquire_the_external_resource() -> None:
    events: list[str] = []
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda: events.append("start"),
        release=lambda: events.append("stop"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)),
        _bindings(ScriptedExecutor()),
        requires=(control_plane,),
    )

    workflow.run(select=Selection(only="build-nanofaas-cli"))

    assert events == []


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


def test_the_control_plane_build_runs_before_images_and_acquire() -> None:
    control_plane = Resource(
        title="Acquire local control plane",
        acquire=lambda: None,
        release=lambda: None,
    )
    function = replace(
        FUNCTION,
        build_argv=("./gradlew", ":functions:java:word-stats:bootBuildImage"),
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)),
        _bindings(ScriptedExecutor()),
        control_plane_build_argv=("./gradlew", ":control-plane:bootJar"),
        requires=(control_plane,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.build-local-control-plane",
        "003.build-image-word-stats-java",
        "004.acquire-local-control-plane",
        "005.acquire-word-stats-java",
        "006.list-functions",
        "007.invoke-word-stats-java",
        "008.release-word-stats-java",
        "009.release-local-control-plane",
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
Expected: FAIL — `build_cli_workflow()` non accetta `requires`/`control_plane_build_argv`, e `CliFunction` non ha `build_argv`.

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
    control_plane_build_argv: tuple[str, ...] | None = None,
    requires: tuple[Resource, ...] = (),
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove.

    Build commands do not require the control plane. `requires` wraps only commands
    that use its API, so selection of a build task stays offline.
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
        )
    )
    if control_plane_build_argv is not None:
        workflow.add(
            CommandTask(
                title="Build local control plane",
                argv=control_plane_build_argv,
                executor=executor,
                role=request.cli_role,
                cwd=cwd,
            )
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
                )
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
Expected: PASS, 42 test, copertura sopra il 90%.

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
git commit -m "Build CLI artifacts before acquiring external resources"
```

---

### Task 3: `backend` conta, e il control plane locale si avvia da solo

**Files:**
- Modify: `packages/nanolab/src/nanolab/plans/cli.py`
- Create: `packages/nanolab/scenarios-v2/cli-container.yaml`
- Test: `packages/nanolab/tests/plans/test_cli.py`

**Interfaces:**
- Consumes: `managed_process_resource` (Task 1), `build_cli_workflow(..., control_plane_build_argv=..., requires=...)` e `CliFunction.build_argv` (Task 2).
- Produces: `build_cli_plan(..., endpoint: str | None = None)`, con endpoint locale calcolato per `container` e obbligatorio per `k8s`.

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
        "001.build-nanofaas-cli",
        "002.build-local-control-plane",
        "003.build-image-word-stats-java",
        "004.acquire-local-control-plane",
        "005.acquire-word-stats-java",
        "006.list-functions",
        "007.invoke-word-stats-java",
        "008.release-word-stats-java",
        "009.release-local-control-plane",
    ]


def test_container_backend_builds_the_control_plane_with_the_container_module() -> None:
    plan = build_cli_plan(
        _scenario(backend="container"),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
    )
    build = next(
        task for task in plan.compile().tasks
        if task.task_id.endswith(".build-local-control-plane")
    )

    assert build.task.argv == (
        "./gradlew",
        ":control-plane:bootJar",
        "-PcontrolPlaneModules=container-deployment-provider",
        "--no-daemon",
    )


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
    invoke = next(
        task for task in plan.compile().tasks
        if task.task_id.endswith(".invoke-word-stats-java")
    )

    assert not any("local-control-plane" in task_id for task_id in task_ids)
    assert not any("build-local-control-plane" in task_id for task_id in task_ids)
    assert not any("build-image" in task_id for task_id in task_ids)
    assert "http://stack.example:30080" in " ".join(invoke.task.argv)


def test_k8s_backend_requires_an_explicit_endpoint() -> None:
    with pytest.raises(ValueError, match="explicit control-plane URL"):
        build_cli_plan(
            _scenario(backend="k8s"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )


def test_container_backend_runs_only_on_the_host_role() -> None:
    with pytest.raises(ValueError, match="host role"):
        build_cli_plan(
            _scenario(backend="container"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
            cli_role="stack",
        )


def test_pool_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="cli workflow supports"):
        build_cli_plan(
            _scenario(backend="pool"),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )
```

Il helper `_scenario` esistente accetta già `**overrides`, quindi `_scenario(backend="container")` funziona senza modifiche. Aggiorna inoltre tutti i test preesistenti che costruiscono un piano `k8s` valido passando esplicitamente `endpoint="http://stack.example:30080"`; il solo test negativo lo omette.

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
LOCAL_CONTROL_PLANE_BUILD_ARGV = (
    "./gradlew",
    ":control-plane:bootJar",
    "-PcontrolPlaneModules=container-deployment-provider",
    "--no-daemon",
)


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
    endpoint: str | None = None,
    namespace: str = "nanofaas-e2e",
    repo_root: Path | None = None,
) -> Workflow:
    if config.workflow != "cli":
        raise ValueError("CLI plan requires a cli scenario")
    if config.backend not in ("container", "k8s"):
        raise ValueError(f"cli workflow supports container or k8s, not {config.backend!r}")
    root = repo_root or Path.cwd()
    local = config.backend == "container"
    if local and cli_role != "host":
        raise ValueError("container cli workflow must run on the host role")
    if not local and endpoint is None:
        raise ValueError("k8s cli workflow requires an explicit control-plane URL")
    target_endpoint = LOCAL_ENDPOINT if local else endpoint
    assert target_endpoint is not None
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
        endpoint=target_endpoint,
        namespace=namespace,
    )
    requires = (_local_control_plane(root),) if local else ()
    return build_cli_workflow(
        request,
        bindings,
        cwd=root,
        control_plane_build_argv=LOCAL_CONTROL_PLANE_BUILD_ARGV if local else None,
        requires=requires,
    )
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

### Task 4: Un solo contratto per CLI, TUI ed endpoint esterno

**Files:**
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/tui/app.py`
- Delete: `packages/nanolab/src/nanolab/cli/preflight.py`
- Modify: `packages/nanolab/tests/cli/test_command_surface.py`
- Delete: `packages/nanolab/tests/cli/test_preflight.py`
- Modify: `packages/nanolab/tests/test_tui_app.py`
- Modify: `docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md`

**Interfaces:**
- Consumes: `build_cli_plan(..., endpoint=None)` dal Task 3.
- Produces: `run` e `plan` rifiutano un `cli/k8s` senza `--control-plane-url`; la TUI usa `cli-container.yaml`.

**Perché.** `preflight_control_plane` è specifica per `cli`; gli altri workflow ricevono già un no-op. Dopo aver spostato la readiness nell'acquire locale e aver reso obbligatorio l'URL esterno, non resta alcun chiamante utile: mantenere modulo e test significherebbe conservare codice morto. Il caso `k8s` non introduce un secondo URL di management solo per anticipare lo stesso errore che produrrà la prima chiamata CLI.

- [ ] **Step 1: Scrivere i test che falliscono**

In `packages/nanolab/tests/cli/test_command_surface.py`, sostituisci `test_run_aborts_local_cli_before_building_workflow_when_preflight_fails` con:

```python
def test_run_requires_an_explicit_url_for_k8s_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(app, ["run", "scenarios-v2/cli.yaml"])

    assert result.exit_code != 0
    assert "--control-plane-url is required for a k8s cli scenario" in result.output
    assert "Traceback" not in result.output
    build.assert_not_called()
```

Aggiorna `test_run_uses_custom_control_plane_url_for_preflight_and_cli_plan`: rinominalo `test_run_passes_custom_control_plane_url_to_cli_plan`, rimuovi il mock e le asserzioni sul preflight e conserva l'asserzione su `build_cli_plan(..., endpoint=...)`.

Aggiungi:

```python
def test_run_container_cli_builds_and_runs_without_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = MagicMock()
    build = MagicMock(return_value=workflow)
    monkeypatch.setattr(product_module, "_workflow", build)

    result = CliRunner().invoke(
        app,
        ["run", "scenarios-v2/cli-container.yaml"],
    )

    assert result.exit_code == 0, result.output
    assert build.call_args.kwargs["control_plane_url"] is None
    workflow.run.assert_called_once()
```

Nello stesso file:

- rimuovi l'import di `PreflightError`;
- elimina `test_plan_does_not_run_preflight`, ormai privo di comportamento da testare;
- usa `cli-container.yaml` nei test di rendering, slicing, slug invalido e passaggio della `Selection` che non stanno verificando il caso `k8s`;
- aggiorna il rendering completo ad aspettarsi `001.build-nanofaas-cli` e `009.release-local-control-plane`;
- aggiorna lo slice `--only list-functions` ad aspettarsi acquire/release sia della function sia del control plane locale;
- rimuovi il monkeypatch del preflight da `test_run_passes_the_requested_selection_to_sonata`.

In `packages/nanolab/tests/test_tui_app.py`:

- aggiorna il dispatch CLI atteso da `cli.yaml` a `cli-container.yaml`;
- elimina `test_failed_cli_preflight_uses_static_view_without_starting_workflow`;
- rimuovi l'import di `PreflightError`;
- rimuovi tutti i monkeypatch di `preflight_control_plane`;
- aggiungi un'asserzione nel test del run CLI che lo scenario caricato abbia `backend == "container"`.

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml \
    packages/nanolab/tests/cli/test_command_surface.py \
    packages/nanolab/tests/test_tui_app.py -q
```
Expected: FAIL — `run` usa ancora il default localhost, la TUI apre ancora `cli.yaml` e chiama ancora il preflight.

- [ ] **Step 3: Rendere esplicito l'endpoint nel command surface**

In `packages/nanolab/src/nanolab/cli/product.py`:

1. rimuovi l'import di `PreflightError` e `preflight_control_plane`;
2. cambia il default di `_workflow(..., control_plane_url=...)` in `None`;
3. nel ramo `cli`, passa il valore opzionale direttamente a `build_cli_plan`;
4. per gli altri workflow usa `control_plane_url or "http://127.0.0.1:8080"` dove serve;
5. aggiungi:

```python
def _require_cli_endpoint(
    scenario: ScenarioConfig,
    control_plane_url: str | None,
) -> None:
    if (
        scenario.workflow == "cli"
        and scenario.backend == "k8s"
        and control_plane_url is None
    ):
        raise typer.BadParameter(
            "--control-plane-url is required for a k8s cli scenario"
        )
```

6. chiama `_require_cli_endpoint(scenario_config, control_plane_url)` in `run_command` e `plan_command` prima di `_workflow`;
7. elimina interamente il blocco che invoca `preflight_control_plane`;
8. passa `control_plane_url` a `_workflow` senza sostituirlo prima con localhost.

Il default locale per i workflow legacy resta confinato a `_workflow`; non deve più rendere indistinguibile “opzione assente” da “localhost scelto”.

- [ ] **Step 4: Allineare la TUI e rimuovere il codice morto**

In `packages/nanolab/src/nanolab/tui/app.py`:

```python
_SCENARIO_FILES = {
    # ...voci invariate...
    ("cli", "validate"): "cli-container.yaml",
    # ...voci invariate...
}
```

Rimuovi l'import di `PreflightError`/`preflight_control_plane` e l'intero blocco `try/except` che li usa. La preview compila già il piano e l'acquire della run consegna il control plane pronto.

Elimina:

```text
packages/nanolab/src/nanolab/cli/preflight.py
packages/nanolab/tests/cli/test_preflight.py
```

Verifica che non restino riferimenti:

```bash
rg -n "PreflightError|preflight_control_plane" packages/nanolab
```

Expected: nessun risultato.

- [ ] **Step 5: Allineare la spec**

In `docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md`:

1. in D1 elimina “come oggi” dal caso `k8s` e specifica che l'assenza di `--control-plane-url` è un errore;
2. in D2 sostituisci l'ordine compilato con:

```text
001.build-nanofaas-cli
002.build-local-control-plane
003.build-image-word-stats-java
004.acquire-local-control-plane
005.acquire-word-stats-java
006.list-functions
007.invoke-word-stats-java
008.release-word-stats-java
009.release-local-control-plane
```

Spiega subito sotto che i build restano fuori dal lifetime della risorsa e che il jar viene costruito dal workflow, non preparato a mano.

3. sostituisci D3 con:

```markdown
### D3 — Nessun preflight separato nel percorso `cli`

Per `backend: container`, l'acquire del control plane attende la porta di management
e consegna la risorsa pronta.

Per `backend: k8s`, `--control-plane-url` è obbligatorio e la prima chiamata CLI è la
verifica di raggiungibilità. Non si introduce un secondo URL di management per
duplicare la stessa richiesta pochi secondi prima.

Il gate contro un control plane vero è lo smoke test `cli-container` in CI: parte a
porte libere, esegue il workflow completo e verifica lo spegnimento finale.
```

4. nella sezione `Verifica`, sostituisci il bullet sul test del preflight reale con:

```markdown
- Smoke test CI a freddo contro un control plane vero, con verifica delle porte e
  dei container prima e dopo il run.
```

- [ ] **Step 6: Eseguire la suite nanolab e i controlli statici**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add packages/nanolab/src/nanolab/cli/product.py \
        packages/nanolab/src/nanolab/tui/app.py \
        packages/nanolab/tests/cli/test_command_surface.py \
        packages/nanolab/tests/test_tui_app.py \
        docs/superpowers/specs/2026-07-26-cli-workflow-target-design.md
git add -u packages/nanolab/src/nanolab/cli/preflight.py \
           packages/nanolab/tests/cli/test_preflight.py
git commit -m "Use one control plane contract across CLI entry points"
```

---

### Task 5: Il gate reale di CI

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: tutto quanto sopra.
- Produces: un run reale `cli-container` su Docker, non soltanto la compilazione del piano.

- [ ] **Step 1: Aggiornare la generazione dei piani**

Nello step `Generate representative plans`, sostituisci la riga di `cli.yaml` e aggiungi il caso locale:

```yaml
          uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli.yaml --control-plane-url http://control-plane.example:30080
          uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli-container.yaml
```

- [ ] **Step 2: Verificare il piano locale**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli-container.yaml
```
Expected: nove righe, da `001.build-nanofaas-cli` a `009.release-local-control-plane`. `plan` non avvia processi e non costruisce artefatti.

- [ ] **Step 3: Aggiungere lo smoke test reale**

Dopo `Generate representative plans`, aggiungi:

```yaml
      - name: Run cli-container smoke test
        run: |
          set -euo pipefail
          if curl -sS -m 2 http://127.0.0.1:18080/v1/functions >/dev/null 2>&1; then
            echo "API port 18080 is already in use" >&2
            exit 1
          fi
          if curl -sS -m 2 http://127.0.0.1:18081/actuator/health >/dev/null 2>&1; then
            echo "Management port 18081 is already in use" >&2
            exit 1
          fi
          uv run --locked --package nanolab \
            nanolab run packages/nanolab/scenarios-v2/cli-container.yaml
          if curl -sS -m 2 http://127.0.0.1:18080/v1/functions >/dev/null 2>&1; then
            echo "API port 18080 remained open after cleanup" >&2
            exit 1
          fi
          if curl -sS -m 2 http://127.0.0.1:18081/actuator/health >/dev/null 2>&1; then
            echo "Management port 18081 remained open after cleanup" >&2
            exit 1
          fi
          remaining="$(docker ps --filter "name=nanofaas-" --format '{{.Names}}')"
          test -z "$remaining"
```

Il comando `nanolab run` resta una riga autonoma sotto `set -e`: nessuna pipe e nessun `echo $?` può mascherarne il fallimento. Il workflow costruisce da sé il jar, quindi non si spostano artefatti e non serve alcun ripristino manuale. Il fallimento durante un consumer e l'uscita anticipata del processo sono già coperti rispettivamente nei Task 2 e 1.

- [ ] **Step 4: Ripetere lo smoke test in locale**

Con Docker attivo:

```bash
cd ~/Downloads/nanolab
set -euo pipefail
if curl -sS -m 2 http://127.0.0.1:18080/v1/functions >/dev/null 2>&1; then
  echo "API port 18080 is already in use" >&2
  exit 1
fi
if curl -sS -m 2 http://127.0.0.1:18081/actuator/health >/dev/null 2>&1; then
  echo "Management port 18081 is already in use" >&2
  exit 1
fi
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
  nanolab run packages/nanolab/scenarios-v2/cli-container.yaml
test -z "$(docker ps --filter "name=nanofaas-" --format '{{.Names}}')"
```

Expected: exit 0, nove fasi passate, nessun container `nanofaas-*` residuo. Ripeti i due controlli `curl` dello step CI per verificare che entrambe le porte siano chiuse.

- [ ] **Step 5: Verifica finale**

Run tutti i comandi elencati in Global Constraints. Expected: tutti PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/Downloads/nanolab
git status --short
git add .github/workflows/ci.yml
git commit -m "Run the cli-container smoke test in CI"
```
