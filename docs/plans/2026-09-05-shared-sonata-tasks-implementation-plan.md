# Piano esecutivo: catalogo condiviso dei task in Sonata

Data: 2026-09-05. Stato: **piano da eseguire; nessuna implementazione effettuata**.

Documento di motivazione: [proposta architetturale](2026-09-05-shared-sonata-tasks-proposal.md).
Inventario vincolante: [mappa dei file](2026-09-05-shared-sonata-tasks-migration-map.json).
Separazioni vincolanti per responsabilità: [allegato decontaminazione](2026-09-05-shared-sonata-tasks-decontamination.md).
Il piano e il suo allegato fissano le scelte esecutive; la proposta precedente
resta la motivazione e non prevale sulle decisioni qui aggiornate.

## 1. Istruzioni per l'agente esecutore

Eseguire le attività T00–T12, inclusa T04A, nell'ordine indicato. Non sostituire il progetto
con una nuova analisi architetturale. Per ogni attività leggere i file elencati,
modificare codice e relativi consumatori, eseguire le verifiche indicate e
registrare il risultato. Non segnare un'attività completata se una sua verifica
è omessa o fallisce. Distinguere un controllo non eseguibile da uno superato.

Questo documento autorizza la progettazione del lavoro, non attesta che commit,
push, pubblicazione o migrazione siano già stati richiesti/eseguiti. Durante
l'implementazione seguire l'autorizzazione effettiva della sessione. Preparare
localmente tutto il lavoro prima di un eventuale passaggio esterno da autorizzare.
Non aggiornare la issue #23 né eseguire deploy, teardown o provisioning reali
come effetto implicito della lettura del piano.

Usare due checkout isolati se i repository hanno modifiche estranee. Non
sovrascrivere file dell'utente. Se Sonata non è scrivibile, preparare un checkout
in una directory consentita: non trattare il vincolo sul percorso originale
come motivo per lasciare incompleto il codice preparabile altrove.

Percorsi abbreviati nel resto del documento:

| Sigla | Percorso relativo al repository indicato |
|---|---|
| `OLD` | nanolab: `packages/sonata-tasks/src/sonata_tasks` |
| `OT` | nanolab: `packages/sonata-tasks/tests` |
| `S` | Sonata: `packages/sonata-tasks/src/sonata_tasks` |
| `ST` | Sonata: `packages/sonata-tasks/tests` |
| `N` | nanolab: `packages/nanolab/src/nanolab/tasks` |
| `NT` | nanolab: `packages/nanolab/tests/tasks` |

Baseline ispezionata: nanolab `d9c0783002c0067c557cbbe9f21dd58556f937b7`,
Sonata `a9aa38fb9982751dfe9624ef811ae165a496b206`. L'engine effettivamente
richiesto da nanolab è `e81d559952d4a168edffbe3c7108bd9a017046e9`.
La mappa fotografa 85 file sorgente/asset: 24 trasferimenti in Sonata, 45 in
nanolab, 16 separazioni. Gli hash servono a rilevare cambiamenti successivi,
non a impedire aggiornamenti motivati dell'inventario.

## 2. Decisioni definitive per questa implementazione

1. Repository Sonata con **due distribuzioni**: `sonata-engine` resta in
   `src/sonata_engine`; `sonata-tasks` nasce in `packages/sonata-tasks`.
   Non creare `workflow-tasks` o namespace Python condivisi da due wheel.
2. Versione iniziale del catalogo estratto: `0.2.0`, con cambi incompatibili
   dichiarati rispetto al package locale `0.1.0`. Non modificare il numero di
   versione dell'engine per questa estrazione.
3. Nessuna modifica del comportamento dell'engine è richiesta dal piano.
   Dipendenza minima certificata iniziale: esattamente il commit engine oggi
   consumato da nanolab, indicato sopra. Testare anche contro il checkout Sonata
   di sviluppo, senza dichiarare supportate versioni non provate.
4. Meccanismo comune in `S/core/command.py`; modelli in `S/execution/models.py`;
   protocolli in `S/execution/ports.py`. Nessun secondo modello autorevole delle
   stesse opzioni. I moduli per tool mantengono nomi semplici (`docker.py`, ecc.).
5. Opzioni comuni tramite dataclass `CommandOptions`, non `**kwargs`/`Unpack`.
   Aggiornare i consumatori interni insieme al cambio. I vecchi percorsi import
   generici possono essere re-export sottili; le vecchie firme non sono garantite.
6. `ExecutionRole = str`, default unico `host`. `RoleBindings` usa una mapping;
   il `Literal` dei ruoli nanolab vive in `N/execution.py`. Nessun `stack`
   obbligatorio nel catalogo, nessun contextvar per gli executor.
7. Mantenere classi nominate, senza gerarchie che servono solo ad aggiungere
   prefissi all'argv. Nessuna conversione sistematica a `FnTask` o `PhaseTask`.
8. Richiedere una chiave semantica esplicita **alla costruzione** di un comando
   con argv callable o verifica callable arbitraria. È una semplificazione
   deliberata della proposta: evita di aggiungere all'engine un protocollo di
   preflight del resume. Il chiamante può sempre eseguire callable passando una
   chiave, anche senza journal. La chiave non è dedotta da qualname o bytecode.
9. Nessuna compatibilità automatica dei vecchi journal: completamento/teardown
   con la vecchia installazione, poi nuova directory di esecuzione. Preservare
   vecchio lockfile e artefatti necessari. Non modificare fingerprint nei journal.
10. Trasferire i provider VM dopo eliminazione dei default di prodotto. Lasciare
    playbook Ansible, orchestrazione provisioning, scenari k6, policy di metriche,
    reportistica e composite release specifici in nanolab. Estrarre invece i
    meccanismi Ansible/k6/HTTP/Prometheus secondo T04A: la contaminazione non
    giustifica lasciare nel client un intero modulo misto. Le responsabilità
    finali hanno destinazioni precise nella mappa e nell'allegato.
11. Anche `resources.py` resta a nanolab: traduce la semantica delle risorse
    nanoFaaS in limiti Docker/Kubernetes. `testing.py` attuale resta a nanolab:
    contiene risposte del control plane; il catalogo riceve fake generici nuovi.
12. Nessuna nuova integrazione `sonata-nanofaas`, API HTTP o plugin discovery.
    Il secondo consumer è un programma Python autonomo incluso in Sonata;
    certifica l'indipendenza tecnica, non requisiti di prodotti ancora sconosciuti.

