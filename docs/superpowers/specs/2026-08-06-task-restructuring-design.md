# Ristrutturazione dei task in nanolab — proposta di design

Stato: **proposta in revisione**. Non implementare finché non è approvata.
Data: 2026-08-06 · revisione 2 (forma a sottoclassi sottili)

## 1. Scopo

Ristrutturare i task **dentro nanolab**, così che:

- definire un workflow sia composizione, non scrittura di meccanismi;
- creare un task nuovo costi la sua forma di parametri e nient'altro;
- il riuso fra workflow resti esplicito e nominato.

Il trasloco verso il repo `sonata` è una fase successiva. Questa ristrutturazione
lo precede di proposito: si sposta una boundary pulita, non la si pulisce
spostandola.

**Cosa questo design NON promette.** Il numero di classi resta sostanzialmente
invariato (42 oggi, ~44 dopo). Il guadagno non è meno classi: è meno
duplicazione, meno difetti misurati, e un confine di package che significa
qualcosa. Chi cerca un calo di conteggio in questo documento non lo troverà.

## 2. Diagnosi misurata

Stato attuale: **42 classi di task** nei tre package (`nanolab`, `sonata-tasks`,
`workflow-tasks`).

### 2.1 Ventisette classi contengono solo un `__init__`

27 delle 42 (**~1010 LOC**) definiscono **solo `__init__`**: calcolano un argv e
chiamano `super().__init__`. Nessun `run()`, nessun campo, nessun comportamento.

```
CliFunctionApplyTask  CliFunctionDeleteTask  CliFunctionInvokeTask
ContainerResourceCheckTask  CosignTask  DeployDockerCompose  DestroyDockerCompose
DockerBuildTask  DockerInspectTask  DockerPushTask  DockerTask  GradleTask
HelmInstallTask  HelmUninstallTask  HttpFunctionDeleteTask  HttpFunctionInvokeTask
HttpFunctionRegisterTask  HttpStatusCheckTask  ImagetoolsCreateTask
ImagetoolsInspectTask  K8sResourceCheckTask  KubectlTask  PrometheusScrapeCheckTask
SkopeoCopyTask  SkopeoInspectTask  SyftTask  WaitForDockerCompose
```

Questo di per sé **non è un difetto**: una sottoclasse che fissa un argv è una
forma legittima di unità nominata. Il difetto è cosa fa ciascuna nella propria
firma, misurato sotto.

### 2.2 Il difetto vero: le capacità irraggiungibili

`CommandTask` espone 8 capacità. Ogni sottoclasse **ridichiara a mano** un
sottoinsieme dei parametri passthrough nella propria firma, invece di inoltrarli.
Risultato:

| capacità di `CommandTask` | wrapper su 27 che la rendono irraggiungibile |
|---|---|
| `remote_dir` | **27** |
| `timeout_seconds` | **27** |
| `expected_exit_codes` | 26 |
| `env` | 24 |
| `verify` | 15 |
| `title` | 13 |
| `role` | 5 |
| `cwd` | 0 |

`CommandTask` ha acquisito `timeout_seconds`, `remote_dir` e
`expected_exit_codes` che **nessuna delle 27 sottoclassi può raggiungere**. Chi
vuole un timeout su `helm upgrade` deve scendere a `CommandTask` grezzo e
riscrivere l'argv a mano — cioè rinunciare al wrapper che esisteva per non farlo.

Definizione operativa di *fragile base class*: aggiungere una capacità alla base
richiede 27 modifiche, e finché non si fanno la capacità è invisibile.

Aggravante: `CommandTask` è un `@dataclass` e le sottoclassi ne sovrascrivono
`__init__`, disattivando silenziosamente quello generato. Un campo nuovo sul
dataclass non raggiunge nessuna sottoclasse, **senza errore**.

### 2.3 Otto classi ripetono lo stesso `run()`

I task del pipeline loadtest hanno tutti una forma sola:

```python
def run(self, inputs):
    outcome = load_outcome(inputs, self.title)   # leggi l'upstream, tipizzato
    ...usa un adapter iniettato...
    return TaskOutcome(value=replace(outcome, campo=...))
```

