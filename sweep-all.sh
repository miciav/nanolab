#!/bin/bash
# Sweep the control plane's CPU budget: 4, 3, 2, 1 cores, full matrix each.
# Detached from any agent session on purpose: the previous attempt died when its
# background tasks were killed, taking the caffeinate assertions with it.
# Provisioning is kept between matrices; .dockerignore excludes **/build and
# .gradle, so the docker context is identical and the native RUN layer is a
# cache hit rather than fifteen minutes of native-image.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs

for cpu in 4 3 2 1; do
  echo "$(date '+%F %T') === matrice a ${cpu} core ==="
  # No --fresh: a matrix that already has complete cells resumes into them.
  ./nanolab.sh compare \
    packages/nanolab/scenarios-v2/runtime-comparison-cpu${cpu}.yaml \
    --environment packages/nanolab/environments/azure-comparison.yaml \
    --run-dir $R/azure-matrix-cpu${cpu} \
    --repetitions 3 \
    >> $R/azure-matrix-cpu${cpu}.log 2>&1
  echo "$(date '+%F %T') matrice a ${cpu} core: rc=$?  celle=$(find $R/azure-matrix-cpu${cpu} -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
done

echo "$(date '+%F %T') === teardown ==="
G=maurinoRicerca-rg
az vm delete -g $G -n nanofaas-comparison -y
az vm delete -g $G -n nanofaas-comparison-loadgen -y
for r in nanofaas-comparison-nic nanofaas-comparison-loadgen-nic; do az network nic delete -g $G -n $r; done
for r in nanofaas-comparison-pip nanofaas-comparison-loadgen-pip; do az network public-ip delete -g $G -n $r; done
for r in nanofaas-comparison-vnet nanofaas-comparison-loadgen-vnet; do az network vnet delete -g $G -n $r; done
for r in nanofaas-comparison-nsg nanofaas-comparison-loadgen-nsg; do az network nsg delete -g $G -n $r; done
# Disks outlive their VM as orphans; one pass is never enough.
for pass in 1 2; do
  az disk list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name" \
    | while read -r d; do az disk delete -g $G -n "$d" -y; done
done
echo "$(date '+%F %T') residui:"
az resource list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name"
echo "$(date '+%F %T') fine"