## 3. Contratti da implementare, senza alternative da scegliere

### 3.1 Opzioni, specifica e risultato

In `S/execution/models.py`:

```python
@dataclass(frozen=True, slots=True)
class CommandOptions:
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    remote_dir: str | None = None
    expected_exit_codes: frozenset[int] = frozenset({0})
    timeout_seconds: float | None = None

@dataclass(frozen=True, slots=True)
class CommandTaskSpec:
    task_id: str
    summary: str
    argv: tuple[str, ...]
    role: str = "host"
    options: CommandOptions = field(default_factory=CommandOptions)
```

Il codice illustrativo omette import e validatori, non i campi. Copiare e
proteggere `env` con `MappingProxyType(dict(env))`; convertire codici in frozenset.
Rifiutare timeout non finiti/non positivi, insieme vuoto dei codici, ruolo vuoto
e argv statico vuoto. Un argv dinamico vuoto va rifiutato quando viene risolto.
`cwd` mantiene il significato locale; `remote_dir` quello remoto. Non combinarli
implicitamente. `execution_role` può restare come proprietà che restituisce
`role`; non deve più interpretare `None` o scegliere destinazioni alternative.

Conservare `TaskResult`, i suoi campi e `ok`, per evitare una riscrittura del
canale dati insieme all'estrazione. Un executor reale deve restituire un codice
intero e uno stato coerente. `CommandTask` controlla sia lo stato sia il codice
rispetto alle **opzioni della richiesta**, non a un secondo default del risultato.
`return_code=None` non è un successo di esecuzione; il dry-run restituisce il
risultato sintetico coerente senza invocare il processo.

Aggiornare ogni lettura di `spec.env`, `spec.cwd`, ecc. in `spec.options.…`.
Non duplicare i campi nella spec per mantenere contemporaneamente due verità.
I call site di `dataclasses.replace` devono sostituire `options` oppure usare
`replace(spec.options, ...)`: verificare anche questi consumatori.

### 3.2 Executor e routing

`CommandTaskExecutor` è un Protocol con:

```python
def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult: ...
def binding_key(self, role: str) -> str: ...
```

`binding_key` è pura: non esegue SSH, non crea VM e non legge credenziali.
Restituisce l'identità semantica stabile della destinazione configurata.
L'executor locale usa una chiave fornita dal chiamante, default `local`.
Gli adapter remoti ricevono `target_key` obbligatoria, derivata dal client da
provider, nome VM/host, utente e contesto configurato; mai password o token.
Non usare indirizzi ottenibili soltanto dopo l'acquire come prerequisito di compile.
Se cambia la destinazione configurata, cambia la chiave; non basta il nome `stack`.

`RoleBindings(executors: Mapping[str, CommandTaskExecutor])` copia la mapping,
rifiuta chiavi vuote e solleva `ValueError` per un ruolo assente.
`RoleBoundCommandTaskExecutor` delega `run` e `binding_key` allo stesso binding.
Gli executor foglia ricevono la spec con il ruolo originale: non devono
rifiutare `builder` solo perché operano localmente. L'executor foglia è già una
destinazione; il routing appartiene al wrapper.

La costruzione di `CommandTask` chiama `binding_key(role)` per validare il
binding senza effetti. Anche l'esecuzione diretta di una spec tramite router
deve fallire prima di chiamare un backend se manca il ruolo.

In nanolab sostituire `RoleBindings(host=…, stack=…)` con un builder locale
che crea la mapping omettendo le destinazioni non configurate. Cercare anche
accessi `.host`, `.stack`, `.arm_builder` e sostituirli con `executor_for`.

### 3.3 CommandTask e fingerprint

Firma finale keyword-only:

```python
CommandTask(
    *, title: str, argv: Argv, executor: CommandTaskExecutor,
    role: str = "host", options: CommandOptions | None = None,
    verify: Callable[[TaskResult], None] | None = None,
    semantic_key: str | None = None,
)
```

`None` per `options` crea un valore vuoto una sola volta; non è un secondo
sistema di default. `_spec(inputs)` resta il solo ponte verso la spec risolta.
`idempotent` resta `False` come oggi, salvo task specifici che giustificano già
un comportamento differente. Un fingerprint non abilita skip o retry.

`semantic_key` non vuota è obbligatoria se argv è callable o verify è presente.
Per wrapper con verifica interna, generarla da versione dell'operazione e
configurazione controllata dal wrapper. Per codice client deve essere esplicita
e includere i valori catturati rilevanti; documentare che aggiornarla è
responsabilità del chiamante. Non usare una costante generica uguale per tutte
le closure. Un digest di una configurazione JSON tipizzata è ammesso.

Creare `S/core/fingerprint.py` con una sola funzione di canonicalizzazione:
mapping ordinate, set ordinati, Path convertiti in stringa, numeri finiti,
nessun `default=str`. Hash SHA-256 del JSON canonico. Il payload pubblico del
task contiene soltanto versione schema e digest, non argv/env in chiaro.
La canonicalizzazione deve includere:

- argv statico, oppure tag `dynamic` e chiave semantica;
- tutte le opzioni;
- ruolo e `executor.binding_key(role)`;
- chiave semantica, se presente, e versione del contratto comando (`1`).

Il digest non è un meccanismo di custodia dei segreti; la novità deve evitare
di aggiungere valori sensibili a log e journal. Non modificare in questo progetto
il sistema esistente di logging degli output dei processi.

Per i task che restituiscono valori, conservare la distinzione tra nuova
esecuzione e `ReusableTask` con sole prove. Testare il fingerprint sia nel
workflow esterno sia in `Steps` annidati senza cambiare il compiler.

### 3.4 Backend e timeout

Creare `S/execution/local.py::LocalCommandTaskExecutor` basato sulla stdlib,
utilizzabile senza Shellcraft. Usa argv senza shell implicita, cwd e ambiente
ereditato dal processo con override espliciti. Raccoglie stdout/stderr separati.
Il suo contratto iniziale prevede output al completamento, non streaming.

