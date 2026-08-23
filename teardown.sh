#!/bin/bash
set -u
G=maurinoRicerca-rg
echo "$(date '+%F %T') === teardown ==="
az vm delete -g $G -n nanofaas-comparison -y
az vm delete -g $G -n nanofaas-comparison-loadgen -y
for r in nanofaas-comparison-nic nanofaas-comparison-loadgen-nic; do az network nic delete -g $G -n $r; done
for r in nanofaas-comparison-pip nanofaas-comparison-loadgen-pip; do az network public-ip delete -g $G -n $r; done
for r in nanofaas-comparison-vnet nanofaas-comparison-loadgen-vnet; do az network vnet delete -g $G -n $r; done
for r in nanofaas-comparison-nsg nanofaas-comparison-loadgen-nsg; do az network nsg delete -g $G -n $r; done
# I dischi sopravvivono alla VM come orfani, e una passata non basta mai.
for pass in 1 2 3; do
  az disk list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name" \
    | while read -r d; do [ -n "$d" ] && az disk delete -g $G -n "$d" -y; done
done
echo "$(date '+%F %T') residui:"
az resource list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name"
echo "$(date '+%F %T') teardown finito"
