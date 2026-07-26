# Sonata VM and Helm CLI Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Apply TDD
> with `superpowers:test-driven-development`, and run
> `superpowers:verification-before-completion` before publishing either branch.

**Goal:** Portare VM, bootstrap e release Helm dentro un unico workflow Sonata per
`cli/k8s --provision`, eseguire la CLI nella VM ed esporre lo stesso percorso nella
TUI.

**Architecture:** Sonata riceve soltanto le primitive generiche mancanti:
`Resource[T]`, `TaskInputs` e dipendenze tra risorse. `sonata-tasks` costruisce
Resource VM/Helm e comandi risolvibili a runtime; `nanolab` compone provider,
bootstrap, valori Helm, function e interfacce CLI/TUI. Il vecchio
`provision_environment` resta intatto per i workflow legacy.

**Tech Stack:** Python 3.12, uv, pytest, Ruff, basedpyright, import-linter,
Typer, Rich, Sonata, workflow-tasks, Multipass, k3s, Helm.

**Spec:** `docs/superpowers/specs/2026-07-26-sonata-vm-helm-cli-design.md`

---

## Vincoli globali

- Baseline nanolab: `9878efe` su `main`.
- Baseline Sonata: `0bafb9ab3778b54ca3f08a75c1f3ccd0ed3e017b` su `main`.
- Crea due branch normali, senza worktree:
  - Sonata: `codex/resource-values`;
  - nanolab: `codex/sonata-vm-helm-cli`.
- Non riaprire il percorso `cli/container`: processo locale, readiness, build e
  rifiuto di `--provision`/`--keep` sono già il comportamento approvato.
- `cli/k8s` senza `--provision` continua a richiedere
  `--control-plane-url` ed esegue la CLI sull'host.
- `cli/k8s --provision` costruisce la CLI sull'host, poi la esegue nella VM
  contro `http://127.0.0.1:30080`.
- VM, Helm e function sono `infrastructure=True`; `--keep` conserva l'intera
  catena, incluse le dipendenze transitive.
- La TUI espone due voci CLI: `Container` e `Kubernetes (provisioned)`.
  Quest'ultima non propone “Use existing”: richiede un ambiente non locale,
  imposta sempre `provision=True` e propone soltanto `Cleanup`/`Keep`.
- `WorkflowContext` resta esclusivamente osservabilità. Non deve contenere
  valori prodotti dai task o accesso al runner.
- Nessun DAG generico task-to-task e nessuna serializzazione dei valori runtime.
- `sonata-engine` non importa VM, Helm, executor o nanolab.
- `sonata_tasks` non importa `nanolab` né i package legacy vietati dai contratti.
- Nessun task applicativo assegna il proprio `task_id`: l'identità resta del compiler.
- Prima di ogni commit: `git status --short`, poi `git add` soltanto dei file
  elencati dal task. Non usare `git add -A`.
- Non aggiungere trailer `Co-Authored-By`.
- Non fissare nanolab al commit di un branch Sonata. Il pin finale deve indicare un
  commit già presente in `origin/main` di Sonata.

## Topologia attesa

Il piano completo con una function deve essere esattamente:

```text
001.build-nanofaas-cli
002.acquire-stack-vm
003.provision-base-vm-dependencies
004.install-k3s
005.ensure-local-registry-container
006.configure-k3s-registry-access
007.sync-repository-into-vm
008.acquire-control-plane-helm-release
009.acquire-word-stats-java
010.list-functions
011.invoke-word-stats-java
012.release-word-stats-java
013.release-control-plane-helm-release
014.release-stack-vm
```

La slice `--only invoke-word-stats-java` conserva soltanto consumer selezionato e
risorse transitive:

```text
Acquire stack VM
Acquire control-plane Helm release
Acquire word-stats-java
Invoke word-stats-java
Release word-stats-java
Release control-plane Helm release
Release stack VM
```

I task ordinari di bootstrap non sono prerequisite automatiche di una slice. Una
slice avanzata su VM appena creata richiede quindi una VM già preparata; è lo stesso
contratto di selection già approvato.

## Comandi di verifica

Da `/Users/micheleciavotta/Downloads/sonata`:

```bash
uv run pytest
uv run ruff check --no-cache .
uv run basedpyright
uv build --clear
```