Su POSIX lanciare con sessione propria; usare `communicate(timeout=...)`;
su scadenza terminare il gruppo, attendere fino a 5 secondi, quindi uccidere il
gruppo se necessario, drenare i pipe e attendere il processo. Sollevare
`CommandTimeoutError`, sottoclasse di `TimeoutError`, con timeout e output
parziale. Applicare cleanup anche a un'interruzione Python. Non restituire un
exit code sintetico che potrebbe essere considerato successo. Su piattaforme
non POSIX rifiutare il timeout prima dello spawn finché non è implementata una
garanzia equivalente; l'esecuzione senza timeout può usare il percorso normale.
`remote_dir` su questo executor è sempre un errore prima dello spawn.

Conservare in `S/execution/adapters.py` gli adapter `HostCommandTaskExecutor`
e `VmCommandTaskExecutor` per i runner esistenti. Shellcraft oggi non espone
timeout: **non fingere di inoltrarlo con un keyword inesistente**. In questa
release entrambi rifiutano `timeout_seconds != None` con
`UnsupportedCommandOptionError` prima di chiamare il runner. L'adapter host
rifiuta anche remote_dir. Quello VM inoltra env/remote_dir; cwd è rifiutato
dall'adapter generico. Nei call site nanolab che oggi danno cwd a comandi VM,
fare la conversione esplicita descritta in T08, senza perderne il significato.

Nanolab mantiene i backend esistenti per conservare lo streaming e i fake
attuali; adotta il nuovo executor locale solo dove è richiesto un timeout
locale effettivo. Il supporto timeout remoto è esplicitamente assente, non una
funzione incompleta lasciata al prossimo agente.

## 4. Mappa dei trasferimenti e separazioni

La mappa JSON è esaustiva per il contenuto attuale di `OLD`. Ogni voce indica
origine, hash, azione, destinazioni e simboli di primo livello. Gli `__init__`
sono riscritti in base ai simboli rimasti; non copiare export che creano cicli.
Una destinazione con scope «no client implementation» non richiede un file vuoto.
Quando più origini confluiscono nella stessa destinazione (per esempio il
trasporto e il client Prometheus), unire le responsabilità indicate: non copiare
i file uno dopo l'altro sovrascrivendoli. Le voci split richiedono la lettura
dei metodi delle classi, non soltanto dell'inventario dei simboli di primo livello.

Regole aggiuntive per gli split originari e i moduli modificati; gli altri
otto split e la separazione delle politiche sono specificati in T04A:

| Origine | Operazione esatta |
|---|---|
| `__init__.py` | S esporta solo CommandTask, CommandOptions, CommandTaskSpec, TaskResult; N può riesportare i simboli di prodotto ancora usati. Gli import dei tool sono qualificati. |
| `execution/roles.py` | S contiene alias str; N/execution.py contiene NanolabRole e builder binding, senza ridefinire gli executor. |
| `kubectl.py` | S conserva KubectlTask, ClusterIpEndpointTask e k8s_deployment_readiness. N/kubectl.py contiene k8s_function_resources_absent e importa i primitivi generici. |
| `release_composites.py` | Solo command_specs_composite entra in S/composites.py. Attestazione, matrice e push/inspect rimangono in N/release_composites.py; rimuovere la loro duplicazione del ponte. |
| `vm/__init__.py` | S espone vm_resource, tipizzata su VmLifecycleProtocol anziché sull'adapter concreto. |
| `vm/models.py` | S conserva modelli generici e VmRequest senza hpa_scale_to_zero e lettura env. N/vm/models.py definisce NanolabVmRequest(VmRequest), aggiunge il campo e lettura env; alias locale VmRequest=NanolabVmRequest per limitare il cambio applicativo. |
| `vm/ports.py` | S definisce contratti lifecycle, exec e transfer; usare tipi risultato espliciti. Aggiungere transfer_to mancante, senza Any o getattr-success. |
| `vm/multipass.py` | S/vm/providers/multipass.py contiene provider e helper SDK. Funzioni SSH condivise vanno in S/vm/ssh.py senza import multipass. Costanti REPO_SYNC_* e repo_rsync_command vanno in N/vm/sync.py. Il builder SSH per rsync può delegare al modulo SSH condiviso. |

Altri cambi obbligatori:

- `vm/azure.py` e `vm/proxmox.py` diventano `S/vm/providers/{azure,proxmox}.py`.
  Non importano più helper da multipass: spostare quelli comuni in `vm/ssh.py`
  e normalizzazione dei risultati in `vm/results.py`. L'import Azure non deve
  richiedere multipass. Conservare teardown, controllo cloud, NAT e diagnostica.
- I provider gestiti richiedono un nome VM esplicito prima della chiamata SDK.
  Nanolab applica i precedenti default (`nanofaas-azure`, ecc.) nel suo builder.
  Per `remote_project_dir` aggiungere `remote_project_name` al costruttore,
  senza default di prodotto; se omesso e il metodo è richiesto, errore esplicito.
  Nanolab passa `nanofaas`. Nessun default silenzioso `sonata` al suo posto.
- `N/vm/orchestrator.py` resta l'integrazione Ansible/provisioning di nanolab,
  consuma i provider generici e la policy sync locale. `N/provisioning/providers.py`
  importa ciascun SDK provider solo nel ramo corrispondente alla richiesta.
- `S/vm/adapters.py` usa un Protocol completo per ensure/connection_host/teardown;
  preserva la propagazione delle credenziali e verifica anche l'esito teardown.
- `S/compose.py`: togliere role/env da DockerComposeProject; passarli ai task e
  alla resource tramite role/options. Introdurre remove_volumes=False come
  parametro esplicito del teardown/resource insieme a remove_orphans=False.
  Il reset prima del deploy passa nel composite client isolated_compose_resource
  secondo C01. Conservare build nel progetto, con scelta esplicita dei call site.
- `S/helm.py`: togliere role da HelmReleaseSpec; passarlo al task/resource.
  Conservare il timeout Helm distinto da CommandOptions.timeout_seconds.
- `S/registry.py`: rendere container un parametro obbligatorio; default immagini
  del tool possono restare, nomi nanoFaaS no. Nanolab passa REGISTRY_CONTAINER_NAME.
- `S/registry_tunnel.py`: parametri unit_name, listen_port, upstream_port
  espliciti e titolo opzionale; eliminare _UNIT/_STOP/_RESET globali. Nanolab
  passa nome/porte/titolo precedenti. Conservare always_release e compensazione.
