# Sonata CLI Workflow Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggiungere la selezione di sotto-workflow a sonata-engine, reimplementare il workflow `cli` di nanolab su sonata dentro una nuova libreria `sonata-tasks`, e cancellare l'originale.

**Architecture:** Sonata guadagna `Selection` e `select=` su `compile()`/`run()`: il filtro agisce sulle definizioni consumer prima che il compiler splici acquire/release, così una slice conserva il proprio cleanup. `sonata-tasks` contiene una sola classe task (`CommandTask`, con hook di verifica opzionale) e il builder del workflow `cli`; riusa gli executor di `workflow-tasks` perché sonata per statuto non prende remote execution. nanolab instrada il workflow `cli` sul ramo migrato in `cli/product.py` e nella TUI, poi l'implementazione originale viene cancellata.

**Tech Stack:** Python 3.12, uv workspace, pytest, Ruff, basedpyright, import-linter, dataclasses.

**Spec:** `docs/superpowers/specs/2026-07-25-sonata-migration-cli-workflow-design.md`

## Global Constraints

- **Due repository.** Sonata: `~/Downloads/sonata` (branch `main`, parte da `f13c830`). nanolab: `~/Downloads/nanolab` (branch `main`). I task 1–3 sono in sonata, i task 4–9 in nanolab.
- **C1 — l'identità del task è del compiler.** Nessun task dichiara un `task_id`. Non si introduce una seconda identità (niente `operation_id`, alias, o campo stabile "per lo slicing"). `CommandTaskSpec.task_id` si valorizza a `""`.
- **C2 — il cleanup è una `Resource`.** Niente liste `cleanup_tasks`. Un acquire che può produrre un effetto prima di fallire compensa localmente best-effort prima di rilanciare; l'errore della compensazione si aggiunge come nota senza mascherare il primario.
- **C3 — lo slicing è dell'engine.** Il filtro sta in sonata, non in nanolab. I plan builder restituiscono un `Workflow` costruito con `add()`.
- **C4 — convivenza breve.** Nessuno scenario `cli-sonata.yaml`, nessun comando `nanolab sonata run`, nessun adapter che faccia sembrare un `Workflow` sonata un `Workflow` legacy.
- **C5 — riuso del layer di esecuzione.** `CommandTaskSpec`, `HostCommandTaskExecutor`, `VmCommandTaskExecutor`, `RoleBindings` si riusano da `workflow-tasks`, non si reimplementano.
- **C6 — sonata si modifica, non si aggira.** Durante lo sviluppo `sonata-tasks` punta a sonata via source locale uv; il Task 9 sostituisce il pin con `rev` completo immutabile e allinea il lockfile.
- **Boundary di sonata:** nessun import di `nanofaas`, `nanolab`, `workflow_tasks`, provider VM, Ansible, load-test. Verificato da `tests/test_package_boundaries.py`.
- **Python 3.12:** `sonata-engine` richiede `>=3.12`. Il Task 4 alza `requires-python` a `>=3.12` sul workspace root e su `packages/nanolab`.
- **Messaggi di commit: NIENTE trailer `Co-Authored-By`** (requisito utente, sovrascrive il default dell'harness).
- **Comandi di test (verificati funzionanti prima di scrivere questo piano):**

  Sonata, da `~/Downloads/sonata`:
  ```bash
  uv run pytest
  uv run ruff check --no-cache .
  uv run basedpyright
  ```

  nanolab, da `~/Downloads/nanolab` (la variabile `NANOFAAS_ROOT` è obbligatoria, altrimenti 12 file di test falliscono in collection con `KeyError: 'NANOFAAS_ROOT'`):
  ```bash
  NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
      pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
  uv run --all-packages --all-groups \
      pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
  uv run --all-packages --all-groups \
      pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
  ```

- **Baseline verdi da cui si parte:** sonata 124 test; workflow-tasks copertura 93.17% (soglia 90%); nanolab 654 test.
- **Se `pytest` fallisce con `bad interpreter` o risolve un Python di sistema:** il venv ha shebang stantii. Rigenera con `uv venv --clear && uv sync --all-packages --all-groups` da `~/Downloads/nanolab`.

## File Structure

### Repository sonata (`~/Downloads/sonata`)

| File | Responsabilità | Cambio |
|---|---|---|
| `src/sonata_engine/core/selection.py` | `Selection`: quali consumer sopravvivono | **create** |
| `src/sonata_engine/errors.py` | aggiungere `SelectionError` | modify |
| `src/sonata_engine/core/workflow.py` | `compile(select=)`, `run(select=)`, risoluzione slug | modify |
| `src/sonata_engine/core/__init__.py` | esportare `Selection` | modify |
| `src/sonata_engine/__init__.py` | esportare `Selection`, `SelectionError` | modify |
| `README.md` | sezione sulla selezione | modify |
| `tests/core/test_selection.py` | validazione dell'oggetto `Selection` | **create** |
| `tests/core/test_compile_selection.py` | filtro, ambiguità, risorse preservate, rinumerazione | **create** |
| `tests/core/test_run_selection.py` | `run(select=)` e fail-closed con `resume` | **create** |

### Repository nanolab (`~/Downloads/nanolab`)

| File | Responsabilità | Cambio |
|---|---|---|
| `pyproject.toml` | `requires-python`, membro workspace `sonata-tasks` | modify |
| `packages/sonata-tasks/pyproject.toml` | metadati del package | **create** |
| `packages/sonata-tasks/.importlinter` | contratto di boundary | **create** |
| `packages/sonata-tasks/src/sonata_tasks/__init__.py` | export pubblici | **create** |
| `packages/sonata-tasks/src/sonata_tasks/command.py` | `CommandTask`: un comando via executor, con verifica opzionale | **create** |
| `packages/sonata-tasks/src/sonata_tasks/cli.py` | modelli richiesta, risorse function, `build_cli_workflow()` | **create** |
| `packages/sonata-tasks/tests/test_command.py` | comportamento di `CommandTask` | **create** |
| `packages/sonata-tasks/tests/test_cli.py` | topologia compilata, cleanup, verifica invoke, slicing | **create** |
| `packages/nanolab/pyproject.toml` | `requires-python`, dipendenza `sonata-tasks` | modify |
| `packages/nanolab/.importlinter` | `sonata_tasks` non dipende da `nanolab` | modify |
| `packages/nanolab/src/nanolab/plans/cli.py` | costruisce il `Workflow` sonata | **rewrite** |
| `packages/nanolab/src/nanolab/cli/product.py` | ramo migrato in `run`/`plan`, doppio bind del sink | modify |
| `packages/nanolab/src/nanolab/cli/progress.py` | accetta il vocabolario eventi di sonata | modify |
| `packages/nanolab/src/nanolab/tui/event_aggregator.py` | idem, lato TUI | modify |
| `packages/nanolab/src/nanolab/tui/workflow_controller.py` | doppio bind del sink | modify |
| `packages/nanolab/src/nanolab/tui/app.py` | preview/plan dal `CompiledWorkflow` | modify |
| `packages/nanolab/tests/plans/test_cli.py` | **rewrite** contro il nuovo builder |
| `packages/workflow-tasks/src/workflow_tasks/workflows/cli.py` | | **delete** (Task 9) |
| `packages/workflow-tasks/tests/workflows/test_cli.py` | | **delete** (Task 9) |
| `.github/workflows/ci.yml` | gate per `sonata-tasks` | modify |

---

### Task 1: Il modello `Selection`

**Repository:** `~/Downloads/sonata`

**Files:**
- Create: `src/sonata_engine/core/selection.py`
- Modify: `src/sonata_engine/errors.py`
- Test: `tests/core/test_selection.py`

**Interfaces:**
- Consumes: niente (primo task).
- Produces: `Selection(only: str | None = None, start: str | None = None, until: str | None = None)` con property `is_empty: bool`; `SelectionError(Exception)` esportata da `sonata_engine.errors`.

- [ ] **Step 1: Scrivere il test che fallisce**

Crea `tests/core/test_selection.py`:

```python
from __future__ import annotations

import dataclasses

import pytest

from sonata_engine.core.selection import Selection
from sonata_engine.errors import SelectionError


def test_empty_selection_is_no_filter() -> None:
    assert Selection().is_empty is True


def test_any_field_makes_the_selection_a_filter() -> None:
    assert Selection(only="build").is_empty is False
    assert Selection(start="build").is_empty is False
    assert Selection(until="build").is_empty is False


def test_only_is_mutually_exclusive_with_start() -> None:
    with pytest.raises(SelectionError, match="mutually exclusive"):
        Selection(only="build", start="publish")


def test_only_is_mutually_exclusive_with_until() -> None:
    with pytest.raises(SelectionError, match="mutually exclusive"):
        Selection(only="build", until="publish")


def test_start_and_until_may_be_combined() -> None:
    selection = Selection(start="build", until="publish")

    assert (selection.start, selection.until) == ("build", "publish")


def test_selection_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Selection().only = "build"  # type: ignore[misc]
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_selection.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'sonata_engine.core.selection'`

- [ ] **Step 3: Aggiungere `SelectionError`**

In `src/sonata_engine/errors.py`, in fondo al file:

```python
class SelectionError(Exception):
    """Raised when a `Selection` cannot be resolved against a workflow's tasks.

    Covers a malformed selection (mutually exclusive fields, inverted range) and
    one that does not resolve to exactly one task per endpoint (unknown or
    ambiguous slug).
    """
```

- [ ] **Step 4: Implementare `Selection`**

Crea `src/sonata_engine/core/selection.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from sonata_engine.errors import SelectionError


@dataclass(frozen=True, slots=True)
class Selection:
    """Which consumer tasks survive compilation, addressed by title slug.

    Selection deliberately names tasks by slug rather than by compiled
    `task_id`: ordinals renumber over the survivors, so an ID is not a stable
    handle for the very operation that changes it.

    Only consumer tasks are selectable. Acquire/release units are engine-owned
    and are re-spliced around whichever consumers survive, so a slice keeps its
    cleanup without the caller arranging anything.
    """

    only: str | None = None
    start: str | None = None
    until: str | None = None

    def __post_init__(self) -> None:
        if self.only is not None and (self.start is not None or self.until is not None):
            raise SelectionError("only is mutually exclusive with start and until")

    @property
    def is_empty(self) -> bool:
        """True when this selection filters nothing."""
        return self.only is None and self.start is None and self.until is None
```

- [ ] **Step 5: Eseguire il test e verificare che passi**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_selection.py -v`
Expected: PASS, 6 test.

- [ ] **Step 6: Controlli statici**

Run: `cd ~/Downloads/sonata && uv run ruff check --no-cache . && uv run basedpyright`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/Downloads/sonata
git add src/sonata_engine/core/selection.py src/sonata_engine/errors.py tests/core/test_selection.py
git commit -m "Add the Selection model for workflow slicing"
```

---

### Task 2: `compile(select=)`

**Repository:** `~/Downloads/sonata`

**Files:**
- Modify: `src/sonata_engine/core/workflow.py`
- Test: `tests/core/test_compile_selection.py`

**Interfaces:**
- Consumes: `Selection` e `SelectionError` dal Task 1.
- Produces: `Workflow.compile(*, select: Selection | None = None) -> CompiledWorkflow`. Il metodo privato `_merge_resources` cambia firma e prende le definizioni come parametro: `_merge_resources(self, definitions: list[tuple[Task[Any], tuple[Resource, ...]]]) -> list[CompiledTask[Any]]`.

- [ ] **Step 1: Scrivere i test che falliscono**

Crea `tests/core/test_compile_selection.py`:

```python
from __future__ import annotations

import pytest

from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.selection import Selection
from sonata_engine.core.task import Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import SelectionError


class _Noop(Task[None]):
    def __init__(self, title: str) -> None:
        self.title = title

    def run(self) -> TaskOutcome[None]:
        return TaskOutcome()


def _resource(title: str = "Acquire vm") -> Resource:
    return Resource(title=title, acquire=lambda: None, release=lambda: None)


def _workflow_with_resource() -> tuple[Workflow, Resource]:
    """build -> [acquire] -> list -> invoke -> [release]."""
    resource = _resource()
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("List"), requires=(resource,))
    workflow.add(_Noop("Invoke"), requires=(resource,))
    return workflow, resource


def test_no_selection_compiles_every_task() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build",
        "002.acquire-vm",
        "003.list",
        "004.invoke",
        "005.release-vm",
    ]


def test_empty_selection_is_the_same_as_no_selection() -> None:
    workflow, _resource_ = _workflow_with_resource()

    assert workflow.compile(select=Selection()).tasks == workflow.compile().tasks


def test_only_keeps_one_consumer_and_its_resource_lifecycle() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(only="invoke"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.invoke",
        "003.release-vm",
    ]


def test_only_on_a_task_without_resources_drops_the_lifecycle() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(only="build"))

    assert [task.task_id for task in compiled.tasks] == ["001.build"]


def test_start_keeps_the_inclusive_tail() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(start="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.list",
        "003.invoke",
        "004.release-vm",
    ]


def test_until_keeps_the_inclusive_head() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(until="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.build",
        "002.acquire-vm",
        "003.list",
        "004.release-vm",
    ]


def test_start_and_until_delimit_an_inclusive_range() -> None:
    workflow, _resource_ = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(start="list", until="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.list",
        "003.release-vm",
    ]


def test_unknown_slug_is_rejected_and_lists_what_is_available() -> None:
    workflow, _resource_ = _workflow_with_resource()

    with pytest.raises(SelectionError, match="no task matches slug 'deploy'") as error:
        workflow.compile(select=Selection(only="deploy"))

    assert "build" in str(error.value)


def test_a_resource_slug_is_not_selectable() -> None:
    workflow, _resource_ = _workflow_with_resource()

    with pytest.raises(SelectionError, match="no task matches slug 'acquire-vm'"):
        workflow.compile(select=Selection(only="acquire-vm"))


def test_duplicate_titles_are_ambiguous_when_selected() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("Build"))

    with pytest.raises(SelectionError, match="matches 2 tasks"):
        workflow.compile(select=Selection(only="build"))


def test_duplicate_titles_stay_legal_without_a_selection() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("Build"))

    assert [task.task_id for task in workflow.compile().tasks] == ["001.build", "002.build"]


def test_inverted_range_is_rejected() -> None:
    workflow, _resource_ = _workflow_with_resource()

    with pytest.raises(SelectionError, match="comes after"):
        workflow.compile(select=Selection(start="invoke", until="build"))


def test_a_title_with_no_slug_characters_is_a_compile_error() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("!!!"))

    with pytest.raises(ValueError, match="empty slug"):
        workflow.compile()


def test_compilation_stays_deterministic_under_selection() -> None:
    workflow, _resource_ = _workflow_with_resource()
    selection = Selection(start="list")

    assert workflow.compile(select=selection).tasks == workflow.compile(select=selection).tasks


def test_selection_does_not_mutate_the_workflow() -> None:
    workflow, _resource_ = _workflow_with_resource()

    workflow.compile(select=Selection(only="build"))

    assert len(workflow.compile().tasks) == 5
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_compile_selection.py -v`
Expected: FAIL. La maggior parte con `TypeError: Workflow.compile() got an unexpected keyword argument 'select'`; `test_a_title_with_no_slug_characters_is_a_compile_error` fallisce con `Failed: DID NOT RAISE`.

- [ ] **Step 3: Aggiungere gli helper di risoluzione degli slug**

In `src/sonata_engine/core/workflow.py`, subito dopo la funzione `_slugify` esistente:

```python
def _resolve_slug(slugs: list[str], wanted: str) -> int:
    """Index of the single consumer whose slug is `wanted`.

    Ambiguity is an error rather than an implicit multi-select: duplicate titles
    are legal (the ordinal disambiguates their IDs) but stop being addressable.
    """
    matches = [index for index, slug in enumerate(slugs) if slug == wanted]
    if not matches:
        available = ", ".join(sorted(set(slugs)))
        raise SelectionError(f"no task matches slug {wanted!r}; available: {available}")
    if len(matches) > 1:
        raise SelectionError(
            f"slug {wanted!r} matches {len(matches)} tasks; "
            "titles must be unique for a task to be selectable"
        )
    return matches[0]
```

Aggiungi gli import in cima al file:

```python
from sonata_engine.core.selection import Selection
from sonata_engine.errors import (
    InvalidTaskOutcomeError,
    ResumeConfigurationError,
    SelectionError,
)
```

(la riga `from sonata_engine.errors import InvalidTaskOutcomeError, ResumeConfigurationError` esistente va sostituita da questa.)

- [ ] **Step 4: Implementare il filtro e cambiare `compile()`**

Sostituisci il metodo `compile` esistente e aggiungi `_select`. Il corpo di `compile` diventa:

```python
    def compile(self, *, select: Selection | None = None) -> CompiledWorkflow:
        """Assign stable, deterministic IDs to the recorded task definitions.

        Each `Resource` referenced across `requires` gets one acquire unit
        spliced immediately before its first consumer and one release unit
        immediately after its last consumer. Releases landing at the same point
        run in reverse acquisition order (last-acquired-first-released). IDs are
        `{ordinal:03d}.{slug}` derived from position in the final merged
        sequence; duplicate titles are disambiguated by ordinal.

        `select` filters consumer definitions BEFORE resources are spliced, so
        the surviving consumers still get their acquire/release units. Ordinals
        renumber over the survivors: a sliced run is a different topology, and
        the journal fingerprint will refuse to resume across it.
        """
        if not self.workflow_id:
            raise ValueError("Workflow.compile() requires a non-empty workflow_id")

        merged = self._merge_resources(self._select(select))
        compiled_tasks = tuple(
            CompiledTask(
                task_id=f"{ordinal:03d}.{_slugify(entry.task.title)}",
                task=entry.task,
                required_resources=entry.required_resources,
                kind=entry.kind,
                resource=entry.resource,
            )
            for ordinal, entry in enumerate(merged, start=1)
        )
        return CompiledWorkflow(workflow_id=self.workflow_id, tasks=compiled_tasks)

    def _select(
        self, select: Selection | None
    ) -> list[tuple[Task[Any], tuple[Resource, ...]]]:
        """Filter consumer definitions by title slug, leaving resources to the compiler."""
        slugs = [_slugify(task.title) for task, _requires in self._definitions]
        for slug, (task, _requires) in zip(slugs, self._definitions):
            if not slug:
                raise ValueError(f"task title {task.title!r} produces an empty slug")
        if select is None or select.is_empty:
            return list(self._definitions)
        if select.only is not None:
            return [self._definitions[_resolve_slug(slugs, select.only)]]
        first = _resolve_slug(slugs, select.start) if select.start is not None else 0
        last = (
            _resolve_slug(slugs, select.until)
            if select.until is not None
            else len(self._definitions) - 1
        )
        if first > last:
            raise SelectionError(
                f"start {select.start!r} comes after until {select.until!r}"
            )
        return list(self._definitions[first : last + 1])
```

- [ ] **Step 5: Far prendere le definizioni a `_merge_resources` come parametro**

Cambia la firma e la prima riga del ciclo di `_merge_resources`. Da:

```python
    def _merge_resources(self) -> list[CompiledTask[Any]]:
        """Splice acquire/release units around consumers (IDs assigned by caller)."""
        # First/last consumer index per resource, in discovery order.
        first: dict[Resource, int] = {}
        last: dict[Resource, int] = {}
        for index, (_task, requires) in enumerate(self._definitions):
```

a:

```python
    def _merge_resources(
        self, definitions: list[tuple[Task[Any], tuple[Resource, ...]]]
    ) -> list[CompiledTask[Any]]:
        """Splice acquire/release units around consumers (IDs assigned by caller)."""
        # First/last consumer index per resource, in discovery order.
        first: dict[Resource, int] = {}
        last: dict[Resource, int] = {}
        for index, (_task, requires) in enumerate(definitions):
```

E più in basso, nello stesso metodo, cambia:

```python
        for index, (task, requires) in enumerate(self._definitions):
```

a:

```python
        for index, (task, requires) in enumerate(definitions):
```

- [ ] **Step 6: Eseguire i test e verificare che passino**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_compile_selection.py -v`
Expected: PASS, 15 test.

- [ ] **Step 7: Eseguire l'intera suite di sonata**

Run: `cd ~/Downloads/sonata && uv run pytest`
Expected: PASS. 124 test preesistenti più i nuovi. Se un test preesistente fallisce con "empty slug", il titolo di quel task va corretto nel test, non la validazione.

- [ ] **Step 8: Controlli statici**

Run: `cd ~/Downloads/sonata && uv run ruff check --no-cache . && uv run basedpyright`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd ~/Downloads/sonata
git add src/sonata_engine/core/workflow.py tests/core/test_compile_selection.py
git commit -m "Filter consumers by slug before compiling resource lifecycles"
```

---

### Task 3: `run(select=)` ed export pubblici

**Repository:** `~/Downloads/sonata`

**Files:**
- Modify: `src/sonata_engine/core/workflow.py`
- Modify: `src/sonata_engine/core/__init__.py`
- Modify: `src/sonata_engine/__init__.py`
- Modify: `README.md`
- Test: `tests/core/test_run_selection.py`

**Interfaces:**
- Consumes: `Selection` (Task 1), `compile(select=)` (Task 2).
- Produces: `Workflow.run(*, journal=None, resume=False, verifiers=None, select: Selection | None = None) -> WorkflowResult`. `Selection` e `SelectionError` importabili da `sonata_engine`.

- [ ] **Step 1: Scrivere i test che falliscono**

Crea `tests/core/test_run_selection.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from sonata_engine import (
    JournalConfig,
    Resource,
    Selection,
    SelectionError,
    Task,
    TaskOutcome,
    Workflow,
    WorkflowTopologyMismatchError,
)


class _Recording(Task[None]):
    def __init__(self, title: str, log: list[str]) -> None:
        self.title = title
        self._log = log

    def run(self) -> TaskOutcome[None]:
        self._log.append(self.title)
        return TaskOutcome()


def _workflow(log: list[str]) -> Workflow:
    resource = Resource(
        title="Acquire vm",
        acquire=lambda: log.append("acquire"),
        release=lambda: log.append("release"),
    )
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Recording("Build", log))
    workflow.add(_Recording("List", log), requires=(resource,))
    workflow.add(_Recording("Invoke", log), requires=(resource,))
    return workflow


def test_run_without_selection_executes_everything() -> None:
    log: list[str] = []

    _workflow(log).run()

    assert log == ["Build", "acquire", "List", "Invoke", "release"]


def test_run_with_only_executes_the_slice_and_its_resource_lifecycle() -> None:
    log: list[str] = []

    result = _workflow(log).run(select=Selection(only="invoke"))

    assert log == ["acquire", "Invoke", "release"]
    assert [execution.task_id for execution in result.tasks] == [
        "001.acquire-vm",
        "002.invoke",
        "003.release-vm",
    ]


def test_run_releases_the_resource_when_a_selected_task_fails() -> None:
    log: list[str] = []

    class _Boom(Task[None]):
        title = "Invoke"

        def run(self) -> TaskOutcome[None]:
            raise RuntimeError("boom")

    resource = Resource(
        title="Acquire vm",
        acquire=lambda: log.append("acquire"),
        release=lambda: log.append("release"),
    )
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Recording("Build", log))
    workflow.add(_Boom(), requires=(resource,))

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run(select=Selection(only="invoke"))

    assert log == ["acquire", "release"]


def test_run_propagates_an_unresolvable_selection() -> None:
    with pytest.raises(SelectionError, match="no task matches slug"):
        _workflow([]).run(select=Selection(only="deploy"))


def test_resume_refuses_to_cross_a_sliced_topology(tmp_path: Path) -> None:
    journal = JournalConfig(tmp_path / "journal.jsonl")
    _workflow([]).run(journal=journal)

    with pytest.raises(WorkflowTopologyMismatchError):
        _workflow([]).run(journal=journal, resume=True, select=Selection(only="invoke"))
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_run_selection.py -v`
Expected: FAIL con `ImportError: cannot import name 'Selection' from 'sonata_engine'`.

- [ ] **Step 3: Aggiungere `select` a `run()`**

In `src/sonata_engine/core/workflow.py`, sostituisci il metodo `run`:

```python
    def run(
        self,
        *,
        journal: JournalConfig | None = None,
        resume: bool = False,
        verifiers: Mapping[str, Verifier] | None = None,
        select: Selection | None = None,
    ) -> WorkflowResult:
        """Compile and run this workflow using compiler-owned task identities.

        `select` narrows the run to a slice of consumer tasks; their resources
        are still acquired and released around them. A sliced run has a
        different fingerprint, so `resume` across one fails closed.
        """
        return self._run_compiled(
            self.compile(select=select),
            journal=journal,
            resume=resume,
            verifiers=verifiers,
        )
```

- [ ] **Step 4: Esportare i nuovi nomi**

In `src/sonata_engine/core/__init__.py` aggiungi l'import e la voce in `__all__`:

```python
from sonata_engine.core.selection import Selection
```

`__all__` deve contenere `"Selection"`, in ordine alfabetico dopo `"Resource"`.

In `src/sonata_engine/__init__.py`:
- aggiungi `Selection` alla lista importata da `sonata_engine.core`, in ordine alfabetico dopo `Resource`;
- aggiungi `SelectionError` alla lista importata da `sonata_engine.errors`, in ordine alfabetico dopo `ResumeConfigurationError`;
- aggiungi `"Selection"` e `"SelectionError"` a `__all__`, mantenendolo ordinato alfabeticamente.

- [ ] **Step 5: Eseguire i test e verificare che passino**

Run: `cd ~/Downloads/sonata && uv run pytest tests/core/test_run_selection.py -v`
Expected: PASS, 5 test.

- [ ] **Step 6: Documentare la selezione nel README**

In `README.md`, subito prima della sezione `## Journal and resume`, inserisci:

```markdown
## Selecting a slice

`Selection` narrows a run to some of its consumer tasks, addressed by title slug.

```python
from sonata_engine import Selection

workflow.run(select=Selection(only="build-image"))
workflow.run(select=Selection(start="build-image", until="publish-manifest"))
```

Resources are not selectable: the compiler re-splices acquire and release around
whichever consumers survive, so a slice keeps its cleanup. Ordinals renumber over
the survivors, which makes a sliced run a different topology — `resume` across one
fails closed.
```

- [ ] **Step 7: Suite completa e controlli statici**

Run:
```bash
cd ~/Downloads/sonata
uv run pytest && uv run ruff check --no-cache . && uv run basedpyright
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/Downloads/sonata
git add src/sonata_engine tests/core/test_run_selection.py README.md
git commit -m "Run a selected slice of a workflow"
```

- [ ] **Step 9: Annotare il commit per il pin**

Run: `cd ~/Downloads/sonata && git rev-parse HEAD`
Annota lo SHA completo: serve al Task 9. Da qui in poi non fare altri commit in sonata senza aggiornare quel pin.

---

### Task 4: Il package `sonata-tasks` e `CommandTask`

**Repository:** `~/Downloads/nanolab`

**Files:**
- Modify: `pyproject.toml`
- Modify: `packages/nanolab/pyproject.toml`
- Create: `packages/sonata-tasks/pyproject.toml`
- Create: `packages/sonata-tasks/.importlinter`
- Create: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Create: `packages/sonata-tasks/src/sonata_tasks/py.typed` (file vuoto)
- Create: `packages/sonata-tasks/src/sonata_tasks/command.py`
- Test: `packages/sonata-tasks/tests/test_command.py`

**Interfaces:**
- Consumes: `Task`, `TaskOutcome` da `sonata_engine`; `CommandTaskSpec`, `TaskResult` da `workflow_tasks.tasks.models`; `CommandTaskExecutor` da `workflow_tasks.execution.bindings`; `ExecutionRole` da `workflow_tasks.execution.roles`.
- Produces: `CommandTask(title: str, argv: tuple[str, ...], executor: CommandTaskExecutor, role: ExecutionRole = "host", cwd: Path | None = None, expected_exit_codes: frozenset[int] = frozenset({0}), verify: Callable[[TaskResult], None] | None = None)` con `run() -> TaskOutcome[TaskResult]`.

- [ ] **Step 1: Creare lo scheletro del package**

Crea `packages/sonata-tasks/pyproject.toml`:

```toml
[project]
name = "sonata-tasks"
version = "0.1.0"
description = "nanoFaaS workflow tasks built on the Sonata engine."
requires-python = ">=3.12"
dependencies = [
    "sonata-engine",
    "workflow-tasks",
]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
sonata_tasks = ["py.typed"]

[dependency-groups]
dev = [
    "basedpyright>=1.39.3",
    "grimp>=3.14",
    "import-linter>=2.11",
    "pytest>=9.0.3",
    "pytest-cov>=6.0",
    "ruff>=0.15.12",
]

[tool.uv.sources]
sonata-engine = { path = "../../../sonata", editable = true }
workflow-tasks = { workspace = true }

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --cov=sonata_tasks --cov-report=term-missing --cov-fail-under=90"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["F", "SLF"]

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["F841", "SLF001"]

[tool.basedpyright]
include = ["src/sonata_tasks"]
typeCheckingMode = "basic"
reportMissingTypeStubs = false
reportPrivateUsage = false
```

`path = "../../../sonata"` è relativo a `packages/sonata-tasks/`, quindi risolve a `~/Downloads/sonata`. È la source locale temporanea richiesta da C6; il Task 9 la sostituisce.

Crea `packages/sonata-tasks/.importlinter`:

```ini
[importlinter]
root_packages =
    sonata_tasks
include_external_packages = True

[importlinter:contract:no_product_deps]
name = sonata_tasks must not import nanolab or tui_toolkit
type = forbidden
source_modules = sonata_tasks
forbidden_modules =
    nanolab
    tui_toolkit

[importlinter:contract:no_legacy_workflows]
name = sonata_tasks must not import the legacy workflow engine or workflow builders
type = forbidden
source_modules = sonata_tasks
forbidden_modules =
    workflow_tasks.core
    workflow_tasks.workflows
```

Il secondo contratto è il guardrail di C5: `sonata_tasks` può usare il layer di esecuzione di `workflow_tasks` ma mai il suo engine né i suoi workflow.

Crea `packages/sonata-tasks/src/sonata_tasks/py.typed` come file vuoto:

```bash
mkdir -p packages/sonata-tasks/src/sonata_tasks packages/sonata-tasks/tests
touch packages/sonata-tasks/src/sonata_tasks/py.typed
```

- [ ] **Step 2: Registrare il package nel workspace e alzare a Python 3.12**

Nel `pyproject.toml` alla radice, sostituisci il file con:

```toml
[project]
name = "nanolab-workspace"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
package = false

[tool.uv.workspace]
members = [
    "packages/nanolab",
    "packages/sonata-tasks",
    "packages/workflow-tasks",
    "packages/tui-toolkit",
]
```

In `packages/nanolab/pyproject.toml`:
- cambia `requires-python = ">=3.11"` in `requires-python = ">=3.12"`;
- aggiungi `"sonata-tasks",` alla lista `dependencies`, dopo `"pyyaml>=6.0.2"` e prima di `"rich>=13.8"` (l'ordine è alfabetico fra i pacchetti locali in coda: metti `"sonata-tasks",` subito prima di `"tui-toolkit",`);
- nella sezione `[tool.uv.sources]` aggiungi `sonata-tasks = { workspace = true }` prima di `tui-toolkit`.

- [ ] **Step 3: Sincronizzare e verificare che il workspace risolva**

Run:
```bash
cd ~/Downloads/nanolab
uv sync --all-packages --all-groups
```
Expected: risolve senza errori e installa `sonata-engine` da `~/Downloads/sonata` in editable, più `sonata-tasks`.

Se fallisce con un conflitto su `requires-python`, controlla che sia il root sia `packages/nanolab` dichiarino `>=3.12`.

- [ ] **Step 4: Scrivere il test che fallisce**

Crea `packages/sonata-tasks/tests/test_command.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sonata_engine import TaskOutcome
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.command import CommandTask


@dataclass
class RecordingExecutor:
    """Records the specs it is handed and replays a canned result."""

    result: TaskResult = field(
        default_factory=lambda: TaskResult(task_id="", status="passed", return_code=0)
    )
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return self.result


def test_a_passing_command_carries_its_result_as_the_outcome_value() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="passed", return_code=0, stdout="ok")
    )
    task = CommandTask(title="List functions", argv=("cli", "fn", "list"), executor=executor)

    outcome = task.run()

    assert isinstance(outcome, TaskOutcome)
    assert outcome.value is not None
    assert outcome.value.stdout == "ok"


def test_the_spec_carries_no_task_id_because_identity_belongs_to_the_compiler() -> None:
    executor = RecordingExecutor()
    CommandTask(title="List functions", argv=("cli",), executor=executor).run()

    assert executor.seen[0].task_id == ""
    assert executor.seen[0].summary == "List functions"


def test_role_and_cwd_reach_the_spec() -> None:
    executor = RecordingExecutor()
    task = CommandTask(
        title="Build",
        argv=("./gradlew", "installDist"),
        executor=executor,
        role="stack",
        cwd=Path("/repo"),
    )

    task.run()

    assert executor.seen[0].execution_role == "stack"
    assert executor.seen[0].cwd == Path("/repo")


def test_a_failing_command_raises_with_stderr_in_the_message() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=2, stderr="no such function")
    )
    task = CommandTask(title="Invoke thing", argv=("cli",), executor=executor)

    with pytest.raises(RuntimeError, match="Invoke thing failed \\(exit 2\\): no such function"):
        task.run()


def test_a_failing_command_falls_back_to_stdout_then_to_a_placeholder() -> None:
    stdout_only = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=1, stdout="bad request")
    )
    with pytest.raises(RuntimeError, match="bad request"):
        CommandTask(title="Apply", argv=("cli",), executor=stdout_only).run()

    silent = RecordingExecutor(result=TaskResult(task_id="", status="failed", return_code=1))
    with pytest.raises(RuntimeError, match="no output"):
        CommandTask(title="Apply", argv=("cli",), executor=silent).run()


def test_expected_exit_codes_reach_the_spec() -> None:
    executor = RecordingExecutor()
    task = CommandTask(
        title="Delete",
        argv=("cli",),
        executor=executor,
        expected_exit_codes=frozenset({0, 1}),
    )

    task.run()

    assert executor.seen[0].expected_exit_codes == frozenset({0, 1})


def test_the_verify_hook_runs_on_success_and_can_reject_the_result() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="passed", return_code=0, stdout="{}")
    )

    def reject(result: TaskResult) -> None:
        raise RuntimeError(f"unusable payload: {result.stdout}")

    task = CommandTask(title="Invoke", argv=("cli",), executor=executor, verify=reject)

    with pytest.raises(RuntimeError, match="unusable payload"):
        task.run()


def test_the_verify_hook_is_skipped_when_the_command_itself_failed() -> None:
    executor = RecordingExecutor(
        result=TaskResult(task_id="", status="failed", return_code=1, stderr="boom")
    )
    calls: list[TaskResult] = []

    task = CommandTask(
        title="Invoke", argv=("cli",), executor=executor, verify=calls.append
    )

    with pytest.raises(RuntimeError, match="boom"):
        task.run()
    assert calls == []
```

- [ ] **Step 5: Eseguire il test e verificare che fallisca**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: FAIL con `ModuleNotFoundError: No module named 'sonata_tasks.command'`.

- [ ] **Step 6: Implementare `CommandTask`**

Crea `packages/sonata-tasks/src/sonata_tasks/command.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from sonata_engine import Task, TaskOutcome
from workflow_tasks.execution.bindings import CommandTaskExecutor
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class CommandTask(Task[TaskResult]):
    """One command, run through a role-bound executor, as a Sonata task.

    Sonata deliberately owns no remote execution, so the executor comes from
    `workflow_tasks`. What this class adds is the Sonata contract: a title the
    compiler slugifies into the task's identity, and a `TaskOutcome` carrying
    the command result as an in-process value.

    `verify` turns a command into a checked command: it runs only after the
    command itself succeeded, and raising from it fails the task. That is how a
    shell pipeline of `grep`s becomes a Python assertion.
    """

    title: str
    argv: tuple[str, ...]
    executor: CommandTaskExecutor
    role: ExecutionRole = "host"
    cwd: Path | None = None
    expected_exit_codes: frozenset[int] = field(default_factory=lambda: frozenset({0}))
    verify: Callable[[TaskResult], None] | None = None

    @override
    def run(self) -> TaskOutcome[TaskResult]:
        result = self.executor.run(self._spec())
        if result.status != "passed":
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise RuntimeError(f"{self.title} failed (exit {result.return_code}): {detail}")
        if self.verify is not None:
            self.verify(result)
        return TaskOutcome(value=result)

    def _spec(self) -> CommandTaskSpec:
        # task_id is empty on purpose: identity belongs to the compiler, and
        # CommandTaskSpec only uses this field to label its TaskResult.
        return CommandTaskSpec(
            task_id="",
            summary=self.title,
            argv=self.argv,
            role=self.role,
            cwd=self.cwd,
            expected_exit_codes=self.expected_exit_codes,
        )
```

Crea `packages/sonata-tasks/src/sonata_tasks/__init__.py`:

```python
"""nanoFaaS workflow tasks built on the Sonata engine."""

from sonata_tasks.command import CommandTask

__all__ = ["CommandTask"]
```

- [ ] **Step 7: Eseguire il test e verificare che passi**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: PASS, 8 test, copertura sopra il 90%.

- [ ] **Step 8: Verificare che le suite esistenti non siano regredite**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups \
    pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
```
Expected: PASS entrambe (654 test nanolab; workflow-tasks sopra la soglia di copertura).

- [ ] **Step 9: Controlli statici e contratti di import**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
cd ~/Downloads/nanolab
git add pyproject.toml uv.lock packages/nanolab/pyproject.toml packages/sonata-tasks
git commit -m "Add the sonata-tasks package with a Sonata command task"
```

---

### Task 5: Il workflow `cli` su sonata

**Repository:** `~/Downloads/nanolab`

**Files:**
- Create: `packages/sonata-tasks/src/sonata_tasks/cli.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Test: `packages/sonata-tasks/tests/test_cli.py`

**Interfaces:**
- Consumes: `CommandTask` (Task 4); `Selection`, `Resource`, `Workflow` da `sonata_engine`; `RoleBindings`, `RoleBoundCommandTaskExecutor` da `workflow_tasks.execution.bindings`.
- Produces:
  - `CliFunction(name: str, image: str, payload: str, resources: dict[str, object] | None = None)`
  - `CliWorkflowRequest(functions: tuple[CliFunction, ...], cli_role: ExecutionRole = "host", namespace: str = "nanofaas-e2e", endpoint: str = "http://127.0.0.1:8080", binary: str = "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli")`
  - `build_cli_workflow(request: CliWorkflowRequest, bindings: RoleBindings, *, workflow_id: str = "cli", cwd: Path | None = None) -> Workflow`

- [ ] **Step 1: Scrivere i test che falliscono**

Crea `packages/sonata-tasks/tests/test_cli.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from sonata_engine import Selection
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow

SUCCESS = '{"status":"success","output":{"words":2}}'


@dataclass
class ScriptedExecutor:
    """Records specs and replays a result per matched argv fragment.

    `responses` maps a substring of the joined argv to the result to return;
    anything unmatched passes with empty output.
    """

    responses: dict[str, TaskResult] = field(default_factory=dict)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        joined = " ".join(task.argv)
        for fragment, result in self.responses.items():
            if fragment in joined:
                return result
        return TaskResult(task_id="", status="passed", return_code=0, stdout=SUCCESS)

    @property
    def titles(self) -> list[str]:
        return [spec.summary for spec in self.seen]


FUNCTION = CliFunction(
    name="word-stats-java",
    image="localhost:5000/nanofaas/java-word-stats:e2e",
    payload='{"text":"hello world"}',
)
OTHER = CliFunction(
    name="json-transform-python",
    image="localhost:5000/nanofaas/python-json:e2e",
    payload='{"a":1}',
)


def _bindings(executor: ScriptedExecutor) -> RoleBindings:
    return RoleBindings(host=executor, stack=executor)


def test_a_single_function_compiles_to_the_expected_topology() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.list-functions",
        "004.invoke-word-stats-java",
        "005.release-word-stats-java",
    ]


def test_running_it_applies_before_listing_and_deletes_last() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    workflow.run()

    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_the_function_is_deleted_even_when_invoke_fails() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="failed", return_code=1, stderr="unreachable"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="unreachable"):
        workflow.run()

    assert "Delete word-stats-java" in executor.titles


