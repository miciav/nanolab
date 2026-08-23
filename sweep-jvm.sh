#!/bin/bash
# The JVM 2x2: collector x JIT tiering, at the two CPU budgets that matter -
# 1, where the sweep says G1 wins, and 2, the knee where the JVM stops being
# throttled. No native compilation in this matrix at all, so the prepare is a
# gradle bootJar and four docker builds rather than forty minutes.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs

for cpu in 2 1; do
  echo "$(date '+%F %T') === JVM 2x2 a ${cpu} core ==="
  ./nanolab.sh compare \
    packages/nanolab/scenarios-v2/runtime-comparison-cpu${cpu}.yaml \
    --environment packages/nanolab/environments/azure-comparison.yaml \
    --run-dir $R/azure-jvm-2x2-cpu${cpu} \
    --variants jvm,jvm-g1,jvm-c2,jvm-g1-c2 \
    --repetitions 3 \
    >> $R/azure-jvm-2x2-cpu${cpu}.log 2>&1
  echo "$(date '+%F %T') cpu${cpu}: rc=$?  celle=$(find $R/azure-jvm-2x2-cpu${cpu} -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
done

echo "$(date '+%F %T') === teardown ==="
G=maurinoRicerca-rg
az vm delete -g $G -n nanofaas-comparison -y
az vm delete -g $G -n nanofaas-comparison-loadgen -y
for r in nanofaas-comparison-nic nanofaas-comparison-loadgen-nic; do az network nic delete -g $G -n $r; done
for r in nanofaas-comparison-pip nanofaas-comparison-loadgen-pip; do az network public-ip delete -g $G -n $r; done
for r in nanofaas-comparison-vnet nanofaas-comparison-loadgen-vnet; do az network vnet delete -g $G -n $r; done
for r in nanofaas-comparison-nsg nanofaas-comparison-loadgen-nsg; do az network nsg delete -g $G -n $r; done
for pass in 1 2; do
  az disk list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name" \
    | while read -r d; do az disk delete -g $G -n "$d" -y; done
done
echo "$(date '+%F %T') residui:"
az resource list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name"
echo "$(date '+%F %T') fine"
