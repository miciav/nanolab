#!/bin/bash
# A/B del salto di thread, 2 bracci x 3 ripetizioni, stessa matrice.
#   A = jvm-c2-hop  (comportamento pre-31a0dac8: salto sempre)
#   B = jvm-c2      (il fix: salto solo con chiave di idempotenza)
# Stesso provisioning, stesso sorgente, stessa immagine a meno di una stringa
# nell'argfile. Le tre ripetizioni servono a dare la dispersione: la sezione 23.1
# ha misurato 5,7x fra due matrici sullo stesso build, quindi un braccio da una
# notte diversa non e' un controllo.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs
echo "$(date '+%F %T') === A/B salto: 2x, 2 core, 2 varianti x 3 ripetizioni ==="
./nanolab.sh compare \
  packages/nanolab/scenarios-v2/runtime-comparison-load2x.yaml \
  --environment packages/nanolab/environments/azure-comparison.yaml \
  --run-dir "$R/azure-ab-hop" --variants jvm-c2-hop,jvm-c2 --repetitions 3
echo "$(date '+%F %T') rc=$?  celle=$(find "$R/azure-ab-hop" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
./teardown.sh
