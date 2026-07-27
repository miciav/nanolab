# VM e Helm come risorse Sonata nel workflow `cli/k8s`

**Data:** 2026-07-26
**Stato:** design approvato
**Baseline nanolab:** `9878efe`
**Baseline sonata:** `0bafb9ab3778b54ca3f08a75c1f3ccd0ed3e017b`
**Sonata validata:** `527d042a83ed55b6cc6334885121241146204fcf`
(`origin/main`, versione `0.2.0`)
**Precede:**
[migrazione del workflow CLI](2026-07-25-sonata-migration-cli-workflow-design.md) e
[bersaglio del workflow CLI](2026-07-26-cli-workflow-target-design.md)

## Obiettivo

Portare il lifecycle della VM e della release Helm dentro il piano Sonata del workflow
`cli` quando lo scenario usa `backend: k8s` e viene richiesto `--provision`.

Il run deve diventare un solo workflow compilato, visibile da CLI e TUI:

```text
build locale
→ acquire VM
→ bootstrap della VM
→ acquire release Helm
→ acquire function
→ verifica CLI
→ release function
→ release Helm
→ release VM
```

Il context manager `provision_environment` resta per i workflow legacy ma non avvolge
più `cli/k8s --provision`.

## Baseline da preservare

Durante la progettazione sono entrati in `main` cambiamenti che fanno parte del
contratto di questo incremento:

- `cli/container` avvia già un control plane locale tramite una `Resource` Sonata;
- il processo locale attende la readiness e viene sempre terminato;
- `cli/k8s` senza provisioning richiede un `--control-plane-url` esplicito;
- build CLI, jar e immagini precedono l'acquisizione di risorse esterne;
- `cli/container` rifiuta `--provision`, ambienti non locali e `--keep`;
- la TUI espone oggi il percorso CLI container.

Questi comportamenti non vengono riaperti. Il nuovo verticale riguarda soltanto
`cli/k8s --provision`.

## Perché Sonata deve cambiare

La VM non è un semplice side effect senza risultato:

```python
VmInfo(name, host, user, home)
```

Il valore effettivo viene scoperto dall'acquire e serve ai task di bootstrap e al
release. Oggi `Resource` accetta solo `() -> None`, mentre `EnsureVmRunning` conserva
il risultato in un campo mutabile letto dopo un workflow separato.

Anche Helm dipende dalla VM. Questa relazione è topologia del workflow, non logica del
prodotto: una slice che conserva Helm deve conservare transitivamente la VM, e i release
devono essere ordinati Helm prima di VM.

Quindi Sonata riceve due capacità generiche:

1. valori runtime tipizzati prodotti dalle `Resource`;
2. dipendenze dichiarate tra `Resource`.

## S1 — `TaskInputs`, non un secondo `WorkflowContext`

Sonata possiede già `WorkflowContext`, usato dal reporting per correlare flow, task ed
eventi. Non viene esteso con stato di esecuzione.

I task ricevono invece un oggetto stretto:

```python
class TaskInputs:
    def resource(self, resource: Resource[T]) -> T: ...


class Task(Generic[T], ABC):
    @abstractmethod
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]: ...
```

Separazione:

| Concetto | Responsabilità |
|---|---|
| `WorkflowContext` | correlazione e reporting |
| `TaskInputs` | valori delle dipendenze dichiarate del task |
| `_RunState` | memoria mutabile privata del runner |
| `WorkflowResult` | risultati osservabili dopo il run |

`TaskInputs` non espone ID compilati, sink, journal, risultati arbitrari o accesso al
runner. Chiedere una risorsa non dichiarata fallisce esplicitamente.

La rottura di firma è deliberata: Sonata è ancora `0.1` e non si introduce uno shim
basato sull'ispezione delle firme.

## S2 — `Resource[T]`

```python
@dataclass(frozen=True, slots=True)
class Resource(Generic[T]):
    title: str
    acquire: Callable[[TaskInputs], T]
    release: Callable[[TaskInputs, T], None]
    requires: tuple[Resource[Any], ...] = ()
    infrastructure: bool = False
    acquire_idempotent: bool = False
```

Il runner:

1. crea un `_RunState` per run;
2. esegue l'acquire con gli input delle risorse dipendenti;
3. valida il `TaskOutcome` del `ResourceOp`;
4. pubblica il valore nel run state;
5. abilita il release;
6. passa al release lo stesso valore prodotto dall'acquire.

La presenza del valore è distinta dal suo contenuto, quindi `Resource[None]` resta
valida. I valori non entrano nel journal e non vengono serializzati. Un resume
riesegue gli acquire e ricostruisce lo stato runtime.

