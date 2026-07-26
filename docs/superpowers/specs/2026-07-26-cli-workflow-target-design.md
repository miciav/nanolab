# Dare un bersaglio al workflow `cli` (solo sonata)

**Data:** 2026-07-26
**Stato:** design da approvare
**Precede:** [2026-07-25-sonata-migration-cli-workflow-design.md](2026-07-25-sonata-migration-cli-workflow-design.md) — eredita il contratto C1–C6

## Il problema

Il workflow `cli` deve parlare con un control plane, ma nessuno gli garantisce che esista
e che sia raggiungibile. Non ha mai avuto un bersaglio funzionante, in **nessuna**
configurazione — scoperto eseguendolo dal vivo per la prima volta il 2026-07-26:

- **In locale:** `preflight_control_plane` sonda `<url>/actuator/health` sull'URL
  dell'API. Ma l'actuator sta su una porta separata (`management.server.port: 8081`
  contro `server.port: 8080`). Il preflight quindi riceve 404 e aborta il run, anche con
  un control plane perfettamente sano. Nessun test l'ha mai visto perché **tutti**
  simulano `urlopen`: pinnano la forma della guardia, non la correttezza dell'URL.
- **Su k8s:** `validate` deploya il control plane come `ClusterIP`, mentre `cli` gira
  sull'host (`cli_role="host"` sempre). Nessuna raggiungibilità esterna, e in più una
  corsa fra `fn apply` e `invoke` — il Deployment ci mette ~12s e nessuno aspetta.
- Lo scenario dichiara `backend: k8s`, ma `build_cli_plan` **ignora** `config.backend`.
  Lo scenario dice già dove puntare e nessuno lo ascolta.

Sono quattro facce dello stesso problema: il workflow presuppone un bersaglio che nessuno
gli fornisce.

## Il pezzo grosso esiste già

`validate` con `backend: container` risolve lo stesso problema da tempo, e bene:

- `nanolab/plans/validate.py::_container_control_plane` usa
  `workflow_tasks.components.container.managed_process_resource` per avviare
  `java -jar platform/control-plane/build/libs/app.jar` con
  `--server.port=18080 --management.server.port=18081`,
  `--nanofaas.deployment.default-backend=container-local`,
  `--nanofaas.container-local.runtime-adapter=docker`;
- `managed_process_resource` **aspetta già la readiness** (`ready()` sondato fino a 90
  volte a intervalli di 1s) e garantisce la terminazione del processo;
- il suo `health_url` è `http://127.0.0.1:18081/actuator/health` — la porta di management,
  cioè quella giusta;
- le immagini delle function vengono costruite in locale (`function.build_argv`).

Quindi questo incremento è per la maggior parte **riuso**, non codice nuovo.

## La forma della soluzione

### D1 — `backend` conta anche per `cli`

`build_cli_plan` smette di ignorare `config.backend`:

- **`backend: container`** → control plane locale su Docker, avviato dal workflow.
  Diventa il caso normale: nessuna VM, nessun k8s, uno smoke test che gira in una
  ventina di secondi. Endpoint `http://127.0.0.1:18080`, calcolato, non passato a mano.
- **`backend: k8s`** → richiede un `--control-plane-url` esplicito, come oggi. Il
  provisioning di VM e piattaforma è **fuori scope** (vedi "Rimandato").
- **`backend: pool`** → non supportato da `cli`; errore esplicito.

### D2 — Il control plane locale è una `Resource` sonata

```python
Resource(
    title="Acquire local control plane",
    acquire=start_and_wait_ready,
    release=stop,
)
```

Il compiler inserisce l'avvio prima del primo consumer e lo spegnimento dopo l'ultimo, e
lo spegne anche se un task fallisce. È la traduzione uno-a-uno del `ResourceTask` legacy
di `managed_process_resource`.

**Non serve nessuna primitiva nuova di sonata.** L'acquire non deve produrre un valore:
la porta è fissa e nota (18080/18081), quindi i due gap registrati nello spec precedente —
`Resource` che produce un valore, e consumo dell'outcome fra task — non entrano in gioco.
Questo è ciò che distingue questo incremento da quello su VM e helm.

Ordine compilato atteso con `backend: container` e una function:

```
001.build-nanofaas-cli
002.acquire-local-control-plane   <- Resource.acquire (avvia + attende readiness)
003.build-image-word-stats-java
004.acquire-word-stats-java       <- Resource.acquire (fn apply)
005.list-functions
006.invoke-word-stats-java
007.release-word-stats-java
008.release-local-control-plane   <- Resource.release (termina il processo)
```

Le due risorse si annidano correttamente da sole: il control plane viene rilasciato per
ultimo perché è il primo acquisito, e sonata rilascia in ordine inverso.

### D3 — Il preflight sparisce dal percorso sonata

Con D2 la readiness del control plane è garantita dall'acquire della `Resource`, che è il
posto giusto: chi acquisisce la risorsa è responsabile di consegnarla pronta.
`preflight_control_plane` diventa ridondante per `backend: container` e va rimosso dal
percorso `cli`.

Per `backend: k8s` resta un controllo di raggiungibilità, ma **corretto**: deve sondare la
porta di management, che in k8s è un service distinto e quindi va indicata
esplicitamente, non dedotta dall'URL dell'API.

Qualunque cosa resti, deve avere **almeno un test che parla con un control plane vero**.
La lezione di questo bug è che una guardia di rete testata solo con `urlopen` simulata
non è testata.

### D4 — L'attesa dopo `fn apply`

Con `container-local` il control plane ha già `readiness-timeout` e
`readiness-poll-interval` propri, quindi `fn apply` potrebbe bloccarsi fino a container
pronto e far sparire la corsa. **Va verificato, non assunto.** Se `apply` ritorna prima
che la function risponda, l'acquire della risorsa-function deve attendere — come fa
`managed_process_resource` per il processo.

Su `backend: k8s` la corsa c'è di sicuro (misurata: ~12s) e la stessa attesa la risolve.

## Fuori scope

- **Legacy.** Si tocca solo il percorso sonata. I workflow `validate`, `loadtest`,
  `offload`, `offload-loadtest` restano invariati, `preflight_control_plane` incluso per
  quanto li riguarda.
- **Provisioning di VM e piattaforma per `backend: k8s`.** Vale la pena farlo — oggi
  bisogna alzare lo stack a mano con `validate --provision` e passare l'URL — ma è un
  incremento a sé. Nota: la chart helm **supporta già** NodePort su entrambe le porte
  (`controlPlane.service.type` + `nodePorts.http`/`nodePorts.actuator`) e
  `components/helm.py` ha già il flag `expose_node_port`. Non manca niente alla recipe:
  manca chi lo accende per `cli`.
- **VM e helm come task sonata.** Incremento architetturale separato, bloccato sui due
  gap dell'engine.

## Verifica

- Suite `sonata-tasks` con executor finti: ordine compilato con `backend: container`,
  annidamento delle due risorse, control plane fermato anche quando un consumer fallisce.
- Un test che sonda la salute di un control plane vero, non simulato (vedi D3).
- `nanolab run scenarios-v2/cli-container.yaml` a freddo, su una macchina con Docker e
  senza niente in ascolto: deve passare senza argomenti aggiuntivi. È il criterio di
  chiusura — lo stesso errore di prima non deve poter tornare.
