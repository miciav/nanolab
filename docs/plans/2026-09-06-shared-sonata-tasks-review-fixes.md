# Piano esecutivo delle correzioni alla migrazione dei task

Data: 2026-09-06. Stato: **F00–F08 eseguite e verificate; catalogo Sonata
pubblicato e pin finali validati**.

Questo piano risolve i sei finding della review della migrazione locale
nanolab `e9164be` / Sonata `d1001e17df32511168796857471c8d1dbad60a81`.
Prima di eseguirlo verificare HEAD e modifiche locali: questi riferimenti
identificano il lavoro revisionato, non autorizzano reset dei checkout.

Documenti vincolanti:

- [Piano originale](2026-09-05-shared-sonata-tasks-implementation-plan.md):
  contratti §3, dipendenze §6, attività T00–T12 e gate QS/QN.
- [Mappa sorgenti e asset](2026-09-05-shared-sonata-tasks-migration-map.json):
  destinazioni e split; non contiene l'inventario dei test, da aggiungere separatamente.
- [Allegato operativo di decontaminazione](2026-09-05-shared-sonata-tasks-decontamination.md):
  separazione fra meccanismi e policy, casi C01–C07.
- [Registro di avanzamento](sonata-task-migration-progress.md): evidenze e gate pendenti.

Questo documento specifica il recupero della migrazione e non sostituisce i
contratti originali. Una correzione non può riportare policy nanoFaaS in Sonata.
Le correzioni sono state implementate secondo l'ordine seguente; il
[registro](sonata-task-migration-progress.md) contiene evidenze e limiti dei gate.

## Ordine e risultato atteso

Eseguire F00 → F01 → F02 → F03 → F04 → F05 → F06 → F07 → F08.
F01 recupera l'inventario e i test necessari a rendere osservabili le regressioni;
F06 ne completa il porting. Non attendere la fine per aggiungere le regressioni
specifiche di F02–F05. Chiudere ciascuna attività soltanto con evidenza del suo gate.

| Attività | Finding | Risultato | Attività originale da rivalidare |
|---|---|---|---|
| F00 | avanzamento incoerente | baseline riproducibile e stati corretti | T00–T08 |
| F01, F06 | 4, test persi | ogni test storico ha una destinazione verificata | T02–T08 |
| F02 | 1, recupero HTTP | registrazione idempotente funzionante | T07–T08 |
| F03 | 2, directory VM | opzioni coerenti con il target | T02, T08 |
| F04 | 5, fingerprint | configurazione e verifica invalidano il riuso | T03, T08 |
| F05 | 3 e 6, distribuzioni | dipendenze e asset completi | T06–T07, T09 |
| F07 | prevenzione ricadute | gate CI e consumer indipendente | T09–T11 |
| F08 | consegna | pin e lock generati da riferimenti disponibili | T12 |

Percorsi: `S` e `ST` indicano sorgenti e test del catalogo nel repository Sonata;
`N` e `NT` indicano `packages/nanolab/src/nanolab/tasks` e
`packages/nanolab/tests/tasks` nel repository nanolab.

## F00 — Rendere affidabile lo stato iniziale

1. Leggere le istruzioni dei repository; registrare branch, SHA e stato Git di
   entrambi. Preservare le modifiche estranee. Se Sonata non è scrivibile,
   preparare il lavoro in un checkout isolato sotto un percorso consentito.
2. Registrare Python, uv, modalità di installazione e commit engine effettivo.
   L'engine consumato da nanolab resta E =
   `e81d559952d4a168edffbe3c7108bd9a017046e9`; non aggiornare E per aggirare errori.
3. Correggere il registro: T07/T08 non completati; T02–T06 da rivalidare nei
   rispettivi gate; T09–T12 pendenti. Conservare i risultati storici distinguendoli
   da verifiche correnti. Un errore dovuto al nuovo pin non pubblicato non è baseline.
4. Riprodurre separatamente i controlli interessati, dal cwd del rispettivo
   package e con configurazione esplicita per gli strumenti statici. Annotare
   override locali e variabili d'ambiente: non certificano la distribuzione finale.
5. Per ogni problema preesistente riportare comando, errore e confronto con la
   baseline. Non modificare nanoFaaS per sbloccare il controllo della working tree.