`WriteReportTask`, `WriteSummaryTask`, `EvaluateGateTask`,
`VerifyAutoscalingTask`, `FetchResultsTask`, `EvaluateConservationTask`, più
`AggregateBenchmarks` e `EvaluateRegressionGate` su altri tipi. È una `map` sul
valore a monte, con il meccanismo riscritto otto volte.

Anche qui: le otto unità sono legittime. È il **meccanismo** a essere duplicato.

### 2.4 Quattordici factory senza tipi

Sopra `ReleasePhaseTask` ci sono 14 funzioni `def x_task(**kwargs: Any)` che
impostano solo `phase` e `title`. Il `**kwargs: Any` cancella il type checking
su ogni chiamata delle 13 fasi di release.

### 2.5 Gerarchie profonde per comporre stringhe

`ContainerResourceCheckTask` → `DockerInspectTask` → `DockerTask` →
`CommandTask`: quattro livelli, il cui unico effetto cumulativo è anteporre
`docker` e `inspect` a un argv.

### 2.6 Il costo di threading

`executor=` compare **106** volte nei call site, `role=` **140**.

### 2.7 Due `CommandTask` e due runner

Esistono due classi di nome `CommandTask`: quella Sonata
(`sonata_tasks/command.py`) e una legacy (`workflow_tasks/tasks/command_task.py`).
La seconda è viva: `nanolab images` gira interamente sul runner legacy
`workflow_tasks.core.workflow.Workflow` via `plans/_assembly.workflow_from_specs`,
senza mai toccare `sonata_engine`.

Fuori scopo per questa fase, ma va registrato: finché esistono due runner, ogni
regola scritta qui vale per uno solo dei due.

## 3. Il criterio di decisione

La divisione non è "classe o funzione". È **meccanismo contro identità**.

**Il tipo generico possiede il meccanismo**: `run()`, il contratto verso
l'engine, la validazione dell'outcome, il fingerprint. Esiste una volta sola.

**La sottoclasse nominata possiede l'identità e la forma dei parametri**: come si
chiama l'operazione, quali argomenti ha di suo, che titolo si dà. Esiste una per
operazione, ed è l'unità che i workflow importano, riusano e testano.

Da qui tre regole vincolanti:

1. **Una sottoclasse non ridichiara mai i parametri passthrough.** Li inoltra in
   blocco (`**opts: Unpack[CommandOptions]`). Ridichiararli è ciò che ha prodotto
   la tabella §2.2, ed è l'unica cosa che questa ristrutturazione vieta.
2. **Una sottoclasse non ridefinisce mai `run()`** se il meccanismo è già quello
   del tipo generico. Se lo ridefinisce, allora non era una sottoclasse nominata:
   era un task bespoke, e va in §5.4.
3. **La profondità massima è 2** (tipo generico → sottoclasse nominata). Un
   livello intermedio è ammesso solo se ha parametri propri, non solo un prefisso
   di argv.

**Corollario: nessuna unità nominata viene cancellata.** `WriteReportTask`,
`GradleTask`, `HelmInstallTask` restano classi con lo stesso nome. Cambia che
ereditano da un tipo che possiede il meccanismo, e che inoltrano invece di
ridichiarare.

**Un composite è la forma giusta** quando l'unità riusata è una *sequenza*:
`add_platform`, `registry_push_composite`, `loadtest_composite`. Resta funzione.

### 3.1 Perché il tipo concreto conta: il fingerprint di resume

`CompiledWorkflow._fingerprint` (`core/compiled.py:63-71`) include, per ogni
task compilato, `f"{type(task).__module__}.{type(task).__qualname__}"`. Il tipo
concreto **entra nel fingerprint di resume**: mantenere sottoclassi distinte
mantiene distinta la topologia.

Va però detto che oggi è una discriminazione **accidentale e debole**:
`_fingerprint_payload()` restituisce `None` per tutte e 42 le classi, quindi
**l'argv non è nel fingerprint**. `GradleTask("build")` → `GradleTask("test")`
con un `title=` esplicito fisso non cambia il fingerprint nemmeno adesso.

Il design la rende esplicita e più forte:

```python
class CommandTask(Task[TaskResult]):
    def _fingerprint_payload(self) -> object:
        return self.argv if not callable(self.argv) else _qualname(self.argv)

class FnTask(Task[T]):
    def _fingerprint_payload(self) -> object:
        return _qualname(self.fn)
```

