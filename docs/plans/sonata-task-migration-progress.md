# Stato migrazione task Sonata

Avviata: 2026-09-05. Ultimo aggiornamento: 2026-09-06.

Stato corrente: **F00–F08 completate e verificate; pin e lock finali risolvono
riferimenti Sonata pubblicati e immutabili**.

Documenti vincolanti:

- [piano originale](2026-09-05-shared-sonata-tasks-implementation-plan.md);
- [mappa sorgenti e asset](2026-09-05-shared-sonata-tasks-migration-map.json);
- [allegato operativo di decontaminazione](2026-09-05-shared-sonata-tasks-decontamination.md);
- [piano di correzione F00–F08](2026-09-06-shared-sonata-tasks-review-fixes.md);
- [inventario eseguibile dei test storici](sonata-task-test-migration.json).

## Riferimenti e ambiente verificati

| Elemento | Riferimento |
|---|---|
| nanolab | branch `main`, HEAD `e9164bebeaf6a127931f1fbe6a87f8adcc555142`, modifiche della migrazione non committate preservate |
| Sonata di partenza | branch `main`, HEAD `d1001e17df32511168796857471c8d1dbad60a81`, checkout adiacente inizialmente pulito |
| catalogo pubblicato S | `5bd4bf805a03f0a8716b0d5d1a5778f9eb4ca345` su `miciav/sonata` `main` |
| checkout di lavoro Sonata | clone isolato di `d1001e17df32511168796857471c8d1dbad60a81` in `/tmp/sonata-review-fixes.oBkUZg/repo` |
| engine vincolato E | `e81d559952d4a168edffbe3c7108bd9a017046e9` |
| nanoFaaS CI | checkout pulito `89811f7e2534080e318b06088ff0a2d600dcb753` |
| baseline storica dei test | nanolab `d9c0783002c0067c557cbbe9f21dd58556f937b7` |
| strumenti | Python 3.12.3; uv 0.12.9 |

Il checkout Sonata isolato è stato usato perché il repository adiacente non era
scrivibile dal sandbox. Dopo i controlli, le modifiche sono state trasferite nel
checkout reale e pubblicate; il fix del manifest che preserva E per i consumer è
il commit finale S indicato sopra.

## Risultati storici, non usati come certificazione corrente

La prima migrazione dichiarava 799 test nel package locale rimosso, 224 test
engine, 920 test nanolab con un'esclusione e 51 test tui-toolkit. Segnalava inoltre
un test release fermato dalla working tree nanoFaaS sporca e test socket eseguibili
soltanto fuori dal sandbox. Questi dati descrivono la sessione precedente; i gate
seguenti sono stati rieseguiti da baseline pulite e non ereditano quei risultati.

## Stato F00–F08

| Attività | Stato | Evidenza |
|---|---|---|
| F00 | superata | SHA, working tree, versioni e checkout isolati registrati; risultati storici separati dai gate correnti |
| F01 | superata | 79 file e 799 node ID raccolti dal commit storico isolato; inventario JSON completo |
| F02 | superata | 9 regressioni HTTP coprono successo, recupero 409, mismatch, JSON invalido, GET fallito, altro errore e journal/resume |
| F03 | superata | mapping locale/remoto centralizzato e testato su host, SSH e provider VM, inclusi root, sottodirectory, spazi e percorsi non mappabili |
| F04 | superata | chiavi semantiche complete e canoniche; test di equivalenza, invalidazione, processi distinti, opacità dei segreti e resume |
| F05 | superata | dipendenze dirette dichiarate, otto asset Ansible inclusi, tre wheel installate insieme in ambiente pulito |
| F06 | superata | inventario riconciliato, 347 test catalogo e 507 test task nanolab; copertura catalogo 90,46%; 101/101 destinazioni presenti |
| F07 | superata | CI separata engine/catalogo/wheel, sei varianti wheel, gate statici e architetturali, plan e smoke container verificati |
| F08 | superata | S pubblicato; metadata e lock nanolab risolvono catalogo S ed engine E; sync locked, build wheel e installazioni pulite completate |

## Riconciliazione dei test storici

La collection isolata del commit `d9c0783` contiene esattamente 799 node ID e
79 file Python: 75 moduli test e 4 `__init__.py`. Ogni node ID ha una voce in
`sonata-task-test-migration.json` con origine, responsabilità, riferimenti T/C,
destinazione e motivazione.

