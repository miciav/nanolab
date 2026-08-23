#!/bin/bash
# Prova del fuoco: UNA cella sincrona a 3x, per vedere se il control plane
# sopravvive al probe dopo il fix delle LoopResources dedicate.
#
# Guidato dal generatore misto con le quote a zero, cosi' la cella misura anche
# la latenza del probe di liveness - la grandezza che decide se sopravvive, e che
# finora era stata solo dedotta.
#
# Dieci minuti invece delle tre ore della matrice completa. Il fix toglie
# l'accodamento dietro gli 863 task per loop, ma non lo stallo da quota CFS
# (68,9% dei periodi al picco di un 2x): se muore lo stesso, il vincolo e' la
# CPU e la matrice completa sarebbe stata spesa per scoprirlo.
# Il rilascio Helm va rimosso PRIMA, non dopo: il tag :jvm-c2 e' mutabile, e un
# rilascio gia' installato con la stessa specifica non innesca alcun rollout. La
# prima prova del fuoco ha misurato per tre minuti il control plane precedente,
# avviato alle 19:36 UTC, senza il fix. imagePullPolicy e' Always, quindi basta
# che il pod sia nuovo.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
ssh -o StrictHostKeyChecking=no azureuser@20.61.67.252 \
  'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; sudo -E helm uninstall nanofaas -n nanofaas-e2e 2>/dev/null; true'
echo "$(date '+%F %T') === prova del fuoco: sync 3x, 1 cella ==="
./nanolab.sh compare \
  packages/nanolab/scenarios-v2/sync-baseline-load3x.yaml \
  --environment packages/nanolab/environments/azure-comparison.yaml \
  --run-dir packages/nanolab/runs/azure-smoke3x --variants jvm-c2 --repetitions 1
echo "$(date '+%F %T') rc=$?"