Un acquire fallito non pubblica il valore e non abilita il release. Se può aver
prodotto un effetto parziale, compensa localmente prima di rilanciare, secondo C2.

## S3 — Dipendenze tra risorse

Il compiler espande transitivamente le dipendenze con un ordinamento topologico stabile.

Per:

```text
function requires helm
helm requires vm
invoke requires function
```

la slice `only=invoke-word-stats-java` compila:

```text
Acquire VM
Acquire Helm
Acquire function
Invoke function
Release function
Release Helm
Release VM
```

Un ciclo è un errore di compilazione. L'accesso runtime è più stretto della chiusura
usata per il lifecycle: un consumer vede soltanto le risorse dichiarate direttamente;
l'acquire/release di una Resource vede la propria `requires`.

Il fingerprint include anche le dipendenze compilate, così un resume non attraversa
una modifica del grafo che lasci casualmente invariati titoli e tipi.

Con `keep_infrastructure=True`, trattenere una risorsa trattiene transitivamente anche
le sue dipendenze: il motore non può conservare Helm e distruggere la VM sottostante.

## N1 — Confine tra package

### `sonata-engine`

Contiene soltanto:

- `TaskInputs`;
- `Resource[T]`;
- run state e routing dei valori;
- dipendenze, ciclo, selection, fingerprint e retention transitiva.

Non importa VM, Helm, command executor o nanolab.

### `sonata-tasks`

Contiene:

- `CommandTask` adattato a `TaskInputs`, con argv risolvibili a runtime;
- `managed_process_resource` riscritto come `Resource[Popen]`, senza cella mutabile;
- builder della Resource VM;
- builder generico della Resource Helm;
- acquire function k8s con attesa del rollout.

Continua a riusare modelli, provider ed executor da `workflow-tasks` fino
all'incremento finale del layer infrastrutturale.

### `nanolab`

Decide:

- quale provider VM costruire;
- quali task di bootstrap inserire;
- valori Helm del control plane nanoFaaS;
- scenario, ambiente, CLI e TUI.

## N2 — Piano compilato `cli/k8s --provision`

Con una function:

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

La CLI viene costruita sull'host prima dell'acquire VM. Il sync copia nella VM la
distribuzione già costruita. I comandi CLI successivi girano nella VM, evitando porte
pubbliche, firewall Azure/Proxmox e discovery di URL esterni.

L'endpoint del control plane è interno alla VM. La chart espone il control plane come
NodePort e la CLI usa `http://127.0.0.1:30080`.

La release usa l'immagine di control plane dichiarata dalla chart; questo workflow
verifica la CLI e non ricostruisce il control plane corrente come farebbe `validate`.

## N3 — Readiness della function k8s

Il provider container-local attende già la function, ma k8s no. Nel percorso provisioned
l'acquire della function diventa:

1. `fn apply`;
2. attesa bounded che il Deployment `fn-<name>` compaia;
3. `kubectl rollout status` con timeout.

L'attesa è parte dell'acquire, non un consumer separato: una slice che conserva la
function conserva anche la readiness.

Se apply o readiness falliscono, l'acquire tenta `fn delete` best-effort e rilancia
l'errore primario con l'eventuale errore di cleanup come nota.

Il percorso k8s non provisioned conserva il contratto attuale: URL esplicito e
infrastruttura gestita dall'utente. Senza accesso kubectl dichiarato nanolab non può
promettere la readiness del deployment remoto.

## N4 — CLI

`nanolab run cli.yaml`:

- senza `--provision`: richiede `--control-plane-url`, usa la CLI sull'host e non
  acquisisce VM o Helm;
- con `--provision`: richiede un ambiente non locale, ignora la necessità di un URL
  esplicito, compila VM e Helm nel workflow ed esegue la CLI nella VM.

`nanolab plan` guadagna `--provision` per mostrare la stessa topologia del run.

Solo il percorso legacy continua a usare il context manager `provision_environment`.

## N5 — TUI

La sezione CLI espone due voci:

```text
Container
Kubernetes (provisioned)
```

Il percorso container resta invariato. Quello Kubernetes:

- carica `cli.yaml`;
- richiede un ambiente non locale;
- imposta sempre `provision=True`;
- non mostra “Use existing”, perché la TUI non raccoglie un URL esplicito;
- permette `Cleanup` o `Keep`;
- costruisce preview e run dallo stesso workflow Sonata provisioned.

## N6 — Errori e cleanup