Da `/Users/micheleciavotta/Downloads/nanolab`:

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
uv run --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests -q
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups basedpyright --project packages/workflow-tasks
uv run --all-packages --all-groups basedpyright --project packages/tui-toolkit
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv lock --check
```

---

### Task 1: Introdurre `TaskInputs` e i valori di `Resource[T]` in Sonata

**Repo:** `/Users/micheleciavotta/Downloads/sonata`

**Files:**

- Create: `src/sonata_engine/core/inputs.py`
- Modify: `src/sonata_engine/core/task.py`
- Modify: `src/sonata_engine/core/resource_task.py`
- Modify: `src/sonata_engine/core/workflow.py`
- Modify: `src/sonata_engine/core/__init__.py`
- Modify: `src/sonata_engine/__init__.py`
- Modify: `src/sonata_engine/errors.py`
- Test: `tests/core/test_task.py`
- Test: `tests/core/test_resource_cleanup.py`
- Test: `tests/core/test_workflow.py`
- Test: `tests/typecheck/task_contracts.py`
- Mechanically update task signatures in:
  `tests/core/test_compile_selection.py`,
  `tests/core/test_compiled.py`,
  `tests/core/test_run_selection.py`,
  `tests/test_journal.py`,
  `tests/test_resume.py`.

**Produces:**

```python
class TaskInputs:
    @classmethod
    def empty(cls) -> TaskInputs: ...

    def resource(self, resource: Resource[T]) -> T: ...


class Task(Generic[T], ABC):
    @abstractmethod
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]: ...


@dataclass(frozen=True, slots=True)
class Resource(Generic[T]):
    title: str
    acquire: Callable[[TaskInputs], T]
    release: Callable[[TaskInputs, T], None]
    requires: tuple[Resource[Any], ...] = ()
    infrastructure: bool = False
    acquire_idempotent: bool = False
```

- [ ] **Step 1: creare il branch e confermare la baseline**

```bash
git switch -c codex/resource-values 0bafb9ab3778b54ca3f08a75c1f3ccd0ed3e017b
git status --short --branch
```

- [ ] **Step 2: scrivere test rossi**

Copri almeno:

1. acquire produce un oggetto e il consumer riceve lo stesso oggetto;
2. release riceve esattamente il valore dell'acquire;
3. `Resource[None]` è distinta da risorsa non acquisita;
4. un consumer che chiede una risorsa non dichiarata riceve
   `UndeclaredResourceError`;
5. una risorsa dichiarata ma non disponibile riceve
   `ResourceUnavailableError`;
6. il valore non entra nel journal;
7. un resume riesegue l'acquire e ricostruisce il valore;
8. typecheck di `TaskInputs.resource()` conserva `T`.

Usa un valore con identità osservabile:

```python
token = object()
resource = Resource[object](
    title="Acquire token",
    acquire=lambda _inputs: token,
    release=lambda _inputs, value: released.append(value),
)
```

Esegui:

```bash
uv run pytest tests/core/test_task.py tests/core/test_resource_cleanup.py tests/core/test_workflow.py tests/test_resume.py -q
uv run basedpyright
```

Atteso: fallimento per API e firme mancanti.

- [ ] **Step 3: implementare il minimo**

`TaskInputs` deve essere una vista immutabile di:

- mapping `Resource[Any] -> object`;
- set delle risorse accessibili al task.

Il runner possiede una `_RunState` privata con mapping mutabile e un sentinel di
presenza. La sequenza dell'acquire è:

1. costruire gli input permessi;
2. eseguire e validare il `TaskOutcome`;
3. pubblicare `outcome.value`;
4. aggiungere il release ai pending.

Non pubblicare il valore prima della validazione del `TaskOutcome`. Il release
rimuove il valore dopo il tentativo, anche se il callback fallisce.

`ResourceOp` deve portare la Resource e il tipo di operazione, non una closure
senza input:

```python
class ResourceOperation(StrEnum):
    ACQUIRE = "acquire"
    RELEASE = "release"
