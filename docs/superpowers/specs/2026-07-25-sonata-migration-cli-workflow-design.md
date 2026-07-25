# Migrazione a sonata-engine — contratto e primo incremento (workflow `cli`)

**Data:** 2026-07-25
**Stato:** design approvato, piano da scrivere

## Obiettivo

Sostituire `workflow-tasks` con `sonata-engine` più una nuova libreria locale
`sonata-tasks`. La migrazione procede **un workflow alla volta**, in tre passi per
incremento:

1. reimplementare il workflow su sonata dentro `sonata-tasks`;
2. testarlo;
3. quando funziona, cancellare l'originale e far puntare nanolab al nuovo.

Questo documento fissa il contratto valido per **tutti** gli incrementi e specifica in
dettaglio il **primo**: il workflow `cli`.

## Perché `sonata-tasks` esiste

Sonata è dichiaratamente product-independent: non prende remote execution, VM provider,
Ansible, load-test. Quello che oggi sta in `workflow_tasks` si divide quindi in due:

| Oggi in `workflow_tasks` | Destinazione |
|---|---|
| `core/workflow.py`, `core/task.py`, `core/resource_task.py`, `workflow/*` | `sonata-engine` (già esiste) |
| `workflows/*`, `tasks/*`, `execution/*`, `vm/*`, `loadtest/*`, `components/*`, `infra/*`, `shell` | `sonata-tasks` |

Stato finale: `workflow-tasks` sparisce. Il layer exec/vm/loadtest trasloca **in un
incremento finale a sé**, non a ogni workflow: non è un workflow, è infrastruttura, e si
sposta una volta sola. Fino ad allora `sonata-tasks` dipende da `workflow-tasks` per
l'esecuzione dei comandi.

## Contratto di migrazione (vale per ogni incremento)

### C1 — L'identità del task appartiene al compiler

Nessun task dichiara il proprio ID. `Workflow.compile()` assegna
`{ordinale:03d}.{slug}` derivato dal titolo. Non si introduce una seconda identità
accanto a `task_id`: niente `operation_id`, niente alias, niente campo stabile
"per lo slicing".

### C2 — Il cleanup è una `Resource`, non una lista separata

`workflow.cleanup_tasks` non esiste in sonata. Ogni coppia crea/distruggi diventa
`Resource(acquire=..., release=...)`; il compiler inserisce l'acquire prima del primo
consumer e il release dopo l'ultimo, ed esegue i release in ordine inverso anche in caso
di fallimento.

### C3 — Lo slicing è dell'engine, non del prodotto

Sonata è in fase iniziale ed è nostro: quando una capacità appartiene per natura
all'orchestratore, si aggiunge a sonata invece di aggirarla a valle. Lo slicing è il
primo caso.

Selezionare un sottoinsieme di un workflow interagisce con lo splicing di
acquire/release, che **solo il compiler** sa fare. Un slicer esterno ha due strade, e
sono entrambe cattive: filtrare dopo il compile (butta via le unità di risorsa e rompe
il cleanup) oppure filtrare prima, il che costringe ogni consumer a esporre i workflow
come liste di definizioni invece di usare l'API fluente `add()`. La seconda funziona ma
è l'engine che spinge fuori un problema proprio.

Quindi la selezione entra in sonata:

```python
@dataclass(frozen=True, slots=True)
class Selection:
    only: str | None = None
    start: str | None = None
    until: str | None = None

workflow.run(select=Selection(only="list-functions"))
workflow.compile(select=...)   # stesso filtro, per il dry-run
```

Regole:

- la selezione avviene per **slug** del titolo (`list-functions`), risolto dall'engine;
- si selezionano solo i task consumer. Le unità acquire/release sono engine-owned e
  seguono i consumer superstiti: il compiler le ri-splicia da sé, quindi il cleanup
  resta corretto senza logica dedicata;
- slug sconosciuto → errore esplicito;
- gli ordinali si rinumerano sui superstiti (`001`, `002`, …). Un run sliciato è quindi
  una topologia diversa e il fingerprint fa fallire un `resume` che lo attraversi: è il
  comportamento giusto, fail-closed.

I plan builder restituiscono un `Workflow` normale, costruito con `add()`.

Conseguenza sulla CLI di nanolab: la selezione passa da `--only cli.function.list` a
`--only list-functions`. Cambio di sintassi utente, va documentato.

L'hack `acquired_by_selection` in `nanolab/cli/product.py::_slice` — quello che rimappa
`.delete.` → `.apply.` per preservare il cleanup — **non** si cancella nel primo
incremento: serve ancora ai workflow non migrati. Muore da solo quando migra l'ultimo.

### C4 — Convivenza breve, mai permanente

