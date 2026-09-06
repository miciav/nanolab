# Allegato esecutivo: separare meccanismi e politiche di prodotto

Parte vincolante del [piano di implementazione](2026-09-05-shared-sonata-tasks-implementation-plan.md).
Eseguire come attività **T04A**, dopo T04 e prima T05; le modifiche client vengono
integrate con T07–T08. Le sigle OLD, S, N, OT, ST, NT hanno il significato del piano.

## Regola di analisi

La classificazione di un file non basta. Per ogni classe/funzione pubblica,
esaminare anche helper transitivi, costruttore, default, run, verifica e cleanup.
Nel registro progressi compilare queste colonne:

```text
simbolo | operazione tecnica | decisioni di prodotto | effetti collaterali
        | soluzione | simbolo generico finale | adattamento client | test
```

Le responsabilità possono essere mescolate anche senza nomi nanoFaaS: cancellare
prima di distribuire, sommare serie con etichette diverse, imporre JSON o decidere
che una soglia fallita sia comunque un risultato da riportare sono scelte da
riconoscere. La presenza di un parametro non dimostra da sola una buona separazione.

Usare questo ordine di scelta:

1. **Parametro tipizzato** per una variante nativa dello stesso strumento.
2. **Funzione pura estratta** per trasformazioni riutilizzabili senza lifecycle.
3. **Composizione/adapter** quando si combina un meccanismo con una policy o si
   risolve una configurazione specifica del client.
4. **Sottoclasse nominata** quando rappresenta realmente una specializzazione
   sostituibile, conserva contratto/risultati/errori e aggiunge parametri propri.
   Non usare override di run per reinserire di nascosto cleanup o azioni ulteriori.
5. **Task distinto** quando cambiano semantica dell'esecuzione, risultato o
   ownership. Non forzare l'ereditarietà solo perché due operazioni lanciano curl.

Una specializzazione client dipende dal generico; il generico non importa mai
il client, non lo riconosce con isinstance e non contiene flag `nanofaas_mode`.
Non aggiungere hook before/after universali quando una funzione/composite basta.

## C01 — Compose: distribuire non significa azzerare l'ambiente

**Evidenza:** OLD/compose.py::docker_compose_resource esegue
DestroyDockerCompose **prima** di DeployDockerCompose; il destroy usa --volumes
e --remove-orphans. La policy di isolamento degli esperimenti contamina sia
acquire sia release, non soltanto la firma del comando down.

**Implementazione:**

- S/compose.py contiene DeployDockerCompose, WaitForDockerCompose,
  DestroyDockerCompose e docker_compose_resource generici.
- DestroyDockerCompose riceve remove_volumes=False e remove_orphans=False,
  entrambi espliciti anche nella resource. Il destroy esplicito rimane possibile.
- La resource generica **non esegue down prima di up**. La sua acquisizione è
  deploy→readiness; conserva compensazione su acquisizione parziale e release.
  Documentare che gestisce il progetto affidatole dal chiamante: non promette
  di preservare un progetto preesistente se viene scelto come risorsa gestita.
- N/compose.py::isolated_compose_resource compone la sequenza storica
  clear→deploy→wait con gli stessi Task/Steps generici e compensazione. Non
  copia argv, parser o executor. Passa True per volumi e orphan sia al clear
  sia al teardown, conserva titoli/ordine precedenti.
- Tutti i plan nanolab che usavano docker_compose_resource passano al composite
  locale. Una distribuzione permanente usa DeployDockerCompose, senza resource
  che le faccia teardown al termine.

**Test:** generico acquire non emette down e release non include --volumes né
--remove-orphans per default; composite nanolab riproduce la sequenza precedente;
fallimento a ciascun passaggio conserva compensation e ownership documentate.

## C02 — k6: runner generico, protocollo dello script nel client

**Evidenza:** OLD/k6.py::k6_argv inserisce sempre NANOFAAS_URL e, opzionalmente,
NANOFAAS_PAYLOAD. K6Task risolve il target di prodotto e il config mescola
parametri k6 con il protocollo dello script.

