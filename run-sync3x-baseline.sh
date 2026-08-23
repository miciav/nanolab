#!/bin/bash
# La base sincrona a 3x che manca, col pool di VU dichiarato.
#
# L'unico run a 3x e' delle 11:47 del 2026-08-23: pool derivato di 2.700 VU, con
# il 13,0% degli arrivi trattenuti dal generatore. e2d06bf ha dichiarato 5.000
# per scenario alle 14:31, e da li' in poi ogni run tiene 10.000 e non scarta
# nulla — ma nessuno a 3x. Senza questi tre run il braccio misto a 3x misura
# qualcosa che non si puo' confrontare con niente.
#
# Tre ripetizioni, stesso scenario e stesso ambiente del misto: cambia solo il
# generatore, che qui e' quello puramente sincrono.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs
echo "$(date '+%F %T') === base sincrona 3x, pool dichiarato, 3 ripetizioni ==="
./nanolab.sh compare \
  packages/nanolab/scenarios-v2/runtime-comparison-load3x.yaml \
  --environment packages/nanolab/environments/azure-comparison.yaml \
  --run-dir "$R/azure-sync3x-baseline" --variants jvm-c2 --repetitions 3
echo "$(date '+%F %T') rc=$?  celle=$(find "$R/azure-sync3x-baseline" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
echo "$(date '+%F %T') === finito; le VM restano in piedi ==="