**Gate:** registro con stati verificabili, nessuna dichiarazione di completamento
fondata soltanto su import via PYTHONPATH o su log di esecuzioni precedenti.

## F01 — Recuperare i test e costruire la tracciabilità

1. Estrarre in una directory temporanea i test da nanolab commit
   `d9c0783002c0067c557cbbe9f21dd58556f937b7`, percorso
   `packages/sonata-tasks/tests`. Non ripristinare il vecchio package nel workspace.
2. Recuperare anche fixture, conftest, configurazione e dipendenze di test.
   Raccogliere i node ID nel contesto storico isolato; confrontarli con i 799 test
   riportati nella baseline e spiegare eventuali differenze di raccolta.
3. Creare `docs/plans/sonata-task-test-migration.json` con una voce per node ID:
   origine e commit, destinazioni repo/file/node ID, responsabilità coperta,
   riferimento Txx/Cxx, stato e motivazione di eventuale sostituzione.
   Un test parametrizzato deve conservare tracciabilità dei casi.
4. Assegnare i test secondo T02–T07 e C01–C07: generici in ST, dominio in NT,
   misti separati con le rispettive fixture. Non distribuire in base al solo nome
   del modulo e non eliminare assert per adattarsi alla nuova implementazione.
5. Portare subito i test HTTP, binding e fingerprint necessari a F02–F04;
   registrare i restanti come pendenti per F06. Adattare import, monkeypatch e
   API esplicite; non simulare moduli mancanti per nascondere dipendenze errate.

**Gate:** tutti i 79 file rimossi sono contabilizzati; ogni test storico ha una
destinazione o una sostituzione motivata. Il solo conteggio finale non prova equivalenza.

## F02 — Ripristinare il recupero della registrazione HTTP

**File:** `N/http_function.py`, test HTTP recuperati in NT.

1. Aggiungere una regressione: POST con exit 22 seguito da GET con manifest
   corrispondente. Il test deve fallire prima del fix con l'AttributeError osservato.
2. Nel secondo `CommandTaskSpec` usare le opzioni di `self.options`, ad esempio
   mediante `dataclasses.replace`, cambiando soltanto i codici attesi in `{0}`.
   Preservare env, cwd, remote_dir e timeout; non aggiungere alias legacy al core.
3. Cercare negli override e composite migrati altri accessi alle proprietà rimosse
   di CommandTask e correggere i casi equivalenti, distinguendoli da campi validi
   di altre classi. Preservare il contratto di TaskResult e dei codici attesi.
4. Coprire POST riuscito, registrazione già presente uguale, manifest diverso,
   JSON non valido, GET fallito e altro errore POST. Verificare che il recupero
   controlli l'identità e non trasformi qualsiasi errore in successo.
5. Verificare numero/ordine dei comandi, URL, ruolo e opzioni inoltrate.

**Gate:** test HTTP verdi, nessun AttributeError e nessuna modifica alla policy
di idempotenza originale. Nessuna chiamata HTTP reale necessaria.

## F03 — Correggere la semantica delle directory per i target remoti

**File:** nanolab `cli/execution.py`, `plans/*.py`, `N/cli.py`,
`N/cli_function.py`, altri producer di CommandOptions; test execution bindings.

1. Inventariare tutti i producer di cwd/remote_dir per host, stack, cloud e
   load generator. Per ciascuno distinguere cwd del launcher locale e directory
   del comando remoto; usare il contesto di composizione che conosce entrambi.
2. Applicare T08.4: cwd del launcher va al runner; directory del progetto va
   convertita tramite il mapping `remote_path_for_local` in remote_dir.
   Percorsi esterni/non mappabili devono produrre un errore esplicito.
3. Centralizzare il mapping nel client evitando conversioni sparse. Conservare
   le restrizioni del catalogo: VM rifiuta cwd locale, host rifiuta remote_dir.
   Non scartare cwd silenziosamente e non riutilizzare il path host come remoto.
4. Testare con i veri adapter e runner fake: host, SSH, provider VM, root del
   progetto, sottodirectory, cwd del launcher, directory non mappabile.
   Aggiungere percorsi con spazi e verificare directory e argv effettivi.