```

Il release può leggere la propria Resource oltre alle dipendenze dichiarate.

- [ ] **Step 4: migrare tutte le firme Sonata**

Aggiorna tutte le implementazioni/test double da `run(self)` a
`run(self, _inputs: TaskInputs)`. Non introdurre uno shim con `inspect.signature`.

- [ ] **Step 5: verificare e committare**

```bash
uv run pytest
uv run ruff check --no-cache .
uv run basedpyright
git diff --check
git status --short
git add src/sonata_engine/core/inputs.py src/sonata_engine/core/task.py src/sonata_engine/core/resource_task.py src/sonata_engine/core/workflow.py src/sonata_engine/core/__init__.py src/sonata_engine/__init__.py src/sonata_engine/errors.py tests/core/test_task.py tests/core/test_resource_cleanup.py tests/core/test_workflow.py tests/core/test_compile_selection.py tests/core/test_compiled.py tests/core/test_run_selection.py tests/test_journal.py tests/test_resume.py tests/typecheck/task_contracts.py
git commit -m "feat: add typed resource values"
```

---

### Task 2: Compilare dipendenze tra Resource in Sonata

**Repo:** `/Users/micheleciavotta/Downloads/sonata`

**Files:**

- Modify: `src/sonata_engine/core/workflow.py`
- Modify: `src/sonata_engine/core/compiled.py`
- Modify: `src/sonata_engine/errors.py`
- Modify: `src/sonata_engine/journal.py`
- Test: `tests/core/test_compiled.py`
- Test: `tests/core/test_compile_selection.py`
- Test: `tests/core/test_run_selection.py`
- Test: `tests/core/test_resource_cleanup.py`
- Test: `tests/test_journal.py`
- Test: `tests/test_resume.py`

- [ ] **Step 1: scrivere test rossi sul grafo**

Costruisci `vm`, `helm(requires=(vm,))`,
`function(requires=(helm,))` e un consumer che richiede `function`.

Verifica:

- ordine acquire VM → Helm → function;
- ordine release function → Helm → VM;
- risorse condivise acquisite una volta;
- ordinamento stabile tra dipendenze sorelle;
- ciclo diretto e indiretto produce `ResourceDependencyCycleError` con il percorso;
- `select=Selection(only="invoke")` conserva la chiusura transitiva;
- il consumer vede `function` ma non può leggere direttamente `helm` o `vm`;
- acquire/release Helm vedono VM;
- due grafi con task/titoli uguali ma archi diversi hanno fingerprint diverso.

```bash
uv run pytest tests/core/test_compiled.py tests/core/test_compile_selection.py tests/core/test_run_selection.py tests/test_journal.py -q
```

- [ ] **Step 2: implementare DFS stabile e accesso stretto**

Espandi `requires` con DFS in ordine dichiarato e colori
`unseen/visiting/done`. Usa la chiusura solo per placement e lifecycle;
`CompiledTask.required_resources` del consumer resta la tupla dichiarata
direttamente.

Per gli acquire/release generati, `required_resources` contiene le dipendenze
dirette della Resource.

- [ ] **Step 3: rendere il fingerprint sensibile agli archi**

Nel payload del fingerprint registra, per ciascun acquire, gli ID compilati
degli acquire richiesti. Non usare `repr()` delle callback o indirizzi runtime.

- [ ] **Step 4: retention transitiva con `keep_infrastructure`**

Prima del run calcola il set retained:

```text
ogni Resource infrastructure
+ chiusura transitiva delle sue requires
```

Salta il release per tutto il set, non soltanto per la Resource che porta il
flag. Aggiungi test sia sul percorso felice sia sul cleanup dopo errore.

- [ ] **Step 5: verificare e committare**

```bash
uv run pytest
uv run ruff check --no-cache .
uv run basedpyright
git diff --check
git status --short
git add src/sonata_engine/core/workflow.py src/sonata_engine/core/compiled.py src/sonata_engine/errors.py src/sonata_engine/journal.py tests/core/test_compiled.py tests/core/test_compile_selection.py tests/core/test_run_selection.py tests/core/test_resource_cleanup.py tests/test_journal.py tests/test_resume.py
git commit -m "feat: compile resource dependency graphs"
```

---

### Task 3: Documentare e pubblicare la candidata Sonata

**Repo:** `/Users/micheleciavotta/Downloads/sonata`

**Files:**

- Modify: `README.md`
- Test: `tests/test_package_boundaries.py`
- Test: `tests/typecheck/task_contracts.py`

- [ ] **Step 1: aggiungere esempi pubblici**

Documenta:

- differenza tra `WorkflowContext` e `TaskInputs`;
- `Resource[T]` con acquire/release;
- Resource con `requires`;
- selection e retention transitive;
- valori runtime non journalizzati e ricostruiti al resume.

- [ ] **Step 2: verificare export e confini**

Gli utenti devono importare da `sonata_engine`:

```python
from sonata_engine import Resource, TaskInputs
```

Il test di package boundary deve continuare a impedire dipendenze applicative.

- [ ] **Step 3: eseguire la suite completa e committare**

```bash
uv run pytest
uv run ruff check --no-cache .
uv run basedpyright
uv build --clear
git diff --check
git status --short
git add README.md tests/test_package_boundaries.py tests/typecheck/task_contracts.py
git commit -m "docs: explain resource inputs and dependencies"
git log --oneline 0bafb9ab..HEAD
```

- [ ] **Step 4: push della branch candidata**

```bash
git push -u origin codex/resource-values
```

Non aggiornare ancora il pin nanolab: prima il codice Sonata deve essere
revisionato e poi integrato in `origin/main`.

---

### Task 4: Migrare `sonata-tasks` alla nuova firma senza cambiare comportamento

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify temporarily: `pyproject.toml`
- Modify: `packages/sonata-tasks/src/sonata_tasks/command.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/cli.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/process.py`
- Test: `packages/sonata-tasks/tests/test_command.py`
- Test: `packages/sonata-tasks/tests/test_cli.py`
- Test: `packages/sonata-tasks/tests/test_process.py`
- Modify generated: `uv.lock`

- [ ] **Step 1: confermare il branch creato con i documenti**

La pianificazione crea `codex/sonata-vm-helm-cli` da `9878efe` e vi committa
design e piano. Prima di implementare verifica branch e merge-base:

```bash
git status --short --branch
git branch --show-current
git merge-base HEAD main
```

Il branch deve essere `codex/sonata-vm-helm-cli` e il merge-base deve essere
`9878efe`. Se l'esecuzione parte da un clone dove il branch non è presente,
crealo da `9878efe` e porta soltanto il commit dei documenti.

- [ ] **Step 2: usare Sonata locale durante lo sviluppo**

Nel solo periodo di sviluppo, aggiungi alla root `[tool.uv.sources]`:

```toml
sonata-engine = { path = "../sonata", editable = true }
```

Mantieni la dependency dichiarata in
`packages/sonata-tasks/pyproject.toml`; la source workspace la sostituisce
soltanto in sviluppo. Rigenera il lock:

```bash
uv lock
```

- [ ] **Step 3: scrivere test rossi per il nuovo contratto**

I test diretti devono chiamare:

```python
task.run(TaskInputs.empty())
resource.acquire(TaskInputs.empty())
```

Il processo gestito deve diventare `Resource[Popen[Any]]`; il release deve
ricevere il processo prodotto, senza campo/cella mutabile.

- [ ] **Step 4: implementare la migrazione minima**

Aggiorna le callback CLI function a `acquire(inputs)` e
`release(inputs, value)`. In questo task non aggiungere ancora VM, Helm,
readiness k8s o argv dinamici.

- [ ] **Step 5: verificare e committare**

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups ruff check packages/sonata-tasks
git diff --check
git status --short
git add pyproject.toml packages/sonata-tasks/src/sonata_tasks/command.py packages/sonata-tasks/src/sonata_tasks/cli.py packages/sonata-tasks/src/sonata_tasks/process.py packages/sonata-tasks/tests/test_command.py packages/sonata-tasks/tests/test_cli.py packages/sonata-tasks/tests/test_process.py uv.lock
git commit -m "refactor: adopt Sonata task inputs"
```