def test_a_failed_apply_compensates_best_effort_before_propagating() -> None:
    # The apply command is a bash script whose CLI arguments are shell-quoted,
    # so "fn apply" does not appear literally in the joined argv. "mktemp" does,
    # and only there.
    executor = ScriptedExecutor(
        responses={
            "mktemp": TaskResult(
                task_id="", status="failed", return_code=1, stderr="conflict"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="conflict"):
        workflow.run()

    # The apply may have registered the function before failing, so the task
    # compensates itself. The engine never releases an acquire that did not pass.
    assert executor.titles == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "Delete word-stats-java",
    ]


def test_invoke_rejects_a_non_success_status() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="",
                status="passed",
                return_code=0,
                stdout='{"status":"error","output":{}}',
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="did not report success"):
        workflow.run()


def test_invoke_rejects_a_response_without_output() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="passed", return_code=0, stdout='{"status":"success"}'
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="carried no output"):
        workflow.run()


def test_invoke_rejects_malformed_json() -> None:
    executor = ScriptedExecutor(
        responses={
            "invoke word-stats-java": TaskResult(
                task_id="", status="passed", return_code=0, stdout="<html>502</html>"
            )
        }
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    with pytest.raises(RuntimeError, match="was not JSON"):
        workflow.run()


def test_two_functions_release_each_one_after_its_last_consumer() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION, OTHER)), _bindings(executor)
    )

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.acquire-json-transform-python",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.invoke-json-transform-python",
        "008.release-json-transform-python",
    ]