- `archive.py`, `transfer.py`, `registry_tunnel.py`: sostituire provider/request
  Any con Protocol generico parametrizzato dal tipo richiesta, con metodi e
  risultati espliciti. Un risultato senza return_code intero non è successo.
  Nessun allargamento dei percorsi cancellati durante cleanup.
- `GradleTask`: parametro daemon=False, argomento corrispondente --daemon oppure
  --no-daemon; nanolab mantiene False. Conservare target e proprietà del tool.
- `S/shell.py` resta un'integrazione Shellcraft con eventi Sonata, importabile
  solo con extra shell. Non esportarla eager dal package principale.
- `S/testing.py` nuovo: RecordingExecutor con lista spec eseguite, risposte
  preimpostate ed execution key; nessuna risposta del protocollo nanoFaaS.

## 5. Sequenza dei commit e dipendenze

```text
T00 baseline
  → T01 package Sonata
  → T02 modelli/executor
  → T03 CommandTask/fingerprint
  → T04 tool
  → T04A separazione meccanismi/politiche (C01–C08)
  → T05 resource/transfer
  → T06 VM
  → T07 spostamento dominio in nanolab
  → T08 adozione API e taglio package locale
  → T09 distribuzione isolata e consumer
  → T10 CI/documentazione
  → T11 verifiche finali
  → T12 consegna e passaggio delle revisioni
```

T01–T06 sono commit del branch Sonata. T07–T08 sono un solo commit integrabile
nel branch nanolab: la separazione numerata serve all'esecuzione, non autorizza
a consegnare un commit con import rotti. T09–T10 possono richiedere un commit
in ciascun repository. Nessun commit nanolab pubblicabile deve dipendere da un
percorso locale della macchina o da uno SHA non disponibile nel remoto previsto.

### T00 — Fotografare stato e baseline

**File:** mappa JSON, manifest dei due repository, workflow CI, test esistenti.

1. Leggere eventuali AGENTS.md applicabili ai checkout effettivi. Confrontare HEAD
   e hash dei file con la mappa. Classificare nuovi file con le regole della §4;
   una nuova responsabilità non classificabile va segnalata, non spostata a caso.
2. Creare un registro locale `docs/plans/sonata-task-migration-progress.md`
   con HEAD iniziali, versione Python/uv, T00–T12, verifiche e SHA dei commit.
3. Eseguire QN0 e QS0 della §7 sul codice iniziale. Annotare errori preesistenti;
   non correggerli tacitamente né abbassare soglie per ottenere il verde.
4. Salvare il risultato di `nanolab plan` per gli scenari della CI e la forma
   dei workflow compilati nei test: ordine, titoli, argv statici, ruoli, lifecycle.
   I nuovi fingerprint sono esclusi dall'equivalenza attesa.

**Uscita:** inventario aggiornato e baseline riproducibile. Non eseguire il
comando `nanolab images`: non esiste più. Non chiudere #23 automaticamente.

### T01 — Creare il package nel repository Sonata

**File nuovi:** `packages/sonata-tasks/pyproject.toml`, README.md,
`S/__init__.py`, `S/py.typed`, `S/core/__init__.py`,
`S/execution/__init__.py`, directory test, `.importlinter` del catalogo.
**File modificati:** Sonata `pyproject.toml`, `uv.lock`.

1. Aggiungere `[tool.uv.workspace] members = ["packages/sonata-tasks"]` al root
   Sonata senza cambiare setuptools package-dir dell'engine o trasferirne i file.
2. Manifest catalogo: Python >=3.12, versione 0.2.0, setuptools, py.typed.
   Dipendenza base unica: sonata-engine dal commit E della §6.
3. Dev groups: pytest, pytest-cov, basedpyright, ruff, import-linter e grimp con
   i vincoli attuali del package; importare configurazione lint/types/coverage
   attuale, mantenendo cov-fail-under=90. Non introdurre nuove regole stile globali.
4. Dichiarare extra shell, multipass, azure, proxmox con gli stessi pin SDK
   attuali. Aggiungere extra prometheus con httpx>=0.27. I task HTTP/curl e
   i controlli metriche via comando non dipendono da questo extra. Gli extra VM
   includono pydantic e shellcraft. Pydantic-settings resta una dipendenza del
   client che legge le variabili d'ambiente, non del catalogo. Base e import root
   non richiedono VM.
5. Per lo sviluppo del workspace, sorgente sonata-engine workspace=true;
   la dipendenza buildata resta quella Git E. Distinguere deliberatamente i due
   ambienti e provarli entrambi in T09. Nessun pin si aggiorna automaticamente.

**Verifica:** build engine ancora contenente solo sonata_engine, build catalogo
contenente solo sonata_tasks; import engine senza catalogo; `uv lock` riuscito.
Il package vuoto non deve essere considerato un rilascio completo.

### T02 — Modelli, executor e fake

**Origini:** OLD/tasks/models.py, tasks/executors.py, execution/bindings.py,
execution/roles.py. **Destinazioni:** S/execution/{models,ports,bindings,
adapters,local,roles}.py, S/errors.py, S/testing.py.

1. Implementare §3.1 e §3.2. Non conservare campi duplicati per compatibilità.
2. Spostare adapter esistenti aggiornando accesso alle opzioni, target_key,
   validazione risultato e rifiuto delle capacità non supportate.
3. Implementare executor stdlib e timeout §3.4. Nessuna modifica a Shellcraft
   esterno è necessaria o parte del piano.
4. Spostare/adattare OT/test_models.py, test_executors.py, test_bindings.py in ST.
   Leggere anche nanolab/tests/test_task_models.py e test_task_executors.py:
   trasferire a ST solo gli assert sul contratto generico, mantenere a nanolab
   gli assert sul suo wiring.
5. Aggiungere ST/test_command_options.py e test_local_executor.py. Prove locali
   usando sys.executable e tempfile: env/cwd, stdout/stderr, codice accettato non
   zero, codice inatteso, dry-run senza effetti, timeout e assenza del figlio
   POSIX dopo cleanup. Usare processi brevi, cleanup nel finally e margini
   temporali non fragili; niente sleep lunghi o comandi cloud.
6. Test adapter: opzione non supportata solleva prima di incrementare il numero
   di chiamate del fake; ruolo builder funziona e ruolo assente fallisce prima
   del backend; env originale mutata non altera options/spec.

**Uscita:** protocolli controllati dal type checker e test mirati verdi;
nessuna dipendenza nanoFaaS o SDK in modelli/executor base.