**Destinazioni:** S/k6.py, S/k6_models.py; N/k6.py e N/loadtest/models.py.

- S/k6_models.py contiene K6Stage, K6RunResult e un K6Config generico con
  script_path, summary_output_path, stages, env, vus, duration. Env copiata e
  protetta; nessun target_url o payload_path. Mantenere i tipi/valori del risultato.
- S/k6.py::k6_argv(config) costruisce solo argomenti k6 e le variabili env
  esplicitamente fornite. Non riserva nomi di variabili per un'applicazione.
- S/k6.py::K6Task riceve config statico oppure Callable[[TaskInputs], K6Config],
  executor/role/options/title/semantic_key e require_pass=False. Il resolver
  dinamico segue le regole di chiave obbligatoria del piano e viene invocato
  solo a run. Fingerprint include config statico o chiave dinamica, require_pass,
  opzioni e binding. Conservare timestamps e gestione codici 0/99/errore.
- I codici 0/99 sono parte del contratto di questo task k6, non personalizzabili
  attraverso expected_exit_codes: rifiutare un override differente dal default
  delle options e costruire internamente la spec con {0,99}. require_pass decide
  esplicitamente se 99 solleva oppure produce passed=False.
- N/loadtest/models.py conserva il **config di scenario** precedente, con
  target_url/payload_path, importando K6Stage/K6RunResult da Sonata.
  N/k6.py::K6Task è un adapter tramite sottoclasse del task generico: nel
  costruttore prepara un resolver che converte il config di scenario nel config
  generico, inserendo NANOFAAS_URL/PAYLOAD e risolvendo la resource a runtime.
  Non ridefinisce run; il risultato è sostituibile e il meccanismo resta unico.
- Mantenere N/k6.py::k6_argv come adapter puro per gli eventuali chiamanti
  esistenti; delega alla funzione generica. La precedenza delle variabili env
  di prodotto rimane quella precedente, coperta da test client.

**Test:** script estraneo a nanoFaaS con env TARGET_URL e nessuna NANOFAAS_*;
stages/vus/duration e codice 99; adapter nanolab con target statico e Resource,
stesse variabili e argv precedenti. Il consumer autonomo di T09 include questo
task con fake: la decontaminazione non è validata dal solo import Docker.

## C03 — Metriche: endpoint e traduzione delle porte appartengono al client

**Evidenza:** OLD/metrics.py::_resolve_prometheus_url trasforma :8080 in :8081
e aggiunge /actuator/prometheus quando l'endpoint è una Resource. Lo stesso
task si comporta quindi diversamente in base alla forma dell'input.

**Destinazioni:** S/metrics.py e N/metrics.py.

- S/metrics.py riceve URL completo stringa oppure resolver Callable[[TaskInputs],
  str] con chiave semantica. Non altera schema, host, porta o path.
- Trasferire metric_sum, verifiche delle soglie, PrometheusScrapeCheckTask e
  PrometheusMinimumCheckTask. Le soglie/etichette configurano l'operazione
  generica; nessun nome di metrica specifico viene aggiunto dalla libreria.
- N/metrics.py contiene il resolver di endpoint actuator e wrapper di
  compatibilità che lo iniettano. Conservare la precedente distinzione stringa
  versus Resource per nanolab, senza portarla nel generico.
- Documentare il perimetro del parser attuale: non dichiararlo parser completo
  OpenMetrics né ampliarne silenziosamente la semantica durante l'estrazione.

**Test:** endpoint https://metrics.example:9443/custom/metrics rimane identico
sia statico sia dinamico; stesse soglie e risultati. Nel wrapper client solo
la Resource control-plane subisce la trasformazione storica. Test con metrica
job_requests_total e label tenant estranee a nanoFaaS.

## C04 — HTTP: controllo dello status indipendente dal protocollo nanoFaaS

