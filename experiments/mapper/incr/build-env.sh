#!/usr/bin/env bash
# The tools the incremental pipeline (boulder.py) runs in, as a tree on /data like the benchmark's (build.sh), with
# pycolmap built against its COLMAP, so that Python gets the GPU bundle adjustment the command line has (PyPI's
# wheel has a Ceres without CUDA).
#   build-env.sh [bench dir] [full|plain]        JOBS=<cores> (6)
# full (build-incr-full, the one to time with): everything built here with -O3 -march=x86-64-v3 (AVX2 and FMA, on any
#   CPU since about 2015): Ceres master with SuiteSparse, METIS, OpenBLAS, CUDA and cuDSS; COLMAP 4.2.1 (patched:
#   patch_colmap.py) with CUDA, Caspar, ONNX Runtime's CUDA provider and OpenMP; pycolmap; OpenCV 4.14; OpenMVS 2.4
#   with CUDA and OpenMP. Ends by checking that each of those is really in (verify-env.sh) and fails if one isn't.
# plain (build-incr): the benchmark's "plain" tree as it is (no SuiteSparse in Ceres, baseline x86-64) plus pycolmap,
#   for trying things before full is built.
# Sources are copied from the benchmark's tree without its build folders; nothing in that tree is changed.
# boulder.py runs its steps in build-$INCR_TOOLS (incr-full unless set).
set -euo pipefail
B=${1:-/data/tom/mapperbench} V=${2:-full} JOBS=${JOBS:-6} IMG=nvidia/cuda:12.9.1-devel-ubuntu24.04
T=$B/build-incr; [ "$V" = full ] && T=$B/build-incr-full
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$T/opt" "$T/tmp"; chmod 1777 "$T/tmp"
if [ ! -d "$T/usr" ]; then
  ionice -c3 nice -n19 cp -a "$B/build-plain/usr" "$B/build-plain/etc" "$B/build-plain/var" "$T/"
fi
for src in colmap $([ "$V" = full ] && echo ceres opencv openMVS vcglib cgal6); do
  if [ ! -d "$T/opt/$src" ]; then
    mkdir -p "$T/opt/$src"
    (cd "$B/build-plain/opt/$src" && tar cf - --exclude='./build*' .) | (cd "$T/opt/$src" && tar xf -)
  fi
done
NAME=bench-incr-build-$V
trap 'docker stop "$NAME" >/dev/null 2>&1 || true' EXIT
timeout 6h docker run --rm --name "$NAME" --log-driver none --cpus "$JOBS" --memory 48g --memory-swap 48g \
  --pids-limit 512 --gpus all -v "$T/usr:/usr" -v "$T/var:/var" -v "$T/etc:/etc" -v "$T/opt:/opt" -v "$T/tmp:/tmp" \
  -v "$HERE:/incr:ro" -e LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libcudss/12 -e CMAKE_BUILD_PARALLEL_LEVEL="$JOBS" \
  -e DEBIAN_FRONTEND=noninteractive -e V="$V" -e JOBS="$JOBS" --entrypoint bash $IMG /incr/build-env-inside.sh
