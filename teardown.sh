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
# Verifica, non annuncio. Il 2026-08-24 questo script ha stampato "teardown
# finito" lasciando in piedi la VM principale, la sua rete e il disco: `az vm
# delete` era fallito e niente lo aveva guardato. Una VM che sopravvive a un
# teardown riuscito costa finche' qualcuno non se ne accorge, ed e' esattamente
# il tipo di guasto che nessuno va a cercare.
residui=$(az resource list -g $G -o tsv --query "[?contains(name,'nanofaas-comparison')].name")
if [ -n "$residui" ]; then
  echo "$(date '+%F %T') TEARDOWN INCOMPLETO, restano:"
  echo "$residui"
  echo "$(date '+%F %T') le risorse qui sopra COSTANO: cancellale a mano"
  exit 1
fi
echo "$(date '+%F %T') teardown verificato: nessuna risorsa residua"
