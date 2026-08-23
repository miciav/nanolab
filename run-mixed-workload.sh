#!/bin/bash
# Carico misto — sync, async e una quota di chiavi di idempotenza — a 2x e 3x,
# tre ripetizioni per braccio.
#
# Perche' esiste: ogni run fatto finora era 100% sincrono, e con sync-queue
# spento le due porte finiscono nella STESSA coda per funzione (20 posti, 2
# slot). A traffico solo sincrono, col pool a 5.000 VU, la piattaforma rifiuta
# gia' il 14,9-15,0% degli arrivi a 2x. Quanto di quel budget se lo prende
# l'async e' cio' che questi run misurano.
#
# Il 3x non ha ancora una base sincrona con lo stesso pool: l'unico run a 3x e'
# delle 11:47 del 2026-08-23, con 2.700 VU e il 13,0% degli arrivi trattenuti
# dal generatore. Va aggiunto, o il braccio misto a 3x resta senza confronto.
#
# Le VM restano in piedi alla fine: il teardown e' ./teardown.sh, una decisione
# separata, perche' un run finito male va guardato prima di essere distrutto.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs

for scale in 2 3; do
  echo "$(date '+%F %T') === misto ${scale}x, 3 ripetizioni ==="
  ./nanolab.sh compare \
    "packages/nanolab/scenarios-v2/mixed-workload-load${scale}x.yaml" \
    --environment packages/nanolab/environments/azure-comparison.yaml \
    --run-dir "$R/azure-mixed-${scale}x" --variants jvm-c2 --repetitions 3
  echo "$(date '+%F %T') rc=$?  celle=$(find "$R/azure-mixed-${scale}x" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
done
echo "$(date '+%F %T') === finito; le VM restano in piedi ==="