Il resume discrimina su **cosa il task fa**, non solo sul nome della classe che
lo avvolge. È un miglioramento indipendente dal resto, e il più economico del
documento.

## 4. Architettura target e confine di package

```
PACKAGE  workflow-tasks          — meccanismi ed esecuzione
│
├── L1  TIPI GENERICI                                        3 classi
│       CommandTask · FnTask[T] · PhaseTask
│       CommandOptions (TypedDict)                       ← punto di dichiarazione unico
│
└──     esecuzione già presente, invariata
        CommandTaskExecutor · RoleBindings · ExecutionRole · CommandTaskSpec · TaskResult


PACKAGE  sonata-tasks            — implementazioni nominate
│
├── L2a  sottoclassi di CommandTask, per modulo-tool             27
├── L2b  sottoclassi di FnTask                                    8
├── L2c  fasi di PhaseTask                                       13
└── L1b  bespoke, con run() proprio                               4
          RunK6Task · CapturePrometheusTask
          ClusterIpEndpointTask · FileTransferTask


PACKAGE  nanolab                 — composizione
│
├── L3   composite riusabili                                      6
└── L4   plan, un Workflow per scenario                           6
```

Il confine risponde alla richiesta di separare i tipi dalle implementazioni, e ha
un secondo effetto: **dà finalmente un significato alla divisione fra i due
package**, che finora era storica. `workflow-tasks` = meccanismi; `sonata-tasks`
= implementazioni nominate.

Prepara anche la fase 2: `workflow-tasks` è per intero materiale da repo
`sonata`; `sonata-tasks` si dividerà lì per l'asse strumento-contro-nanoFaaS.

Conseguenza pratica: `CommandTask` si sposta da `sonata_tasks/command.py` a
`workflow_tasks`. La direzione della dipendenza è già quella giusta
(`sonata-tasks` dipende da `workflow-tasks`), quindi non serve alcuna inversione.

## 5. I tipi generici

### 5.1 `CommandTask` — spostata, più `_fingerprint_payload`

Invariata nel comportamento. Due modifiche: si sposta in `workflow-tasks`, e
acquisisce `_fingerprint_payload` (§3.1).

### 5.2 `FnTask[T]` — nuova, ~18 righe

```python
@dataclass
class FnTask(Task[T]):
    """Un task il cui corpo è una callable, con l'upstream come ingresso."""
    title: str
    fn: Callable[[TaskInputs], T]

    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        return TaskOutcome(value=self.fn(inputs))

    def _fingerprint_payload(self) -> object:
        return _qualname(self.fn)
```

Le otto unità di §2.3 diventano sottoclassi sottili, con `run()` scritto **una
volta sola** nel tipo:

```python
class WriteReportTask(FnTask[LoadtestOutcome]):
    def __init__(self, *, report: WriteK6Report, title: str = "Write the report") -> None:
        super().__init__(
            title=title,
            fn=lambda i: replace(load_outcome(i, title), report=report.run()),
        )
```

Motivazione SE: l'engine non ha alcun task che avvolga una callable — ha
`Resource` con acquire/release callable, non l'equivalente per un task. È un buco
reale, non un doppione.

### 5.3 `PhaseTask` — generalizzazione di `ReleasePhaseTask`

```python
@dataclass(frozen=True)
class PhaseTask(ReusableTask):
    phase: str
    run_dir: Path
    scope: Mapping[str, Any]            # era `identity`: cosa distingue l'esecuzione
    inputs: Mapping[str, Any]           # cosa entra nel reuse_key
    work: Callable[[TaskInputs], Iterable[Evidence]]
    prerequisites: tuple[Path, ...] = ()
    verify: Callable[[tuple[Evidence, ...]], None] | None = None   # era expected_images
    title: str = ""
```

Tre cambi rispetto a `ReleasePhaseTask`:

1. `identity: ReleaseIdentity` → `scope: Mapping` — toglie l'unica dipendenza di
   dominio senza perdere nulla: `identity.as_entry()` è già un mapping.
