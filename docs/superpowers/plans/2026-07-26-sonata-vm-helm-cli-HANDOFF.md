# Consegna — Task 12 di `2026-07-26-sonata-vm-helm-cli`

Documento per chi riprende il lavoro a freddo. Il piano è
`docs/superpowers/plans/2026-07-26-sonata-vm-helm-cli.md`; lo spec è
`docs/superpowers/specs/2026-07-26-sonata-vm-helm-cli-design.md`.

## Stato

**Sonata** — `main` a `527d042a83ed55b6cc6334885121241146204fcf`, versione **0.2.0**,
pushato. Contiene le tre primitive nuove: `Resource[T]` con valore, `TaskInputs`, e
dipendenze fra risorse. La branch `codex/resource-values` è stata revisionata prima del
merge; i quattro finding Important emersi sono stati corretti e ri-verificati. 171 test.

**nanolab** — branch `codex/sonata-vm-helm-cli`, 11 commit sopra `main` (`9878efe`).
Task 1–11 del piano completi. Il pin punta al commit sonata qui sopra, l'override locale
è stato rimosso, e la suite è stata verificata **contro il pin**, non contro il checkout
locale.

Baseline verde adesso:

| | |
|---|---|
| nanolab | 687 test |
| sonata-tasks | 99.55% (gate 90) |
| workflow-tasks | 93.14% (gate 90) |
| tui-toolkit | 93.67% (gate 80) |
| statici | ruff pulito, basedpyright 0/0 su tutti e quattro, 11 contratti di import |

## Cosa manca: Task 12, più la review finale

### Step 1 — contratto CI statico

Vedi il Task 12 nel piano. Non ancora fatto.

### Step 3 — validazione live contro Multipass

È il pezzo grosso, dieci scenari con VM reale, k3s e Helm:

1. `nanolab plan ...cli.yaml --environment ... --provision`
2. run completo
3. list/invoke riusciti
4. function assente dopo cleanup
5. release Helm assente dopo cleanup
6. VM distrutta dopo cleanup
7. slice `--only invoke-word-stats-java` su VM già preparata
8. fallimento forzato di invoke, con cleanup in ordine function → Helm → VM
9. run `--keep`: VM, Helm e function devono restare
10. pulizia manuale esplicita di ciò che è stato trattenuto

Poi il passaggio TUI, che **richiede una persona ai prompt**: CLI → Kubernetes
(provisioned) → environment Multipass → Run → Cleanup. La preview deve mostrare i 14
task e il dashboard deve ricevere eventi sonata reali per acquire, bootstrap, Helm,
function e release.

### Step 4–5 — ledger di validazione e commit finale

### Review finale dell'intero branch — mai fatta

I task 8–11 hanno avuto ciascuno la sua review, ma il branch **non ha mai avuto una
review complessiva**. Va fatta prima del merge, su modello capace, sul diff
`9878efe..HEAD`. È il passaggio che vede quello che le review per-task non possono
vedere: coerenza fra task, deriva architetturale, problemi emergenti dalla combinazione.

## Comandi

Da `~/Downloads/nanolab`. **`NANOFAAS_ROOT` è obbligatoria** per la suite nanolab: senza,
12 file falliscono in collection, ed è preesistente.

```bash
NANOFAAS_ROOT=~/Downloads/mcFaas uv run --all-packages --all-groups \
    pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests -q
uv run --all-packages --all-groups pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests -q
uv run --all-packages --all-groups pytest -c packages/workflow-tasks/pyproject.toml packages/workflow-tasks/tests -q
uv run --all-packages --all-groups pytest -c packages/tui-toolkit/pyproject.toml packages/tui-toolkit/tests -q
uv run --all-packages --all-groups ruff check packages
uv run --all-packages --all-groups basedpyright --project packages/nanolab
uv run --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv lock --check
```

## Trappole già pagate in questa sessione

- **Mai mettere una pipe sul comando `nanolab run`.** Una `| tail` maschera l'exit code
  e un run fallito sembra riuscito. È successo davvero: un run fallì all'invoke e la
  pipe restituì exit 0. Redirigi su file e stampa `$?` a parte.
- **Un `run_in_background` può non partire affatto.** Un subagent ha atteso uno smoke
  test il cui log restava a 0 byte e senza processo. Se serve l'esito, esegui in
  foreground con timeout ampio.
- **`nanolab plan --provision` legge `~/.ssh` in fase di costruzione del piano**
  (`nanolab/plans/cli.py:124`, e di nuovo via `MultipassVmProvider.__init__`). Non
  rompe la CI — la lettura degrada a `None`, verificato puntando `HOME` a una directory
  senza chiavi — ma un comando che dovrebbe essere inerte tocca credenziali reali.
  Candidato a fix prima che la CI live diventi seria.
- **Se un check live fallisce, è un finding.** Non aggirarlo con argomenti extra e non
  indebolire l'asserzione: l'incremento precedente aveva spedito un gate che non poteva
  passare, ed è esattamente il fallimento contro cui questi controlli esistono.
- **Non toccare il percorso `cli/container`**, né il `provision_environment` legacy: sono
  comportamento approvato.

## Finding differiti, da triagare nella review finale

- **Important** — `nanolab/plans/cli.py:124` legge `~/.ssh` a tempo di costruzione del
  piano (vedi sopra). Appartiene al codice del Task 8.
- `nanolab/plans/cli.py:194` e `:205-207` (rifiuto provider locale, dispatch
  azure/proxmox) non hanno test — è una seconda copia dello stesso ramo non testato che
  esiste già in `provisioning.py:200,204-207`.
- Guardia difensiva `TypeError` a `nanolab/plans/cli.py:130`: mai esercitata.
- `_build_workflow` nel ramo `cli` non passa `dry_run` a `_workflow`/`build_cli_plan`,
  quindi preview e run reale costruiscono in modo identico. Nessun bug osservabile,
  perché sonata compila pigramente.
- Il report del Task 8 dichiara "99 test sonata-tasks" come baseline: il numero vero era
  59, trasposto da "99.52% cov". Solo il report, il codice è corretto.

## Ledger

`.superpowers/sdd/2026-07-26-sonata-vm-helm-cli/progress.md` contiene la cronologia
completa: cosa è stato revisionato, cosa è stato aggiudicato e perché. Non è tracciato da
git. Leggilo prima di ripartire.