**Evidenza:** HttpStatusCheckTask è generico ma vive in OLD/http_function.py e
impone Content-Type JSON quando c'è payload. Il resto del modulo contiene
contratti /v1/functions e verifiche di dominio.

**Destinazioni:** S/http.py; N/http_function.py.

- Trasferire Endpoint (str | Resource[str]), endpoint_argv (oggi _argv) e
  HttpStatusCheckTask in S/http.py. Il helper mantiene solo risoluzione dell'URL.
- Il task riceve headers: Mapping[str,str], vuota per default, e payload
  opzionale. Nessun Content-Type implicito. La presenza del body usa `is not
  None`, così anche stringa vuota è un body deliberato. Preservare il controllo
  status con curl senza -f. Mantenere le opzioni comuni e chiave della verifica
  derivata da expected_status e configurazione.
- N/http_function.py contiene un wrapper che passa Content-Type JSON per il
  payload dove il protocollo client lo richiede; gli altri task di funzione
  importano il helper generico ma conservano URL e verifiche nanoFaaS.
- Non estrarre HttpFunctionContractTask come se fosse un controllo HTTP universale.

**Test:** GET 204, POST testuale con Content-Type text/plain, body vuoto, status
502 accettato come aspettativa, errore trasporto distinto da mismatch status;
gli assert JSON/offload preesistenti restano a nanolab.

## C05 — Prometheus: trasporto, aggregazione e diagnostica sono responsabilità diverse

**Evidenza:** OLD/loadtest/prometheus.py fissa tentativi/backoff, cattura ogni
Exception e somma serie per timestamp perdendo le etichette. In
loadtest/tasks.py::_unreachable_hint un timeout viene attribuito categoricamente
alla raggiungibilità sulla base di osservazioni del laboratorio.

**Destinazioni:** S/prometheus.py, N/loadtest/{prometheus,adapters,tasks}.py.

- S/prometheus.py contiene HttpPrometheusClient e modelli immutabili
  PrometheusSeries(labels, samples), PrometheusSample(timestamp, value) e
  PrometheusRetryPolicy(attempts=1, backoff_seconds=0). Httpx è extra prometheus;
  questo modulo non viene importato dal package root.
- Il client riceve base_url, timeout_seconds=20, retry_policy e transport/client
  HTTP iniettato. query_range(expr, start, end, step_seconds) restituisce serie
  separate con etichette e campioni. server_time conserva la query time().
- Retry solo per errori di trasporto httpx, con numero tentativi configurato;
  controllare status HTTP, envelope e forma della risposta. JSON malformato,
  errori di query e errori di programmazione non diventano retry indiscriminati.
  Conservare esplicitamente le semantiche speciali dei valori Prometheus già
  supportate; non riutilizzare la validazione JSON finita dei fingerprint sui
  campioni restituiti dalla query.
- N/loadtest/prometheus.py conserva query_prometheus_range_series come adapter
  che somma per timestamp, con la funzione pura sum_series_by_timestamp locale.
  Configura attempts=3 e backoff_seconds=2 per preservare la policy nanolab.
  N/loadtest/adapters.py espone il protocollo storico agli snapshot/report.
- CapturePrometheusSnapshot e le sue scelte di finestra/attesa/artefatto restano
  nel client, usando il trasporto estratto. Non duplicare la richiesta HTTP.
- Correggere _unreachable_hint nel client: «il timeout può indicare problemi
  di raggiungibilità; verificare …», senza affermare che lentezza del server sia
  impossibile. La libreria riporta causa tecnica e contesto, non diagnosi certe
  della rete dell'utente.

**Test:** due serie con label diverse restano due in Sonata e vengono sommate
solo dall'adapter nanolab; timeout con retry configurato; risposta non JSON,
errore HTTP e query invalida distinti; nessun accesso rete nei test (MockTransport).
Test snapshot/report esistenti invariati nei risultati, salvo diagnostica corretta.

## C06 — Syft/Cosign: lo strumento non coincide con la policy di release