def test_selecting_one_invoke_keeps_that_function_lifecycle_only() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION, OTHER)), _bindings(executor)
    )

    workflow.run(select=Selection(only="invoke-json-transform-python"))

    assert executor.titles == [
        "Apply json-transform-python",
        "Invoke json-transform-python",
        "Delete json-transform-python",
    ]


def test_the_endpoint_and_namespace_reach_every_cli_call() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(
            functions=(FUNCTION,),
            endpoint="http://stack.example:8080",
            namespace="research",
        ),
        _bindings(executor),
    )

    workflow.run()

    cli_calls = [spec for spec in executor.seen if spec.summary != "Build nanofaas-cli"]
    assert all("http://stack.example:8080" in " ".join(spec.argv) for spec in cli_calls)
    assert all("research" in " ".join(spec.argv) for spec in cli_calls)


def test_apply_builds_the_manifest_on_the_target_not_in_this_process() -> None:
    executor = ScriptedExecutor()
    function = replace(
        FUNCTION,
        image="registry.example/research:v2",
        resources={"limits": {"cpu": 1.0, "memoryMiB": 512}},
    )
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(function,)), _bindings(executor)
    )

    workflow.run()

    apply_spec = next(spec for spec in executor.seen if spec.summary.startswith("Apply"))
    script = apply_spec.argv[-1]
    assert apply_spec.argv[:2] == ("bash", "-lc")
    assert "mktemp" in script
    assert "registry.example/research:v2" in script
    assert '"memoryMiB":512' in script


