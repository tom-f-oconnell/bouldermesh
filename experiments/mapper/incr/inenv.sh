#!/usr/bin/env bash
# Runs a command in the pipeline's tools (the tree build-env.sh made), for trying a step by hand:
#   inenv.sh [-g] <command...>        -g: with the GPU, holding the benchmark's GPU lock while it runs
# 6 cores and 32 GB, /data/tom/mapperbench as /b, this folder as /incr and the benchmark's scripts as /bench.
set -euo pipefail
B=${BENCH:-/data/tom/mapperbench} T=${BENCH:-/data/tom/mapperbench}/build-incr
HERE=$(cd "$(dirname "$0")" && pwd)
GPU=() LOCK=()
if [ "${1:-}" = -g ]; then shift; GPU=(--gpus all); LOCK=(flock "$B/gpu.lock"); fi
NAME=bench-incr-dev-$$
trap 'docker stop "$NAME" >/dev/null 2>&1 || true' EXIT
"${LOCK[@]}" docker run --rm --name "$NAME" --log-driver none --tmpfs /tmp:exec,size=8g --cpus "${CORES:-6}" \
  --memory 32g --memory-swap 32g --pids-limit 512 "${GPU[@]}" -w /tmp \
  -v "$T/usr:/usr" -v "$T/etc:/etc" -v "$T/opt:/opt:ro" -v "$B:/b" -v "$HERE:/incr:ro" -v "$B/scripts:/bench:ro" \
  -e HOME=/tmp -e LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libcudss/12 -e OMP_NUM_THREADS="${CORES:-6}" \
  --entrypoint nice nvidia/cuda:12.9.1-devel-ubuntu24.04 -n 19 "$@"