### T03 — Comando unico e identità semantica

**File:** OLD/command.py → S/core/command.py; S/core/fingerprint.py;
S/command.py re-export; S/tasks/models.py e executors.py re-export temporanei;
ST/test_command.py, test_fingerprint.py, test_resume_commands.py.

1. Implementare §3.3, mantenendo tipo Task[TaskResult] e TaskOutcome.
2. Re-export dei vecchi moduli generici: stessi oggetti, non copie/sottoclassi
   parallele; nessuna promessa di conservare il fingerprint dei vecchi moduli.
3. Test: verifica chiamata solo dopo successo; code/status incoerenti rifiutati;
   errore conserva stdout/stderr utili; binding mancante fallisce alla costruzione.
4. Fingerprint: stesso titolo con argv diversi; env, cwd, remote_dir, timeout,
   codici, ruolo, binding key e semantic_key diversi devono cambiarlo; mapping
   equivalente con ordine diverso no. Il payload non contiene token di test.
5. Callable non eseguita a compile; chiave assente rifiutata; chiavi diverse
   distinguono callable con stesso qualname. Stessi casi dentro Steps annidati.
6. Resume: usare journal temporanei e i test dell'engine come modello; ordinari
   command task producono nuovamente il valore secondo le regole correnti;
   un fallimento non idempotente non acquisisce retry automatico.

**Uscita:** nessuna modifica al compiler, nessun FnTask, niente skip-on-success
aggiunto al catalogo. Test QS della §7 sul nuovo gruppo.

### T04 — Catalogo degli strumenti

**File:** OLD/{docker,compose,helm,kubectl,gradle,skopeo,syft,cosign,imagetools}.py
→ S; split kubectl secondo §4. Test omonimi OT → ST.

1. Ogni wrapper di comando eredita direttamente da CommandTask, dichiara parametri
   del tool e keyword comuni executor, role, options, title, verify, semantic_key
   dove applicabili. Non ridichiarare cwd/env/timeout separatamente.
2. Comporre argv con helper privati, preservando ordine, quoting e significato dei
   flag. Conservare nomi delle classi. Le stringhe shell già necessarie non vanno
   trasformate arbitrariamente in nuovi script durante la migrazione.
3. Applicare le decisioni Gradle, Compose, Helm della §4. La resource passa le
   stesse opzioni ai suoi task; se una verifica interna è configurabile,
   includerne parametri nella chiave semantica.
4. `ClusterIpEndpointTask` resta Task[str] col proprio run; aggiungere payload
   per i suoi parametri statici, senza inventare una gerarchia FnTask.
5. Test argv per ogni wrapper, inclusi valori con spazi/caratteri shell; test
   parametrico delle opzioni attraverso la spec, con RecordingExecutor.
   Test specifico Compose senza --volumes per default e con --volumes esplicito;
   Helm timeout tool indipendente dal timeout processo.

**Uscita:** import di ciascun tool senza SDK/reportistica; test di comportamento,
non assert sulla lunghezza MRO. Nessuna perdita di verifiche precedenti.

### T04A — Rimuovere la contaminazione di dominio

**Specifica esecutiva:** applicare integralmente C01–C08 nell'allegato
decontaminazione. I meccanismi estratti in S devono avere test su un dominio
diverso; gli adapter/composite/sottoclassi N mantengono il comportamento nanolab.

**File e test:** split compose.py, k6.py, metrics.py, http_function.py,
loadtest/models.py, loadtest/prometheus.py, loadtest/adapters.py e infra/ansible.py
secondo mappa. Aggiungere ST/test_{http,k6,metrics,prometheus,ansible}.py;
separare i test relativi da OT e mantenere quelli di policy a NT.
Tutti i test in OT con questi soggetti, anche nelle sottodirectory loadtest/infra,
vanno assegnati per responsabilità: trasporto/parser/runner a ST, policy/adapter
di scenario a NT. Nessun test va cancellato perché il nome file non coincide.

**Uscita:** registro audit per simbolo, test di entrambe le metà e assenza di
duplicazione. Non limitarsi a rendere opzionali i nomi nanoFaaS nei costruttori.

### T05 — Resource generiche, trasferimenti e ponte spec→Steps

**File:** OLD/{archive,buildx,compensation,process,registry,registry_tunnel,
transfer}.py → S; command_specs_composite → S/composites.py.
**Test:** OT/test_{archive,buildx,compensation,process,registry,registry_tunnel,
transfer}.py → ST; separare i test del ponte da test_release_composites.py.

1. Applicare parametri e protocolli della §4. Non introdurre default del prodotto.
2. Ponte: copiare role senza `or "stack"`, conservare options intere e ordine.
   Titolo fallback generico `Command {i}`. Nessun trasferimento di image plan Any.
3. Conservare acquire/release, requires, always_release, external, compensation
   e revive. Non cambiare incidentalmente l'ownership di oggetti già esistenti.
4. Test con fake: acquire riuscito, consumer fallito, acquire parziale,
   compensazione che fallisce senza nascondere l'errore iniziale, resource esterna,
   oggetto preesistente, retention. Preservare i test già esistenti su questi casi.
5. Due tunnel con unit/porte diverse non condividono comandi globali. Un transfer
   con risultato malformato fallisce. I cleanup archive mantengono esattamente
   i percorsi assegnati dal chiamante.

**Uscita:** catalogo senza convenzioni nanoFaaS; test lifecycle verdi e bridge
con la stessa semantica ruolo della spec.

### T06 — VM e dipendenze opzionali

**File:** split VM della §4; S/vm/{__init__,models,ports,tasks,adapters,ssh,
results}.py e providers/{__init__,multipass,azure,proxmox}.py.

1. Separare modelli, environment settings, policy sync e orchestrazione secondo
   mappa. Non trasferire VmOrchestrator in Sonata.
2. Rendere i provider indipendenti tra loro; conservare i pin SDK correnti.
   Fornire re-export dei vecchi moduli vm.azure/proxmox/multipass limitati ai
   simboli generici trasferiti, senza importare tutti i provider da vm.__init__.
3. I provider non applicano i default VM/directory del prodotto. Adeguare i test
   a nomi espliciti. Conservare retry/diagnostica e verifiche di eliminazione
   effettiva già presenti, senza eseguire chiamate reali.