---

### Task 5: Risolvere argv ed environment dei comandi a runtime

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify: `packages/sonata-tasks/src/sonata_tasks/command.py`
- Test: `packages/sonata-tasks/tests/test_command.py`

**Interface:**

```python
Argv = tuple[str, ...] | Callable[[TaskInputs], tuple[str, ...]]

@dataclass
class CommandTask(Task[TaskResult]):
    argv: Argv
    env: Mapping[str, str] = field(default_factory=dict)
```

- [ ] **Step 1: scrivere test rossi**

Verifica che:

- una tupla statica resti invariata;
- il resolver riceva `TaskInputs`;
- il resolver possa leggere una `Resource[VmInfo]` dichiarata;
- `env` arrivi a `CommandTaskSpec`;
- il resolver venga chiamato una volta per esecuzione;
- un errore del resolver fallisca prima di invocare l'executor.

- [ ] **Step 2: implementare il minimo**

Rendi `_spec(inputs)` l'unico punto che risolve argv ed env. Non aggiungere un
secondo tipo di task remoto.

- [ ] **Step 3: verificare e committare**

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_command.py -q
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
git diff --check
git status --short
git add packages/sonata-tasks/src/sonata_tasks/command.py packages/sonata-tasks/tests/test_command.py
git commit -m "feat: resolve command arguments at runtime"
```

---

### Task 6: Aggiungere la Resource VM generica

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Create: `packages/sonata-tasks/src/sonata_tasks/vm.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Create: `packages/sonata-tasks/tests/test_vm.py`