2. `expected_images` → hook `verify` — il controllo matrice immagini diventa una
   funzione in nanolab, e il tipo smette di sapere cosa sia un'immagine.
3. `versioned_release_run_dir` esce dal `__post_init__`: lo applica il chiamante.
   Un task non deve riscrivere un proprio campo.

`_AttestImageTask` diventa una sottoclasse nominata di `PhaseTask`.

**Le 13 fasi di release restano funzioni tipizzate, non sottoclassi** — ed è
l'unica eccezione alla forma scelta, con una ragione: le 13 non hanno una forma
di parametri propria, differiscono solo per due costanti (`phase`, `title`). Una
sottoclasse il cui intero contenuto sono due stringhe è più cerimonia che
identità. La regola generale resta "sottoclasse quando l'unità ha parametri
propri"; queste non ne hanno.

Effetto collaterale che vale da solo: oggi `phase_inputs` è derivato a mano e in
modo diverso per ogni fase (source-tests include `argv/env/remote_dir/cwd/timeout`,
amd64 solo `argv/role/remote_dir`). Siccome alimenta `reuse_key`, una derivazione
incompleta può far saltare al resume una fase che è cambiata. Una derivazione
unica da `CommandTaskSpec` chiude la classe di bug.

### 5.4 Le quattro classi bespoke

Hanno `run()` proprio, quindi per la regola 2 del §3 non sono sottoclassi
nominate ma task a sé:

| classe | perché |
|---|---|
| `RunK6Task` | supervisiona il watcher di repliche intorno al run |
| `CapturePrometheusTask` | I/O HTTP proprio con finestra temporale |
| `ClusterIpEndpointTask` | esegue e parsa, produce `str` non `TaskResult` |
| `FileTransferTask` | I/O su filesystem, non un comando |

(`FetchVmResults` e `RunPlaybook`, che una stesura precedente elencava qui, non
sono `Task` di Sonata ma adapter semplici: restano sotto L1, invariati.)

## 6. Le sottoclassi nominate di `CommandTask`

Il punto di dichiarazione unico dei passthrough (PEP 692, su 3.12):

```python
class CommandOptions(TypedDict, total=False):
    role: ExecutionRole
    cwd: Path | None
    env: Mapping[str, str]
    remote_dir: str | None
    expected_exit_codes: frozenset[int]
    timeout_seconds: int | None
    verify: Callable[[TaskResult], None] | None
    title: str
```

Ogni sottoclasse dichiara **solo i parametri propri del tool** e inoltra il resto:

```python
class GradleTask(CommandTask):
    """Esegue target Gradle attraverso il wrapper del repository."""

    def __init__(
        self,
        *targets: str,
        executor: CommandTaskExecutor,
        properties: Mapping[str, str] | None = None,
        **opts: Unpack[CommandOptions],
    ) -> None:
        if not targets:
            raise ValueError("a Gradle task needs at least one target")
        props = tuple(f"-P{k}={v}" for k, v in (properties or {}).items())
        opts.setdefault("title", f"Run gradle {' '.join(targets)}")
        super().__init__(
            argv=("./gradlew", *targets, *props, "--no-daemon"),
            executor=executor,
            **opts,
        )
```

Conseguenze dirette sulla diagnosi §2.2:

- le 8 capacità diventano raggiungibili da tutte e 27 le operazioni;
- aggiungerne una nona costa **due righe** (campo su `CommandTask` + chiave su
  `CommandOptions`) e arriva ovunque, type-checked;
- la profondità §2.5 scende da 4 a 2: `ContainerResourceCheckTask` eredita
  direttamente da `CommandTask` e compone il proprio argv per intero.

Le 27 classi restano nei moduli per tool che già esistono (`docker.py`,
`helm.py`, `kubectl.py`, `skopeo.py`, …) e mantengono i nomi attuali: nessun
call site cambia forma.

## 7. I composite riusabili

Unità di riuso a livello di *sequenza*, tutte già esistenti e invariate:

| composite | usato da |
|---|---|
| `add_platform` | validate, loadtest, offload-loadtest |
| `loadtest_composite` | loadtest, offload-loadtest |
| `build_loadgen_body_tasks` | loadtest × 3 provider |
| `command_specs_composite` | release: source-tests, amd64, arm64 |
| `registry_push_composite` | release |
| `attest_composite` | release |