4. `vm_resource` accetta VmLifecycleProtocol. `VmLifecycleAdapter.destroy`
   controlla lo stato restituito; preservare credenziali negli ensure/teardown.
5. Test generic VM: OT/test_vm.py e vm/test_vm_tasks.py, test_vm_adapters.py,
   test_adapters_credentials.py, test_azure_provider.py, test_proxmox_provider.py,
   test_multipass_provider.py, test_multipass_provider_extended.py → ST.
   Split vm/test_vm_request.py e test_proxmox_vm_request.py per campi generici
   versus policy client. vm/test_orchestrator.py, test_vm_runners.py e
   test_repo_sync_excludes.py vanno a NT; test env/hpa a NT.
6. Nuovi test in interpreti separati: base import senza pydantic/SDK; extra azure
   importa il provider senza multipass/proxmox; analogamente gli altri extra.
   Import di un provider mancante deve dare indicazione dell'extra necessario,
   intercettando solo il modulo opzionale mancante, non ogni ImportError interno.

**Uscita:** test provider tutti con fake; nessuna installazione cloud reale.
Le garanzie di timeout remoto restano quelle esplicite della §3.4.

### T07 — Trasferire il dominio nel package nanolab

**File:** tutte le voci `nanolab` della mappa e le metà client degli split → N;
test corrispondenti → NT; nanolab pyproject e package-data.

1. Spostare conservando la struttura relativa. Tutti gli asset infra/ansible_assets
   vanno in N/infra/ansible_assets; aggiornare importlib.resources/path derivati
   e `[tool.setuptools.package-data]` di nanolab. Non affidarsi al cwd del repo.
2. `N/testing.py` mantiene fixture del protocollo nanoFaaS. `N/loadtest`, `N/k6.py`,
   `N/metrics.py`, `N/provisioning`, `N/components` mantengono il comportamento di
   prodotto delegando i meccanismi estratti alle implementazioni T04A di Sonata.
3. Tutti i test non assegnati a ST nelle T02–T06 (inclusa T04A) vanno a NT, con eccezioni:
   test_package_boundaries.py va riscritto come contratto del nuovo S;
   test_shell.py resta ST ma richiede extra shell; test_release_composites.py
   è separato tra bridge ST e resto NT. Gli __init__ test seguono le directory.
4. Per test misti non cancellare assert: separare funzioni di test e fixture nei
   due file, poi registrare nella mappa progressi il numero e i node ID trasferiti.
   Evitare collisioni di nomi con test nanolab già presenti usando NT dedicata e
   import-mode=importlib.
5. Aggiornare i riferimenti al dominio in codice/test nanolab secondo la mappa,
   compresi import dal root sonata_tasks, stringhe monkeypatch e import dinamici.
   Vietato un replace globale `sonata_tasks`→`nanolab.tasks`: i tool restano S.
6. Aggiungere a nanolab le dipendenze dirette ora necessarie: httpx, pandas,
   plotly, pydantic-settings, shellcraft e gli SDK effettivamente importati dal
   codice client. Dove il client consuma soltanto il provider condiviso usare
   l'extra corrispondente di sonata-tasks. Non dipendere da un extra per librerie
   importate direttamente senza dichiararlo esplicitamente nel manifest client.

**Uscita intermedia:** nessun file della mappa senza destinazione. Non fare merge
di questa fase finché T08 non ripristina l'intera suite e le dipendenze.

### T08 — Adottare API e rimuovere il package locale

**File principali:** nanolab `cli/execution.py`, `plans/*.py`, `release/*.py`,
N intero, test consumatori, pyproject root/member e uv.lock.

1. Trasformare gli argomenti comuni in CommandOptions in tutti i costruttori di
   task/spec. Aggiornare replace, fixture, fake executor e protocolli client.
   Gli executor finti devono implementare binding_key con una chiave stabile.
2. Costruire il RoleBindings generico dal builder N/execution.py. Conservare i
   ruoli esistenti, i default del prodotto e il fail-fast dei binding mancanti.
3. Applicare le policy rese esplicite: Compose usa isolated_compose_resource
   con reset iniziale, remove_volumes=True e remove_orphans=True; nome registry/unit tunnel/porte precedenti; Gradle daemon=False;
   nome VM e remote_project_name nel provider factory. Nessuna risorsa rinominata.
4. Risolvere i cwd dei comandi remoti nel punto di composizione che conosce sia
   checkout locale sia progetto remoto: se cwd indicava il workspace del launcher
   SSH, passarlo al runner configurato e toglierlo dalle opzioni del comando;
   se indicava una directory del progetto remoto, convertirlo con il mapping
   già usato da remote_path_for_local in remote_dir. Un percorso estraneo ai
   due contesti è errore, non un motivo per scartare cwd. Aggiungere test delle
   due categorie e dei percorsi non mappabili in cli/test_execution_bindings.py.
5. Aggiungere semantic_key a ogni argv/verify callable. Per closure di prodotto
   includere funzione/versione e configurazione catturata serializzabile.
   Per comandi con risorse dinamiche usare identificatori logici della resource;
   non risolverla prima dell'acquire. Nessuna lambda eseguita durante compile.
6. Centralizzare in S/composites.py una funzione command_specs_fingerprint che
   usa la stessa canonicalizzazione comando su spec statiche e binding key.
   Sostituire le derivazioni parziali dei phase_inputs in plans/release_phases.py
   e altri producer di release. Restano nel client le restanti parti della
   chiave di fase: identità, matrice, prerequisiti, versione e ricevuta.
7. `N/release_composites.py` continua a gestire l'attestazione senza generalizzare
   ReleasePhaseTask. Tipizzare i **parametri passthrough delle factory di fase**
   con un TypedDict condiviso locale/Unpack, al posto di kwargs Any: qui le opzioni
   non sono quelle comando. Non inventare 13 nuove sottoclassi.
8. Rimuovere packages/sonata-tasks dal workspace e dalla directory nanolab
   soltanto dopo aver spostato sorgenti/test e aggiornato tutti i riferimenti.
   Collegare il nuovo package con override locali solo per test di sviluppo;
   le dipendenze finali seguono §6.
9. Cercare residui in src/tests/manifest/CI/script; riferimenti storici nei vecchi
   documenti sono ammessi. Ogni import sonata_tasks residuo deve puntare a un
   modulo realmente presente nel nuovo package.