**Evidenza:** SyftTask fissa SPDX JSON e immagine eseguibile; CosignTask fissa
predicate type custom, sbom type spdx e immagine container. Sono scelte in parte
lecite come default del tool, ma non devono impedire altri usi.

- S/syft.py: aggiungere tool_image=SYFT_IMAGE e output_format="spdx-json".
  Il nome del file viene da output_path, non dal formato. Conservare mounting
  e autenticazione. N/release_composites.py passa esplicitamente formato/immagine
  usati nelle prove di release e li include nei phase inputs rilevanti.
- S/cosign.py: aggiungere tool_image=COSIGN_IMAGE, predicate_type="custom",
  sbom_type="spdx"; passarli ai builder. Nessun tipo del predicate vincolato a
  una ricevuta nanoFaaS. Le operazioni non previste continuano a essere rifiutate.
- Il segreto continua a essere letto da file; non introdurre password nell'argv
  per rendere la firma più semplice. Nessuna generalizzazione del sistema di
  autenticazione oltre questi parametri.
- Pin/tool version e output format entrano naturalmente nell'argv/fingerprint;
  se il client li usa per riuso di fase devono entrare anche nella sua chiave.

**Test:** Syft con altro formato/immagine, Cosign con altro predicate type;
identiche operazioni di release quando il client passa la policy storica.
I test di quoting e protezione password restano obbligatori.

## C07 — Ansible: estrarre l'invocazione, conservare il provisioning nel client

**Evidenza:** OLD/infra/ansible.py::AnsibleAdapter combina costruzione del comando
ansible-playbook con ricerca host Multipass, playbook distribuiti, registry e k3s.

- Creare S/ansible.py::AnsiblePlaybookTask(CommandTask), parametri playbook: Path,
  inventory: str, user: str, private_key_path: Path|None, extra_vars:
  Mapping[str,str], più opzioni comuni. Il task costruisce l'argv; inventory,
  percorso assoluto del playbook e configurazione Ansible sono forniti dal client.
- La variabile ANSIBLE_CONFIG viaggia in CommandOptions.env; nessuna ricerca
  implicita dei playbook nanoFaaS e nessun import di un SDK VM nel task generico.
- Estrarre anche build_ansible_argv puro nello stesso modulo. AnsibleAdapter
  locale lo usa nel proprio percorso shell esistente, preservando il tipo di
  risultato e il dry-run, oppure usa il task dove già il chiamante richiede Task;
  non cambiare il contratto dell'adapter per forzare ovunque un workflow.
- N/infra/ansible.py conserva risoluzione host, percorsi asset e metodi
  provision_base/install_k3s/setup_registry. Nessun playbook di prodotto in Sonata.

**Test:** playbook arbitrario in tmp_path, inventory non VM, utente esplicito,
extra vars con spazi e cwd/env; nessun riferimento a registry/k3s/MultiPass.
I test di provisioning client verificano gli stessi argv e asset precedenti.

## C08 — Controllo di sostituibilità e regressioni

Per ogni C01–C07 scrivere due gruppi di test: contratto generico su un problema
estraneo a nanoFaaS e comportamento storico del wrapper client. Il generico
deve funzionare senza configurazione del prodotto; la specializzazione non
deve indebolire le precondizioni/garanzie documentate del tipo base.

Nel registro audit, classificare anche gli altri simboli della mappa: le
decisioni già previste per ruoli, registry, VM, Gradle e archive restano
obbligatorie. Non chiudere una voce con «sembra generico»: indicare quali
effetti, default e dipendenze sono stati letti e quale test copre il confine.

Se una separazione non conserva il contratto, usare un adapter o un Task
distinto. Una classe di dominio che compone un generico non deve copiare la
sua esecuzione. Un flag che attiva un intero scenario di prodotto nel generico
è un fallimento della separazione, anche se elimina un import proibito.

**Gate T04A:** C01–C08 verificati, inventario aggiornato e nessuna duplicazione
dei meccanismi estratti tra Sonata e nanolab. Il trasferimento dei file da solo
non soddisfa questo gate.
