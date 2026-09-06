# Task condivisi in Sonata: valutazione e proposta

Data: 2026-09-05. Stato: proposta, non implementazione.

Seguito operativo: [piano esecutivo](2026-09-05-shared-sonata-tasks-implementation-plan.md),
con [analisi e separazione delle contaminazioni](2026-09-05-shared-sonata-tasks-decontamination.md)
e inventario dei file. Il piano fissa le alternative qui discusse e aggiorna il
perimetro: i moduli misti vengono separati per responsabilità, estraendo anche
i meccanismi generici HTTP, k6, Prometheus e Ansible. Le destinazioni indicative
di questa proposta vanno lette insieme alle decisioni esecutive successive.

## Decisione proposta

Portare nel repository Sonata il catalogo di operazioni riutilizzabili, come
distribuzione `sonata-tasks` separata da `sonata-engine`. Organizzare il catalogo
per capacità e strumenti; tenere i meccanismi comuni in piccoli moduli interni.
Lasciare in nanolab configurazione, politiche e workflow specifici di nanoFaaS.

Non ricreare `workflow-tasks`. Non spostare l'attuale `sonata-tasks` in blocco.
Ristrutturazione e trasferimento devono essere un'unica migrazione per gruppi
coerenti, con una destinazione finale definita fin dall'inizio.

Assunzione: i nuovi consumatori usano l'API Python di Sonata. Non sono ancora
noti i loro requisiti concreti. Un client in un altro linguaggio richiederebbe
anche un'interfaccia di processo o servizio: il solo package Python non basta.

## Fonti e stato effettivo

