#!/bin/bash
# Un solo braccio: 2x del profilo, una ripetizione, per verificare che le
# metriche aggiunte oggi rispondano davvero. L'immagine del 3x non conteneva
# NettyServerMetricsConfig (953a1c3b, 12:18, contro uno snapshot delle 11:47),
# quindi netty_* e http_server_requests_* non sono mai state raccolte su Azure.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs
echo "$(date '+%F %T') === carico 2x, 1 ripetizione ==="
./nanolab.sh compare \
  packages/nanolab/scenarios-v2/runtime-comparison-load2x.yaml \
  --environment packages/nanolab/environments/azure-comparison.yaml \
  --run-dir "$R/azure-load2x-verify" --variants jvm-c2 --repetitions 1
echo "$(date '+%F %T') rc=$?  celle=$(find "$R/azure-load2x-verify" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
echo "$(date '+%F %T') === le VM restano in piedi: il teardown e' una decisione, non una coda ==="