Non si aggiungono scenari paralleli (`cli-sonata.yaml`), comandi paralleli
(`nanolab sonata run`) né adapter che facciano sembrare un `Workflow` sonata un
`Workflow` di workflow-tasks. L'originale e la reimplementazione coesistono al massimo
per la durata di un branch. `nanolab run scenarios-v2/cli.yaml` esegue la versione
sonata a fine incremento.

Durante la migrazione `nanolab/cli/product.py` ha necessariamente due rami: uno per i
workflow migrati, uno per i legacy. È costo inerente a una migrazione incrementale, non
impalcatura. Il ramo legacy si cancella con l'ultimo workflow.

### C5 — Riuso del layer di esecuzione

`CommandTaskSpec`, `HostCommandTaskExecutor`, `VmCommandTaskExecutor`, `RoleBindings`
si riusano da `workflow-tasks`. Non si reimplementano. `CommandTaskSpec.task_id` si
valorizza a stringa vuota: l'identità è del compiler (C1), quel campo serve solo a
popolare `TaskResult.task_id`.

Questo **non** è in tensione con C3: sonata per statuto non prende remote execution né
VM provider, quindi l'esecuzione di comandi resta a valle. C3 riguarda le capacità di
orchestrazione — topologia, risorse, selezione, journal — che sono di sonata.

### C6 — Sonata si modifica, non si aggira

Ogni volta che un incremento richiede una capacità di orchestrazione mancante, la si
aggiunge a sonata con i suoi test, invece di forzarla in nanolab. Durante lo sviluppo
coordinato `sonata-tasks` punta a sonata via source locale uv (`../sonata`); prima del
merge si pinna un commit immutabile.

Il boundary resta quello di sonata: nessun import di `nanofaas`, `nanolab`,
`workflow_tasks`, provider VM, Ansible o load-test — c'è già un test che lo verifica
(`tests/test_package_boundaries.py`).

## Incremento 1 — workflow `cli`

Scelto perché è il più piccolo (127 righe) e non tocca VM o risorse cloud: build della
CLI, apply della function, list, invoke, delete.

L'incremento tocca **due repository**: prima sonata (la selezione), poi nanolab.

### Lavoro su sonata: `Selection`

Checkout locale in `~/Downloads/sonata`, pulito su `f13c830`.

Si implementa `Selection` e i parametri `select=` su `compile()` e `run()`, come da C3.
È l'unica capacità che l'incremento 1 richiede a sonata. Test: selezione per slug,
slug sconosciuto, `start`/`until`, e soprattutto che una risorsa i cui consumer sono
stati parzialmente sliciati venga comunque acquisita e rilasciata attorno a quelli
superstiti.

### Nuovo package `packages/sonata-tasks`

Membro del workspace uv. Distribuzione `sonata-tasks`, import `sonata_tasks`.

Dipendenze:
- `sonata-engine` — durante lo sviluppo via source locale uv su `../sonata` (C6);
  pinnata a un commit immutabile prima del merge;
- `workflow-tasks` (workspace) — solo per il layer di esecuzione, per C5.

```
packages/sonata-tasks/
  pyproject.toml
  src/sonata_tasks/
    __init__.py
    command.py      # CommandTask: base sonata che esegue uno spec via executor
    cli.py          # CliFunction, CliWorkflowRequest, i task, build_cli_workflow()
  tests/
    test_cli.py
```

`command.py` — `CommandTask(Task[TaskResult])`: costruisce lo `CommandTaskSpec`, chiama
l'executor, alza `RuntimeError` se `status != "passed"`, ritorna
`TaskOutcome(value=result)`.

`cli.py` — ridefinisce `CliFunction` e `CliWorkflowRequest` come dataclass locali invece
di importarle da `workflow_tasks.workflows.cli`: quel modulo viene cancellato a fine
incremento, e la dipendenza verso workflow-tasks deve restare confinata al layer
esecuzione (C5).

### Cosa migliora nel porting

Non è un travaso. Il modello sonata elimina il quoting bash che oggi serve a compensare
l'assenza di task veri:

| Oggi | Con sonata |
|---|---|
| `cli_cleanup_specs()` in una lista `cleanup_tasks` separata | `Resource(acquire=apply, release=delete)`, splice automatico (C2) |
| invoke verificato con `bash -lc "... \| grep -q '\"status\":\"success\"'"` | `InvokeFunction.run()` parsa il JSON in Python, ritorna `TaskOutcome[str]` |
| manifest scritto con `mktemp` + `trap` dentro una stringa bash | il task scrive il file temporaneo in Python |
| `task_id` scritti a mano (`cli.function.apply.X`) | ID generati dal compiler |

Workflow compilato atteso, con una function:

```
001.build-nanofaas-cli
002.apply-word-stats-java      <- Resource.acquire, inserito dal compiler
003.list-functions             requires=(fn,)
004.invoke-word-stats-java     requires=(fn,)
005.delete-word-stats-java     <- Resource.release, inserito dal compiler
```

### Wiring in nanolab

- `nanolab/plans/cli.py` — riscritto: `build_cli_workflow(...)` restituisce un
  `Workflow` sonata costruito con `add()`.
- `nanolab/cli/product.py` — `_workflow()` acquisisce un ramo per i workflow migrati che
  passa `select=Selection(only=..., start=..., until=...)` a `run()` / `compile()`. Il
  ramo legacy, `_slice` compreso, resta invariato.
- `nanolab/cli/progress.py` — `ConsoleProgressSink.emit` filtra oggi su
  `task.running`/`task.completed`; sonata emette `task.started`/`task.passed`. Si
  estendono i due set nel sink esistente (~3 righe) invece di scrivere un secondo sink:
  è un renderer di console, non gli importa quale engine emette. `WorkflowEvent` di
  sonata e di workflow_tasks sono strutturalmente identici, il duck typing regge; va
  allargata solo l'annotazione di tipo.
- `product.py` lega il sink via `workflow_tasks.workflow.context.bind_workflow_sink`.
  Sonata ha la propria contextvar: nel ramo migrato va legato **anche**
  `sonata_engine.workflow.context.bind_workflow_sink`. Durante la migrazione convivono
  entrambi; alla fine resta solo quello di sonata.
- Nessuna modifica a `ScenarioConfig`, nessun nuovo scenario: si continua a leggere
  `scenarios-v2/cli.yaml`.

### Cancellazioni a fine incremento (step 3)

- `packages/workflow-tasks/src/workflow_tasks/workflows/cli.py`
- `packages/workflow-tasks/tests/workflows/test_cli.py`
- il vecchio corpo di `nanolab/plans/cli.py` (il file resta, riscritto)
- `packages/nanolab/tests/plans/test_cli.py` — riscritto contro il nuovo builder

### Vincolo: Python 3.12

`sonata-engine` richiede `>=3.12`; `nanolab` e `workflow-tasks` dichiarano `>=3.11`.
L'ambiente gira già su 3.12, ma uv rifiuterà la risoluzione. Va alzato
`requires-python` a `>=3.12` su `packages/nanolab/pyproject.toml` e sul workspace root.
È una modifica fuori dallo scope del porting, va fatta consapevolmente.

### Verifica

- `uv run pytest && uv run ruff check . && uv run basedpyright` in `~/Downloads/sonata`
  — la suite di sonata verde, `Selection` inclusa.
- `uv run --project packages/sonata-tasks pytest` — executor finto, niente Docker né
  k8s. Assert su: ID compilati e loro ordine; apply spliciato prima di `list-functions`
  e delete dopo l'ultimo invoke; delete eseguito anche quando invoke fallisce; invoke
  che fallisce su `{"status":"error"}`; slicing per slug che preserva il delete.
- `uv run --project packages/nanolab pytest` — suite esistente verde.
- `nanolab plan scenarios-v2/cli.yaml` — conferma il wiring senza toccare un cluster.
- `nanolab run scenarios-v2/cli.yaml` contro un control plane reale — validazione finale
  dell'incremento, prima di considerarlo chiuso.

## Gap di sonata già noti, rimandati

Registrati qui perché per C6 vanno colmati **in sonata** quando l'incremento che li
richiede arriva, non aggirati in nanolab sotto scadenza. Nessuno dei due serve al
workflow `cli`, quindi nessuno dei due si tocca ora.

- **Risorse che producono un valore.** `Resource.acquire` è `Callable[[], None]`: una
  risorsa VM non può consegnare il proprio IP ai consumer. Serve a `validate`,
  `loadtest`, `offload`.
- **Passaggio di valori tra task.** `TaskOutcome.value` è documentato come canale
  in-process, ma non esiste un meccanismo perché il task B legga il valore del task A;
  oggi si passa per riferimenti posseduti dall'assemblatore (closure). Se debba
  restare così o diventare una capacità dell'engine è una decisione aperta, da
  prendere quando il primo workflow la richiede davvero — non in astratto.

## Fuori scope

- TUI: `nanolab/tui/workflow_controller.py` consuma eventi workflow_tasks. Finché
  esistono workflow legacy continua a funzionare; si adatta quando serve, non ora.
- Journal e resume di sonata: il workflow `cli` non ne ha bisogno. Si valuteranno sui
  workflow che li giustificano.
- Gli altri quattro workflow (`validate`, `loadtest`, `offload`, `offload-loadtest`):
  un incremento e uno spec ciascuno, che ereditano il contratto C1–C6.