**Uscita:** QN aggiornato e test dei plan verdi, stessa topologia/argv/ownership
baseline salvo cambi deliberati documentati. Cambi ai fingerprint sono attesi.

### T09 — Distribuzioni isolate e consumer indipendente

**File nuovi Sonata:** examples/shared_tasks_client.py,
scripts/check_task_wheel.py, ST/test_public_api.py, ST/test_package_boundaries.py.

1. Esempio: Workflow con DockerBuildTask e DockerInspectTask su RecordingExecutor,
   routing `builder`, CommandOptions(timeout_seconds=600). Il fake registra le
   opzioni senza eseguire Docker; assert su risultato, argv e binding. Aggiungere
   un comando innocuo sys.executable con LocalCommandTaskExecutor realmente
   eseguito in una directory temporanea. Nessuna importazione nanoFaaS.
2. check_task_wheel.py crea venv temporanei fuori dai checkout e installa la wheel
   del catalogo con dipendenze pubblicate, senza --no-deps/PYTHONPATH/editable.
   Usa i pin Git effettivi: eventuale accesso ai repository privati deve essere
   configurato normalmente, senza token nei file o nei log.
3. Nell'ambiente base verificare metadata e import: assenza di nanolab, Plotly,
   pandas e SDK VM; import root/CommandTask/docker/helm senza questi moduli.
   Eseguire l'esempio, incluso k6 generico su fake. Ripetere per shell,
   prometheus e ciascun extra VM isolato.
4. Eseguire tutti i test del catalogo anche contro E in un venv isolato installando
   la wheel e le dipendenze test/extra, con Python `-m pytest` e percorsi assoluti
   dei test. Usare cwd temporaneo, non il workspace che può mascherare gli import.
5. Costruire wheel nanolab e tui-toolkit, installarle in altro ambiente pulito
   con il catalogo esterno e verificare nanolab --help, import entrypoint e
   accesso ai playbook tramite package-data. Niente dipendenze path locali.

**Uscita:** stesso artefatto del catalogo consumato da nanolab e programma
autonomo. Un test nel solo uv workspace non soddisfa questa attività.

### T10 — CI, documentazione e confini

**File Sonata:** .github/workflows/ci.yml, root/catalog pyproject, README,
catalog .importlinter, docs/task-catalog.md.
**File nanolab:** .github/workflows/ci.yml, .github/actions/setup-workspace/action.yml
se necessario, packages/nanolab/.importlinter, devtools/quality.py e relativi test,
README, documenti correnti che descrivono i package.

1. CI Sonata esegue QS, build separata engine/catalogo, T09 base/extra e test
   engine senza catalogo installato. Catalogo non può importare nanolab/tui;
   engine non può importare catalogo. Non usare fake moduli per far passare import.
2. CI nanolab matrix rimane nanolab/tui-toolkit. Le suite trasferite nel client
   sono parte dei test nanolab; quelle generiche girano in Sonata. Rimuovere solo
   il job membro locale, non le sue verifiche senza ricollocazione.
3. Il job wheel nanolab continua a importare sonata_tasks come dipendenza esterna;
   non deve costruirne una seconda wheel dal checkout nanolab.
4. Aggiornare descrizione package, import pubblici, esempi options/binding_key,
   extra, errori timeout e breaking changes. Correggere docstring tui-toolkit
   che attribuiscono ancora gli eventi a sonata_tasks.
5. Documentare migrazione journal e rollback §8. Preparare testo di aggiornamento
   issue #23 nel registro progressi, senza pubblicarlo implicitamente.

**Uscita:** ogni responsabilità CI precedente ha un nuovo proprietario; esempi
documentati sono coperti dall'esecuzione dell'esempio o dai test di API.

### T11 — Verifica conclusiva

1. Eseguire QS e QN completi, senza abbassare soglie, poi T09 se sono cambiati
   packaging o codice dopo l'ultima prova. Confrontare risultati con T00.
2. Eseguire i comandi `nanolab plan` già presenti nella CI: container, k8s,
   CLI container e CLI k8s con provisioning. Nessun run cloud richiesto.
3. Eseguire lo smoke container esistente nel job CI previsto; non inventare
   risorse cloud per validare una migrazione di package. Se indisponibile,
   segnalare il gate pendente e non dichiarare verificata l'equivalenza end-to-end.
4. Audit finale mappa: ogni origine trasferita/eliminata, ogni destinazione
   presente, ogni split rispettato. Confrontare anche test raccolti pre/post e
   spiegare test divisi/accorpati: un calo silenzioso non è accettabile.
5. Cercare nomi/percorsi nanoFaaS nel catalogo. Ammessi solo note di migrazione,
   fixture che verificano l'assenza di policy e casi test esplicitamente client;
   non default o dipendenze runtime.
6. `git diff --check` in entrambi i repository. Nessun file temporaneo, credenziale,
   artefatto cloud o override di percorso personale nei commit.

**Uscita:** checklist finale §9 completa oppure elenco preciso dei gate pendenti.

### T12 — Consegna coordinata

1. Finalizzare commit Sonata, registrare SHA S del catalogo verificato. Verificare
   che l'engine E sia disponibile senza cambiare la dipendenza del client.
2. Quando pubblicazione/push sono autorizzati, rendere disponibile S; poi
   aggiornare nanolab a S con subdirectory, rigenerare lock e ripetere packaging.
   Il checkout locale è sufficiente per preparare le patch, non per fingere
   che un pin remoto ancora inesistente sia installabile.
3. Consegnare due cambi reviewabili: Sonata prima, nanolab dopo. Se Sonata non
   è pubblicabile nella sessione, consegnare entrambi i diff e indicare quel
   passaggio come pendente; non inserire uno SHA inventato o un percorso locale
   come configurazione finale.
4. Allegare output sintetici dei gate, mappa finale, breaking changes e istruzioni
   per vecchi journal. Non chiudere automaticamente #23: il suo percorso images
   originario è stato eliminato, non implementato come descritto.

## 6. Pin e installazione: una procedura unica

Definire `E = e81d559952d4a168edffbe3c7108bd9a017046e9`.
Definire `S` soltanto dopo il commit effettivo Sonata che contiene il catalogo.

Manifest buildato sonata-tasks:

```toml
dependencies = [
  "sonata-engine @ git+https://github.com/miciav/sonata.git@e81d559952d4a168edffbe3c7108bd9a017046e9",
]
```