- acquire VM fallito: teardown best-effort usando l'identità richiesta;
- acquire Helm fallito: uninstall best-effort;
- acquire function fallito: delete best-effort;
- consumer fallito: release function, Helm e VM in ordine inverso;
- errore di un release: gli altri release vengono comunque tentati;
- `--keep`: function, Helm e VM restano; la retention transitiva impedisce teardown
  delle dipendenze;
- lifecycle external: ensure e bootstrap avvengono, teardown è un no-op esplicito.

Il `VmInfo` ricostruito dal request si usa soltanto per compensare un acquire che non ha
prodotto il valore. Il normale release usa sempre il `VmInfo` restituito dall'acquire.

## Fuori scope

- migrazione dei workflow legacy;
- spostamento definitivo di provider, Ansible ed executor fuori da `workflow-tasks`;
- un DAG generico task-to-task o consumo arbitrario di `TaskOutcome` consumer;
- parallelismo;
- provisioning del percorso `cli/container`;
- supporto TUI per un cluster k8s esistente con URL inserito a mano;
- build del control plane corrente nel workflow CLI.

## Verifica

### Sonata

- `TaskInputs` espone solo dipendenze dichiarate;
- `Resource[None]` e `Resource[T]`;
- valore identico consegnato a consumer e release;
- dipendenze lineari e diamond;
- ciclo;
- selection con chiusura transitiva;
- cleanup su fallimento;
- retention transitiva;
- isolamento tra run;
- resume senza serializzazione dei valori;
- fingerprint sensibile al grafo.

### nanolab

- suite `sonata-tasks` per command, process, VM, Helm e readiness;
- topologia provisioned esatta;
- percorsi container e k8s non provisioned invariati;
- CLI `run` e `plan --provision`;
- TUI container e Kubernetes provisioned;
- import contracts, type checking, coverage e wheel smoke.

### Live

Su Multipass, partendo da VM assente:

1. run completo con cleanup: function assente, release Helm assente, VM assente;
2. run con `--keep`: function, release Helm e VM presenti;
3. slice invoke sulla VM trattenuta: acquire/release transitivi e cleanup finale;
4. fallimento consumer: cleanup function → Helm → VM;
5. TUI Kubernetes provisioned: preview di 14 fasi ed eventi Sonata reali.

## Ledger di validazione

### Baseline statica

La candidata Sonata integrata è il commit
`527d042a83ed55b6cc6334885121241146204fcf`, presente in `origin/main`, versione
`0.2.0`: 171 test passati.

Prima delle correzioni emerse durante Task 12, nanolab aveva questa baseline:

- 687 test nanolab passati;
- `sonata-tasks` al 99,55% di coverage;
- `workflow-tasks` al 93,14%;
- `tui-toolkit` al 93,67%;
- Ruff, basedpyright sui quattro package, 11 contratti di import, lock e build verdi.

Il gate completo di Step 2 è stato rieseguito sul commit nanolab `db96001`. Questo
manifest tracciato è la fonte durevole dell'esito:

```text
NANOFAAS_ROOT=<checkout-mcFaas> uv run --all-packages --all-groups \
  pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups \
  pytest -c packages/<package>/pyproject.toml packages/<package>/tests -q
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/<package>
uv run --all-packages --all-groups \
  lint-imports --config packages/<package>/.importlinter --no-cache
uv lock --check
uv build --package <sonata-tasks|nanolab>
git diff --check
```

Nel secondo comando `<package>` è stato espanso sui tre package restanti; nel quarto
e quinto su tutti e quattro i package per type check e import-linter.

| Gruppo di comandi | Exit code | Risultato |
|---|---:|---|
| `pytest` con config/package nanolab e `NANOFAAS_ROOT` | 0 | 690 test passati |
| `pytest` con config/package `sonata-tasks` | 0 | 69 test passati, coverage 99,55% |
| `pytest` con config/package `workflow-tasks` | 0 | 475 test passati, coverage 93,43% |
| `pytest` con config/package `tui-toolkit` | 0 | 51 test passati, coverage 97,11% |
| `ruff check packages` | 0 | verde |
| `basedpyright --project` per i quattro package | 0 ciascuno | 0 errori, 0 warning |
| `lint-imports` per i quattro package | 0 ciascuno | 11/11 contratti, ripartiti 4 + 2 + 3 + 2 |
| `uv lock --check` | 0 | 61 package |
| `uv build --package sonata-tasks` | 0 | sdist e wheel |
| `uv build --package nanolab` | 0 | sdist e wheel |
| `git diff --check` e `git status --short` | 0 | diff valido e working tree pulito |

