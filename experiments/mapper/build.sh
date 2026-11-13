#!/usr/bin/env bash
# Builds the benchmark's newer tools in a folder on /data rather than as a Docker image (hal's root pool, where Docker
# keeps images, was full): COLMAP 4.2.1 (patch_colmap.py's logging and thresholds; CUDA, Caspar, ONNX Runtime with
# CUDA for its learned features) on Ceres master (CUDA, cuDSS, SuiteSparse, METIS, OpenBLAS, Eigen 5), and OpenMVS
# 2.4.0 with CUDA on OpenCV 4.14.
#   build.sh <tree dir> [plain|complete|v3|native]      JOBS=<cores> (12)
# plain is -O3 without link-time optimization; native adds -march=native and link-time optimization, so it only runs
# on CPUs like the one it was built on; ss is plain with SuiteSparse in Ceres, which Ceres master leaves out unless
# asked (so plain's and native's sparse solves on the CPU are Eigen's own, and only the GPU's are cuDSS).
# All are built for the RTX 30 series only (CMAKE_CUDA_ARCHITECTURES=86).
# The tree holds the nvidia/cuda devel image's /usr, /var and /etc (copied on the first run) plus /opt for sources
# and builds: run its tools in that image with the tree mounted over those (bench.py does). One tree per variant.
set -euo pipefail
B=$1 V=${2:-plain} JOBS=${JOBS:-12} IMG=nvidia/cuda:12.9.1-devel-ubuntu24.04
HERE=$(cd "$(dirname "$0")" && pwd)
if [ ! -d "$B/usr" ] && [ -d "$(dirname "$B")/build-plain/usr" ] && [ "$V" != plain ]; then
  cp -a "$(dirname "$B")/build-plain" "$B"
fi
if [ ! -d "$B/usr" ]; then
  mkdir -p "$B"
  docker run --rm --name "bench-copy-$V" --log-driver none --cpus 4 --memory 8g --memory-swap 8g --pids-limit 512 -v "$B:/b" \
    --entrypoint bash $IMG -c 'cp -a /usr /var /etc /b/'
fi
mkdir -p "$B/opt" "$B/tmp"; chmod 1777 "$B/tmp"     # apt's sandbox user writes to /tmp
trap 'docker stop "bench-build-$V" >/dev/null 2>&1 || true' EXIT
timeout 6h docker run --rm --name "bench-build-$V" --log-driver none --cpus "$JOBS" --memory 64g --memory-swap 64g --pids-limit 512 \
  -v "$B/usr:/usr" -v "$B/var:/var" -v "$B/etc:/etc" -v "$B/opt:/opt" -v "$B/tmp:/tmp" -v "$HERE:/bench:ro" \
  --entrypoint bash $IMG /bench/build-inside.sh "$V" "$JOBS"
# a copy of plain's tree starts with its sources and system packages; then every part is built again for the variant
python3 "$HERE/verify_build.py" "$B"      # fails the build if anything that matters for speed is missing or off