def test_delete_tolerates_an_already_absent_function() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,)), _bindings(executor)
    )

    workflow.run()

    delete_spec = next(spec for spec in executor.seen if spec.summary.startswith("Delete"))
    assert delete_spec.expected_exit_codes == frozenset({0, 1})


def test_the_workflow_can_run_entirely_on_the_stack_role() -> None:
    executor = ScriptedExecutor()
    workflow = build_cli_workflow(
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="stack"), _bindings(executor)
    )

    workflow.run()

    assert all(spec.execution_role == "stack" for spec in executor.seen)


def test_a_loadgen_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="host or stack"):
        CliWorkflowRequest(functions=(FUNCTION,), cli_role="loadgen")


def test_at_least_one_function_is_required() -> None:
    with pytest.raises(ValueError, match="at least one function"):
        CliWorkflowRequest(functions=())
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_cli.py -q
```
Expected: FAIL con `ModuleNotFoundError: No module named 'sonata_tasks.cli'`.

- [ ] **Step 3: Implementare i modelli di richiesta e la verifica di invoke**

Crea `packages/sonata-tasks/src/sonata_tasks/cli.py`:

```python
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from sonata_engine import Resource, Workflow
from workflow_tasks.execution.bindings import (
    CommandTaskExecutor,
    RoleBindings,
    RoleBoundCommandTaskExecutor,
)
from workflow_tasks.execution.roles import ExecutionRole
from workflow_tasks.tasks.models import TaskResult

