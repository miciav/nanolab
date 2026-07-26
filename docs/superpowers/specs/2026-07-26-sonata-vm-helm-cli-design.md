# VM e Helm come risorse Sonata nel workflow `cli/k8s`

**Data:** 2026-07-26
**Stato:** design approvato
**Baseline nanolab:** `9878efe`
**Baseline sonata:** `0bafb9ab3778b54ca3f08a75c1f3ccd0ed3e017b`
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