## 8. Come si compongono i workflow

```python
def build_X_plan(config, bindings, *, repo_root) -> Workflow:
    executor = RoleBoundCommandTaskExecutor(bindings)
    wf = Workflow(workflow_id="X")
    vm = vm_resource(...)                                   # risorse
    platform = add_platform(wf, request, executor=executor, requires=(vm,))
    wf.add(
        Steps(title="...", steps=(
            GradleTask("build", executor=executor, role="stack"),
            DockerBuildTask(image=..., executor=executor, role="stack", timeout_seconds=600),
            WriteReportTask(report=report_adapter),
        )),
        requires=(*platform.resources, *platform.functions),
    )
    return wf
```

Il `timeout_seconds=600` sulla seconda riga è il punto: oggi non si può scrivere.

I sei workflow esistenti:

| workflow | spina dorsale | passi propri |
|---|---|---|
| **validate** | `add_platform` | `HttpFunctionInvokeTask`, `ContainerResourceCheckTask` in uno `Steps` |
| **cli** | `add_platform` + risorse VM/Helm | `CliFunctionApplyTask` / `InvokeTask` / `DeleteTask` |
| **loadtest** | `add_platform` + `loadtest_composite` | `RunK6Task` (bespoke) poi 4 sottoclassi di `FnTask` |
| **offload** | risorse edge/cloud (processi gestiti) | invocazioni via sottoclassi di `CommandTask` |
| **offload-loadtest** | 2× `add_platform` + `loadtest_composite` | `EvaluateConservationTask` |
| **release** | 13 fasi tipizzate su `PhaseTask` | ogni fase: `command_specs_composite` + `work=` |

Per **release**, la fase tipo:

```python
amd64 = phase(
    "amd64-build",
    run_dir=release_dir,
    scope=identity.as_entry(),
    inputs=phase_inputs_from(amd64_commands),          # derivazione unica
    prerequisites=(source_tests.receipt,),
    verify=image_matrix(release_images),
    work=lambda i: run_image_steps(amd64_steps, i, executor, release_images,
                                   registry=False, architecture="amd64"),
)
```

## 9. La decisione aperta: `executor` e `role`

`Unpack[CommandOptions]` toglie il threading di 6 parametri su 8, ma `executor`
(106 occorrenze) e `role` (140) restano espliciti a ogni chiamata.

**Raccomandazione: lasciarli espliciti in questa fase.** `role` cambia davvero
per task (host vs stack vs loadgen) ed è l'informazione che più spesso spiega un
fallimento. `executor` è quasi costante per workflow, ma legarlo a un contextvar
introduce una dipendenza nascosta in un codebase che ne ha già due (il sink
eventi, in doppia copia).

Se dopo la ristrutturazione il rumore resta fastidioso, un `CommandContext`
esplicito passato una volta è un cambio additivo, più facile a firme già
semplificate.

## 10. Verifica

- I test esistenti per le 27 sottoclassi verificano l'argv prodotto: restano
  validi **senza modifiche**, perché nomi e firme proprie non cambiano. È il
  vantaggio principale della forma a sottoclassi.
- **Test nuovo, oggi non scrivibile**: un test parametrico sulle 27 sottoclassi
  che passa `timeout_seconds` e `remote_dir` e verifica che arrivino nella
  `CommandTaskSpec`. È l'asserzione che la tabella §2.2 non torni a riempirsi, e
  va scritto **prima** della migrazione, così fallisce 27 volte e poi passa.
- **Test di profondità**: `len(type(t).__mro__) <= 4` per ogni sottoclasse
  nominata, a presidio della regola 3 del §3.
- `FnTask`: test che le 8 sottoclassi non ridefiniscano `run` — `"run" not in
  vars(cls)`, che è la regola 2 del §3 resa eseguibile.
- `PhaseTask`: test che `reuse_key` cambi al variare di ogni campo di `inputs`.
- Fingerprint: test che due `CommandTask` con argv diversi e stesso titolo
  producano fingerprint diversi — oggi non è vero (§3.1).
- `import-linter`: il contratto guadagna una direzione da verificare,
  `sonata_tasks` → `workflow_tasks` e mai il contrario.
