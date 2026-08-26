#!/bin/bash
# Load sweep on serial+C2 at two cores: 1x, 2x, 3x, 4x the profile.
#
# The 1x arm is not redundant with the archived matrices. The same build at the
# same budget measured p95 3.7ms in one matrix and 21.1 in another, so a control
# from a different night is not a control (section 23.1).
#
# What each arm is for: 2x and 3x walk toward the two predicted walls, and 4x
# sits just past the concurrency one - two slots over 0.526ms of service caps a
# function near 3,799 rps, while the control plane's own CPU model puts its
# ceiling near 5,157. Whichever arrives first, the prediction is quantitative.
set -u
export NANOFAAS_ROOT=/Users/micheleciavotta/Downloads/mcFaas/.worktrees/dispatch-instrumentation
cd /Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation || exit 1
R=packages/nanolab/runs

for s in 1 2 3 4; do
  echo "$(date '+%F %T') === carico ${s}x ==="
  ./nanolab.sh compare \
    packages/nanolab/scenarios-v2/runtime-comparison-load${s}x.yaml \
    --environment packages/nanolab/environments/azure-comparison.yaml \
    --run-dir "$R/azure-load${s}x" --variants jvm-c2 --repetitions 3 \
    >> "$R/azure-load${s}x.log" 2>&1
  echo "$(date '+%F %T') ${s}x: rc=$?  celle=$(find "$R/azure-load${s}x" -name k6-summary.json 2>/dev/null | wc -l | tr -d ' ')"
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