**Produces:**

```python
vm_resource(
    *,
    title: str,
    lifecycle: VmLifecycleAdapter,
    config: VmConfig,
    fallback_info: VmInfo,
    external: bool = False,
) -> Resource[VmInfo]
```

- [ ] **Step 1: scrivere test rossi**

Copri:

- acquire restituisce l'esatto `VmInfo` prodotto da `EnsureVmRunning`;
- release distrugge quell'esatto valore, non un campo mutabile;
- lifecycle external non distrugge;
- acquire fallito compensa usando `fallback_info`;
- errore di compensazione viene aggiunto come nota, senza mascherare l'errore
  primario;
- la Resource è `infrastructure=True`.

- [ ] **Step 2: implementare riusando gli adapter esistenti**

Non spostare provider/configuration in Sonata. L'helper può usare
`workflow_tasks` per la meccanica VM, ma non deve importare `nanolab`.

- [ ] **Step 3: verificare contratti e committare**

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_vm.py -q
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
git diff --check
git status --short
git add packages/sonata-tasks/src/sonata_tasks/vm.py packages/sonata-tasks/src/sonata_tasks/__init__.py packages/sonata-tasks/tests/test_vm.py
git commit -m "feat: add Sonata VM resource"
```

---

### Task 7: Aggiungere la Resource Helm generica

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Create: `packages/sonata-tasks/src/sonata_tasks/helm.py`
- Modify: `packages/sonata-tasks/src/sonata_tasks/__init__.py`
- Create: `packages/sonata-tasks/tests/test_helm.py`

**Produces:**

```python
@dataclass(frozen=True, slots=True)
class HelmReleaseSpec:
    release: str
    chart: str
    namespace: str
    values: tuple[str, ...]
    role: ExecutionRole = "stack"
    timeout: str = "5m"