| Esito inventario | Casi storici |
|---|---:|
| ported | 650 |
| split | 76 |
| replaced | 73 |
| totale | 799 |

Le suite correnti raccolgono 347 node ID in `sonata-tasks` e 507 sotto
`packages/nanolab/tests/tasks`. I totali correnti non sono usati come prova di
equivalenza numerica: split e sostituzioni sono giustificati voce per voce
nell'inventario. La mappa operativa contiene 85 voci e 101 destinazioni; il
controllo di esistenza sui due checkout restituisce `missing=0`.

## Audit di decontaminazione C01–C07

| Caso | Meccanismo in Sonata | Policy nel client | Evidenza |
|---|---|---|---|
| C01 Compose | deploy, wait e release generici | reset iniziale e cleanup volumi/orphan in nanolab | `sonata_tasks/tests/test_compose.py`, `test_composites.py`; test migrati delle risorse nanolab |
| C02 k6 | argv, configurazione ed esecuzione k6 | risoluzione URL e payload nanoFaaS | `sonata_tasks/tests/test_tool_catalog.py`; `nanolab/tests/tasks/migrated/test_k6.py` |
| C03 metriche | URL/resolver generico e task di verifica | mapping actuator 8080→8081 e aggregazione scenario | `sonata_tasks/tests/test_metrics.py`; test release/loadtest nanolab |
| C04 HTTP | header, payload e status espliciti | manifest e recupero registrazione funzione | `sonata_tasks/tests/test_adapters.py`; `test_http_function_migration.py` |
| C05 Prometheus | serie etichettate e query HTTP | retry e aggregazione del caso d'uso | `sonata_tasks/tests/test_prometheus.py`; `test_prometheus_adapter_migrated.py` |
| C06 Syft/Cosign | tool parametrizzabili | formati e policy della release | `sonata_tasks/tests/test_syft.py`, `test_cosign.py`; test release nanolab |
| C07 Ansible | argv e `AnsiblePlaybookTask` | asset, host e provisioning nanolab | test VM/catalogo; test migrati bootstrap, provisioning e asset wheel |

La ricerca nei sorgenti runtime di `sonata-tasks` non trova riferimenti di prodotto
nanoFaaS. Le stringhe nanoFaaS rimaste nei test servono a verificare l'assenza di
dipendenze e il comportamento di migrazione del consumer.

## Gate correnti

Tutti i comandi elencati hanno exit code 0. Gli override in `/tmp` sono stati
usati solo per controlli sorgente; le prove di distribuzione finali sono state
eseguite da wheel e pin Git pubblicati, senza editable o `PYTHONPATH`.

| Ambito | Comando o controllo | Ambiente | Risultato |
|---|---|---|---|
| Sonata engine | pytest con coverage | checkout Sonata, installazione engine-only locked | 224 passed, coverage 96%; `sonata_tasks` non importabile nel job engine |
| Sonata catalogo | pytest con coverage, soglia invariata | checkout Sonata, `uv sync --locked --all-packages --all-groups --all-extras` | 347 passed, coverage 90,46% |
| nanolab completo | pytest | checkout nanolab, nanoFaaS pulito allo SHA CI, socket consentiti | 1431 passed |
| nanolab task | pytest `packages/nanolab/tests/tasks` | workspace corrente | 507 passed |
| tui-toolkit | pytest con coverage | workspace corrente | 51 passed, coverage 93,67% |
| lint | `ruff check` | Sonata engine/catalogo e nanolab | verde |
| tipi | `basedpyright` | Sonata engine/catalogo, nanolab e tui-toolkit con configurazione esplicita | 0 errori |
| architettura | `lint-imports` | Sonata, nanolab e tui-toolkit | 2 + 3 + 1 contratti verdi |
| SonarQube | SonarScanner 8.0.1 / Community Build 26.7 | nanolab: 266 file Python; Sonata: 76 file engine + catalogo | 0 issue aperte in entrambi i progetti |
| lock Sonata | `uv lock --check`, poi `uv lock` e `uv sync --locked` | checkout Sonata | lock rigenerato da uv dopo i pin SDK; sync locked verde |
| plan | cinque costruzioni CLI/container/k8s | workspace nanolab | tutte verdi, incluso ordine VM/Helm e assenza del task registry legacy |
| smoke reale | workflow container completo | Docker locale, porte 18080/18081 | 24 passi verdi; cleanup completo, porte chiuse, nessun container `nanofaas-*` |
| pin finali | `uv lock --check`; `uv sync --locked --all-packages --all-groups --all-extras` | workspace nanolab | catalogo `5bd4bf80`; engine `e81d5599`; risoluzione e sync verdi |
| distribuzione finale | build delle tre wheel; installazioni pulite consumer e catalogo | due venv nuove fuori dai checkout | CLI/import/asset verdi; `direct_url.json` conferma S per catalogo ed E per engine |

