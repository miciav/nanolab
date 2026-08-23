#!/bin/bash
# Quanta memoria costano N VU preallocate, con QUESTO script.
# Endpoint morto: le richieste falliscono subito, ma le VU vengono allocate lo
# stesso - l'allocazione avviene all'avvio, non alla prima iterazione.
S=/Users/micheleciavotta/Downloads/nanolab/.worktrees/dispatch-instrumentation/packages/nanolab/assets/k6/runtime-comparison.js
for n in "$@"; do
  NANOFAAS_URL=http://127.0.0.1:1 K6_MAX_VUS=$n K6_RATE_SCALE=2.0 \
    k6 run --quiet "$S" >/dev/null 2>&1 &
  pid=$!
  peak=0
  for _ in $(seq 1 22); do
    rss=$(ps -o rss= -p $pid 2>/dev/null | tr -d ' ')
    [ -n "$rss" ] && [ "$rss" -gt "$peak" ] && peak=$rss
    sleep 0.5
  done
  kill $pid 2>/dev/null; wait $pid 2>/dev/null
  python3 -c "print(f'{$n:>6} VU/scenario ({2*$n:>6} totali): picco {$peak/1048576:6.2f} GiB  ({$peak*1024/(2*$n)/1048576:5.2f} MiB per VU)')"
done