Manifest nanolab: sonata-engine resta E; sonata-tasks usa URL diretto con
`@<S>#subdirectory=packages/sonata-tasks` e gli extra effettivamente necessari
in `sonata-tasks[shell,prometheus,multipass,azure,proxmox] @ git+…`.
Nel root nanolab sostituire il source workspace con la sorgente Git equivalente
(`git`, `rev` completo e `subdirectory`). Root e metadata della wheel devono
indicare gli stessi E/S. I puntini e `<S>` qui sono notazione: non sono valori
da committare. Lo script wheel deve controllare che non restino placeholder.

Per prove locali prima che S sia disponibile, usare un file uv temporaneo o
override non committato che selezioni il checkout Sonata. Registrarli come
test di sviluppo, separati dal gate di installazione definitiva. Dopo il pin
finale rigenerare il lock con uv, mai modificando a mano uv.lock.

## 7. Comandi di verifica

Sono comandi da eseguire **nell'implementazione**. La stesura di questo piano
non li ha eseguiti né attesta che oggi siano verdi.

`QN0` (nanolab prima dell'estrazione; cwd root nanolab):

```bash
uv sync --locked --all-packages --all-groups
uv run --locked --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests
```

`QS0` (cwd root Sonata, prima del workspace):

```bash
uv sync --locked --dev
uv run --locked pytest
uv run --locked ruff check .
uv run --locked basedpyright
```

`QS` (cwd root Sonata, dopo creazione workspace):

```bash
uv sync --locked --all-packages --all-groups --all-extras
uv run --locked --all-packages --all-groups --all-extras pytest -c pyproject.toml tests
uv run --locked --all-packages --all-groups --all-extras pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run --locked --all-packages --all-groups --all-extras ruff check src tests
uv run --locked --all-packages --all-groups --all-extras ruff check --config packages/sonata-tasks/pyproject.toml packages/sonata-tasks
uv run --locked --all-packages --all-groups --all-extras basedpyright --project .
uv run --locked --all-packages --all-groups --all-extras basedpyright --project packages/sonata-tasks
uv run --locked --all-packages --all-groups --all-extras lint-imports --config packages/sonata-tasks/.importlinter --no-cache
uv build --package sonata-engine --out-dir dist
uv build --package sonata-tasks --out-dir dist
```

Per test mirati T02–T06 usare gli stessi prefissi/config, limitando ai file di
test del gruppo. La soglia coverage globale si applica alla suite intera; nel
giro mirato usare `--no-cov`, poi eseguire QS completo al completamento dei gruppi.
Non alterare la soglia nel manifest per far passare un sottoinsieme.

`QN` (cwd root nanolab, dopo rimozione membro locale):

```bash
uv sync --locked --all-packages --all-groups
uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests
uv run --locked --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests
uv run --locked --all-packages --all-groups ruff check packages
uv run --locked --all-packages --all-groups basedpyright --project packages/nanolab
uv run --locked --all-packages --all-groups basedpyright --project packages/tui-toolkit
uv run --locked --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --locked --all-packages --all-groups lint-imports --config packages/tui-toolkit/.importlinter --no-cache
uv build --all-packages --out-dir dist
```

I test nanolab che leggono nanoFaaS richiedono NANOFAAS_ROOT configurato come
nella CI esistente, con la revisione prevista da setup-workspace. Non usare un
checkout casuale per dichiarare passata la baseline. Conservare le condizioni
di esecuzione della CI per fork senza accesso alla sorgente privata.

## 8. Journal, rollback e condizioni di arresto

Prima del cambio installazione, verificare i run aperti con gli strumenti già
disponibili nel prodotto. Conservare checkout/lock precedenti per riprendere o
fare teardown di quelle esecuzioni. Il nuovo catalogo crea fingerprint diversi;
non rimuovere il controllo di compatibilità e non riscrivere ricevute/journal.
Usare nuove run directory anche se gli argv visibili sembrano identici.

Il rollback riporta **insieme** manifest, lockfile e versione del codice client
alla revisione precedente. Non prova a riprendere con il vecchio codice i run
creati dalla nuova versione. Le risorse già avviate richiedono la versione che
ne conosce il journal; nessuna migrazione esegue cleanup distruttivi automatici.

Arrestare solo la parte dipendente quando manca un dato necessario: accesso
alla revisione E, pubblicazione di S, sorgente nanoFaaS per il gate end-to-end o
un cambiamento di baseline che altera il contratto. Continuare codice, test con
fake e documentazione indipendenti. Una nuova directory o funzione non richiede
una nuova approvazione progettuale se rientra chiaramente nelle regole della mappa.

## 9. Checklist di completamento

- [ ] Ogni voce dell'inventario ha destinazione implementata; nessun test perso.
- [ ] Solo Sonata distribuisce sonata-tasks; nanolab non ne conserva una copia.
- [ ] Engine indipendente e senza nuove dipendenze di esecuzione.
- [ ] Base catalogo installabile/importabile senza VM, reportistica o nanoFaaS.
- [ ] Opzioni immutabili con un'unica fonte; nessun passthrough perso.
- [ ] Timeout locale verificato, adapter incapaci rifiutano esplicitamente.
- [ ] Ruolo builder funziona; binding assente e risultato malformato falliscono.
- [ ] Fingerprint copre opzioni/binding/chiavi; callable non eseguite a compile.
- [ ] Nessuna nuova idempotenza o possibilità di skip implicita.
- [ ] Default di prodotto trasferiti al client senza rinominare risorse.
- [ ] Audit per responsabilità C01–C08 completato: meccanismi estratti una volta,
      politiche client in adapter/composite/specializzazioni con test separati.
- [ ] Cleanup, compensazione, retention e credenziali VM preservati dai test.
- [ ] Consumer indipendente e nanolab usano la stessa wheel, fuori dai workspace.
- [ ] QN/QS, build, smoke e import isolati completati o gate pendenti dichiarati.
- [ ] E/S coerenti in metadata e lock, nessun placeholder o path personale.
- [ ] Istruzioni journal/rollback e cambi incompatibili consegnati.
- [ ] Registro progressi contiene commit effettivi e risultati, non solo caselle.

La consegna finale deve elencare i due cambi, le verifiche effettuate e gli
eventuali passaggi esterni ancora pendenti. Non dichiarare terminata la migrazione
perché il nuovo albero dei file esiste: deve essere consumabile e verificato.