## Verifica delle distribuzioni

Sono state costruite le wheel `sonata_tasks-0.2.0`, `nanolab-0.1.0` e
`tui_toolkit-0.1.0`. L'installazione congiunta in un virtualenv pulito ha risolto
51 distribuzioni, ha eseguito `nanolab --help` da `/tmp`, ha importato i tre
package da `site-packages` e ha letto tutti gli otto asset Ansible inclusi.

Una seconda installazione pulita del solo catalogo base ha importato API pubblica,
Docker e Helm ed eseguito `examples/shared_tasks_client.py`; non ha installato o
caricato nanolab, httpx, pandas, plotly o SDK provider. La matrice dello script
`scripts/check_task_wheel.py` ha superato separatamente `base`, `shell`,
`prometheus`, `multipass`, `azure` e `proxmox`. Per ciascun extra l'import del
modulo opzionale atteso è riuscito.

## Attività originali rivalidate

- [x] T00 — baseline e inventario iniziale
- [x] T01 — package Sonata
- [x] T02 — modelli, executor e fake
- [x] T03 — CommandTask e fingerprint
- [x] T04 — strumenti
- [x] T04A — separazione meccanismi/politiche
- [x] T05 — resource, trasferimenti e composite
- [x] T06 — VM ed extra opzionali
- [x] T07 — dominio nel package nanolab
- [x] T08 — adozione API e rimozione package locale
- [x] T09 — wheel e consumer indipendente
- [x] T10 — CI, documentazione e confini
- [x] T11 — verifica conclusiva
- [x] T12 — consegna coordinata e verifica dei riferimenti pubblicati

## Chiusura F08 e riferimenti pubblicati

Il catalogo è pubblicato in Sonata a S =
`5bd4bf805a03f0a8716b0d5d1a5778f9eb4ca345`; l'engine resta vincolato a E =
`e81d559952d4a168edffbe3c7108bd9a017046e9`. I due manifest nanolab espongono
gli URL PEP 508 completi e il root impone E sull'intero grafo, evitando che la
sorgente workspace del monorepo remoto sostituisca il contratto pubblicato.

`uv lock` ha aggiornato il catalogo da `d1001e17` a `5bd4bf80`; `uv lock
--check` e il sync bloccato sono verdi. Le tre wheel sono state ricostruite. In
un ambiente pulito, l'installazione della wheel nanolab ha acquisito il catalogo
da S e l'engine da E; CLI, import e asset Ansible sono verdi. In un secondo
ambiente pulito, la wheel del catalogo ha acquisito autonomamente engine E.
Le due installazioni sono separate perché passare anche una wheel locale del
catalogo al resolver introdurrebbe una seconda URL in conflitto con S.

## Bozza aggiornamento issue #23

> Completate e verificate F00–F08 del piano di recupero. I 799 test storici sono
> riconciliati in un inventario node-by-node; le suite correnti passano con 347
> test Sonata tasks (90,46% coverage), 507 test task nanolab e 1431 test nanolab
> complessivi. Sono verdi lint, type checking, contratti di import, cinque plan,
> smoke container reale e la matrice wheel base + cinque extra. Le 101
> destinazioni della mappa esistono e l'audit C01–C07 mantiene le policy di
> prodotto in nanolab. F08/T12 è chiusa: Sonata S è pubblicato, il lock nanolab
> conserva engine E e risolve catalogo S, e le installazioni finali da wheel sono
> state verificate fuori dai checkout.

La bozza di aggiornamento della issue non è stata pubblicata automaticamente.