from sonata_tasks.command import CommandTask


@dataclass(frozen=True, slots=True)
class CliFunction:
    """One function the CLI workflow registers, invokes, and removes."""

    name: str
    image: str
    payload: str
    resources: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CliWorkflowRequest:
    """Everything the CLI workflow needs that is not an executor."""

    functions: tuple[CliFunction, ...]
    cli_role: ExecutionRole = "host"
    namespace: str = "nanofaas-e2e"
    endpoint: str = "http://127.0.0.1:8080"
    binary: str = "clients/cli/build/install/nanofaas-cli/bin/nanofaas-cli"

    def __post_init__(self) -> None:
        if self.cli_role == "loadgen":
            raise ValueError("CLI workflow can run only on host or stack")
        if not self.functions:
            raise ValueError("CLI workflow requires at least one function")


def _verify_invocation(result: TaskResult) -> None:
    """Assert the control plane reported a usable invocation.

    This is the whole point of porting invoke to a real task: the old workflow
    piped the response through two `grep -q` calls inside a bash string, which
    could not tell "not JSON" from "status was error".
    """
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(
            f"invocation response was not JSON: {result.stdout[:200]!r}"
        ) from error
    if not isinstance(response, dict):
        raise RuntimeError(f"invocation response was not JSON object: {result.stdout[:200]!r}")
    if response.get("status") != "success":
        raise RuntimeError(f"invocation did not report success: {response.get('status')!r}")
    if "output" not in response:
        raise RuntimeError("invocation carried no output")
```

- [ ] **Step 4: Implementare la costruzione dei comandi**

Aggiungi in fondo a `packages/sonata-tasks/src/sonata_tasks/cli.py`:

```python
def _cli_argv(request: CliWorkflowRequest, *arguments: str) -> tuple[str, ...]:
    return (
        request.binary,
        "--endpoint",
        request.endpoint,
        "--namespace",
        request.namespace,
        *arguments,
    )


def _apply_script(request: CliWorkflowRequest, function: CliFunction) -> str:
    """Shell that writes the manifest next to the CLI and applies it.

    The manifest is created with `mktemp` on the target on purpose. Writing it
    here with `tempfile` would work on the host role and silently break on the
    stack role, where the CLI runs elsewhere and would never see a local path.
    """
    body: dict[str, object] = {
        "name": function.name,
        "image": function.image,
        "executionMode": "DEPLOYMENT",
        "timeoutMs": 5000,
        "concurrency": 2,
        "queueSize": 20,
        "maxRetries": 3,
    }
    if function.resources is not None:
        body["resources"] = function.resources
    manifest = json.dumps(body, separators=(",", ":"))
    apply_command = " ".join(
        shlex.quote(value)
        for value in _cli_argv(request, "fn", "apply", "--file", "$manifest")
    ).replace("'$manifest'", '"$manifest"')
    return (
        f"manifest=$(mktemp); trap 'rm -f \"$manifest\"' EXIT; "
        f"printf '%s' {shlex.quote(manifest)} > \"$manifest\"; " + apply_command
    )
```

- [ ] **Step 5: Implementare la risorsa per function**

Aggiungi in fondo a `packages/sonata-tasks/src/sonata_tasks/cli.py`:

```python
def _function_resource(
    request: CliWorkflowRequest,
    function: CliFunction,
    executor: CommandTaskExecutor,
    cwd: Path | None,
) -> Resource:
    """The registered function as an acquire/release pair.

    Sonata splices the apply before the first task that needs the function and
    the delete after the last one, and runs the delete even when a consumer
    fails. That is why the old separate `cleanup_specs` list is gone.
    """
    apply_task = CommandTask(
        title=f"Apply {function.name}",
        argv=("bash", "-lc", _apply_script(request, function)),
        executor=executor,
        role=request.cli_role,
        cwd=cwd,
    )
    delete_task = CommandTask(
        title=f"Delete {function.name}",
        argv=_cli_argv(request, "fn", "delete", function.name),
        executor=executor,
        role=request.cli_role,
        cwd=cwd,
        expected_exit_codes=frozenset({0, 1}),
    )

    def acquire() -> None:
        try:
            apply_task.run()
        except BaseException as error:
            # The apply may have registered the function before failing. The
            # engine will not release an acquire that did not pass, so the
            # compensation has to happen here, best-effort.
            try:
                delete_task.run()
            except BaseException as cleanup_error:
                error.add_note(f"Best-effort delete after a failed apply failed: {cleanup_error}")
            raise

    def release() -> None:
        delete_task.run()

    return Resource(
        title=f"Acquire {function.name}",
        acquire=acquire,
        release=release,
    )
```

Nota: `Resource.release_title` deriva da `title` togliendo il prefisso `Acquire `, quindi la unit di rilascio si chiama `Release <nome>` e il suo ID compilato è `NNN.release-<nome>`. I titoli dei comandi effettivamente eseguiti restano `Apply`/`Delete`.

- [ ] **Step 6: Implementare il builder**

Aggiungi in fondo a `packages/sonata-tasks/src/sonata_tasks/cli.py`:

```python
def build_cli_workflow(
    request: CliWorkflowRequest,
    bindings: RoleBindings,
    *,
    workflow_id: str = "cli",
    cwd: Path | None = None,
) -> Workflow:
    """Build the CLI end-to-end workflow: build, register, list, invoke, remove."""
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
        requires=resources,
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
            requires=(resource,),
        )
    return workflow
```

Aggiorna `packages/sonata-tasks/src/sonata_tasks/__init__.py`:

```python
"""nanoFaaS workflow tasks built on the Sonata engine."""

from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from sonata_tasks.command import CommandTask

__all__ = [
    "CliFunction",
    "CliWorkflowRequest",
    "CommandTask",
    "build_cli_workflow",
]
```

- [ ] **Step 7: Eseguire i test e verificare che passino**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: PASS, 23 test in totale (8 di `test_command.py`, 15 di `test_cli.py`), copertura sopra il 90%.

- [ ] **Step 8: Controlli statici e contratti**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd ~/Downloads/nanolab
git add packages/sonata-tasks
git commit -m "Reimplement the CLI workflow on the Sonata engine"
```

