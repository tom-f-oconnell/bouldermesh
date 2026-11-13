"""Checks that a tree build-tree.sh made (or the app's image) has everything that matters for speed built in and
switched on, from what was actually built and from short runs of it. Exits 1 if a required item is missing or off.

    verify_build.py <tree dir>          in a container on that tree; writes <tree>/opt/verify.json
    verify_build.py app                 the app's image (the baseline's tools); writes <BENCH>/verify-app.json
    verify_build.py --inside <variant>  the checks themselves, inside a tree's container or the image (build-inside.sh
                                        ends with this; the two above run it)
    verify_build.py --table [dir ...]   the saved results side by side, as a Markdown table

Read from the build: Ceres's config.h, each part's CMakeCache.txt, OpenCV's build summary, each binary's NEEDED
libraries (readelf) and what they resolve to (ldd), and how much of each binary's code uses AVX registers or FMA
(objdump). Run: bundle adjustments of a made-up scene of 60 cameras, whose patched log line names the device and the
sparse or dense library chosen, with every library the process really loaded (LD_DEBUG=libs: which BLAS, CHOLMOD,
cuDSS); a learned-feature extraction on the GPU, for ONNX Runtime's CUDA provider and cuDNN really being loaded; and
OpenMVS naming its CUDA device. GPU runs hold /gpu.lock when it is there (the benchmark's gpu.lock).

A row's "ok" is true or false for a required item and null for one only recorded (instruction set, LTO, precision).
"""
import fcntl
import json
import os
import random
import re
import struct
import subprocess
import sys
import time
import zlib
from math import cos, pi, sin, sqrt
from pathlib import Path

MARCH = {"plain": None, "complete": None, "v3": "x86-64-v3", "native": "native",
         "incr-full": "x86-64-v3"}     # the incremental-pipeline agent's tree (experiments/mapper/incr)       # what each variant is built for
WORK = Path("/tmp/verify")


def sh(cmd: str, env: dict | None = None, timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, errors="replace", timeout=timeout,
                       env={**os.environ, **(env or {})})
    return p.returncode, p.stdout + p.stderr


def cache(path: str) -> dict[str, str]:
    """A CMakeCache.txt's entries."""
    out = {}
    if Path(path).exists():
        for line in open(path, errors="replace"):
            if m := re.match(r"([\w.-]+):\w+=(.*)", line):
                out[m.group(1)] = m.group(2).strip()
    return out


def needed(binary: str) -> list[str]:
    return re.findall(r"\(NEEDED\)\s+Shared library: \[(.+?)\]", sh(f"readelf -d {binary}")[1])


def simd(binary: str) -> str:
    """How much of a binary's code is AVX (ymm/zmm registers) and FMA: evidence of what it was compiled for."""
    out = sh(f"objdump -d --no-show-raw-insn {binary} | awk '/^ +[0-9a-f]+:/{{n++}} /%ymm/{{y++}} /%zmm/{{z++}} "
             f"/vfn?m(add|sub)/{{f++}} END{{print n+0, y+0, z+0, f+0}}'", timeout=900)[1].split()
    if len(out) != 4 or not int(out[0]):
        return "?"
    n, y, z, f = (int(x) for x in out)
    return f"ymm {100 * y / n:.1f}%, zmm {100 * z / n:.2f}%, FMA {100 * f / n:.2f}% of {n / 1e6:.1f}M instructions"


def loaded(cmd: str, env: dict | None = None) -> tuple[int, str, list[str]]:
    """Runs a command and returns its exit code, output, and every shared library its processes loaded."""
    sh(f"rm -rf {WORK}/ld; mkdir -p {WORK}/ld")
    rc, out = sh(cmd, {**(env or {}), "LD_DEBUG": "libs", "LD_DEBUG_OUTPUT": f"{WORK}/ld/x"})
    libs = set()
    for f in (WORK / "ld").glob("x.*"):
        libs |= set(re.findall(r"calling init: (\S+)", f.read_text(errors="replace")))
    return rc, out, sorted({os.path.realpath(l) for l in libs})      # what e.g. libblas.so.3 really is


