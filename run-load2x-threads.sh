#!/bin/bash
# Stesso braccio del run azure-load2x-verify: 2x, jvm-c2, 2 core, una ripetizione.
# Due variabili cambiate insieme, di proposito:
#   - mcFaas 31a0dac8, che toglie subscribeOn(boundedElastic) dal percorso comune
#   - tre metriche nuove: event loop per thread, jvm_threads_live, runnable
# Non e' un A/B pulito e non pretende di esserlo: le metriche nuove dicono se
# l'ipotesi dei venti thread era giusta, e il confronto con azure-load2x-verify
# dice se toglierli e' servito. Se le due risposte non concordano, si isola.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs
echo "$(date '+%F %T') === 2x, jvm-c2, 2 core, 1 ripetizione, con il fix ==="
./nanolab.sh compare \
  packages/nanolab/scenarios-v2/runtime-comparison-load2x.yaml \
  --environment packages/nanolab/environments/azure-comparison.yaml \
  --run-dir "$R/azure-load2x-threads" --variants jvm-c2 --repetitions 1
echo "$(date '+%F %T') rc=$?  celle=$(find "$R/azure-load2x-threads" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
echo "$(date '+%F %T') === teardown ==="
./teardown.sh
