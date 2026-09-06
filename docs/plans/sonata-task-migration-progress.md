# Stato migrazione task Sonata

Avviata: 2026-09-05.

## Baseline

- nanolab: `d9c0783002c0067c557cbbe9f21dd58556f937b7`, branch `main`;
  working tree contenente soltanto i quattro documenti non tracciati di questa
  migrazione.
- Sonata: `a9aa38fb9982751dfe9624ef811ae165a496b206`, branch `main`, pulito.
- checkout di lavoro Sonata: `/tmp/sonata-task-migration.ziIh5r`, perché il
  checkout adiacente è in sola lettura nel sandbox.
- Python 3.12.3; uv 0.10.9.

## Verifiche iniziali

- Sonata: 224 test passati; ruff e basedpyright passati; copertura 96%.
- sonata-tasks locale: 799 test passati; copertura 91,35%.
- nanolab: 920 test passati, 1 escluso. L'escluso
  `test_build_release_request_requires_credentials_for_execution` arriva prima
  al controllo della working tree nanoFaaS, già sporca, e fallisce con
  `release requires a clean nanoFaaS Git tree` anziché raggiungere l'asserzione
  sulle credenziali. Nessuna modifica applicata al checkout nanoFaaS.
- I sei test del tunnel Prometheus passano fuori dal sandbox; dentro il sandbox
  l'apertura di socket locali è vietata.
- tui-toolkit: 51 test passati; copertura 93,67%.

## Attività

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
- [ ] T09 — wheel e consumer indipendente
- [ ] T10 — CI, documentazione e confini
- [ ] T11 — verifica conclusiva
- [ ] T12 — consegna coordinata (commit Sonata locale creato; pubblicazione necessaria per il lockfile)

## Audit di decontaminazione

| Simbolo | Operazione tecnica | Politica di prodotto | Effetti | Soluzione | Generico | Client | Test |
|---|---|---|---|---|---|---|---|
| `docker_compose_resource` | lifecycle Compose | reset prima del deploy, volumi/orphan rimossi | stato preesistente cancellato | composite client | resource deploy/wait/release | `isolated_compose_resource` | T04A/C01 |
| `K6Task`/`k6_argv` | esecuzione k6 | env `NANOFAAS_URL/PAYLOAD` | protocollo script imposto | adapter client | config e task k6 | resolver nanoFaaS | T04A/C02 |
| task metriche | scrape e verifica | riscrittura 8080→8081/actuator | endpoint dipendente dalla forma | resolver client | URL completo/resolver | actuator resolver | T04A/C03 |
| `HttpStatusCheckTask` | richiesta e status | JSON implicito | body non JSON mal rappresentato | wrapper client | headers/payload espliciti | JSON wrapper | T04A/C04 |
| client Prometheus | query HTTP | retry e aggregazione serie | label perse | adapter client | serie etichettate | somma/retry scenario | T04A/C05 |
| Syft/Cosign | tool containerizzati | formati/tipi release | catalogo rigido | parametri tipizzati | tool configurabili | policy release esplicita | T04A/C06 |
| AnsibleAdapter | argv ansible | playbook/host/provisioning | SDK e asset richiesti | estrazione argv/task | `AnsiblePlaybookTask` | adapter provisioning | T04A/C07 |

## Commit e risultati

Commit Sonata locale: `d1001e1` (`chore: lock sonata task workspace`; contiene il commit
precedente `76781af`, `feat: add reusable sonata task catalogue`). Il package
locale `packages/sonata-tasks` è stato rimosso da nanolab e il consumer punta a
`sonata-tasks` su Sonata a quello SHA.

Gate dopo l'adozione: nanolab `919 passed, 2 failures baseline/infrastrutturali` con
checkout Sonata locale nel `PYTHONPATH`; il secondo fallimento è il test dei confini
che invoca `uv` e non può risolvere uno SHA non ancora pubblicato. Il test baseline
release resta quello della working tree nanofaas sporca. Sonata engine conserva il
test statico preesistente che richiede zero warning; il nuovo package ha lint ruff
pulito. La copertura isolata del nuovo catalogo è sotto il precedente gate finché i
test VM/provider e i composite estratti non vengono riclassificati e portati nel
package Sonata.
