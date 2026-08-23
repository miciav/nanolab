#!/bin/bash
# Sweep the control plane's CPU budget after the 4-core matrix finishes.
# Provisioning is deliberately kept between matrices: the images do not depend on
# the CPU limit, so buildkit should turn the later prepares into cache hits.
set -u
R=packages/nanolab/runs
echo "$(date '+%F %T') in attesa della matrice a 4 core (pid $1)"
while kill -0 "$1" 2>/dev/null; do sleep 60; done
echo "$(date '+%F %T') matrice a 4 core terminata"

for cpu in 3 2 1; do
  echo "$(date '+%F %T') === matrice a ${cpu} core ==="
  ./nanolab.sh compare \
    packages/nanolab/scenarios-v2/runtime-comparison-cpu${cpu}.yaml \
    --environment packages/nanolab/environments/azure-comparison.yaml \
    --run-dir $R/azure-matrix-cpu${cpu} \
    --repetitions 3 \
    > $R/azure-matrix-cpu${cpu}.log 2>&1
  echo "$(date '+%F %T') matrice a ${cpu} core: rc=$?"
done

echo "$(date '+%F %T') === teardown ==="
G=maurinoRicerca-rg
az vm delete -g $G -n nanofaas-comparison -y
az vm delete -g $G -n nanofaas-comparison-loadgen -y
for r in nanofaas-comparison-nic nanofaas-comparison-loadgen-nic; do az network nic delete -g $G -n $r; done
for r in nanofaas-comparison-pip nanofaas-comparison-loadgen-pip; do az network public-ip delete -g $G -n $r; done
for r in nanofaas-comparison-vnet nanofaas-comparison-loadgen-vnet; do az network vnet delete -g $G -n $r; done
for r in nanofaas-comparison-nsg nanofaas-comparison-loadgen-nsg; do az network nsg delete -g $G -n $r; done
# Disks outlive their VM as orphans and need a second pass, every time.
for pass in 1 2; do
  az disk list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name" \
    | while read -r d; do az disk delete -g $G -n "$d" -y; done
done
echo "$(date '+%F %T') residui:"
az resource list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name"
echo "$(date '+%F %T') fine"