---

### Task 6: Instradare `nanolab run`/`plan` sul workflow sonata

**Repository:** `~/Downloads/nanolab`

**Files:**
- Rewrite: `packages/nanolab/src/nanolab/plans/cli.py`
- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/src/nanolab/cli/progress.py`
- Rewrite: `packages/nanolab/tests/plans/test_cli.py`

**Interfaces:**
- Consumes: `build_cli_workflow`, `CliFunction`, `CliWorkflowRequest` (Task 5); `Selection` (Task 3).
- Produces:
  - `nanolab.plans.cli.build_cli_plan(config: ScenarioConfig, bindings: RoleBindings, *, cli_role: ExecutionRole = "host", endpoint: str = "http://127.0.0.1:8080", namespace: str = "nanofaas-e2e", repo_root: Path | None = None) -> Workflow` (`Workflow` di sonata)
  - `nanolab.cli.product.MIGRATED_WORKFLOWS: frozenset[str]` e `nanolab.cli.product.is_migrated(scenario: ScenarioConfig) -> bool`, consumati dal Task 7.

- [ ] **Step 1: Scrivere i test che falliscono**

Sostituisci interamente `packages/nanolab/tests/plans/test_cli.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sonata_engine import Selection
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.tasks.models import CommandTaskSpec, TaskResult

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.cli import build_cli_plan

SUCCESS = '{"status":"success","output":{"words":2}}'


@dataclass
class RecordingExecutor:
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0, stdout=SUCCESS)


def _scenario(**overrides: object) -> ScenarioConfig:
    payload: dict[str, object] = {
        "workflow": "cli",
        "backend": "k8s",
        "functions": ["word-stats-java"],
    }
    payload.update(overrides)
    return ScenarioConfig.model_validate(payload)


def test_cli_plan_uses_the_selected_role_binding() -> None:
    host = RecordingExecutor()
    stack = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=host, stack=stack),
        cli_role="stack",
    ).run()

    assert host.seen == []
    assert [spec.summary for spec in stack.seen] == [
        "Build nanofaas-cli",
        "Apply word-stats-java",
        "List functions",
        "Invoke word-stats-java",
        "Delete word-stats-java",
    ]


def test_cli_plan_compiles_every_selected_function() -> None:
    plan = build_cli_plan(
        _scenario(
            functions=["word-stats-java", "json-transform-python"],
            resources={"word-stats-java": {"limits": {"memoryMiB": 512}}},
        ),
        RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    )

    assert [task.task_id for task in plan.compile().tasks] == [
        "001.build-nanofaas-cli",
        "002.acquire-word-stats-java",
        "003.acquire-json-transform-python",
        "004.list-functions",
        "005.invoke-word-stats-java",
        "006.release-word-stats-java",
        "007.invoke-json-transform-python",
        "008.release-json-transform-python",
    ]


def test_cli_plan_passes_the_endpoint_and_resolved_resources_through() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(resources={"word-stats-java": {"limits": {"memoryMiB": 512}}}),
        RoleBindings(host=executor, stack=RecordingExecutor()),
        endpoint="http://stack.example:8080",
    ).run()

    apply_script = next(
        spec.argv[-1] for spec in executor.seen if spec.summary.startswith("Apply")
    )
    assert "http://stack.example:8080" in apply_script
    assert '"memoryMiB":512' in apply_script


def test_cli_plan_sends_only_the_payload_input_to_invoke() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    ).run()

    invoke = next(spec for spec in executor.seen if spec.summary.startswith("Invoke"))
    assert '"input"' not in " ".join(invoke.argv)


def test_cli_plan_supports_slicing_by_slug() -> None:
    executor = RecordingExecutor()

    build_cli_plan(
        _scenario(),
        RoleBindings(host=executor, stack=RecordingExecutor()),
    ).run(select=Selection(only="list-functions"))

    assert [spec.summary for spec in executor.seen] == [
        "Apply word-stats-java",
        "List functions",
        "Delete word-stats-java",
    ]


def test_cli_plan_rejects_a_non_cli_scenario() -> None:
    with pytest.raises(ValueError, match="cli scenario"):
        build_cli_plan(
            ScenarioConfig.model_validate(
                {"workflow": "validate", "backend": "k8s", "functions": ["word-stats-java"]}
            ),
            RoleBindings(host=RecordingExecutor(), stack=RecordingExecutor()),
        )
```

- [ ] **Step 2: Eseguire i test e verificare che falliscano**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
```
Expected: FAIL. `build_cli_plan` restituisce ancora il `Workflow` legacy, che non ha `compile()` né `run(select=...)`.

- [ ] **Step 3: Riscrivere il plan builder**

Sostituisci interamente `packages/nanolab/src/nanolab/plans/cli.py`:

```python
import json
from pathlib import Path

from sonata_engine import Workflow
from sonata_tasks.cli import CliFunction, CliWorkflowRequest, build_cli_workflow
from workflow_tasks.execution.bindings import RoleBindings
from workflow_tasks.execution.roles import ExecutionRole

from nanolab.config.scenario import ScenarioConfig
from nanolab.plans.validate import _resolve_function


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
    functions = tuple(
        CliFunction(
            name=resolved.name,
            image=resolved.image,
            payload=json.dumps(json.loads(resolved.payload)["input"], separators=(",", ":")),
            resources=resolved.resources,
        )
        for key in config.functions
        for resolved in (_resolve_function(config, key),)
    )
    request = CliWorkflowRequest(
        functions=functions,
        cli_role=cli_role,
        endpoint=endpoint,
        namespace=namespace,
    )
    return build_cli_workflow(request, bindings, cwd=repo_root or Path.cwd())
```

- [ ] **Step 4: Eseguire i test del plan e verificare che passino**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
```
Expected: PASS, 6 test.

- [ ] **Step 5: Insegnare al sink di console il vocabolario di sonata**

In `packages/nanolab/src/nanolab/cli/progress.py`, sostituisci l'import di `WorkflowEvent` e il metodo `emit`.

Import, in cima:

```python
from sonata_engine.workflow.events import WorkflowEvent as SonataWorkflowEvent
from workflow_tasks.workflow.events import WorkflowEvent
```

Subito sotto gli import, prima della classe:

```python
# Two engines, one console renderer: workflow_tasks says running/completed,
# Sonata says started/passed. The events are structurally identical, so the
# sink only has to accept both vocabularies.
_RUNNING_KINDS = frozenset({"task.running", "task.started"})
_TERMINAL_KINDS = frozenset({"task.completed", "task.passed", "task.failed"})
```

Il corpo di `emit` diventa:

```python
    def emit(self, event: WorkflowEvent | SonataWorkflowEvent) -> None:
        task_id = event.task_id
        if task_id is None:
            return
        if event.kind in _RUNNING_KINDS:
            self._started[task_id] = self._clock()
            self._write(f"[{task_id}] running  {event.title}")
            return
        if event.kind not in _TERMINAL_KINDS:
            return
        now = self._clock()
        elapsed = now - self._started.pop(task_id, now)
        status = "failed" if event.kind == "task.failed" else "passed"
        self.records.append(
            {
                "task_id": task_id,
                "title": event.title,
                "status": status,
                "duration_seconds": round(elapsed, 3),
                "detail": event.detail,
            }
        )
        detail = f"  {event.detail}" if event.detail else ""
        self._write(f"[{task_id}] {status:<8} {elapsed:.1f}s{detail}")
```

- [ ] **Step 6: Instradare `run` e `plan` sul ramo migrato**

In `packages/nanolab/src/nanolab/cli/product.py`:

Aggiungi agli import in cima:

```python
from sonata_engine import CompiledWorkflow, Selection
from sonata_engine.workflow.context import bind_workflow_sink as bind_sonata_sink
```

Subito dopo gli import, prima di `def _read`:

```python
# Workflows already ported to Sonata. The legacy branch below (and `_slice`)
# disappears when this set covers every workflow.
MIGRATED_WORKFLOWS = frozenset({"cli"})


def is_migrated(scenario: ScenarioConfig) -> bool:
    return scenario.workflow in MIGRATED_WORKFLOWS
```

Aggiungi accanto a `_render`:

```python
def _render_compiled(compiled: CompiledWorkflow) -> None:
    for index, compiled_task in enumerate(compiled.tasks, start=1):
        typer.echo(f"{index:02d}  {compiled_task.task_id}  {compiled_task.task.title}")
```

In `run_command`, sostituisci la riga:

```python
            with bind_workflow_sink(sink):
```

con:

```python
            # Both engines are live during the migration and each reads its own
            # contextvar, so the one sink is bound to both.
            with bind_workflow_sink(sink), bind_sonata_sink(sink):
```

Sempre in `run_command`, sostituisci il blocco:

```python
                    workflow = _slice(
                        _workflow(
                            scenario_config,
                            environment_config,
                            control_plane_url=effective_control_plane_url,
                            prometheus_url=prometheus_url or "http://127.0.0.1:9090",
                            run_dir=effective_run_dir,
                        ),
                        only=only,
                        start=start,
                        until=until,
                    )
                    workflow.keep_infrastructure = keep
                    workflow.run()
```

con:

```python
                    workflow = _workflow(
                        scenario_config,
                        environment_config,
                        control_plane_url=effective_control_plane_url,
                        prometheus_url=prometheus_url or "http://127.0.0.1:9090",
                        run_dir=effective_run_dir,
                    )
                    if is_migrated(scenario_config):
                        workflow.keep_infrastructure = keep
                        workflow.run(select=Selection(only=only, start=start, until=until))
                    else:
                        workflow = _slice(workflow, only=only, start=start, until=until)
                        workflow.keep_infrastructure = keep
                        workflow.run()
```

In `plan_command`, sostituisci il blocco:

```python
        _render(
            _slice(
                _workflow(
                    scenario_config,
                    environment_config,
                    control_plane_url=control_plane_url or "http://127.0.0.1:8080",
                    prometheus_url=prometheus_url or "http://127.0.0.1:9090",
                    run_dir=run_dir,
                    dry_run=True,
                ),
                only=only,
                start=start,
                until=until,
            )
        )