Il gruppo pytest focalizzato su legacy, wrapper e topologia ha passato 5 test. I
comandi `nanolab plan` hanno verificato queste forme e postcondizioni:

- CLI container: exit 0, 9 task, con `009` release finale;
- CLI k8s non provisioned: exit 0, 5 task;
- CLI k8s provisioned con fixture Multipass: exit 0, 14 task, inclusi
  `002.acquire-stack-vm`, `008.acquire-control-plane-helm-release` e
  `014.release-stack-vm`;
- slice provisioned `--only invoke-word-stats-java`: exit 0, closure di 7 unità;
- `validate-container`: exit 0, 6 task;
- `validate-k8s`: exit 0, 12 task.

Il manifest e il lock puntano entrambi all'esatto SHA immutabile Sonata
`527d042a83ed55b6cc6334885121241146204fcf`, versione `0.2.0`, senza override
locale. `/private/tmp/nanolab-task12-verify.nRYNWc` contiene soltanto diagnostica
locale transitoria del rerun; non è la fonte dell'evidenza qui registrata.

La CI compila inoltre un vero piano `plan --provision` con un environment Multipass
di fixture, senza avviare alcuna VM. Il contratto verifica il piano di 14 task e gli
ID `002.acquire-stack-vm`, `008.acquire-control-plane-helm-release` e
`014.release-stack-vm`, preservando gli smoke non provisioned e container.

### Difetti dell'incremento emersi durante la validazione

La review ha rilevato che la costruzione del piano cercava credenziali in `~/.ssh`.
La discovery delle credenziali è ora differita al provisioning effettivo: compilare
un piano non scandisce più `~/.ssh`, mentre restano invariati i default del planner
legacy.

Il primo run live, partito da una VM pulita, è fallito deterministicamente in Helm:
il registry localhost vuoto non conteneva le immagini control-plane e function. Era
un difetto dell'incremento, non un difetto preesistente. La configurazione usa ora
le immagini ufficiali GHCR corrispondenti alla versione Gradle del prodotto. I
manifest sono stati verificati per `amd64` e `arm64`; tutte le mappature pubblicabili
del catalogo sono coperte rispetto ai riferimenti effettivi di `PublishPlan`.

Non restano difetti live irrisolti emersi da Task 12. Rimane aperta soltanto la
review finale dell'intero branch.

### Validazione live

Il codice live validato è il commit nanolab
`2ef60637b1e8e68d141326859659bd772bb48790`. Il run finale ha superato tutti i
controlli Task 12 da 1 a 10 con queste sequenze e postcondizioni durevoli:

| Scenario | Esito | Sequenza/postcondizione verificata |
|---|---|---|
| `plan ...cli.yaml --environment <multipass> --provision` | exit 0, 14 task | topologia completa `001`–`014` descritta in N2 |
| run completo Cleanup | exit 0 | list e invoke passati; release function → Helm → VM; function e release Helm assenti, VM distrutta |
| slice `--only invoke-word-stats-java` | 7 unità compilate ed eseguite | acquire VM → Helm → function → invoke → release function → Helm → VM |
| invoke forzato con JSON `status=error` | exit 1 reale | tutti gli acquire eseguiti; cleanup function → Helm → VM completato |
| run `--keep` | PASS | VM, release Helm e function presenti dopo il run |
| cleanup manuale esplicito | exit 0 | risorse trattenute eliminate; nessuna istanza Multipass residua |

La sequenza eventi osservata nel run completo includeva le 14 fasi nell'ordine
compilato N2: build; acquire VM; cinque task bootstrap; acquire Helm; acquire
function; list; invoke; release function; release Helm; release VM. Nel fallimento
forzato i tre eventi di release sono rimasti presenti e ordinati function → Helm →
VM.

`/private/tmp/nanolab-task12-live-fixed.EtihhD/` contiene soltanto diagnostica
locale transitoria; l'evidenza durevole è il manifest qui sopra.

La walkthrough TUI è un'attestazione manuale interattiva osservata nel PTY di
controllo sullo stesso commit, tramite:

```text
CLI
→ Kubernetes (provisioned)
→ multipass
→ Run
→ Cleanup
```

Il dashboard finale mostrava esattamente 14 fasi, tutte `success`, con righe evento
e log Sonata reali per VM, bootstrap, Helm, function, list, invoke e release. La VM
ha raggiunto lo stato `Deleted`, l'unica istanza esatta è stata eliminata e
`multipass list` ha restituito `No instances found`. La sessione TUI è stata
osservata direttamente; non esistono screenshot, transcript o altri artefatti
persistenti della sessione.