- Copertura: entrambi i package sono a `--cov-fail-under=90`.

## 11. Lavoro prerequisito

Questo design non è eseguibile così com'è. Un prerequisito è un blocco duro, gli
altri sono debito che conviene saldare prima.

### P1 — Portare `nanolab images` su `sonata_engine.Workflow` — **bloccante**

> Tracciata in [#23](https://github.com/miciav/nanolab/issues/23).

Il design sposta `CommandTask` da `sonata_tasks/command.py` a `workflow-tasks`
(§4). Ma **in `workflow-tasks` esiste già una classe di nome `CommandTask`**
(`tasks/command_task.py`), esportata dal suo `__init__`. Le due non possono
coabitare nello stesso package: o si rinomina una delle due — cioè si tocca ogni
call site per poi ritoccarlo dopo — o si elimina la legacy prima di iniziare.

Eliminare la legacy significa portare il suo unico consumatore vivo. Perimetro
misurato:

| cosa | dimensione |
|---|---|
| `cli/images.py` → `build_image_workflow` | ~110 righe, muta `workflow.tasks` in place |
| 3 task legacy in `images.py` | `ImageArchiveTransportTask`, `_BakeFileStageTask`, `_RemoteFileCleanupTask` |
| `plans/_assembly.workflow_from_specs` | 28 righe — sparisce |
| `workflow_tasks/core/` + `tasks/command_task.py` | 131 righe — spariscono |
| test da riscrivere | 5 file |

Il lavoro è contenuto perché il ponte esiste già: `command_specs_composite`
(`sonata_tasks/release_composites.py:51`) fa esattamente
`CommandTaskSpec[] → Steps` di `CommandTask` Sonata, ed è già testato. Restano da
convertire i 3 task bespoke di `images.py` e da sostituire la mutazione di
`workflow.tasks` con `workflow.add()`.

Guadagno indipendente da questo progetto: sparisce il secondo runner, quindi
`nanolab images` acquisisce journal, resume e selection, che oggi non ha.

### P2 — Unificare i due contextvar degli eventi — **non bloccante, consigliato**

`workflow_tasks/workflow/` è un fork di `sonata_engine/workflow/`: `events.py` e
`models.py` sono byte-identici, gli altri divergono per accidenti storici. Le due
copie usano **contextvar distinti** (`workflow_tasks_sink` e
`sonata_engine_sink`) e **vocabolari distinti** (`task.running`/`task.completed`
contro `task.started`/`task.passed`), quindi `cli/product.py:522` e
`tui/workflow_controller.py:68` fanno doppio bind e il TUI gestisce due dialetti.

Non blocca questo progetto, ma P1 lo rende quasi gratuito: quando `nanolab
images` smette di usare il runner legacy, sparisce l'unico emettitore del
dialetto `workflow_tasks`, e l'unificazione diventa la cancellazione di un fork
senza produttori.

Da notare: `phase()`, `step()`, `success()`, `warning()`, `skip()`, `fail()` e
`build_phase_event` hanno **zero chiamanti**. Il TUI gestisce `phase.started`
(`tui/event_aggregator.py:104`) senza che nessuno lo emetta.

### P3 — Decidere il destino di `components/` — **da valutare**

`workflow_tasks/components/` produce `ScenarioOperation`, consumato dal percorso
legacy. Dopo P1 va verificato se resta un produttore o se anche quello strato
diventa senza consumatori. Non misurato in questo documento.

### Ordine

```
P1  (bloccante)  →  P2  (facilitato da P1)  →  questo design  →  fase 2: trasloco in sonata
                    P3  (da valutare dopo P1)
```

## 12. Cosa questa fase NON fa

- Non riduce il numero di classi (42 → ~44). Vedi §1.
- Non sposta niente verso il repo `sonata`.
- Non tocca la fork degli eventi né il doppio contextvar.
- **Non elimina il runner legacy.** `workflow_tasks/core/` è vivo: `nanolab
  images` ci gira sopra per intero (§2.7). Portarlo su `sonata_engine.Workflow` è
  un lavoro a sé, e finché non è fatto convivono due runner e due `CommandTask`.
- Non introduce un DSL dichiarativo: i plan restano Python.