5. Costruire i workflow CLI/container/k8s interessati e attraversare il routing
   fino al runner fake. RecordingExecutor da solo non rileva questa regressione.
   Verificare anche le opzioni timeout rifiutate dagli adapter secondo §3.4.

**Gate:** nessun comando VM riceve cwd locale; launcher e comando usano le
directory previste. Nessun provisioning reale.

## F04 — Completare i fingerprint semantici

**File:** `S/core/command.py`, `S/core/fingerprint.py`,
factory generiche e client che costruiscono argv/verify dinamici; test ST e NT.

1. Inventariare le closure argv e verify, incluso endpoint HTTP, manifest,
   backend/prefix attesi, payload, repliche, bootstrap, metriche e composite.
   Registrare dati catturati e loro rappresentazione nella chiave.
2. Aggiungere la regressione riprodotta: stesso manifest/endpoint, backend atteso
   diverso deve produrre fingerprint diverso. Ripetere per prefix e verificatori
   degli altri task, anche quando argv è statico.
3. Comporre chiavi versionate usando la canonicalizzazione condivisa. Includere
   configurazione serializzabile e identificatori logici delle risorse dinamiche.
   Per endpoint dinamico, modificare immagine/payload/limiti deve cambiare la chiave.
4. Non usare repr, ordine d'inserimento, indirizzi oggetto, qualname o bytecode
   come identità. Non eseguire closure né acquisire risorse durante il fingerprint.
   Non introdurre credenziali in chiavi leggibili o log: rispettare il contratto
   di fingerprint e trattamento dei valori sensibili del piano originale.
5. Testare sia differenze semantiche sia equivalenze: dizionari riordinati,
   oggetti ricostruiti e processi distinti devono dare lo stesso fingerprint.
6. Aggiungere un test di integrazione con il journal/resume dell'engine:
   configurazione invariata compatibile, verifica/configurazione cambiata non
   riutilizzata silenziosamente. Conservare la policy di incompatibilità dei
   journal precedenti alla migrazione; non riscriverli.

**Gate:** tutti i callable inventariati sono coperti da una chiave completa;
test di stabilità, invalidazione e assenza di side effect verdi.

## F05 — Riparare dipendenze e inclusione degli asset

**File:** pyproject root e member nanolab, pyproject del catalogo,
`N/infra/ansible.py`, `N/infra/ansible_assets`, verifiche wheel.

1. Costruire la tabella import runtime → distribuzione → dipendenza diretta/extra.
   Includere import dinamici e percorsi di CLI, report, provisioning e provider.
2. Dichiarare in nanolab httpx, pandas, plotly, pydantic-settings, shellcraft e
   SDK effettivamente importati; scegliere versioni/pin dai manifest storici e
   dal contratto §6. Richiedere gli extra Sonata per le funzionalità consumate.
   Non spostare queste dipendenze nel catalogo base per far passare nanolab.
3. Dichiarare package-data per tutti i file Ansible necessari, anche annidati.
   Confrontare file per file con gli asset della mappa; includere cfg, playbook,
   template e requirements ove presenti. Escludere cache e file temporanei.
4. Verificare il resolver degli asset dal package installato con cwd esterno
   al repository. Testare la costruzione del comando Ansible con runner fake.
5. Costruire wheel catalogo/nanolab/tui-toolkit e controllare METADATA e contenuto
   ZIP. Installarle con dipendenze in ambienti puliti, senza editable, PYTHONPATH
   o `--no-deps`. Eseguire `nanolab --help`, import runtime e accesso a ogni asset.
6. Prima che il commit Sonata sia disponibile usare un ambiente di sviluppo
   esplicitamente isolato per le prove locali. La prova sui pin Git finali resta
   obbligatoria in F08; un override temporaneo non certifica il lock di consegna.

**Gate:** import e asset disponibili fuori dai checkout; catalogo base leggero
e senza dipendenze di prodotto; installazione finale da pin ancora distinta
da eventuali prove preliminari locali.

## F06 — Completare la suite recuperata e l'audit di decontaminazione

1. Portare tutti i test pendenti di F01. Per gli split verificare entrambi i
   lati: contratto generico ST e policy nanolab NT, con riferimento C01–C07.