def quaternion(R: list) -> list[float]:
    """(w, x, y, z) of a rotation matrix, from whichever of the four is largest."""
    t = [R[0][0] + R[1][1] + R[2][2], R[0][0] - R[1][1] - R[2][2], R[1][1] - R[0][0] - R[2][2], R[2][2] - R[0][0] - R[1][1]]
    i = t.index(max(t))
    v = sqrt(1 + t[i]) / 2
    rest = {0: [(R[2][1] - R[1][2]), (R[0][2] - R[2][0]), (R[1][0] - R[0][1])],
            1: [(R[2][1] - R[1][2]), (R[0][1] + R[1][0]), (R[0][2] + R[2][0])],
            2: [(R[0][2] - R[2][0]), (R[0][1] + R[1][0]), (R[1][2] + R[2][1])],
            3: [(R[1][0] - R[0][1]), (R[0][2] + R[2][0]), (R[1][2] + R[2][1])]}[i]
    a, b, c = (x / (4 * v) for x in rest)
    return [[v, a, b, c], [a, v, b, c], [a, b, v, c], [a, b, c, v]][i]


def scene(d: Path, cameras: int = 60, points: int = 3000) -> None:
    """A made-up COLMAP text model: cameras on a ring around a cloud of points, each seeing 40% of them, with half a
    pixel of noise on the observations and the points nudged, so that bundle adjustment has something to do."""
    rnd = random.Random(1)
    d.mkdir(parents=True, exist_ok=True)
    P = [[rnd.uniform(-1, 1) for _ in range(3)] for _ in range(points)]
    seen = [[] for _ in range(points)]
    cams, imgs = [], []
    for i in range(cameras):
        a = 2 * pi * i / cameras
        C = [5 * cos(a), 5 * sin(a), rnd.uniform(-1, 1)]
        norm = sqrt(sum(c * c for c in C))
        z = [-c / norm for c in C]
        x = [-z[1], z[0], 0.0]                      # cross((0, 0, 1), z)
        xn = sqrt(x[0] ** 2 + x[1] ** 2)
        x = [v / xn for v in x]
        y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
        R = [x, y, z]
        t = [-sum(R[r][k] * C[k] for k in range(3)) for r in range(3)]
        q = quaternion(R)
        obs = []
        for j, p in enumerate(P):
            if rnd.random() < 0.4:
                c = [sum(R[r][k] * p[k] for k in range(3)) + t[r] for r in range(3)]
                obs.append((1200 * c[0] / c[2] + 960 + rnd.gauss(0, 0.5), 1200 * c[1] / c[2] + 540 + rnd.gauss(0, 0.5), j + 1))
                seen[j].append((i + 1, len(obs) - 1))
        cams.append(f"{i + 1} SIMPLE_RADIAL 1920 1080 1210 960 540 0\n")
        imgs.append(f"{i + 1} {' '.join(f'{v:.9f}' for v in q + t)} {i + 1} im{i:03d}.png\n"
                    + " ".join(f"{u:.3f} {v:.3f} {j}" for u, v, j in obs) + "\n")
    (d / "cameras.txt").write_text("".join(cams))
    (d / "images.txt").write_text("".join(imgs))
    (d / "points3D.txt").write_text("".join(
        f"{j + 1} {' '.join(f'{v + rnd.gauss(0, 0.01):.6f}' for v in p)} 128 128 128 1.0 "
        + " ".join(f"{i} {k}" for i, k in seen[j]) + "\n" for j, p in enumerate(P) if len(seen[j]) >= 2))


def png(path: Path, w: int = 960, h: int = 720, seed: int = 0) -> None:
    """A grey image of soft random blobs, for a feature extractor to find something in."""
    rnd = random.Random(seed)
    blobs = [(rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(6, 40), rnd.uniform(-90, 90)) for _ in range(150)]
    rows = [bytearray([128] * w) for _ in range(h)]
    for bx, by, r, v in blobs:
        for yy in range(max(0, int(by - r)), min(h, int(by + r))):
            row = rows[yy]
            for xx in range(max(0, int(bx - r)), min(w, int(bx + r))):
                d2 = ((xx - bx) ** 2 + (yy - by) ** 2) / (r * r)
                if d2 < 1:
                    row[xx] = max(0, min(255, int(row[xx] + v * (1 - d2))))
    raw = b"".join(b"\0" + bytes(r) for r in rows)
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))    # noqa: E731
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, part: str, item: str, want: str, got, ok) -> None:
        """ok: True/False for a required item, None for one that is only recorded."""
        self.rows.append({"part": part, "item": item, "want": want, "got": str(got), "ok": ok})

    def flags(self, part: str, c: dict, variant: str, ipo: str) -> None:
        """A part's build type, optimization, instruction set and link-time optimization, from its CMake cache."""
        flags = f"{c.get('CMAKE_CXX_FLAGS', '')} | {c.get('CMAKE_CXX_FLAGS_RELEASE', '')}"
        self.add(part, "build type", "Release", c.get("CMAKE_BUILD_TYPE", "?"), c.get("CMAKE_BUILD_TYPE") == "Release")
        self.add(part, "optimization", "-O3 (or -O2 from the project), NDEBUG", flags,
                 bool(re.search(r"-O[23]", flags)) and "-g " not in flags + " ")
        march = (re.search(r"-march=(\S+)", flags) or [None, None])[1]
        self.add(part, "instruction set", MARCH[variant] or "baseline x86-64", march or "baseline x86-64", march == MARCH[variant])
        self.add(part, "link-time optimization", "recorded", c.get(ipo, "OFF"), None)