def helm_release_resource(
    spec: HelmReleaseSpec,
    *,
    executor: CommandTaskExecutor,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[HelmReleaseSpec]: ...
```

- [ ] **Step 1: scrivere test rossi**

Verifica:

- acquire usa `helm upgrade --install`, `--create-namespace`, `--wait` e timeout;
- values sono argomenti, non shell interpolata;
- release usa `helm uninstall --ignore-not-found --wait`;
- Helm dichiara la VM in `requires`;
- acquire fallito tenta uninstall best-effort;
- errore di cleanup è una nota sull'errore primario;
- Resource è infrastructure.

- [ ] **Step 2: implementare il minimo**

Il builder è generico: niente nome chart nanoFaaS, porte o valori di prodotto
hard-coded in `sonata-tasks`.

- [ ] **Step 3: verificare e committare**

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_helm.py -q
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
git diff --check
git status --short
git add packages/sonata-tasks/src/sonata_tasks/helm.py packages/sonata-tasks/src/sonata_tasks/__init__.py packages/sonata-tasks/tests/test_helm.py
git commit -m "feat: add Sonata Helm resource"
```

---

### Task 8: Comporre VM, bootstrap, Helm e readiness nel piano CLI

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify: `packages/sonata-tasks/src/sonata_tasks/cli.py`
- Modify: `packages/sonata-tasks/tests/test_cli.py`
- Modify: `packages/nanolab/src/nanolab/plans/cli.py`
- Modify: `packages/nanolab/tests/plans/test_cli.py`
- Reuse unchanged:
  `packages/nanolab/src/nanolab/cli/vm_provider.py`,
  `packages/nanolab/src/nanolab/cli/provisioning.py`.

- [ ] **Step 1: testare prima la topologia di prodotto**

In `packages/nanolab/tests/plans/test_cli.py` aggiungi un ambiente Multipass
finto e verifica gli esatti 14 ID della sezione “Topologia attesa”.

Verifica inoltre:

- build CLI usa ruolo `host`;
- bootstrap usa executor host con argv SSH risolti dal `VmInfo`;
- list/invoke/apply/delete usano ruolo `stack`;
- endpoint CLI è `http://127.0.0.1:30080`;
- Helm richiede VM;
- function richiede Helm;
- `only=invoke-word-stats-java` conserva VM, Helm e function;
- container e k8s non provisioned non cambiano.

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
```

- [ ] **Step 2: separare ruolo di build e ruolo runtime**

Estendi `CliWorkflowRequest` con `build_role: ExecutionRole = "host"`.
`Build nanofaas-cli`, build control plane e build immagini usano `build_role`;
apply/list/invoke/delete usano `cli_role`.

- [ ] **Step 3: costruire bootstrap runtime**

Riusa i planner esistenti:

```text
plan_vm_provision_base
plan_k3s_install
plan_registry_ensure_container
plan_k3s_configure_registry
plan_repo_sync_to_vm
```

Produci le operazioni prima del run con una request placeholder. Ogni
`CommandTask.argv` le ritargetta a runtime usando il `VmInfo` ottenuto da
`TaskInputs` e `retarget_bootstrap_operation`. Conserva `env`, `cwd`,
`remote_dir` ed exit codes del `CommandTaskSpec`.

Il sync deve includere `clients/cli/build/install/nanofaas-cli` già costruito.

- [ ] **Step 4: costruire valori Helm in nanolab**

Usa la chart del repository con:

- namespace dedicato;
- demo disabilitate;
- HTTP NodePort `30080`;
- actuator NodePort `30081`;
- registry/configurazione già usata dal control plane k3s.

La Resource Helm usa ruolo stack e `requires=(vm,)`.

- [ ] **Step 5: aggiungere readiness function soltanto al provisioned k8s**

Estendi il builder function con callback opzionale di readiness. Dopo `fn apply`:

1. attendi bounded che compaia `deployment/fn-<name>`;
2. esegui `kubectl rollout status deployment/fn-<name> --timeout=...`.

Entrambi girano nella VM. Se apply/readiness falliscono, esegui `fn delete`
best-effort e rilancia l'errore primario.

Non modificare container-local né k8s non provisioned.

- [ ] **Step 6: verificare e committare**

```bash
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_cli.py -q
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/plans/test_cli.py -q
uv run --all-packages --all-groups basedpyright --project packages/sonata-tasks
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/sonata-tasks/.importlinter --no-cache
git diff --check
git status --short
git add packages/sonata-tasks/src/sonata_tasks/cli.py packages/sonata-tasks/tests/test_cli.py packages/nanolab/src/nanolab/plans/cli.py packages/nanolab/tests/plans/test_cli.py
git commit -m "feat: provision CLI workflow with Sonata"
```

---

### Task 9: Collegare `run` e `plan --provision` senza wrapper legacy

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify: `packages/nanolab/src/nanolab/cli/product.py`
- Modify: `packages/nanolab/tests/cli/test_command_surface.py`

- [ ] **Step 1: scrivere test rossi sulla matrice CLI**

Copri:

| Scenario | Provision | Risultato |
|---|---:|---|
| cli/container local | no | percorso attuale |
| cli/container | sì | errore |
| cli/k8s | no | URL esplicito obbligatorio |
| cli/k8s nonlocal | sì | URL non richiesto, workflow Sonata provisioned |
| workflow legacy nonlocal | sì | usa ancora `provision_environment` |

Verifica anche che `plan cli.yaml --provision --environment ...` mostri i 14
task e che `run cli.yaml --provision` non entri nel context manager legacy.

- [ ] **Step 2: propagare `provision` nel builder**

Estendi `_workflow(..., provision: bool = False)` e passa scenario/environment
a `build_cli_plan` quando serve costruire le risorse.

Modifica `_require_cli_endpoint` per richiedere URL soltanto quando:

```python
scenario.workflow == "cli"
and scenario.backend == "k8s"
and not provision
```

- [ ] **Step 3: un solo proprietario del lifecycle**

Nel comando `run`, usa `provision_environment` soltanto se il percorso non è
`cli/k8s` Sonata provisioned. Il workflow Sonata riceve
`keep_infrastructure=keep` e possiede VM/Helm/function.

Aggiungi `--provision` e `--environment` al comando `plan` con le stesse
validazioni del run.

- [ ] **Step 4: verificare e committare**

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/cli/test_command_surface.py -q
uv run --all-packages --all-groups basedpyright --project packages/nanolab
git diff --check
git status --short
git add packages/nanolab/src/nanolab/cli/product.py packages/nanolab/tests/cli/test_command_surface.py
git commit -m "feat: expose Sonata provisioning in CLI"
```

---

### Task 10: Esporre Kubernetes provisioned nella TUI

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify: `packages/nanolab/src/nanolab/tui/app.py`
- Modify: `packages/nanolab/tests/test_tui_app.py`
- Test as needed: `packages/nanolab/tests/test_tui_navigation.py`

- [ ] **Step 1: scrivere test rossi sul menu**

Il menu CLI deve essere:

```python
CLI_MENU = [
    Choice("Container", "container", ...),
    Choice("Kubernetes (provisioned)", "kubernetes", ...),
]
```

e la mappa:

```python
("cli", "container"): "cli-container.yaml"
("cli", "kubernetes"): "cli.yaml"
```

Verifica che il percorso Kubernetes:

- rifiuti un ambiente local con messaggio leggibile;
- non mostri `_PROVISION_CHOICES`;
- imposti sempre `provision=True`;
- mostri `Cleanup`/`Keep` sia prima del run sia nel summary;
- passi `provision=True` alla preview e al workflow reale;
- non entri in `provision_environment`;
- preview e run mostrino/eseguano gli stessi ID Sonata;
- `Keep` imposti `workflow.keep_infrastructure=True`;
- Back da cleanup torni ad Action senza contaminare il percorso container.

- [ ] **Step 2: rendere il tipo di percorso esplicito**

Non dedurre il provisioning soltanto dal nome file. Conserva una proprietà
locale derivata dalla coppia `(section, action)`:

```python
provisioned_cli = scenario_name == "cli.yaml"
```

oppure un piccolo mapping booleano adiacente a `_SCENARIO_FILES`. Non introdurre
una nuova gerarchia di classi per due opzioni.

- [ ] **Step 3: usare lo stesso workflow per plan, preview e run**

Estendi `_build_workflow(..., provision: bool)` e propaga il flag a
`nanolab.cli.product._workflow`.

Per `Kubernetes (provisioned)`:

1. selezione ambiente non locale;
2. scelta Plan/Run;
3. scelta Cleanup/Keep per Run;
4. costruzione workflow Sonata con `provision=True`;
5. esecuzione diretta sotto i sink esistenti.

Il percorso container resta identico e continua a non accettare Keep.

- [ ] **Step 4: verificare e committare**

```bash
NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas uv run --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/test_tui_app.py packages/nanolab/tests/test_tui_navigation.py -q
uv run --all-packages --all-groups basedpyright --project packages/nanolab
git diff --check
git status --short
git add packages/nanolab/src/nanolab/tui/app.py packages/nanolab/tests/test_tui_app.py packages/nanolab/tests/test_tui_navigation.py
git commit -m "feat: expose provisioned CLI path in TUI"
```

---

### Task 11: Integrare Sonata main e fissare il pin riproducibile

**Ordine obbligatorio:** prima review/merge Sonata, poi pin nanolab.

- [ ] **Step 1: revisionare Sonata**

Da `/Users/micheleciavotta/Downloads/sonata`:

```bash
git status --short --branch
uv run pytest
uv run ruff check --no-cache .
uv run basedpyright
uv build --clear
git diff 0bafb9ab...HEAD
```

Richiedere review con `superpowers:requesting-code-review`; correggere soltanto
finding verificati con `superpowers:receiving-code-review`.

- [ ] **Step 2: integrare Sonata**

Creare/mergiare la PR Sonata. Poi:

```bash
git fetch origin
git merge-base --is-ancestor HEAD origin/main
git rev-parse origin/main
```

Il primo comando deve uscire `0`. Registra il full SHA risultante come
`SONATA_MAIN_SHA`.

- [ ] **Step 3: rimuovere l'override locale e aggiornare il pin**

Da `/Users/micheleciavotta/Downloads/nanolab`:

- rimuovi la source path temporanea dalla root;
- sostituisci il pin in `packages/sonata-tasks/pyproject.toml` con il full
  `SONATA_MAIN_SHA`;
- rigenera `uv.lock`.

```bash
uv lock
uv lock --check
rg -n "sonata.git" packages/sonata-tasks/pyproject.toml uv.lock
```

La ricerca non deve mostrare path locale o nome della feature branch.

- [ ] **Step 4: committare il pin**

```bash
git diff --check
git status --short
git add packages/sonata-tasks/pyproject.toml uv.lock
git commit -m "build: pin Sonata resource support"
```

---

### Task 12: CI, regressione completa e validazione live

**Repo:** `/Users/micheleciavotta/Downloads/nanolab`

**Files:**

- Modify: `.github/workflows/ci.yml`
- Modify: `docs/superpowers/specs/2026-07-26-sonata-vm-helm-cli-design.md`
- Modify: `docs/superpowers/plans/2026-07-26-sonata-vm-helm-cli.md`

- [x] **Step 1: aggiungere un contratto CI statico**

Aggiungi alla CI un piano rappresentativo provisioned con un environment
Multipass di fixture, senza avviare la VM. Il comando deve verificare almeno:

```text
002.acquire-stack-vm
008.acquire-control-plane-helm-release
014.release-stack-vm
```

Mantieni anche i smoke esistenti per `cli.yaml` non provisioned e
`cli-container.yaml`.

- [x] **Step 2: eseguire tutte le verifiche nanolab**

Esegui tutti i comandi della sezione “Comandi di verifica”, poi:

```bash
uv build --package sonata-tasks
uv build --package nanolab
git diff --check
```

Controlla esplicitamente:

- contratti di import;
- workflow-tasks sopra il gate 90%;
- piano container invariato;
- piano k8s non provisioned invariato;
- piano provisioned a 14 task;
- slice con closure Resource;
- cleanup UI visibile sugli eventi Sonata;
- nessun import/uso del wrapper legacy nel percorso CLI provisioned.

- [x] **Step 3: validare dal vivo contro Multipass**

Con una VM disposable:

1. `nanolab plan ...cli.yaml --environment ... --provision`;
2. run completo;
3. verifica list/invoke riusciti;
4. verifica function assente dopo cleanup;
5. verifica Helm release assente dopo cleanup;
6. verifica VM distrutta dopo cleanup;
7. slice `--only invoke-word-stats-java` su VM preparata;
8. fallimento forzato di invoke e cleanup in ordine function → Helm → VM;
9. run `--keep` e verifica che VM, Helm e function restino;
10. cleanup manuale esplicito delle risorse trattenute.

Per la TUI, una persona guida i prompt:

```text
CLI
→ Kubernetes (provisioned)
→ environment Multipass
→ Run
→ Cleanup
```

Verifica che la preview mostri i 14 task e il dashboard riceva eventi reali
Sonata per acquire, bootstrap, Helm, function e release.

- [x] **Step 4: aggiornare il ledger di validazione**

Nella spec registra:

- SHA Sonata su main;
- conteggi test e coverage;
- esiti live;
- eventuali difetti preesistenti distinti dai difetti dell'incremento.

Marca i checkbox di questo piano soltanto con evidenza eseguita.

- [ ] **Step 5: commit finale e review**

Il commit documentale viene creato dopo i gate statici e live. La review finale del
diff completo `9878efe...HEAD` resta l'unico gate pendente e questo checkbox non può
essere marcato finché non è conclusa.

```bash
git status --short
git add .github/workflows/ci.yml docs/superpowers/specs/2026-07-26-sonata-vm-helm-cli-design.md docs/superpowers/plans/2026-07-26-sonata-vm-helm-cli.md
git commit -m "test: validate provisioned CLI workflow"
```

Poi esegui `superpowers:requesting-code-review` sul diff completo
`9878efe...HEAD`. Non creare la PR nanolab finché:

- Sonata è su `origin/main`;
- il pin è un full SHA di `origin/main`;
- suite/static/import/build sono verdi;
- la validazione live Cleanup e Keep è documentata;
- CLI e TUI costruiscono la stessa topologia provisioned.
