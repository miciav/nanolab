# Consegna — chiusura di `2026-07-26-sonata-vm-helm-cli`

Documento per chi riprende il lavoro a freddo. Il piano è
`docs/superpowers/plans/2026-07-26-sonata-vm-helm-cli.md`; lo spec, inclusa la fonte
durevole dell'evidenza di validazione, è
`docs/superpowers/specs/2026-07-26-sonata-vm-helm-cli-design.md`.

## Stato corrente

**Sonata** — `origin/main` contiene il commit immutabile
`527d042a83ed55b6cc6334885121241146204fcf`, versione `0.2.0`, con
`Resource[T]`, `TaskInputs` e dipendenze fra risorse. La candidata è stata
revisionata, i finding sono stati risolti e 171 test sono passati.

**nanolab** — usare il checkout normale sul branch `codex/sonata-vm-helm-cli`,
forkato da `main` a `9878efe`. Non creare un worktree. Tasks 1–11 e Task 12
Steps 1–4 sono completi. Il manifest e il lock puntano allo SHA Sonata sopra, senza
override locale.

Il gate statico completo post-fix è verde:

| Gate | Evidenza |
|---|---|
| nanolab | 690 test |
| `sonata-tasks` | 69 test, 99,55% |
| `workflow-tasks` | 475 test, 93,43% |
| `tui-toolkit` | 51 test, 97,11% |
| statici | Ruff verde; basedpyright 0 errori/0 warning su tutti e quattro |
| confini | 11/11 contratti di import |
| riproducibilità | lock su 61 package; sdist e wheel dei due package verdi |
| topologia | container 9, k8s non provisioned 5, provisioned 14, slice 7 |

La validazione CLI live Task 12 da 1 a 10 è verde. Cleanup, slice, fallimento
forzato, Keep e cleanup manuale hanno prodotto le sequenze e le postcondizioni
richieste. Anche la walkthrough TUI manuale interattiva è completa: 14 fasi, tutte
`success`, eventi Sonata reali e nessuna istanza Multipass residua.

I dettagli autosufficienti — comandi/gruppi, exit code, conteggi, sequenze,
postcondizioni e classificazione dei difetti corretti — sono nel
`Ledger di validazione` della spec. I percorsi `/private/tmp` citati lì sono soltanto
diagnostica locale transitoria e non servono per valutare il branch.

## Unico lavoro rimanente

Task 12 Step 5 resta intenzionalmente aperto:

1. eseguire la review complessiva sul diff `9878efe...HEAD`;
2. risolvere e ri-verificare gli eventuali finding;
3. marcare Step 5 soltanto dopo la chiusura della review;
4. usare il flusso di finishing del branch per scegliere merge locale, PR o
   conservazione del branch.

Non creare la PR prima di questa review. Non sono pendenti altri gate statici,
scenari live, aggiornamenti del ledger o walkthrough TUI.

## Difetti trovati e risolti durante Task 12

- La compilazione del piano provisioned scandiva `~/.ssh`. La discovery delle
  credenziali è ora differita all'acquire reale; il piano resta puro e i default
  legacy sono preservati.
- Una VM pulita non trovava nel registry localhost le immagini control-plane e
  function. Era un difetto dell'incremento: ora si usano le immagini ufficiali GHCR
  allineate alla versione Gradle, verificate per `amd64` e `arm64`, con copertura di
  tutte le mappature pubblicabili usate da `PublishPlan`.

Non restano difetti live irrisolti emersi da Task 12.

## Note operative

- **Non mettere una pipe su `nanolab run`.** Maschera l'exit code del processo;
  redirigere eventualmente su file e acquisire il codice del comando direttamente.
- Per una suite nanolab locale, impostare `NANOFAAS_ROOT` al checkout mcFaas.
- Il percorso `cli/container` e `provision_environment` legacy sono contratti
  preservati, non cleanup da eseguire durante la chiusura.
- `.superpowers/sdd/2026-07-26-sonata-vm-helm-cli/progress.md` è una cronologia locale
  non tracciata: non modificarla né includerla nei commit.