def gpu_lock():
    if not Path("/gpu.lock").exists():
        return None
    f = open("/gpu.lock", "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def ba(k: Checks, part: str, name: str, more: str, env: dict, want: dict, libs_want: dict) -> None:
    """One bundle adjustment of the made-up scene with the tree's COLMAP: the device and libraries its log line names,
    and the libraries really loaded."""
    sh(f"rm -rf {WORK}/out; mkdir -p {WORK}/out")
    start = time.time()
    rc, out, libs = loaded(f"colmap bundle_adjuster --log_target stderr --input_path {WORK}/model --output_path {WORK}/out {more}", env)
    secs = time.time() - start
    line = (re.findall(r"BENCH_BA (.*)", out) or ["no BENCH_BA line: " + out[-200:].replace("\n", " ")])[-1]
    said = dict(re.findall(r"(\w+)=(\S+)", line))
    fell = re.findall(r"[^\n]*(?:[Ff]alling back|retrying with CPU|not available|[Ff]ailed)[^\n]*", out)
    good = rc == 0 and all(said.get(a) == b for a, b in want.items()) and not fell
    k.add(part, f"run: {name}", " ".join(f"{a}={b}" for a, b in want.items()),
          f"{line} ({secs:.1f} s, exit {rc}{'; ' + fell[0][-120:] if fell else ''})", good)
    for what, pattern in libs_want.items():
        hit = sorted({Path(l).name for l in libs if re.search(pattern, l)})
        k.add(part, f"run: {name}: {what} loaded", pattern, ", ".join(hit) or "none", bool(hit))


PYCOLMAP = r"""
import os, re, sys
import pycolmap
print("HAS_CUDA", pycolmap.has_cuda, pycolmap.get_num_cuda_devices(), pycolmap.__version__)
s = pycolmap.SyntheticDatasetOptions()
s.num_cameras_per_rig = 1; s.num_frames_per_rig = 60; s.num_points3D = 2000
rec = pycolmap.synthesize_dataset(s)
pycolmap.logging.set_log_destination(pycolmap.logging.INFO, sys.argv[1])
for gpu in (False, True):
    b = pycolmap.BundleAdjustmentOptions(); b.print_summary = False
    b.ceres.use_gpu = gpu; b.ceres.min_num_images_gpu_solver = 0
    b.ceres.max_num_images_direct_dense_cpu_solver = 10; b.ceres.max_num_images_direct_dense_gpu_solver = 10
    pycolmap.bundle_adjustment(rec, b)
"""


def pycolmap(k: Checks) -> None:
    """pycolmap, where the tree has one (the incremental-pipeline agent's): a CUDA build, and sparse bundle
    adjustments that the patched COLMAP under it logs as SuiteSparse on the CPU and cuDSS on the GPU."""
    rc, out = sh("python3 -c 'import pycolmap' 2>&1")
    if rc:
        k.add("pycolmap", "installed", "recorded (only some trees have it)", "no", None)
        return
    (WORK / "pycolmap.py").write_text(PYCOLMAP)
    log = WORK / "pycolmap.log"
    rc, out = sh(f"rm -f {log}; python3 {WORK}/pycolmap.py {log}", timeout=900)
    has = re.search(r"HAS_CUDA (\S+) (\d+) (\S+)", out)
    k.add("pycolmap", "a CUDA build that sees the GPU", "has_cuda True, 1+ devices",
          f"{has.group(3)}: has_cuda {has.group(1)}, {has.group(2)} device(s)" if has else out[-200:],
          bool(has) and has.group(1) == "True" and int(has.group(2)) > 0)
    lines = re.findall(r"BENCH_BA.*", log.read_text(errors="replace") if log.exists() else "")
    cpu = next((l for l in lines if "device=cpu" in l), "no BENCH_BA line on the CPU" + (f" (exit {rc})" if rc else ""))
    gpu = next((l for l in lines if "device=cuda" in l), "no BENCH_BA line on the GPU" + (f" (exit {rc})" if rc else ""))
    k.add("pycolmap", "run: sparse solve on the CPU", "sparse=SUITE_SPARSE", cpu, "sparse=SUITE_SPARSE" in cpu)
    k.add("pycolmap", "run: sparse solve on the GPU", "device=cuda sparse=CUDA_SPARSE", gpu, "sparse=CUDA_SPARSE" in gpu)


def tree(variant: str) -> Checks:
    k, V = Checks(), variant
    lock = gpu_lock()
    WORK.mkdir(parents=True, exist_ok=True)
    scene(WORK / "model")
    # --- Ceres
    cfg = Path("/usr/local/include/ceres/internal/config.h").read_text() if Path("/usr/local/include/ceres/internal/config.h").exists() else ""
    on = lambda name: bool(re.search(rf"^#define {name}\b", cfg, re.M))      # noqa: E731
    c = cache(f"/opt/ceres/build-{V}/CMakeCache.txt")
    k.flags("Ceres", c, V, "CMAKE_INTERPROCEDURAL_OPTIMIZATION")
    for item, name, want in (("SuiteSparse (CHOLMOD, SPQR)", "CERES_NO_SUITESPARSE", False), ("CUDA", "CERES_NO_CUDA", False),
                             ("cuDSS (sparse Cholesky on the GPU)", "CERES_NO_CUDSS", False), ("LAPACK", "CERES_NO_LAPACK", False),
                             ("Eigen sparse", "CERES_USE_EIGEN_SPARSE", True), ("METIS ordering for Eigen sparse", "CERES_NO_EIGEN_METIS", False),
                             ("METIS (nested dissection) with CHOLMOD", "CERES_NO_CHOLMOD_PARTITION", False),
                             ("Schur specializations", "CERES_RESTRICT_SCHUR_SPECIALIZATION", False)):
        k.add("Ceres", item, "on", "on" if on(name) == want else f"off (config.h: {name} {'defined' if on(name) else 'not defined'})",
              bool(cfg) and on(name) == want)
    need = needed("/usr/local/lib/libceres.so")
    for item, pattern in (("links CHOLMOD", "libcholmod"), ("links SPQR", "libspqr"), ("links cuDSS", "libcudss"),
                          ("links cuSOLVER and cuBLAS", "libcusolver"), ("links METIS", "libmetis"),
                          ("links OpenBLAS (not the reference BLAS)", "libopenblas")):
        hit = [n for n in need if pattern in n]
        k.add("Ceres", item, pattern, ", ".join(hit) or "not among its NEEDED libraries", bool(hit))
    k.add("Ceres", "BLAS/LAPACK the build found", "OpenBLAS", c.get("BLAS_openblas_LIBRARY", c.get("BLAS_LIBRARIES", "?")),
          "openblas" in (c.get("BLAS_openblas_LIBRARY", "") + c.get("BLAS_LIBRARIES", "")).lower())
    k.add("Ceres", "code: AVX / FMA", "recorded", simd("/usr/local/lib/libceres.so"), None)
    # --- COLMAP
    c = cache(f"/opt/colmap/build-{V}/CMakeCache.txt")
    k.flags("COLMAP", c, V, "CMAKE_INTERPROCEDURAL_OPTIMIZATION")
    head = sh("colmap -h 2>&1 | head -1")[1].strip()
    k.add("COLMAP", "version, CUDA", "4.2.1 with CUDA", head, "4.2.1" in head and "with CUDA" in head)
    for item, name in (("CUDA", "CUDA_ENABLED"), ("Caspar (bundle adjustment on the GPU)", "CASPAR_ENABLED"),
                       ("ONNX Runtime (learned features)", "ONNX_ENABLED"), ("OpenMP", "OPENMP_ENABLED"), ("SIMD", "SIMD_ENABLED")):
        k.add("COLMAP", item, "ON", c.get(name, "?"), c.get(name) == "ON")
    k.add("COLMAP", "PoseLib", "built in", c.get("PoseLib_SOURCE_DIR", "not in the build"), "PoseLib_SOURCE_DIR" in c)
    k.add("COLMAP", "Caspar precision", "recorded", "float64" if c.get("CASPAR_USE_DOUBLE") == "ON" else "float32", None)
    k.add("COLMAP", "faiss", "recorded", f"opt level {c.get('FAISS_OPT_LEVEL', 'default (generic)')}, LTO {c.get('FAISS_USE_LTO', '?')}", None)
    ldd = sh("ldd /usr/local/bin/colmap")[1]
    ceres = os.path.normpath((re.search(r"libceres\S* => (\S+)", ldd) or [None, "not linked"])[1])
    k.add("COLMAP", "linked to the tree's own Ceres", "/usr/local/lib/libceres", ceres, ceres.startswith("/usr/local/lib/"))
    k.add("COLMAP", "every library resolves", "none missing", ", ".join(re.findall(r"(\S+) => not found", ldd)) or "none missing",
          "not found" not in ldd)
    k.add("COLMAP", "OpenMP runtime", "libgomp", (re.search(r"libgomp\S*", ldd) or ["not linked"])[0], "libgomp" in ldd)
    patched = sh("grep -c BENCH_BA /usr/local/bin/colmap")[1].strip()
    k.add("COLMAP", "the benchmark's logging and threshold patch", "present", "present" if patched not in ("", "0") else "absent",
          patched not in ("", "0"))
    k.add("COLMAP", "code: AVX / FMA", "recorded", simd("/usr/local/bin/colmap"), None)
    ort = sorted(p.name for p in Path("/usr/local/lib").glob("libonnxruntime*.so"))
    k.add("COLMAP", "ONNX Runtime's CUDA provider", "libonnxruntime_providers_cuda.so", ", ".join(ort) or "none",
          "libonnxruntime_providers_cuda.so" in ort)
    prov = sh("ldd /usr/local/lib/libonnxruntime_providers_cuda.so")[1]
    cudnn = sorted(set(re.findall(r"libcudnn\.so\.\d+", sh("ldconfig -p")[1])))      # which the provider opens when first used
    k.add("COLMAP", "the provider's libraries resolve, and cuDNN is installed", "none missing, libcudnn.so.9",
          (", ".join(re.findall(r"(\S+) => not found", prov)) or "none missing") + "; " + (", ".join(cudnn) or "no cuDNN"),
          "not found" not in prov and bool(cudnn))
    blas = {"OpenBLAS": r"libopenblas"}
    ba(k, "COLMAP", "sparse solve on the CPU", "", {"COLMAP_BENCH_DENSE_CPU": "0"},
       {"device": "cpu", "solver": "SPARSE_SCHUR", "sparse": "SUITE_SPARSE"}, {**blas, "CHOLMOD": r"libcholmod"})
    ba(k, "COLMAP", "dense solve on the CPU", "", {"COLMAP_BENCH_DENSE_CPU": "999"}, {"device": "cpu", "solver": "DENSE_SCHUR"}, blas)
    gpu = "--BundleAdjustmentCeres.use_gpu 1"
    ba(k, "COLMAP", "sparse solve on the GPU", gpu, {"COLMAP_BENCH_MIN_GPU_IMAGES": "0", "COLMAP_BENCH_DENSE_GPU": "0"},
       {"device": "cuda", "solver": "SPARSE_SCHUR", "sparse": "CUDA_SPARSE"}, {"cuDSS": r"libcudss"})
    ba(k, "COLMAP", "dense solve on the GPU", gpu, {"COLMAP_BENCH_MIN_GPU_IMAGES": "0", "COLMAP_BENCH_DENSE_GPU": "999"},
       {"device": "cuda", "solver": "DENSE_SCHUR", "dense": "CUDA"}, {"cuSOLVER": r"libcusolver"})
    ba(k, "COLMAP", "Caspar on the GPU", "--BundleAdjustment.backend CASPAR", {}, {"device": "cuda", "solver": "CASPAR"}, {})
    # learned features through ONNX Runtime on the GPU
    (WORK / "images").mkdir(exist_ok=True)
    for i in range(2):
        png(WORK / "images" / f"im{i}.png", seed=i)
    sh(f"rm -f {WORK}/db.db")
    before = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")[1].strip()
    rc, out, libs = loaded(f"colmap feature_extractor --log_target stderr --database_path {WORK}/db.db --image_path {WORK}/images "
                           "--FeatureExtraction.type ALIKED_N16ROT --FeatureExtraction.use_gpu 1")
    names = {Path(l).name for l in libs}
    hit = sorted(n for n in names if re.search(r"onnxruntime_providers_cuda|libcudnn\.so|libcudnn_ops", n))
    complaint = re.findall(r"[^\n]*(?:CPUExecutionProvider|[Ff]ailed to (?:load|create)|not available|[Ff]alling back)[^\n]*", out)
    feats = sh(f"python3 -c \"import sqlite3; print(sqlite3.connect('{WORK}/db.db').execute('select sum(rows) from keypoints').fetchone()[0])\"")[1].strip()
    k.add("COLMAP", "run: ALIKED features on the GPU (ONNX Runtime)", "CUDA provider and cuDNN loaded, features found",
          f"loaded {', '.join(hit) or 'neither'}; {feats} keypoints; exit {rc}; GPU memory in use before {before} MiB"
          + (f"; {complaint[0][-160:]}" if complaint else ""),
          rc == 0 and any("providers_cuda" in n for n in hit) and any("cudnn" in n for n in hit) and not complaint and feats.isdigit())
    pycolmap(k)
    # --- OpenMVS
    c = cache(f"/opt/openMVS/build-{V}/CMakeCache.txt")
    k.flags("OpenMVS", c, V, "OpenMVS_ENABLE_IPO")
    for item, name in (("CUDA", "OpenMVS_USE_CUDA"), ("OpenMP", "OpenMVS_USE_OPENMP"), ("SSE", "OpenMVS_USE_SSE")):
        k.add("OpenMVS", item, "ON", c.get(name, "?"), c.get(name) == "ON")
    need = needed("/usr/local/bin/OpenMVS/DensifyPointCloud")
    k.add("OpenMVS", "DensifyPointCloud links CUDA", "libcuda / libcudart", ", ".join(n for n in need if "cuda" in n) or "none",
          any("cudart" in n for n in need))
    k.add("OpenMVS", "OpenMP runtime", "libgomp", ", ".join(n for n in need if "gomp" in n or "omp" in n) or "none",
          any("gomp" in n for n in need))
    k.add("OpenMVS", "linked to the tree's own OpenCV", "/usr/local/lib/libopencv_core",
          (re.search(r"libopencv_core\S* => (\S+)", sh("ldd /usr/local/bin/OpenMVS/DensifyPointCloud")[1]) or [None, "not linked"])[1], None)
    out = sh(f"cd {WORK} && /usr/local/bin/OpenMVS/DensifyPointCloud none.mvs -w {WORK} 2>&1 | head -40")[1]
    dev = re.search(r"CUDA device \d+ initialized: ([^\n]*)", out)
    k.add("OpenMVS", "run: names its CUDA device", "CUDA device initialized", dev.group(1) if dev else "no such line", bool(dev))
    k.add("OpenMVS", "code: AVX / FMA (DensifyPointCloud)", "recorded", simd("/usr/local/bin/OpenMVS/DensifyPointCloud"), None)
    k.add("OpenMVS", "code: AVX / FMA (TextureMesh)", "recorded", simd("/usr/local/bin/OpenMVS/TextureMesh"), None)
    # --- OpenCV
    c = cache(f"/opt/opencv/build-{V}/CMakeCache.txt")
    k.flags("OpenCV", c, V, "ENABLE_LTO")
    info = Path(f"/opt/opencv/build-{V}/modules/core/version_string.inc")
    info = info.read_text() if info.exists() else ""
    get = lambda label: (re.search(rf"{label}:\s+(.*?)\\n", info) or [None, "?"])[1].strip()       # noqa: E731
    k.add("OpenCV", "Intel IPP", "a version", get("Intel IPP"), get("Intel IPP") not in ("?", "NO"))
    k.add("OpenCV", "CPU dispatch", "AVX2 among them", get("Dispatched code generation"), "AVX2" in get("Dispatched code generation")
          or "AVX2" in get("Baseline"))
    k.add("OpenCV", "baseline instruction set", "recorded", get("Baseline"), None)
    k.add("OpenCV", "parallel backend", "one (pthreads, TBB or OpenMP)", get("Parallel framework"),
          get("Parallel framework") not in ("?", "none", "NO"))
    k.add("OpenCV", "LAPACK", "recorded", get("Lapack"), None)
    if lock:
        lock.close()
    return k


def app() -> Checks:
    """The app's image, which the baseline runs in (COLMAP 3.9.1 and OpenMVS 2.3 on the CPU, torch for VGGT and the
    people masks): what running it shows. app_binaries() adds what its binaries show, as the image has no binutils."""
    k = Checks()
    lock = gpu_lock()
    WORK.mkdir(parents=True, exist_ok=True)
    scene(WORK / "model")
    head = " ".join(sh("colmap -h 2>&1 | head -3")[1].split())
    k.add("COLMAP", "version", "3.9.1", head[:90], "3.9.1" in head)
    k.add("COLMAP", "CUDA (matching on the GPU)", "with CUDA", "with CUDA" if "with CUDA" in head else "without CUDA", "with CUDA" in head)
    ldd = sh("ldd $(which colmap)")[1]
    ceres = (re.search(r"libceres\S* => (\S+)", ldd) or [None, ""])[1]
    k.add("Ceres", "library", "recorded", os.path.realpath(ceres) if ceres else "linked statically or not at all", None)
    sh(f"rm -rf {WORK}/out; mkdir -p {WORK}/out")
    start = time.time()
    rc, out, libs = loaded(f"colmap bundle_adjuster --input_path {WORK}/model --output_path {WORK}/out")
    libs = [l for l in libs if re.search(r"blas|lapack|cholmod|spqr|metis|gomp|tbb", l)]
    short = lambda pattern: ", ".join("/".join(l.split("/")[-2:]) for l in libs if re.search(pattern, l)) or "none"    # noqa: E731
    k.add("Ceres", "run: bundle adjustment of the made-up scene", "finishes", f"exit {rc}, {time.time() - start:.1f} s", rc == 0)
    k.add("Ceres", "run: BLAS loaded", "OpenBLAS (not the reference BLAS)", short("blas"), any("openblas" in l for l in libs))
    k.add("Ceres", "run: CHOLMOD loaded", "libcholmod", short("cholmod"), any("cholmod" in l for l in libs))
    k.add("Ceres", "run: other numeric and threading libraries loaded", "recorded", short(r"lapack|spqr|metis|gomp|tbb"), None)
    out = sh(f"cd {WORK} && /usr/local/bin/OpenMVS/DensifyPointCloud none.mvs -w {WORK} 2>&1 | head -5")[1]
    k.add("OpenMVS", "version", "recorded", (re.search(r"OpenMVS \S+ v[\d.]+", out) or ["?"])[0], None)
    rc, out = sh("python3 -c \"import torch, torchvision; print(torch.__version__, torchvision.__version__, torch.cuda.is_available(), "
                 "torch.backends.cudnn.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')\"")
    k.add("torch", "CUDA and cuDNN, for VGGT and the people masks", "True True", out.strip()[-160:], rc == 0 and "True True" in out)
    if lock:
        lock.close()
    return k


def app_binaries(d: Path) -> list[dict]:
    """Rows from the app image's binaries, copied out to d: what Ceres and OpenMVS link, and what the code is built for."""
    k = Checks()
    need = needed(str(d / "libceres.so"))
    for item, pattern, required in (("links CHOLMOD", "libcholmod", True), ("links SPQR", "libspqr", True),
                                    ("links CUDA libraries", "libcu", None), ("BLAS and LAPACK it links", r"blas|lapack", None)):
        hit = [n for n in need if re.search(pattern, n)]
        k.add("Ceres", item, pattern if required else "recorded", ", ".join(hit) or "none", bool(hit) if required else None)
    need = needed(str(d / "DensifyPointCloud"))
    k.add("OpenMVS", "CUDA", "recorded (the app's is built without)", ", ".join(n for n in need if "cuda" in n) or "none: CPU only", None)
    k.add("OpenMVS", "OpenMP runtime", "libgomp", ", ".join(n for n in need if "omp" in n) or "none", any("omp" in n for n in need))
    for part, item, f in (("Ceres", "code: AVX / FMA", "libceres.so"), ("COLMAP", "code: AVX / FMA", "colmap"),
                          ("OpenMVS", "code: AVX / FMA (DensifyPointCloud)", "DensifyPointCloud"),
                          ("OpenMVS", "code: AVX / FMA (TextureMesh)", "TextureMesh")):
        k.add(part, item, "recorded", simd(str(d / f)), None)
    return k.rows


def finish(variant: str, rows: list[dict], files: list[str]) -> dict:
    failed = [f"{r['part']}: {r['item']} ({r['got'][:80]})" for r in rows if r["ok"] is False]
    return {"variant": variant, "ok": not failed, "failed": failed, "when": time.strftime("%F %T"),
            "files": {f: [os.stat(f).st_mtime_ns, os.stat(f).st_size] for f in files if os.path.exists(f)}, "rows": rows}


def show(result: dict) -> None:
    for r in result["rows"]:
        mark = {True: "ok  ", False: "FAIL", None: "    "}[r["ok"]]
        print(f"{mark} {r['part']:13s} {r['item'][:58]:58s} {r['got'][:150]}")
    print(f"{result['variant']}: " + ("every required item is there" if result["ok"] else f"{len(result['failed'])} required item(s) missing:"))
    for f in result["failed"]:
        print("  FAIL", f)


FILES = ["/usr/local/bin/colmap", "/usr/local/lib/libceres.so", "/usr/local/bin/OpenMVS/DensifyPointCloud"]


def inside(variant: str) -> int:
    result = finish(variant, (app() if variant == "app" else tree(variant)).rows, [] if variant == "app" else FILES)
    show(result)
    print("VERIFY_JSON " + json.dumps(result), flush=True)
    if variant != "app" and os.access("/opt", os.W_OK):
        Path("/opt/verify.json").write_text(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


def saved(tools: str, bench: Path) -> Path:
    return bench / "verify-app.json" if tools == "app" else bench / f"build-{tools}" / "opt" / "verify.json"


def current(tools: str, bench: Path) -> dict | None:
    """A tree's (or the app image's) saved result, if it is of the files there now."""
    f = saved(tools, bench)
    if not f.exists():
        return None
    result = json.loads(f.read_text())
    for name, (mtime, size) in result.get("files", {}).items():
        st = bench / f"build-{tools}" / name.lstrip("/")
        if not st.exists() or [st.stat().st_mtime_ns, st.stat().st_size] != [mtime, size]:
            return None
    return result


def outside(target: str) -> int:
    """Runs the checks in a container, as bench.py runs a step: the tree over the CUDA image, or the app's image."""
    import bench
    is_app = target == "app"
    tools = "app" if is_app else Path(target).resolve().name.removeprefix("build-")
    name = f"bench-verify-{tools}-{os.getpid()}"
    cmd = ["docker", "run", "--rm", "--name", name, "--log-driver", "none", "--tmpfs", "/tmp:exec,size=4g", "--memory", "16g",
           "--memory-swap", "16g", "--pids-limit", "512", "--cpus", "6", "--gpus", "all", "-w", "/tmp",
           "-v", f"{bench.HERE}:/bench:ro", "-v", f"{bench.GPU_LOCK}:/gpu.lock", "-v", f"{bench.BENCH}/cache:/cache"]
    if is_app:
        cmd += ["-e", "HOME=/tmp", "--entrypoint", "python3", bench.APP]
    else:
        t = Path(target).resolve()
        cmd += ["-e", "HOME=/cache", "-e", "LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libcudss/12", "-v", f"{t}/usr:/usr",
                "-v", f"{t}/etc:/etc", "-v", f"{t}/opt:/opt", "--entrypoint", "python3", bench.CUDA_IMAGE]      # as bench.py
        # mounts them: the NVIDIA runtime runs ldconfig in the container, which a read-only /etc fails
    bench.GPU_LOCK.touch()
    p = subprocess.run(cmd + ["/bench/verify_build.py", "--inside", tools], capture_output=True, text=True, errors="replace")
    subprocess.run(["docker", "stop", name], capture_output=True)
    m = re.search(r"^VERIFY_JSON (.*)$", p.stdout, re.M)
    print(re.sub(r"^VERIFY_JSON .*\n", "", p.stdout, flags=re.M) + p.stderr[-2000:])
    if not m:
        return 2
    result = json.loads(m.group(1))
    if is_app:      # (a tree's own is written from inside it) the image has no readelf or objdump: its binaries, out here
        out = bench.BENCH / "cache" / "app-bin"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "run", "--rm", "--name", name, "--log-driver", "none", "--memory", "2g", "--memory-swap", "2g",
                        "--pids-limit", "512", "-v", f"{out}:/out", "--entrypoint", "bash", bench.APP, "-c",
                        "cp -L $(which colmap) /out/colmap; cp -L $(ldd $(which colmap) | awk '/libceres/{print $3}') /out/libceres.so; "
                        "cp -L /usr/local/bin/OpenMVS/DensifyPointCloud /usr/local/bin/OpenMVS/TextureMesh /out/"], capture_output=True)
        result = finish("app", result["rows"] + app_binaries(out), [])
        result["image"] = bench.APP
        show(result)
        saved("app", bench.BENCH).write_text(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


def table(dirs: list[str]) -> None:
    """Saved results side by side: one row an item, one column a tree."""
    import bench
    files = [Path(d) for d in dirs] or [f for f in [saved("app", bench.BENCH)] + sorted(bench.BENCH.glob("build-*/opt/verify.json")) if f.exists()]
    results = [json.loads((f / "opt" / "verify.json" if f.is_dir() else f).read_text()) for f in files]
    items = []
    for r in results:
        for row in r["rows"]:
            key = (row["part"], row["item"])
            if key not in items:
                items.append(key)
    print("| Part | Item | " + " | ".join(r["variant"] for r in results) + " |\n|---|---|" + "---|" * len(results))
    for part, item in items:
        cells = []
        for r in results:
            row = next((x for x in r["rows"] if (x["part"], x["item"]) == (part, item)), None)
            cells.append("" if row is None else ("**missing:** " if row["ok"] is False else "") + row["got"][:110].replace("|", "/"))
        print(f"| {part} | {item} | " + " | ".join(cells) + " |")
    print("| | **verdict** | " + " | ".join("passes" if r["ok"] else f"fails ({len(r['failed'])})" for r in results) + " |")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if sys.argv[1] == "--inside":
        sys.exit(inside(sys.argv[2]))
    if sys.argv[1] == "--table":
        table(sys.argv[2:])
    else:
        sys.exit(outside(sys.argv[1]))