2. Eseguire provider VM con fake e test degli extra in interpreti separati.
   Ripristinare test di report, loadtest, provisioning, release e risorse nel client.
3. Eseguire collection delle suite nuove e confrontarla con l'inventario:
   nessuna origine senza esito; accorpamenti/eliminazioni hanno motivazione e
   copertura sostitutiva identificata. Non imporre uguaglianza numerica se gli
   split introducono più test, ma spiegare ogni perdita di casi.
4. Rieseguire il gate di copertura del catalogo senza abbassare il 90%, ridurre
   i sorgenti misurati o aggiungere esclusioni per compensare test mancanti.
5. Verificare tutte le destinazioni della mappa e le sette separazioni
   dell'allegato. Se emerge una violazione, correggerla e aggiungere una regressione
   nel lato proprietario prima di segnare completata la relativa Txx.

**Gate:** inventario completamente riconciliato, suite recuperate verdi e
copertura catalogo ≥90%; confini e policy dell'allegato verificati.

## F07 — Rendere permanenti i controlli e rivalidare la migrazione

1. Completare T09/T10: consumer autonomo Sonata, matrice wheel base/extra,
   test confini e collegamento dei nuovi test ai job CI corretti.
2. Eseguire QS/QN del piano originale dalle directory previste, con engine E
   e con engine del checkout Sonata come richiesto. Includere engine e tui-toolkit;
   usare la configurazione esplicita dei package per lint e type checking.
3. Eseguire i comandi plan e lo smoke container previsti da T11. Un controllo
   non eseguibile resta pendente con motivo; non equivale a un successo.
4. Controllare che CI non perda responsabilità del job del vecchio package,
   che non restino import invalidi e che il catalogo non dipenda da nanolab.
5. Aggiornare registro, inventario test e stato del piano originale con risultati
   correnti: comando, cwd, SHA, ambiente, exit code e posizione del log.
6. Eseguire `git diff --check` nei due checkout e review dei diff complessivi.

**Gate:** tutti i gate locali previsti superati o esplicitamente pendenti;
nessun finding dichiarato chiuso con il solo passaggio dei test del consumer.

## F08 — Preparare la consegna coordinata e validare i pin finali

1. Preparare cambi reviewabili separati: Sonata per catalogo/test/CI; nanolab
   per adapter, policy, test, dipendenze, asset e documentazione. Non riscrivere
   i commit esistenti né pubblicare come effetto della sola pianificazione.
2. Dopo gli eventuali aggiustamenti della review, fissare lo SHA completo S
   del catalogo. Pubblicarlo soltanto nell'ambito dell'autorizzazione della sessione.
3. Quando S è disponibile sul remoto, aggiornare dipendenze e sorgenti secondo
   §6 del piano originale, con subdirectory `packages/sonata-tasks` ed engine E.
   Rigenerare `uv.lock` tramite uv: vietate modifiche manuali del lock.
4. Verificare risoluzione e sync bloccata con il lock generato. Ripetere le
   installazioni wheel sui riferimenti finali in ambienti nuovi fuori dai checkout.
   Nessun riferimento personale, editable o override locale nella consegna.
5. Se manca disponibilità remota, consegnare i diff locali e indicare esattamente
   quali controlli di installazione/pin restano pendenti. Non dichiarare la
   migrazione conclusa né attribuire quel limite alla baseline.

**Gate finale:** sei finding chiusi con prove; gate originali rivalidati;
test storici riconciliati; wheel e lock installabili dai riferimenti consegnati.

## Checklist per la review finale

- [x] F00: avanzamento corretto e baseline riproducibile.
- [x] F01/F06: inventario completo, test recuperati, copertura ≥90%.
- [x] F02: recupero HTTP e casi negativi verificati.
- [x] F03: mapping directory testato attraverso gli adapter reali.
- [x] F04: fingerprint completi e resume verificato.
- [x] F05: dipendenze dichiarate e asset presenti nella wheel.
- [x] F07: CI, confini, consumer indipendente e gate originali verificati.
- [x] F08: Sonata S pubblicato; metadata, lock e installazioni finali verificati
  con catalogo S ed engine E.

Per ogni casella riportare nel registro l'evidenza; quelle senza evidenza
restano aperte. La soglia di copertura non sostituisce l'audit delle responsabilità.