```

con:

```python
        workflow = _workflow(
            scenario_config,
            environment_config,
            control_plane_url=control_plane_url or "http://127.0.0.1:8080",
            prometheus_url=prometheus_url or "http://127.0.0.1:9090",
            run_dir=run_dir,
            dry_run=True,
        )
        if is_migrated(scenario_config):
            _render_compiled(
                workflow.compile(select=Selection(only=only, start=start, until=until))
            )
        else:
            _render(_slice(workflow, only=only, start=start, until=until))
```

- [ ] **Step 7: Documentare il cambio di sintassi negli help della CLI**

In `packages/nanolab/src/nanolab/cli/product.py`, in `run_command` e in `plan_command`, sostituisci le tre dichiarazioni di opzione:

```python
        only: str | None = typer.Option(None, "--only"),
        start: str | None = typer.Option(None, "--from"),
        until: str | None = typer.Option(None, "--until"),
```

con:

```python
        only: str | None = typer.Option(
            None,
            "--only",
            help=(
                "Run a single task. Migrated workflows address tasks by title slug "
                "(e.g. list-functions) and do not run their prerequisites for you."
            ),
        ),
        start: str | None = typer.Option(
            None,
            "--from",
            help="Start from this task, inclusive. Same addressing as --only.",
        ),
        until: str | None = typer.Option(
            None,
            "--until",
            help="Stop after this task, inclusive. Same addressing as --only.",
        ),
```

- [ ] **Step 8: Eseguire la suite nanolab**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
```
Expected: PASS. Se i test in `packages/nanolab/tests/cli/test_command_surface.py` che montano `build_cli_plan` con `MagicMock(return_value=Workflow(tasks=[]))` falliscono, aggiornali per restituire un `Workflow` di sonata: `Workflow(workflow_id="cli")`. Non aggiungere adapter per farli passare.

- [ ] **Step 9: Verificare il piano a mano**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli.yaml
```
Expected: cinque righe, con gli ID `001.build-nanofaas-cli` … `005.release-word-stats-java`.

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli.yaml --only list-functions
```
Expected: tre righe: `001.acquire-word-stats-java`, `002.list-functions`, `003.release-word-stats-java`.

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/validate-k8s.yaml \
    --environment packages/nanolab/environments/multipass.yaml
```
Expected: il workflow legacy `validate` continua a renderizzarsi come prima.

- [ ] **Step 10: Controlli statici**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
cd ~/Downloads/nanolab
git add packages/nanolab/src packages/nanolab/tests/plans/test_cli.py
git commit -m "Run the cli scenario through the Sonata workflow"
```

---

### Task 7: Il bridge della TUI

**Repository:** `~/Downloads/nanolab`

**Files:**
- Modify: `packages/nanolab/src/nanolab/tui/event_aggregator.py`
- Modify: `packages/nanolab/src/nanolab/tui/workflow_controller.py`
- Modify: `packages/nanolab/src/nanolab/tui/app.py`
- Test: `packages/nanolab/tests/test_workflow_events.py`

**Interfaces:**
- Consumes: `is_migrated` e `MIGRATED_WORKFLOWS` da `nanolab.cli.product` (Task 6).
- Produces: nessuna nuova API pubblica. La TUI accetta il vocabolario eventi di sonata e deriva i passi pianificati da un `CompiledWorkflow`.

- [ ] **Step 1: Scrivere il test che fallisce**

Aggiungi in fondo a `packages/nanolab/tests/test_workflow_events.py`:

```python
def test_the_aggregator_reads_the_sonata_task_vocabulary() -> None:
    from sonata_engine.workflow.events import WorkflowEvent as SonataEvent

    from nanolab.tui.event_aggregator import WorkflowEventAggregator

    aggregator = WorkflowEventAggregator(planned_steps=["Build nanofaas-cli"])

    aggregator.handle_event(
        SonataEvent(
            kind="task.started",
            flow_id="interactive.console",
            task_id="001.build-nanofaas-cli",
            title="Build nanofaas-cli",
        )
    )
    running = aggregator.snapshot().phases[0].status

    aggregator.handle_event(
        SonataEvent(
            kind="task.passed",
            flow_id="interactive.console",
            task_id="001.build-nanofaas-cli",
            title="Build nanofaas-cli",
        )
    )
    passed = aggregator.snapshot().phases[0].status

    assert (running, passed) == ("running", "success")
```

I valori attesi sono quelli che `_mark_phase_running` e `_mark_phase_success` assegnano oggi in `event_aggregator.py` (`"running"` e `"success"`): l'asserzione è che `task.started` porti la fase nello stesso stato di `task.running`, e `task.passed` nello stesso di `task.completed`.

- [ ] **Step 2: Eseguire il test e verificare che fallisca**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml \
    packages/nanolab/tests/test_workflow_events.py -k sonata -q
```
Expected: FAIL. La fase resta nel suo stato iniziale perché `task.started` e `task.passed` non corrispondono a nessun ramo di `handle_event`.

- [ ] **Step 3: Accettare il vocabolario di sonata nell'aggregatore**

In `packages/nanolab/src/nanolab/tui/event_aggregator.py`, sostituisci le due righe di confronto dentro `handle_event`. Da:

```python
        if event.kind == "task.running":
```

a:

```python
        if event.kind in ("task.running", "task.started"):
```

e da:

```python
        if event.kind == "task.completed":
```

a:

```python
        if event.kind in ("task.completed", "task.passed"):
```

Aggiungi il commento sopra `handle_event`:

```python
    # workflow_tasks emits running/completed, Sonata emits started/passed. One
    # aggregator reads both for as long as the migration lasts.
```

- [ ] **Step 4: Eseguire il test e verificare che passi**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml \
    packages/nanolab/tests/test_workflow_events.py -q
```
Expected: PASS.

- [ ] **Step 5: Legare il sink a entrambe le contextvar nel controller**

In `packages/nanolab/src/nanolab/tui/workflow_controller.py`, sostituisci l'import:

```python
from workflow_tasks import bind_workflow_sink
```

con:

```python
from sonata_engine.workflow.context import bind_workflow_sink as bind_sonata_sink
from workflow_tasks import bind_workflow_sink
```

e la riga:

```python
                with bind_workflow_sink(sink):
```

con:

```python
                # Both engines are live during the migration; each reads its own
                # contextvar, so the one sink is bound to both.
                with bind_workflow_sink(sink), bind_sonata_sink(sink):
```

- [ ] **Step 6: Derivare i passi pianificati e il piano dal workflow compilato**

In `packages/nanolab/src/nanolab/tui/app.py`, aggiungi agli import:

```python
from nanolab.cli.product import is_migrated
```