Esaminati la [issue #23](https://github.com/miciav/nanolab/issues/23), senza
commenti al momento della lettura, la proposta del 6 agosto e i checkout:

- nanolab: `d9c0783002c0067c557cbbe9f21dd58556f937b7`;
- Sonata: `a9aa38fb9982751dfe9624ef811ae165a496b206`;
- dipendenza Sonata dichiarata da nanolab: `e81d559`.

I checkout locali non sono una verifica dell'ultimo stato dei branch remoti.
La proposta del 6 agosto è in
`docs/superpowers/specs/2026-08-06-task-restructuring-design.md`.

La issue #23 riguarda precisamente il prerequisito della ristrutturazione:
migrare `nanolab images` ed eliminare il runner legacy. È ancora aperta, ma il
codice ha seguito un percorso diverso: `cc8c245` ha eliminato il comando images
e l'assembler legacy; `8c832ba` ha eliminato `workflow-tasks`; `9d29397` ha
eliminato il bus eventi duplicato. Non va descritto come una migrazione del
vecchio comando con equivalenza funzionale già dimostrata.

Quindi P1 e P2 della vecchia proposta non sono più prerequisiti da realizzare
nella forma originaria. `components/` invece esiste ancora, contiene convenzioni
nanoFaaS e non va considerato automaticamente codice morto.

## Valutazione del design del 6 agosto

| Scelta | Valutazione aggiornata |
|---|---|
| Separare meccanismo e operazioni nominate | Corretta: evita di riscrivere esecuzione, errori e opzioni per ogni tool. |
| Mantenere unità nominate e composite espliciti | Corretta: aiutano composizione e scoperta dell'API. |
| Centralizzare le opzioni | Necessario; va verificato il percorso fino al backend, non solo la firma del wrapper. |
| Lasciare executor e destinazione espliciti | Corretto; i nomi delle destinazioni devono appartenere al client. |
| Ripristinare `workflow-tasks` come package dei meccanismi | Superato e non necessario: due distribuzioni in Sonata bastano. |
| Imporre sottoclassi sottili a tutti i task | Troppo prescrittivo. La forma va scelta in base al comportamento. |
| Convertire tutti i piccoli `run()` in `FnTask` con lambda | Non dimostra un miglioramento: sposta la logica e può peggiorare tracciabilità e identità. |
| Generalizzare subito `ReleasePhaseTask` in `PhaseTask` | Prematuro: ricevute, verifica, idempotenza e chiave di riuso sono un contratto da progettare. |
| Fingerprint basato su argv o nome della callable | Incompleto: non copre configurazione e valori catturati dalle closure. |
| Tenere tutti i composite in nanolab | Non sempre: anche una sequenza generica può essere condivisa. |
| Test su profondità della MRO e assenza di `run` | Vincolano la forma del codice; preferire contratti e comportamento osservabile. |

Le quantità misurate ad agosto non sono un inventario aggiornato. Non le uso
come base per stimare tempi o numero di modifiche.

## Problemi concreti da risolvere prima dell'API condivisa

1. **Dominio incorporato nell'esecuzione.** `execution/roles.py` ammette soltanto
   `host`, `stack`, `loadgen`, `cloud`, `arm-builder`; `RoleBindings` ha campi
   corrispondenti e richiede anche `stack`. Un client con un solo executor locale
   o un ruolo `database` non deve adattarsi a questa topologia.
2. **Installazione e import troppo ampi.** Il manifest richiede tutti gli SDK VM,
   pandas e Plotly; `sonata_tasks/__init__.py` importa eager workflow di prodotto,
   loadtest e VM. Anche importare un sottomodulo Python esegue prima l'initializer
   del package: gli import qualificati da soli non risolvono il problema.
3. **Timeout non applicato dagli adapter.** `CommandTask._spec()` lo conserva,
   ma `HostCommandTaskExecutor` e `VmCommandTaskExecutor` non lo inoltrano e i
   relativi protocolli runner non lo prevedono. Il semplice passthrough proposto
   ad agosto renderebbe configurabile una capacità ancora inefficace.
4. **Fingerprint insufficiente.** `CommandTask` eredita il payload `None`.
   Nell'ambiente locale, due workflow con stesso titolo e argv diversi hanno
   prodotto lo stesso fingerprint. Questo non dimostra da solo uno skip errato:
   la decisione di riuso dipende anche dal tipo di task e dal journal.
5. **Confine di dominio solo sintattico.** Il divieto di importare `nanolab` è
   utile, ma `deployment.py`, `components/helm.py` e `components/images.py`
   contengono namespace, chart e immagini nanoFaaS. Un modulo può dipendere dal
   prodotto anche senza importarne il package.
6. **Contratti permissivi.** `FileTransferTask` usa `Any` per provider e request,
   e tratta un risultato senza `return_code` come successo. I composite registry
   accettano `plan: Any`. Sono contratti da rendere espliciti, non esportare così.
7. **Default divergenti.** `CommandTaskSpec.execution_role` interpreta l'assenza
   di ruolo come `host`; `command_specs_composite` la interpreta come `stack`.
   Nell'API condivisa la destinazione deve avere una sola semantica.

Le verifiche dinamiche dei punti 3 e 4 hanno usato un runner finto: nessun comando
di infrastruttura è stato eseguito. Non è stata eseguita la suite completa, poiché
questa modifica aggiunge solo la proposta.

## Struttura proposta

```text
sonata/                              repository
  src/sonata_engine/                 distribuzione sonata-engine, resta dov'è
  packages/sonata-tasks/
    pyproject.toml                   distribuzione sonata-tasks
    src/sonata_tasks/
      __init__.py                    API minima, senza import degli extra
      core/
        command.py                   CommandTask e opzioni comuni
      execution/
        models.py                    specifica e risultato dei comandi
        ports.py                     protocolli di esecuzione e trasferimento
        bindings.py                  routing opzionale per ruoli del client
        local.py                     adapter del backend locale
      docker.py                      operazioni Docker generiche
      compose.py                     operazioni e resource Compose
      helm.py                        operazioni e resource Helm
      kubectl.py
      gradle.py
      skopeo.py
      cosign.py
      syft.py
      transfer.py
      vm/                            contratti VM e resource
        providers/                   adapter SDK opzionali
      testing.py                     fake pubblici per i consumatori
    tests/

nanolab/
  packages/nanolab/src/nanolab/
    tasks/                           task e composite specifici del prodotto
    plans/                           scenari e composizione applicativa
    release/                         policy e ricevute di release
    ...                              configurazione, CLI, TUI
```

La struttura è indicativa: non creare moduli vuoti o un sottopackage per ogni
tool. Task, configurazione specifica e resource dello stesso strumento possono
stare insieme. Separare file quando la dimensione o le dipendenze lo richiedono.
Non servono gerarchie pubbliche L1/L2/L3, registri plugin o un nuovo DSL.

Direzione delle dipendenze:

```text
nanolab ──────┐
client B ────┼──> sonata-tasks ──> sonata-engine
client C ────┘         │
                      └──> backend / SDK opzionali
```

L'engine continua a possedere compilazione, journal, resume, eventi, `Task`,
`Resource` e `Steps`, senza conoscere Docker, SSH, VM o nanoFaaS. Le dipendenze
specifiche vanno nel catalogo, mai nell'engine.

## Cosa trasferire

| Gruppo attuale | Destinazione e condizione |
|---|---|
| `CommandTask`, modelli e protocolli executor, fake | Sonata, rimuovendo i ruoli di prodotto e chiarendo opzioni e risultati. |
| Docker, Compose, Helm, kubectl, Gradle, skopeo, cosign, syft, buildx | Sonata, mantenendo parametri del tool e rendendo esplicite le policy del chiamante. |
| Registry, tunnel, archive, process, transfer, compensation | Sonata dopo separazione dei default nanoFaaS e dei contratti provider. |
| VM, SDK provider, Ansible | Sonata per contratti e operazioni generiche; selezione da configurazione nanolab e playbook di prodotto restano al client. |
| k6 e acquisizione Prometheus | Estraibili per parti: runner e query generiche; scenari, query nanoFaaS e criteri di successo rimangono al prodotto. |
| `platform`, `function`, `http_function`, `cli_function`, `validate`, `offload`, `deployment`, `components` | Inizialmente `nanolab.tasks` o moduli applicativi equivalenti. Contengono protocollo o convenzioni nanoFaaS. |
| Loadtest, autoscaling, concurrency, report comparativi | Restano al client nella prima estrazione; separare solo capacità richieste da un altro consumatore. |
| `ReleasePhaseTask`, matrice immagini, gate, versioni, ricevute | Restano in `nanolab.release` inizialmente. |
| Composite release | Il ponte spec→Steps è generico; push/inspect e attestazioni possono migrare con input propri tipizzati. La selezione della matrice e le policy restano al client. |
| Telegram observer | Separabile come integrazione opzionale dopo rimozione di convenzioni di ambiente; fuori dal primo gruppo. |

Se più client hanno bisogno proprio delle operazioni nanoFaaS, queste meritano
un package di integrazione dedicato, per esempio `sonata-nanofaas`, distinto dal
catalogo generico. La decisione dipende da quel riuso concreto; non serve
introdurlo preventivamente né replicare il codice tra client.

## API: piccola, esplicita, estendibile

### Operazioni e opzioni

Mantenere le classi nominate quando migliorano scoperta, documentazione e tipi.
Una sottoclasse diretta di `CommandTask` è appropriata per costruire un argv.
Usare funzioni pure private per assemblare parti comuni del comando, senza
costruire livelli di ereditarietà che aggiungono soltanto un prefisso.

Per le opzioni condivise preferisco un oggetto immutabile `CommandOptions`
passato per composizione. Contiene ambiente, directory di lavoro, timeout e
codici accettati; executor, destinazione, titolo e verifica rimangono concetti
espliciti. Le mapping vanno copiate e protette: `frozen=True` da solo non congela
un dizionario. Non duplicare una nuova configurazione accanto alla vecchia spec
senza definire quale sia autorevole e una conversione unica.

Esempio di API proposta, non disponibile oggi:

```python
build_options = CommandOptions(cwd=repo_root, timeout_seconds=600)
task = DockerBuildTask(
    image=image,
    dockerfile="Dockerfile",
    context=".",
    executor=local_executor,
    options=build_options,
)
```

Il client che usa più destinazioni può fornire un executor con routing su
`Mapping[str, CommandTaskExecutor]` e specificare `role="builder"`. I nomi e
l'eventuale enum tipizzato appartengono al client. La mancanza di binding deve
fallire prima di produrre effetti, senza fallback silenzioso su host.

`Unpack[TypedDict]` rimane una buona alternativa se la priorità è conservare i
keyword attuali. Non è sbagliato; l'oggetto opzioni offre una forma più compatta
per condividere configurazione tra molti task. Non offrire permanentemente
entrambe le forme, con ambiguità sulle precedenze. Durante la migrazione un
adapter può convertire i keyword legacy e rifiutare combinazioni conflittuali.

Distinguere il timeout del processo dal `--timeout` di Helm. Un adapter deve
applicare le opzioni dichiarate oppure rifiutarle esplicitamente; questo vale
anche per le differenze tra cwd locale e directory remota. Per esecuzione remota
va definito se il timeout interrompe anche il comando remoto: chiudere soltanto
la connessione non basta a promettere quella garanzia.

### Task Python e fasi

Non imporre `FnTask`. Un `run()` di poche righe, con dipendenze nominate, può
essere la rappresentazione più chiara. Un wrapper generico per callable può
essere aggiunto al catalogo quando utile, senza migrare forzatamente i task e
senza vietare override con test strutturali.

Estrarre in seguito un eventuale `EvidenceTask` solo quando è definito il
contratto comune: input serializzabili, versione semantica, prove prodotte,
verifica, ricevuta atomica e gestione degli errori. Non trasferire implicitamente
`idempotent=True` da `ReleasePhaseTask` a qualsiasi callable. Poter verificare
un risultato e poter ripetere un'operazione interrotta sono proprietà diverse.

### Identità e resume

Distinguere tre cose: identità nel workflow, fingerprint della configurazione,
possibilità di riutilizzare output verificati. Cambiare la classe non è un modo
sufficiente di rappresentare la semantica, e aggiungere un fingerprint non rende
automaticamente un comando riusabile.

Per i comandi statici il payload deve coprire versione dell'operazione, argv,
opzioni e destinazione logica; includere le configurazioni di verifica che
influiscono sulla validità. Serializzazione canonica, mapping ordinati e set
normalizzati. Niente `repr(executor)` o serializzazione permissiva `default=str`.
Credenziali e token non devono comparire in payload diagnostici o journal:
identificare separatamente la configurazione semantica, usando riferimenti
versionati dove appropriato, senza confondere la credenziale con la destinazione.

Per argv dinamici e callable, il qualname non copre closure, configurazione o
modifiche al corpo. Richiedere una chiave/versione semantica esplicita per il
resume supportato; in assenza, rifiutare quel resume senza bloccare l'esecuzione
ordinaria. Il controllo deve funzionare anche dentro `Steps`. Non dedurre la
chiave eseguendo callable durante la compilazione.

L'engine attuale include il percorso Python del tipo nel fingerprint: il
trasferimento di modulo può invalidare i journal. I re-export non preservano
automaticamente `__module__`. La prima migrazione deve dichiarare il limite dei
journal precedenti e gestire prima le risorse trattenute da run aperti. Una
compatibilità trasparente richiede un protocollo esplicito nell'engine e test di
migrazione; non va promessa come effetto del cambio degli import.

## Distribuzione e compatibilità

- Conservare due distribuzioni: engine leggero e catalogo. Usare inizialmente
  extra come `sonata-tasks[azure]` o `[proxmox]`, senza import incrociati eager.
  Non serve pubblicare un package per ogni comando.
- Dipendere obbligatoriamente solo dalle librerie usate dal percorso base.
  Gli SDK restano nei moduli provider. Le dipendenze di reportistica rimaste in
  nanolab non appartengono al nuovo manifest del catalogo.
- Definire una matrice di versioni engine/catalogo/client e provare la versione
  minima supportata. I client fissano revisioni coerenti nel lockfile; una
  dipendenza Git resta accettabile finché non c'è una distribuzione su indice.
- Il nome `sonata-tasks` e il namespace `sonata_tasks` sono già occupati dalla
  distribuzione locale: non installare due copie con proprietari diversi. Prima
  dell'adozione esterna, togliere il membro locale dal workspace e aggiornare le
  dipendenze di nanolab. L'eventuale compatibilità degli import generici vive nel
  nuovo package; non deve importare nanolab al contrario per ripristinare export
  di dominio rimossi.
- Documentare import pubblici, optional extra, eccezioni e contratti di resume.
  Evitare export indiscriminati di tutti i sottopackage nell'initializer.

## Migrazione proposta

1. **Allineare il tracciamento.** Annotare nella #23 il percorso effettivo di
   eliminazione del legacy, verificando se restano requisiti di images non
   coperti. Tracciare il nuovo obiettivo multi-client con un perimetro aggiornato.
   Questo documento non modifica la issue.
2. **Provare il confine su un caso concreto.** Scegliere un workflow nanolab e
   un secondo consumatore. Se il secondo non è ancora disponibile, usare un
   piccolo programma esterno senza nanoFaaS, con destinazione `builder`, come
   verifica provvisoria, senza scambiarlo per validazione dei requisiti reali.
3. **Estrarre il primo gruppo.** Comando, opzioni, risultato, protocolli, backend
   locale, fake e un tool effettivamente usato. Creare direttamente la struttura
   finale in Sonata, con timeout e fingerprint verificati nello stesso gruppo.
4. **Adottarlo in nanolab.** Spostare i moduli di dominio negli spazi applicativi,
   sostituire la distribuzione locale e preservare il comportamento dei workflow.
   Nelle revisioni intermedie non creare dipendenze circolari o namespace doppi.
5. **Trasferire gli altri gruppi coerenti.** Tool, lifecycle generici e provider;
   ogni gruppo arriva con contratti, test e dipendenze opzionali. Composite
   generalizzati soltanto dove il riuso è dimostrabile.
6. **Completare il rilascio.** Test da wheel isolate, matrice delle versioni,
   guida agli import e decisione esplicita sui journal precedenti. Deprecare o
   rimuovere il vecchio codice soltanto dopo adozione dei consumatori coinvolti.

## Criteri di accettazione

- Un consumer usa comando e Docker senza installare nanolab, Plotly o SDK VM.
  Verificarlo in un ambiente pulito da wheel, non soltanto nel workspace uv.
- Il backend finto riceve ogni opzione; una prova locale innocua dimostra che un
  timeout termina davvero il processo secondo il contratto. I backend remoti
  hanno verifiche dedicate alle garanzie che dichiarano.
- I tool mantengono argv, codici accettati e verifiche; keyword sconosciuti e
  opzioni invalide sono intercettati anche dal type checker.
- Un ruolo definito dal client funziona senza modificare Sonata. Un binding
  assente produce errore prima dell'esecuzione.
- Cambiare configurazione semantica cambia fingerprint; riordinare una mapping
  equivalente non lo cambia. Callable con la stessa identità nominale ma chiavi
  semantiche differenti sono distinguibili.
- Test di resume distinguono successo, prove non più valide, interruzione e
  idempotenza; nessuno skip elimina valori runtime necessari a task successivi.
- Le resource conservano cleanup, compensazione su acquire parziale e gestione
  delle risorse esterne; la migrazione non modifica implicitamente l'ownership.
- Contratti di import impediscono engine→catalogo e catalogo→client; una review
  del dominio verifica anche default, percorsi e protocolli impliciti.
- Il workflow nanolab scelto e il consumatore indipendente passano con lo stesso
  artefatto installato. Eseguire poi le suite e i controlli richiesti dai progetti.

Il criterio di riuscita è che un altro client possa importare, configurare,
eseguire e testare un'operazione senza conoscere nanolab. Il numero di classi e
la profondità esatta dell'ereditarietà non sono metriche sufficienti.
