#!/usr/bin/env bash
# pycolmap runs that need no Docker, each under the limits a container would have: probe-batch.sh
cd /data/tom/mapperbench
L=$(echo venv/lib/python3*/site-packages/nvidia/cuda_runtime/lib)
one() {  # <project> <name> <seed> <threads> [settings...]
  local P=$1 name=$2 seed=$3 t=$4; shift 4
  [ -e "probe/$P-$name-s$seed.log" ] && grep -q RESULT "probe/$P-$name-s$seed.log" && return
  LD_LIBRARY_PATH=$L systemd-run --user --scope -q -p CPUQuota=$((t * 100))% -p MemoryMax=24G -p MemorySwapMax=0 -p TasksMax=512 \
    venv/bin/python scripts/probe.py runs/$P/database.db projects/$P/photos probe/$P-$name-s$seed $seed $t "$@" \
    > probe/$P-$name-s$seed.log 2>&1
}
for s in 0 1 2 3 4; do one 31de9a59303a default $s 6 & done; wait
for s in 0 1 2; do one e6de7747fab5 global $s 6 kind=global & one 31de9a59303a global $s 6 kind=global & done; wait
for P in 9f3b4b740847 38f31439985f ce7571e0a4a3; do one $P global 0 10 kind=global & done; wait
for s in 0 1 2; do one 9f3b4b740847 default $s 10 & done; wait
grep -h RESULT probe/*.log | sort