(`app.py` importa già da `nanolab.cli.product`; aggiungi `is_migrated` alla lista se l'import esiste già.)

Aggiungi un helper accanto a `_render_plan`:

```python
    def _plan_rows(self, scenario: ScenarioConfig, workflow: Any) -> list[tuple[str, str]]:
        """(task_id, title) pairs for display, from whichever engine built the workflow.

        A Sonata `Workflow` has no task list until it is compiled, and its
        compiled units carry the title on the task, not on themselves.
        """
        if is_migrated(scenario):
            return [
                (compiled_task.task_id, compiled_task.task.title)
                for compiled_task in workflow.compile().tasks
            ]
        return [(task.task_id, task.title) for task in workflow.tasks]
```

Sostituisci il corpo del ciclo in `_render_plan`. Da:

```python
    def _render_plan(self, *, title: str, workflow: Any) -> None:
        table = Table(expand=True)
        table.add_column("#", justify="right", style="cyan", no_wrap=True)
        table.add_column("Task", style="bold")
        table.add_column("Description")
        for index, task in enumerate(workflow.tasks, start=1):
            table.add_row(f"{index:02d}", task.task_id, task.title)
```

a:

```python
    def _render_plan(
        self, *, title: str, workflow: Any, scenario: ScenarioConfig
    ) -> None:
        table = Table(expand=True)
        table.add_column("#", justify="right", style="cyan", no_wrap=True)
        table.add_column("Task", style="bold")
        table.add_column("Description")
        for index, (task_id, task_title) in enumerate(
            self._plan_rows(scenario, workflow), start=1
        ):
            table.add_row(f"{index:02d}", task_id, task_title)
```

Aggiorna la chiamata (attorno alla riga 365):

```python
            self._render_plan(title=title, workflow=workflow, scenario=scenario)
```

Sostituisci la riga che calcola i passi pianificati (attorno alla riga 425):

```python
                planned_steps=preview.phase_titles,
```

con:

```python
                planned_steps=[
                    task_title for _task_id, task_title in self._plan_rows(scenario, preview)
                ],
```

- [ ] **Step 7: Eseguire la suite nanolab**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
```
Expected: PASS. Se `packages/nanolab/tests/test_tui_workflow.py` o `test_tui_workflow_controller.py` falliscono perché passavano un finto workflow con attributo `.tasks`, aggiornali a uno scenario non migrato o a un `Workflow` sonata reale — non aggiungere un attributo di compatibilità.

- [ ] **Step 8: Controlli statici**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
```
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd ~/Downloads/nanolab
git add packages/nanolab/src/nanolab/tui packages/nanolab/tests/test_workflow_events.py
git commit -m "Render Sonata workflows in the TUI"
```

---

### Task 8: Il gate di CI per `sonata-tasks`

**Repository:** `~/Downloads/nanolab`

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `packages/nanolab/.importlinter`

**Interfaces:**
- Consumes: il package del Task 4.
- Produces: nessuna API. CI esegue test, type-check, contratti di import e smoke del wheel anche per `sonata-tasks`.

- [ ] **Step 1: Aggiungere il contratto di boundary lato nanolab**

In `packages/nanolab/.importlinter`, aggiungi `sonata_tasks` a `root_packages`:

```ini
[importlinter]
root_packages =
    nanolab
    sonata_tasks
    workflow_tasks
include_external_packages = True
```

e aggiungi in fondo al file:

```ini
[importlinter:contract:sonata_tasks_is_independent]
name = sonata_tasks must not depend on nanolab
type = forbidden
source_modules = sonata_tasks
forbidden_modules = nanolab
```

- [ ] **Step 2: Aggiungere i passi di CI**

In `.github/workflows/ci.yml`, dopo lo step `Run workflow-tasks tests`, inserisci:

```yaml
      - name: Run sonata-tasks tests
        run: uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
```

Nello step `Run static checks`, aggiungi in fondo alla lista:

```yaml
          uv run --locked --all-packages --all-groups basedpyright --project packages/sonata-tasks
```

Nello step `Run import contracts`, aggiungi in fondo alla lista:

```yaml
          uv run --locked --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```

Nello step `Smoke-test installed wheels`, sostituisci le due righe di install e import:

```yaml
          uv pip install dist/nanolab-0.1.0-py3-none-any.whl dist/sonata_tasks-0.1.0-py3-none-any.whl dist/workflow_tasks-0.1.0-py3-none-any.whl dist/tui_toolkit-0.1.0-py3-none-any.whl --python .wheel-smoke/bin/python
          .wheel-smoke/bin/python -c "import nanolab, sonata_tasks, workflow_tasks, tui_toolkit"
```

Nello step `Generate representative plans`, aggiungi in fondo:

```yaml
          uv run --package nanolab nanolab plan packages/nanolab/scenarios-v2/cli.yaml
```

- [ ] **Step 3: Verificare i contratti in locale**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
```
Expected: PASS entrambi.

- [ ] **Step 4: Verificare che il lockfile sia coerente**

Run:
```bash
cd ~/Downloads/nanolab
uv lock --check
```
Expected: PASS. Se fallisce, esegui `uv lock` e includi `uv.lock` nel commit.

- [ ] **Step 5: Verificare la build delle distribuzioni**

Run:
```bash
cd ~/Downloads/nanolab
uv build --all-packages --out-dir dist --clear
ls dist
```
Expected: fra i wheel prodotti compare `sonata_tasks-0.1.0-py3-none-any.whl`.

Nota: lo smoke del wheel in CI installa i wheel dalla cartella `dist`; `sonata-tasks` dipende da `sonata-engine`, che il Task 9 pinnerà a un commit Git. Finché il pin è una source locale, quello step fallisce in CI: è previsto, e il Task 9 lo risolve. Non introdurre un pin fittizio per farlo passare adesso.

- [ ] **Step 6: Commit**

```bash
cd ~/Downloads/nanolab
git add .github/workflows/ci.yml packages/nanolab/.importlinter uv.lock
git commit -m "Gate sonata-tasks in CI"
```

---

### Task 9: Cancellare il workflow `cli` legacy e pinnare sonata

**Repository:** `~/Downloads/nanolab` (e uno push in `~/Downloads/sonata`)

**Files:**
- Delete: `packages/workflow-tasks/src/workflow_tasks/workflows/cli.py`
- Delete: `packages/workflow-tasks/tests/workflows/test_cli.py`
- Modify: `packages/sonata-tasks/pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: tutto quanto sopra.
- Produces: nessuna API. `workflow_tasks.workflows.cli` cessa di esistere; `sonata-engine` è pinnata a un commit immutabile.

- [ ] **Step 1: Verificare che il workflow legacy sia davvero orfano**

Run:
```bash
cd ~/Downloads/nanolab
grep -rn "workflows.cli\|workflows import cli\|cli_task_specs\|cli_cleanup_specs" packages --include="*.py" | grep -v __pycache__
```
Expected: gli unici riscontri sono in `packages/workflow-tasks/src/workflow_tasks/workflows/cli.py` e `packages/workflow-tasks/tests/workflows/test_cli.py`. Se compare qualcos'altro, quel consumer va migrato prima: non cancellare.

- [ ] **Step 2: Cancellare l'implementazione originale**

Run:
```bash
cd ~/Downloads/nanolab
git rm packages/workflow-tasks/src/workflow_tasks/workflows/cli.py \
       packages/workflow-tasks/tests/workflows/test_cli.py
```

- [ ] **Step 3: Eseguire la suite workflow-tasks**

Run:
```bash
cd ~/Downloads/nanolab
uv run --all-packages --all-groups \
    pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
```
Expected: PASS, con la copertura ancora sopra la soglia del 90%. Se la copertura scende sotto soglia perché è sparito codice ben coperto, non abbassare la soglia: verifica quali moduli sono ora scoperti e segnalalo nel messaggio di commit.

- [ ] **Step 4: Pushare sonata e ottenere il commit immutabile**

Run:
```bash
cd ~/Downloads/sonata
git push origin main
git rev-parse HEAD
```
Annota lo SHA completo di 40 caratteri.

- [ ] **Step 5: Sostituire la source locale con il pin Git**

In `packages/sonata-tasks/pyproject.toml`, sostituisci la sezione `[tool.uv.sources]`:

```toml
[tool.uv.sources]
sonata-engine = { git = "https://github.com/miciav/sonata.git", rev = "<SHA-COMPLETO-DALLO-STEP-4>" }
workflow-tasks = { workspace = true }
```

E nella lista `dependencies` sostituisci `"sonata-engine",` con:

```toml
    "sonata-engine @ git+https://github.com/miciav/sonata.git@<SHA-COMPLETO-DALLO-STEP-4>",
```

- [ ] **Step 6: Rigenerare e verificare il lockfile**

Run:
```bash
cd ~/Downloads/nanolab
uv lock
uv sync --all-packages --all-groups
grep -n "sonata" uv.lock | head -20
```
Expected: `uv.lock` contiene lo stesso SHA dello step 4, e non contiene più un riferimento al path locale `../../../sonata`.

- [ ] **Step 7: Eseguire tutte e tre le suite**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups \
    pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
```
Expected: PASS tutte e tre.

- [ ] **Step 8: Eseguire tutti i controlli statici e i contratti**

Run:
```bash
cd ~/Downloads/nanolab
uv lock --check
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/workflow-tasks
uv run --all-packages --all-groups basedpyright --project packages/tui-toolkit
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --all-packages --all-groups lint-imports --config packages/workflow-tasks/.importlinter --no-cache
uv run --all-packages --all-groups lint-imports --config packages/tui-toolkit/.importlinter --no-cache
```
Expected: PASS tutti.

- [ ] **Step 9: Verificare lo smoke dei wheel come in CI**

Run:
```bash
cd ~/Downloads/nanolab
uv build --all-packages --out-dir dist --clear
uv venv .wheel-smoke
uv pip install dist/nanolab-0.1.0-py3-none-any.whl dist/sonata_tasks-0.1.0-py3-none-any.whl \
    dist/workflow_tasks-0.1.0-py3-none-any.whl dist/tui_toolkit-0.1.0-py3-none-any.whl \
    --python .wheel-smoke/bin/python
.wheel-smoke/bin/python -c "import nanolab, sonata_tasks, workflow_tasks, tui_toolkit"
.wheel-smoke/bin/nanolab --help
rm -rf .wheel-smoke dist
```
Expected: install e import riescono; `sonata-engine` viene scaricata dal pin Git.

- [ ] **Step 10: Commit**

```bash
cd ~/Downloads/nanolab
git add -A packages .github uv.lock
git status
git commit -m "Delete the legacy cli workflow and pin sonata-engine"
```

Controlla l'output di `git status` prima di committare: devono comparire solo le cancellazioni previste, `packages/sonata-tasks/pyproject.toml` e `uv.lock`.

---

### Task 10: Validazione finale contro un control plane reale

**Repository:** `~/Downloads/nanolab`

**Files:** nessuna modifica attesa. Se questo task ne richiede, sono bug scoperti dalla validazione e vanno corretti con il loro test.

**Interfaces:**
- Consumes: l'incremento completo.
- Produces: la conferma che il workflow migrato funziona davvero, non solo con executor finti.

- [ ] **Step 1: Verificare il piano senza toccare nulla**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli.yaml
```
Expected: i cinque ID compilati, `001.build-nanofaas-cli` … `005.release-word-stats-java`.

- [ ] **Step 2: Verificare che uno slug inesistente venga rifiutato in modo leggibile**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab plan packages/nanolab/scenarios-v2/cli.yaml --only cli.function.list
```
Expected: errore che dice `no task matches slug 'cli.function.list'` ed elenca gli slug disponibili. È il vecchio identificatore: deve fallire in modo esplicito, non silenziosamente eseguire tutto.

- [ ] **Step 3: Eseguire il workflow contro un control plane reale**

Serve un control plane nanoFaaS raggiungibile su `http://127.0.0.1:8080` e la toolchain Gradle disponibile in `NANOFAAS_ROOT`.

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab run packages/nanolab/scenarios-v2/cli.yaml
```
Expected: le cinque fasi passano in ordine e la console mostra una riga `running` e una `passed` per ognuna, con gli ID compilati.

- [ ] **Step 4: Verificare che la function venga rimossa anche dopo un fallimento**

Registra a mano una function con lo stesso nome per far fallire l'apply, poi rilancia e verifica che il control plane non resti sporco:

```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab \
    nanolab run packages/nanolab/scenarios-v2/cli.yaml --only invoke-word-stats-java
curl -s http://127.0.0.1:8080/v1/functions | grep -c word-stats-java
```
Expected: il run passa e il conteggio finale è `0` — la risorsa ha rilasciato la function anche essendo stata acquisita solo per la slice.

- [ ] **Step 5: Verificare la TUI**

Run:
```bash
cd ~/Downloads/nanolab
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --package nanolab nanolab
```
Naviga fino allo scenario `cli`, scegli `plan`, verifica che la tabella mostri gli ID compilati; poi esci e ripeti scegliendo l'esecuzione, verificando che le fasi si illuminino una per una.

- [ ] **Step 6: Commit di eventuali correzioni**

Se gli step 1–5 hanno richiesto correzioni, ognuna deve avere il proprio test di regressione. Commit:

```bash
cd ~/Downloads/nanolab
git add -A packages
git commit -m "Fix <cosa> found validating the migrated cli workflow"
```

Se non è servita nessuna correzione, non c'è niente da committare: l'incremento è chiuso.

---

## Verifica finale dell'incremento

Tutti i comandi seguenti devono passare prima di considerare la migrazione del workflow `cli` completata:

```bash
# Sonata
cd ~/Downloads/sonata
uv run pytest && uv run ruff check --no-cache . && uv run basedpyright

# nanolab
cd ~/Downloads/nanolab
uv lock --check
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups \
    pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
uv run --all-packages --all-groups \
    pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
uv run --all-packages --all-groups ruff check packages
```

E la conferma qualitativa: `packages/workflow-tasks/src/workflow_tasks/workflows/` contiene un workflow in meno, `packages/sonata-tasks/` uno in più, e nessun adapter fra i due engine è stato aggiunto da nessuna parte.
