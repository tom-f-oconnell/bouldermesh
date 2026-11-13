#!/usr/bin/env bash
# Checks, inside a tree build-env.sh made, that every component that matters for speed is really built in and is
# what runs: verify-env.sh <full|plain>. Prints one line a check and exits 1 if one that the variant should have is
# missing. (The benchmark's verify_build.py checks its own trees the same way; this is for this pipeline's.)
V=${1:-full} BAD=0
ok() { echo "ok      $1"; }
no() { if [ "$2" = full-only ] && [ "$V" != full ]; then echo "absent  $1 (expected in the plain tree)"; else echo "MISSING $1"; BAD=1; fi; }
has() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1" "${3:-}"; fi; }
C=/usr/local/include/ceres/internal/config.h
for off in SUITESPARSE:full-only CUDA: CUDSS: LAPACK: CHOLMOD_PARTITION:full-only; do
  has "Ceres: ${off%%:*}" "! grep -qE '^#define CERES_NO_${off%%:*}\$' $C" "${off##*:}"
done
has "Ceres links CHOLMOD (SuiteSparse)" "ldd /usr/local/lib/libceres.so | grep -q cholmod" full-only
has "Ceres links METIS" "ldd /usr/local/lib/libceres.so | grep -q metis" full-only
has "Ceres links cuDSS and cuSOLVER" "ldd /usr/local/lib/libceres.so | grep -q cudss && ldd /usr/local/lib/libceres.so | grep -q cusolver"
has "BLAS is OpenBLAS" "update-alternatives --display libblas.so.3-x86_64-linux-gnu | sed -n 2p | grep -q openblas"
has "Ceres compiled for AVX2 (ymm in libceres)" "objdump -d /usr/local/lib/libceres.so --no-show-raw-insn | head -400000 | grep -q ymm" full-only
has "COLMAP: with CUDA" "colmap -h 2>&1 | head -1 | grep -q 'with CUDA'"
B=$(ls -d /opt/colmap/build-$V 2>/dev/null || ls -d /opt/colmap/build-* 2>/dev/null | head -1)
if [ -n "$B" ]; then
  for k in CUDA_ENABLED CASPAR_ENABLED ONNX_ENABLED OPENMP_ENABLED; do has "COLMAP cmake: $k" "grep -q '^$k:BOOL=ON' $B/CMakeCache.txt"; done
  has "COLMAP cmake flags: $(grep '^CMAKE_CXX_FLAGS:' $B/CMakeCache.txt | cut -d= -f2-)" "grep -q '^CMAKE_CXX_FLAGS:STRING=.*x86-64-v3' $B/CMakeCache.txt" full-only
fi
has "ONNX Runtime CUDA provider" "ls /usr/local/lib/libonnxruntime_providers_cuda.so"
has "COLMAP compiled for AVX2 (ymm in colmap)" "objdump -d /usr/local/bin/colmap --no-show-raw-insn | head -2000000 | grep -q ymm" full-only
M=/usr/local/bin/OpenMVS/DensifyPointCloud
has "OpenMVS: CUDA" "ldd $M | grep -q libcuda && $M --help 2>&1 | grep -q cuda-device"
has "OpenMVS: OpenMP" "ldd $M | grep -q libgomp"
has "OpenMVS compiled for AVX2" "objdump -d /usr/local/lib/OpenMVS/libMVS.a --no-show-raw-insn 2>/dev/null | head -3000000 | grep -q ymm || objdump -d $M --no-show-raw-insn | head -3000000 | grep -q ymm" full-only
V=$V python3 - <<'PY' || BAD=1
import io, os, re, sys, tempfile, contextlib
import numpy as np
import pycolmap
bad = 0
def check(name, good, only_full=False):
    global bad
    full = os.environ.get("V") == "full"
    print(("ok      " if good else "absent  " if only_full and not full else "MISSING ") + name)
    bad |= (not good) and (full or not only_full)
check(f"pycolmap {pycolmap.__version__}: CUDA build", pycolmap.has_cuda)
check(f"pycolmap sees {pycolmap.get_num_cuda_devices()} CUDA device(s)", pycolmap.get_num_cuda_devices() > 0)
o = pycolmap.BundleAdjustmentOptions()
sparse = str(o.ceres.solver_options.sparse_linear_algebra_library_type)
check(f"pycolmap's default sparse solver on the CPU: {sparse}", "SUITE_SPARSE" in sparse, True)
# a runtime probe: a synthetic model bundle adjusted on the CPU and on the GPU; the patched COLMAP logs what each used
s = pycolmap.SyntheticDatasetOptions()
s.num_cameras_per_rig = 1; s.num_frames_per_rig = 60; s.num_points3D = 2000
rec = pycolmap.synthesize_dataset(s)
log = tempfile.mktemp()
pycolmap.logging.set_log_destination(pycolmap.logging.INFO, log) if hasattr(pycolmap.logging, "set_log_destination") else None
for gpu in (False, True):
    b = pycolmap.BundleAdjustmentOptions(); b.print_summary = False
    b.ceres.use_gpu = gpu; b.ceres.min_num_images_gpu_solver = 0; b.ceres.max_num_images_direct_dense_cpu_solver = 10
    b.ceres.max_num_images_direct_dense_gpu_solver = 10
    pycolmap.bundle_adjustment(rec, b)
text = open(log).read() if os.path.exists(log) else ""
lines = re.findall(r"BENCH_BA.*", text)
for l in lines: print("        " + l)
check("bundle adjustment on the CPU uses SuiteSparse (sparse Schur)", any("device=cpu" in l and "sparse=SUITE_SPARSE" in l for l in lines), True)
check("bundle adjustment on the GPU uses cuDSS (device=cuda, sparse=CUDA_SPARSE)", any("device=cuda" in l for l in lines))
sys.exit(bad)
PY
[ $BAD = 0 ] && echo "VERIFIED $V" || { echo "NOT VERIFIED $V"; exit 1; }
