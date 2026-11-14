"""Runs the mapper benchmark's steps, each in its own container, timed, with the machine sampled throughout.

    bench.py feat <project> [--feat sift391] [--cond dev] [--build plain]     features and matches for a boulder
    bench.py map <project> <method> <seed> [--feat ..] [--cond ..] [--build ..] [--rep N]   one mapper run
    bench.py rest <project> <run> [--feat ..]              place the photos the mapper left out, as the app does
    bench.py post <project> <run> [--feat ..] [--tools app] [--cond ..]   undistort, densify, mesh and texture
    bench.py all <project> <method> <seed> [...] [--post app,plain]       map, rest and post in one go
    bench.py held <project> <method> <seed> <percent> [...]   the mapper on a random share of the photos, the rest added
    bench.py heldstats                                     those against the runs on every photo
    bench.py queue <file>                                  a file of the above (one per line), one at a time
    bench.py table                                         every step so far, tab-separated (see also stats.py)
    bench.py agree [project]                               every pair of a boulder's models, laid over each other

Tools are the app's image ("app": COLMAP 3.9.1 and OpenMVS 2.3 on the CPU) or a tree build.sh made ("plain", "native"
or "ss": COLMAP 4.2.1 and OpenMVS 2.4 with CUDA). Conditions: "dev" gives a step CORES cores and as many threads,
for runs where only what is placed matters; "prod" is how the live worker runs a job (no core limit, the tools'
own default thread count, nice 10); "t<N>" is prod with N threads asked for; "c<N>" is prod held to N cores with N
threads (how a job would run while BoulderMesh may only use N of hal's cores), "c<N>t<M>" that with M threads. While
<BENCH>/cores-cap.txt holds a number, a queue runs its production-condition jobs as "c<that>" instead (capped()).
Results go to <BENCH>/r2/<project>/
<feature set>/<run>/: per step a log, a .json (seconds, CPU seconds, exit code, what else was running) and the
samples (gpusample.py). A step whose .json exists isn't run again: delete it to redo the step. Steps that use the GPU
hold <BENCH>/gpu.lock (flock) while they run, so that other GPU work on hal can keep clear of them by taking it too.
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gpusample  # noqa: E402
import stats  # noqa: E402
import verify_build  # noqa: E402

BENCH = Path(os.environ.get("BENCH", "/data/tom/mapperbench"))
R = BENCH / "r2"
APP = os.environ.get("APP", "ghcr.io/tom-f-oconnell/bouldermesh:b5607e9-gpu")
CUDA_IMAGE = "nvidia/cuda:12.9.1-devel-ubuntu24.04"
CORES = int(os.environ.get("CORES", "6"))       # what a "dev" step gets: the one number to lower when hal is shared
MEM = os.environ.get("MEM", "64g")
MVS_APP, MVS_NEW = "/usr/local/bin/OpenMVS", "/usr/local/bin/OpenMVS"
POOL_MIN_GB = 20        # a queue stops rather than start a job with less than this free on hal's root pool
REFERENCE = {"plain"}      # trees timed although verify_build.py fails them: plain lacks SuiteSparse on purpose, to compare
GPU_LOCK = BENCH / "gpu.lock"       # held (flock) through every step that uses the GPU: see step()
LOCK_PATIENCE = 2 * 3600            # seconds a step waits for it before going ahead without (and saying so)
CAP_FILE = BENCH / "cores-cap.txt"  # a number in it: the cores everything of the benchmark may use together

# The app's settings for photos from the internet (pipeline.py), in each COLMAP's spelling
INC = ["--Mapper.min_model_size", "6", "--Mapper.ba_refine_principal_point", "1", "--Mapper.min_num_matches", "20"]
GLOBAL = ["--GlobalMapper.min_model_size", "6", "--GlobalMapper.ba_refine_principal_point", "1",
          "--GlobalMapper.min_num_matches", "20"]
CASPAR = ["--Mapper.ba_local_backend", "CASPAR", "--Mapper.ba_global_backend", "CASPAR"]
# method: (COLMAP family, command, extra arguments, environment)
METHODS = {
    # COLMAP 3.9.1 (the app's)
    "base": ("app", "mapper", [], {}),
    # the same with OpenBLAS (Ubuntu 24.04's, mounted at /ob from build-plain) in place of the image's reference BLAS
    "base_ob": ("app", "mapper", [], {"LD_LIBRARY_PATH": "/ob"}),
    "base_ob1": ("app", "mapper", [], {"LD_LIBRARY_PATH": "/ob", "OPENBLAS_NUM_THREADS": "1"}),
    "lessba": ("app", "mapper", ["--Mapper.ba_global_images_ratio", "1.4", "--Mapper.ba_global_points_ratio", "1.4",
                                 "--Mapper.ba_global_max_num_iterations", "25"], {}),
    "ref2": ("app", "mapper", ["--Mapper.ba_global_max_refinements", "2", "--Mapper.ba_local_max_refinements", "1"], {}),
    # COLMAP 4.2.1's incremental mapper
    "cpu421": ("new", "mapper", [], {}),
    "nofallback": ("new", "mapper", ["--Mapper.structure_less_registration_fallback", "0"], {}),   # as 3.9.1 registers
    "gpuba": ("new", "mapper", ["--Mapper.ba_use_gpu", "1"], {}),      # GPU from 50 images up, COLMAP's own threshold
    "gpuba0": ("new", "mapper", ["--Mapper.ba_use_gpu", "1"], {"COLMAP_BENCH_MIN_GPU_IMAGES": "0"}),   # GPU always
    "gpudense": ("new", "mapper", ["--Mapper.ba_use_gpu", "1"],
                 {"COLMAP_BENCH_MIN_GPU_IMAGES": "0", "COLMAP_BENCH_DENSE_GPU": "3999"}),    # dense Schur on the GPU
    "dense421": ("new", "mapper", [], {"COLMAP_BENCH_DENSE_CPU": "999"}),     # dense Schur on the CPU at any size
    "dense421_ob1": ("new", "mapper", [], {"COLMAP_BENCH_DENSE_CPU": "999", "OPENBLAS_NUM_THREADS": "1"}),
    "cpu421_ob1": ("new", "mapper", [], {"OPENBLAS_NUM_THREADS": "1"}),
    "caspar": ("new", "mapper", CASPAR, {}),
    "caspar_g": ("new", "mapper", ["--Mapper.ba_global_backend", "CASPAR"], {}),    # local stays Ceres on the CPU
    # COLMAP 4.2.1's global mapper, with the app's equivalents
    "global": ("new", "global_mapper", GLOBAL, {}),                  # GPU from 50 images up
    "global0": ("new", "global_mapper", GLOBAL, {"COLMAP_BENCH_MIN_GPU_IMAGES": "0"}),
    "global_cpu": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.gp_use_gpu", "0", "--GlobalMapper.ba_ceres_use_gpu", "0"], {}),
    "global_caspar": ("new", "global_mapper", GLOBAL + ["--GlobalMapper.ba_backend", "CASPAR"], {}),
    "global_def": ("new", "global_mapper", [], {}),                  # COLMAP's defaults, as first tried
    # VGGT's cameras for up to 80 well-linked photos, then COLMAP's triangulation and bundle adjustment (vggt_start.py)
    "vggt": ("app", "vggt", [], {}),
}
GPU_METHODS = {"gpuba", "gpuba0", "gpudense", "caspar", "caspar_g", "global", "global0", "global_caspar", "global_def", "vggt"}
assert GPU_METHODS <= set(METHODS)
# feature set: (COLMAP family, extractor arguments, matcher arguments)
SIFT = ["--ImageReader.camera_model", "SIMPLE_RADIAL", "--ImageReader.single_camera", "0", "--ImageReader.mask_path", "/p/masks"]
AFFINE = ["--SiftExtraction.estimate_affine_shape", "1", "--SiftExtraction.domain_size_pooling", "1"]
FEATS = {
    # the app's: affine shape and domain-size pooling, which COLMAP only has on the CPU; matching on the GPU
    "sift391": ("app", SIFT + AFFINE + ["--SiftExtraction.max_image_size", "2400", "--SiftExtraction.use_gpu", "0"],
                ["--SiftMatching.guided_matching", "1", "--SiftMatching.use_gpu", "1"]),
    "sift421": ("new", SIFT + AFFINE + ["--FeatureExtraction.max_image_size", "2400", "--FeatureExtraction.use_gpu", "0"],
                ["--FeatureMatching.guided_matching", "1", "--FeatureMatching.use_gpu", "1"]),
    # without those two, SIFT runs on the GPU
    "siftgpu391": ("app", SIFT + ["--SiftExtraction.max_image_size", "2400", "--SiftExtraction.use_gpu", "1"],
                   ["--SiftMatching.guided_matching", "1", "--SiftMatching.use_gpu", "1"]),
    "siftgpu421": ("new", SIFT + ["--FeatureExtraction.max_image_size", "2400", "--FeatureExtraction.use_gpu", "1"],
                   ["--FeatureMatching.guided_matching", "1", "--FeatureMatching.use_gpu", "1"]),
    # learned features and matcher, through ONNX Runtime's CUDA provider
    "aliked421": ("new", SIFT + ["--FeatureExtraction.type", "ALIKED_N16ROT", "--FeatureExtraction.max_image_size", "2400",
                                 "--FeatureExtraction.use_gpu", "1"],
                  ["--FeatureMatching.type", "ALIKED_LIGHTGLUE", "--FeatureMatching.use_gpu", "1"]),
    "siftlg421": ("new", SIFT + ["--FeatureExtraction.max_image_size", "2400", "--FeatureExtraction.use_gpu", "1"],
                  ["--FeatureMatching.type", "SIFT_LIGHTGLUE", "--FeatureMatching.use_gpu", "1"]),
}
MATCH = ["--ExhaustiveMatching.block_size", "20", "--TwoViewGeometry.min_num_inliers", "12"]
REST_ARGS = ["--Mapper.abs_pose_min_num_inliers", "15", "--Mapper.abs_pose_min_inlier_ratio", "0.15"]


def condition(cond: str) -> tuple[int | None, int | None, bool]:
    """(core limit, thread count to ask for, whether niced as production is) of a condition."""
    if cond == "dev":
        return CORES, CORES, False
    if cond == "prod":
        return None, None, True
    m = re.fullmatch(r"(?:c(\d+))?(?:t(\d+))?", cond)
    assert cond and m, cond
    cpus = int(m.group(1)) if m.group(1) else None
    return cpus, int(m.group(2)) if m.group(2) else cpus, True


def cap() -> int | None:
    """The cores the benchmark may use in all right now (CAP_FILE), or None while there's no such limit."""
    try:
        return int(CAP_FILE.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def capped(job: list[str], n: int) -> list[str] | None:
    """A queue's job as it is run while only n cores may be used: "prod" becomes "c<n>" and "t<M>" "c<n>t<M>", so
    the results are kept apart from those taken with every core. None for a job asking for more threads than n."""
    cond = job[job.index("--cond") + 1] if "--cond" in job else "dev"
    if cond == "dev" or cond.startswith("c"):
        return job
    if cond != "prod" and int(cond[1:]) > n:
        return None
    out = list(job)
    out[out.index("--cond") + 1] = f"c{n}" if cond == "prod" else f"c{n}{cond}"
    return out


def threads(cond: str, flag: str) -> list[str]:
    n = condition(cond)[1]
    return [flag, str(n)] if n else []


def sh(args: list[str]) -> str:
    return " ".join(shlex.quote(str(a)) for a in args)


_DOCKER = None


def docker_works() -> bool:
    """Whether Docker can start a container (it couldn't on 2026-11-13, while hal's root pool was full). Asked once
    per process, so that a queue's steps all run the same way. RUNNER=bwrap skips Docker; RUNNER=docker fails a job
    rather than do without it."""
    global _DOCKER
    if _DOCKER is None:
        _DOCKER = os.environ.get("RUNNER") != "bwrap" and subprocess.run(
            ["docker", "run", "--rm", "--log-driver", "none", "--entrypoint", "true", CUDA_IMAGE],
            capture_output=True).returncode == 0
        if not _DOCKER and os.environ.get("RUNNER") == "docker":
            _DOCKER = None
            raise RuntimeError("Docker can't start a container, and RUNNER=docker says not to do without it")
    return _DOCKER


def pool_room() -> float | None:
    """GB free on hal's root pool, which Docker's own storage is on and which filled up on 2026-11-13 (the
    benchmark keeps everything else on /data). None where there is no such pool."""
    try:
        out = subprocess.run(["zfs", "list", "-Hp", "-o", "avail", "rpool"], capture_output=True, text=True)
    except FileNotFoundError:      # no ZFS at all (atlas)
        return None
    return int(out.stdout) / 2 ** 30 if out.returncode == 0 and out.stdout.strip().isdigit() else None


def launch(cname: str, tools: str, script: str, mounts: list[str], env: dict, cpus: int | None, nice: bool) -> tuple[list[str], list[str], str]:
    """(command, command that stops it, what the tools really were) for a step's script.

    Under Docker: the app's image, or the CUDA image with a tree's /usr, /etc and /opt over its own. Without it:
    bubblewrap on a tree, in a systemd scope with the same memory and task limits, the GPU driver's libraries from
    <BENCH>/nvlibs (copied from the host); the "app" tools are then Ubuntu 24.04's own COLMAP 3.9.1 package in the
    plain tree (/usr/bin, "u391"): the same version and Ceres as the app's, but not its binary."""
    # a dev step is niced right down, so that one going beside a production-condition run takes little from it
    if tools != "app" and not (BENCH / f"build-{tools}" / "usr/local/bin/colmap").exists():
        raise RuntimeError(f"build-{tools} has no COLMAP: build.sh hasn't made it (or failed)")
    v = verify_build.current(tools, BENCH)
    if tools != "app" and tools not in REFERENCE and not (v and v["ok"]):     # the app's image is the baseline as shipped
        raise RuntimeError(f"build-{tools} hasn't passed verify_build.py: not timed")
    # a dev step is niced right down, so that one going beside a production-condition run takes little from it. The
    # first COLMAP process's BLAS and LAPACK, as loaded (its /proc/<pid>/maps), are logged as BENCH_BLAS lines.
    inner = ('(for i in $(seq 300); do for f in /proc/[0-9]*/comm; do if [ "$(cat $f 2>/dev/null)" = colmap ]; then '
             'grep -oE "/[^ ]*(blas|lapack)[^ ]*" ${f%/comm}/maps 2>/dev/null | sort -u | sed "s/^/BENCH_BLAS /"; exit; fi; '
             'done; sleep 1; done) & '
             'TIMEFORMAT="BENCH_TIME %R %U %S"; time ' + ("nice -n 10 " if nice else "nice -n 19 ") + 'bash -ec "$1"')
    env = dict(env)
    mounts = mounts + [f"{HERE}:/bench:ro"]
    (BENCH / "cache").mkdir(exist_ok=True)      # the learned models COLMAP downloads
    if docker_works():
        # nothing of a step's goes to Docker's own storage (hal's root pool): no log kept, /tmp in memory
        cmd = ["docker", "run", "--rm", "--name", cname, "--log-driver", "none", "--tmpfs", "/tmp:exec,size=8g",
               "--memory", MEM, "--memory-swap", MEM, "--pids-limit", "512", "--gpus", "all", "-w", "/tmp"]
        if cpus:
            cmd += ["--cpus", str(cpus)]
        if tools == "app":
            image = APP
            env["HOME"] = "/tmp"
            mounts += [f"{BENCH}/build-plain/usr/lib/x86_64-linux-gnu/openblas-pthread:/ob:ro"]     # for base_ob
        else:
            tree = BENCH / f"build-{tools}"
            image = CUDA_IMAGE
            mounts += [f"{tree}/usr:/usr", f"{tree}/etc:/etc", f"{tree}/opt:/opt:ro", f"{BENCH}/cache:/cache"]
            env.update(HOME="/cache", LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu/libcudss/12")
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        for m in mounts:
            cmd += ["-v", m]
        return cmd + ["--entrypoint", "bash", image, "-c", inner, "_", script], ["docker", "stop", cname], tools
    tree = BENCH / ("build-plain" if tools == "app" else f"build-{tools}")
    cmd = ["systemd-run", "--user", "--scope", "-q", "--unit", cname, "-p", f"MemoryMax={MEM.upper()}", "-p", "MemorySwapMax=0",
           "-p", "TasksMax=512", *(["-p", f"CPUQuota={cpus * 100}%"] if cpus else []),
           "bwrap", "--die-with-parent", "--ro-bind", f"{tree}/usr", "/usr", "--symlink", "usr/lib", "/lib",
           "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
           "--ro-bind", f"{tree}/etc", "/etc", "--ro-bind", f"{tree}/opt", "/opt",
           "--ro-bind", os.path.realpath("/etc/resolv.conf"), "/etc/resolv.conf", "--proc", "/proc", "--dev-bind", "/dev", "/dev",
           "--tmpfs", "/tmp", "--ro-bind", f"{BENCH}/nvlibs", "/nvlibs", "--bind", f"{BENCH}/cache", "/cache", "--chdir", "/tmp"]
    for m in mounts:
        src, dst, *ro = m.split(":")
        cmd += ["--ro-bind" if ro else "--bind", src, dst]
    if tools == "app":
        env.update(HOME="/tmp", PATH="/usr/bin:/bin", QT_QPA_PLATFORM="offscreen",
                   LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/nvlibs")     # not the tree's own Ceres in /usr/local/lib
    else:
        env.update(HOME="/cache", PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/cuda/bin",
                   LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu/libcudss/12:/nvlibs")
    for k, v in env.items():
        cmd += ["--setenv", k, v]
    return (cmd + ["bash", "-c", inner, "_", script], ["systemctl", "--user", "stop", cname + ".scope"],
            "u391" if tools == "app" else tools)


def on_gpu(args: list[str]) -> bool:
    """Whether a COLMAP command's arguments ask for the GPU."""
    return any(a.endswith("use_gpu") and str(b) == "1" for a, b in zip(args, args[1:]))


def step(d: Path, name: str, tools: str, script: str, mounts: list[str], cond: str, env: dict | None = None,
         timeout: float = 8 * 3600, gpu: bool = False) -> dict:
    """Runs a bash script as one timed step with the tools named, unless d/<name>.json says it was.

    A step that uses the GPU (gpu) first takes GPU_LOCK, as anyone else's GPU tests on hal that mustn't overlap a
    timed run do (`flock <BENCH>/gpu.lock <command>`); the wait isn't in its time, and is recorded as lock_wait.
    After LOCK_PATIENCE it goes ahead without the lock (gpu_locked false in its record), rather than hang a queue
    on a holder that never lets go. A container Docker couldn't start leaves no record, so the step is made again.

    Returns the step's record."""
    rec_file = d / f"{name}.json"
    if rec_file.exists():
        return json.loads(rec_file.read_text())
    d.mkdir(parents=True, exist_ok=True)
    cpus, _, nice = condition(cond)
    cname = f"bench-{name}-{os.getpid()}-{int(time.time()) % 100000}"
    cmd, stop, really = launch(cname, tools, script, mounts, env or {}, cpus, nice)
    log = d / f"{name}.log"
    samples = d / f"{name}.gpu.jsonl"
    samples.unlink(missing_ok=True)
    lock, asked, said = None, time.time(), time.time()
    while gpu and lock is None and time.time() - asked < LOCK_PATIENCE:
        lock = open(GPU_LOCK, "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            lock = None
            if time.time() - said > 600:
                said = time.time()
                print(f"{time.strftime('%T')} {d.relative_to(R)} {name}: waiting {(said - asked) / 60:.0f} min for gpu.lock",
                      flush=True)
            time.sleep(2)
    if gpu and lock is None:
        print(f"{time.strftime('%T')} {d.relative_to(R)} {name}: gpu.lock never came free, going ahead without it", flush=True)
    lock_wait = round(time.time() - asked, 1)
    sampler = subprocess.Popen([sys.executable, str(HERE / "gpusample.py"), str(samples), cname])
    started = time.time()
    try:
        with open(log, "w") as f:
            f.write(f"# {sh(cmd)}\n")
            f.flush()
            try:
                rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 124
    finally:
        sampler.terminate()
        subprocess.run(stop, capture_output=True)
        if lock:
            lock.close()
    text = log.read_text(errors="replace")
    if rc == 125 and docker_works():
        raise RuntimeError(f"Docker couldn't start the container: {text[-300:]!r}")
    t = re.findall(r"BENCH_TIME ([\d.]+) ([\d.]+) ([\d.]+)", text)
    wall, user, system = (float(x) for x in t[-1]) if t else (time.time() - started, 0.0, 0.0)
    rec = {"step": name, "tools": really, "runner": "docker" if docker_works() else "bwrap", "cond": cond, "rc": rc,
           "wall": wall, "cpu_s": round(user + system, 1),
           "outer_wall": round(time.time() - started, 1), "started": time.strftime("%F %T", time.localtime(started)),
           "gpu_locked": lock is not None, "lock_wait": lock_wait,
           "verify": {k: v for k, v in (verify_build.current("app" if really == "app" else tools, BENCH) or {}).items()
                      if k in ("variant", "ok", "failed", "when")},
           "marks": {k: float(v) for k, v in re.findall(r"BENCH_MARK (\w+) ([\d.]+)", text)},
           "blas": sorted(set(re.findall(r"^BENCH_BLAS (\S+)", text, re.M))),
           "host": gpusample.summary(str(samples)) if samples.exists() else {}}
    rec_file.write_text(json.dumps(rec, indent=1))
    print(f"{time.strftime('%T')} {d.relative_to(R)} {name}: {wall:.0f} s, rc {rc}, {rec['cpu_s']:.0f} CPU-s, "
          f"{flags(rec['host']) or 'quiet'}", flush=True)
    return rec


def flags(host: dict) -> str:
    """What else was running during a step: W the BoulderMesh worker, B the benchmark's own other containers (a
    build), C other users' CPU work (4+ cores), G someone else's processes on the GPU."""
    cores = host.get("cores", {})
    others = {k: v for k, v in cores.items() if k not in ("own", "bmw", "bench", "tom", "root")}
    out = []
    if cores.get("bmw", 0) >= 0.5 or "bmw" in host.get("others_on_gpu", []):
        out.append(f"W({cores.get('bmw', 0):.0f})")
    if cores.get("bench", 0) >= 0.5:
        out.append(f"B({cores['bench']:.0f})")
    if sum(others.values()) >= 4:
        out.append("C(" + ",".join(f"{k}:{v:.0f}" for k, v in others.items() if v >= 1) + ")")
    if host.get("other_mib_max", 0) >= 300:
        out.append(f"G({','.join(host['others_on_gpu'])}:{host['other_mib_max']}MiB)")
    return " ".join(out)


def feat_dir(P: str, feat: str, cond: str = "dev", build: str = "plain") -> Path:
    name = feat + (f"+{build}" if build != "plain" and FEATS[feat][0] == "new" else "") + (f"@{cond}" if cond != "dev" else "")
    return R / P / name


def features(P: str, feat: str, cond: str, build: str) -> Path:
    """A boulder's features and matches. The first set made of the app's (sift391, dev) is the one every mapper run
    starts from, so that only the mapper varies; one made under another condition is only for its timing."""
    family, extract, match = FEATS[feat]
    tools = "app" if family == "app" else build
    d = feat_dir(P, feat, cond, build)
    d.mkdir(parents=True, exist_ok=True)
    old = BENCH / "runs" / P / "database.db"      # made by the first version of these scripts, at 4 cores
    if feat == "sift391" and cond == "dev" and old.exists() and not (d / "database.db").exists():
        shutil.copyfile(old, d / "database.db")
        (d / "match.json").write_text(json.dumps({"step": "match", "rc": 0, "adopted": str(old)}))
        (d / "extract.json").write_text(json.dumps({"step": "extract", "rc": 0, "adopted": str(old)}))
    if not (d / "extract.json").exists():
        (d / "database.db").unlink(missing_ok=True)
    log = [] if family == "app" else ["--log_target", "stderr"]
    spell = "Sift" if family == "app" else "Feature"
    mounts = [f"{BENCH}/projects/{P}:/p:ro", f"{d}:/w"]
    step(d, "extract", tools, sh(["colmap", "feature_extractor", *log, "--database_path", "/w/database.db",
                                  "--image_path", "/p/photos", *extract, *threads(cond, f"--{spell}Extraction.num_threads")]),
         mounts, cond, gpu=on_gpu(extract))
    step(d, "match", tools, sh(["colmap", "exhaustive_matcher", *log, "--database_path", "/w/database.db", *MATCH, *match,
                                *threads(cond, f"--{spell}Matching.num_threads")]), mounts, cond, gpu=on_gpu(match))
    return d


def run_name(method: str, seed: int, cond: str, build: str, rep: int) -> str:
    return (f"{method}-s{seed}" + (f"+{build}" if build != "plain" and METHODS[method][0] == "new" else "")
            + (f"@{cond}" if cond != "dev" else "") + (f"-r{rep}" if rep > 1 else "") + ("" if docker_works() else "~bw"))


def run_parts(run: str) -> tuple[str, str, str]:
    """(method, build, condition) of a run's name."""
    m = re.fullmatch(r"(\w+?)-s\d+(?:\+([a-z]+))?(?:@(\w+))?(?:-r\d+)?(?:~bw)?", run)
    return m.group(1), m.group(2) or "plain", m.group(3) or "dev"


def mapper_script(method: str, seed: int, cond: str, more: list[str], out: str = "/r/sparse") -> str:
    """A method's mapper command, on /r/database.db."""
    family, command, extra, _ = METHODS[method]
    if command == "vggt":
        return "python3 /bench/vggt_start.py /r/database.db /photos /r"
    if command == "global_mapper":
        args = ["--log_target", "stderr", "--default_random_seed", seed, "--GlobalMapper.random_seed", seed,
                *threads(cond, "--GlobalMapper.num_threads")]
    elif family == "new":
        args = ["--log_target", "stderr", "--default_random_seed", seed, "--Mapper.random_seed", seed, *INC,
                *threads(cond, "--Mapper.num_threads")]
    else:
        args = ["--random_seed", seed, *INC, *threads(cond, "--Mapper.num_threads")]
    return sh(["colmap", command, "--database_path", "/r/database.db", "--image_path", "/photos",
               "--output_path", out, *args, *extra, *more])


def models_of(sparse: Path) -> dict[str, list[str]]:
    return {m.name: sorted(stats.images(m)) for m in sorted(sparse.iterdir()) if (m / "images.bin").exists()}


def mapper(P: str, method: str, seed: int, feat: str, cond: str, build: str, rep: int) -> str:
    """One mapper run on a boulder's features. Returns the run's name."""
    family, command, _, env = METHODS[method]
    assert family == "new" or FEATS[feat][0] == "app", "3.9.1 can't read a 4.2.1 database"
    tools = "app" if family == "app" else build
    run = run_name(method, seed, cond, build, rep)
    d = feat_dir(P, feat) / run
    if (d / "map.json").exists():
        return run
    if not (feat_dir(P, feat) / "database.db").exists():      # the queue had no feat line for it: made (or adopted) here
        features(P, feat, "dev", build)
    shutil.rmtree(d, ignore_errors=True)
    if d.exists():      # an interrupted run's files that its container wrote as root: put aside, as they can't be deleted
        d.rename(d.with_name(f"{d.name}.stale{int(time.time())}"))
    (d / "sparse").mkdir(parents=True)
    shutil.copyfile(feat_dir(P, feat) / "database.db", d / "database.db")     # COLMAP 4 rewrites a 3.9.1 database
    mounts = [f"{BENCH}/projects/{P}/photos:/photos:ro", f"{d}:/r"]
    if command == "vggt":       # its weights (4.7 GB), copied from the worker's cache the first time
        cache = BENCH / "cache" / "models"
        if not cache.exists():
            shutil.copytree("/data/tom/data/cache/models/huggingface", cache / "huggingface", symlinks=True)
        mounts.append(f"{cache}:/data/cache/models")
        env = {"DATA_DIR": "/data", "HF_HUB_OFFLINE": "1"}
    step(d, "map", tools, mapper_script(method, seed, cond, []), mounts, cond, env, timeout=14 * 3600,
         gpu=method in GPU_METHODS)
    (d / "database.db").unlink(missing_ok=True)
    (d / "models.json").write_text(json.dumps(models_of(d / "sparse")))
    return run


def held(P: str, method: str, seed: int, percent: int, feat: str, cond: str, build: str) -> None:
    """What adding photos to a finished model costs: the mapper on a random <percent>% of a boulder's photos, then
    the rest added three ways, each from that model: the mapper carried on with the old cameras fixed (add_fixed),
    carried on with nothing fixed (add_free), and the app's image_registrator + bundle_adjuster (add_reg). All with
    the method's own COLMAP. heldstats compares them with the method's run on every photo."""
    import random
    import sqlite3
    family, command, _, env = METHODS[method]
    assert command == "mapper"
    tools = "app" if family == "app" else build
    new = tools != "app"
    d = feat_dir(P, feat) / f"held{percent}-{run_name(method, seed, cond, build, 1)}"
    if (d / "add_reg.json").exists():
        return
    d.mkdir(parents=True, exist_ok=True)
    db = feat_dir(P, feat) / "database.db"
    if not db.exists():
        features(P, feat, "dev", build)
    if not (d / "kept.txt").exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        names = sorted(n for n, in con.execute("select name from images"))
        kept = sorted(random.Random(f"{P}-{seed}-{percent}").sample(names, round(len(names) * percent / 100)))
        (d / "kept.txt").write_text("".join(f"{n}\n" for n in kept))
        shutil.copyfile(db, d / "database.db")
        (d / "sparse").mkdir(exist_ok=True)
    mounts = [f"{BENCH}/projects/{P}/photos:/photos:ro", f"{d}:/r"]
    listed = ["--Mapper.image_list_path" if new else "--image_list_path", "/r/kept.txt"]
    step(d, "map", tools, mapper_script(method, seed, cond, listed), mounts, cond, env, timeout=14 * 3600,
         gpu=method in GPU_METHODS)
    models = models_of(d / "sparse")
    (d / "models.json").write_text(json.dumps(models))
    if not models:
        return
    best = max(models, key=lambda m: len(models[m]))
    fix = ["--Mapper.fix_existing_frames" if new else "--Mapper.fix_existing_images", "1"]
    log = ["--log_target", "stderr"] if new else []
    for name, more in (("add_fixed", fix), ("add_free", [])):
        step(d, name, tools, f"mkdir -p /r/{name}\n" + mapper_script(method, seed, cond, ["--input_path", f"/r/sparse/{best}", *more],
                                                                    f"/r/{name}"), mounts, cond, env, timeout=14 * 3600,
             gpu=method in GPU_METHODS)
    step(d, "add_reg", tools, "mkdir -p /r/add_reg\necho BENCH_MARK register $(date +%s.%N)\n"
         + sh(["colmap", "image_registrator", *log, "--database_path", "/r/database.db", "--input_path", f"/r/sparse/{best}",
               "--output_path", "/r/add_reg", *REST_ARGS, *threads(cond, "--Mapper.num_threads")])
         + "\necho BENCH_MARK adjust $(date +%s.%N)\n"
         + sh(["colmap", "bundle_adjuster", *log, "--input_path", "/r/add_reg", "--output_path", "/r/add_reg",
               *(["--BundleAdjustmentCeres.use_gpu", "1"] if new else [])])
         + "\necho BENCH_MARK done $(date +%s.%N)", mounts, cond, env, gpu=new)
    (d / "database.db").unlink(missing_ok=True)


def heldstats() -> None:
    """Each held-out test against its method's run on every photo: seconds to add against the full run's mapper,
    held-out photos placed against those the full run placed (in its largest model), how far the photos already
    placed moved, and how well the added cameras agree with the full run's (stats.agreement, fitted on the old ones)."""
    print("boulder\trun\tkept\theld\tsubset_s\tsubset_placed\tfull_s\tfull_placed\tfull_placed_of_held\tway\tadd_s"
          "\tplaced_of_held\told_moved_pos\told_moved_deg\tadded\tadded_pos_median\tadded_pos_p90\tadded_deg_median\tadded_deg_p90")
    for d in sorted(R.glob("*/*/held*")):
        if not (d / "models.json").exists():
            continue
        m = re.fullmatch(r"held(\d+)-(.*)", d.name)
        full = d.parent / m.group(2)
        models = json.loads((d / "models.json").read_text())
        if not models or not (full / "models.json").exists():
            continue
        best = max(models, key=lambda k: len(models[k]))
        kept = set((d / "kept.txt").read_text().split("\n")) - {""}
        every = {n for v in json.loads((full / "models.json").read_text()).values() for n in v} | kept
        fm = json.loads((full / "models.json").read_text())
        fbest = max(fm, key=lambda k: len(fm[k]))
        full_poses, sub_poses = stats.poses(full / "sparse" / fbest), stats.poses(d / "sparse" / best)
        sec = lambda f: json.loads(f.read_text())["wall"] if f.exists() else float("nan")      # noqa: E731
        con_names = set(full_poses) | kept
        for way in ("add_fixed", "add_free", "add_reg"):
            if not (d / way / "images.bin").exists():
                continue
            p = stats.poses(d / way)
            added = set(p) - set(sub_poses)
            moved = stats.agreement(sub_poses, p) or {}
            ag = stats.agreement(p, full_poses, fit=set(sub_poses), report=added) or {}
            held_out = (every | set(p) | con_names) - kept
            print("\t".join(str(x) for x in [
                d.parts[-3], d.name, len(kept), len(held_out), round(sec(d / "map.json")), len(sub_poses),
                round(sec(full / "map.json")), len(full_poses), len(set(full_poses) - kept), way, round(sec(d / f"{way}.json")),
                len(added), moved.get("pos_median", ""), moved.get("deg_median", ""), ag.get("cameras", ""),
                ag.get("pos_median", ""), ag.get("pos_p90", ""), ag.get("deg_median", ""), ag.get("deg_p90", "")]))


def rest(P: str, run: str, feat: str) -> None:
    """The app's step after the mapper, on a run's largest model: the photos the mapper left out are tried against
    it (image_registrator, REST_ARGS), then everything is refined together (bundle_adjuster). Photos whose camera
    is still the database's first guess are dropped before and after, as OpenMVS aborts on them. With the COLMAP
    the features were made by (3.9.1 for the app's), so 4.2.1's models are written as text for it first."""
    d = feat_dir(P, feat) / run
    o = d / "rest"
    if (o / "placed.json").exists() or not (d / "models.json").exists():
        return
    models = json.loads((d / "models.json").read_text())
    o.mkdir(exist_ok=True)
    if not models:
        (o / "placed.json").write_text("[]")
        return
    best = max(models, key=lambda m: len(models[m]))
    method, build, cond = run_parts(run)
    made_by = "app" if METHODS[method][0] == "app" else build
    tools = "app" if FEATS[feat][0] == "app" else build
    new = tools != "app"
    log = ["--log_target", "stderr"] if new else []
    mounts = [f"{d}:/r"]
    step(o, "convert", made_by, sh(["colmap", "model_converter", *(["--log_target", "stderr"] if made_by != "app" else []),
                                    "--input_path", f"/r/sparse/{best}", "--output_path", "/r/rest/txt",
                                    "--output_type", "TXT"]).replace("colmap", "mkdir -p /r/rest/txt; colmap", 1), mounts, "dev")
    shutil.copyfile(feat_dir(P, feat) / "database.db", o / "db.db")
    drop = lambda src, dst, names: (f"python3 /bench/stats.py unrefined db.db {src} > {names}\n"      # noqa: E731
                                    f"if [ -s {names} ]; then mkdir -p {dst}; colmap image_deleter {sh(log)} --input_path {src} "
                                    f"--output_path {dst} --image_names_path {names}; else cp -r {src} {dst}; fi\n")
    script = ("cd /r/rest\nrm -rf kept rest rest2 drop.txt drop2.txt\n"         # an interrupted attempt's
              + drop("txt", "kept", "drop.txt") + "mkdir -p rest\necho BENCH_MARK register $(date +%s.%N)\n"
              + sh(["colmap", "image_registrator", *log, "--database_path", "db.db", "--input_path", "kept",
                    "--output_path", "rest", *REST_ARGS, *threads(cond, "--Mapper.num_threads")])
              + "\necho BENCH_MARK adjust $(date +%s.%N)\n"
              + sh(["colmap", "bundle_adjuster", *log, "--input_path", "rest", "--output_path", "rest",
                    *(["--BundleAdjustmentCeres.use_gpu", "1"] if new else [])])
              + "\necho BENCH_MARK done $(date +%s.%N)\n" + drop("rest", "rest2", "drop2.txt"))
    env = METHODS[method][3] if made_by == "app" and METHODS[method][1] == "mapper" else {}     # base_ob's BLAS here too
    rec = step(o, "rest", tools, script, mounts, cond, env, gpu=new)
    (o / "db.db").unlink(missing_ok=True)
    placed = sorted(stats.images(o / "rest2")) if rec["rc"] == 0 and (o / "rest2").exists() else []
    (o / "placed.json").write_text(json.dumps(placed))


def post(P: str, run: str, feat: str, tools: str, cond: str) -> None:
    """The app's steps after placing: undistort (COLMAP), import, densify, mesh and texture (OpenMVS), each timed,
    from a run's model after rest(). Without the people masks the app gives OpenMVS."""
    d = feat_dir(P, feat) / run
    o = d / (f"post-{tools}" + (f"@{cond}" if cond != "dev" else ""))
    if not (d / "rest" / "rest2").exists():
        return
    new = tools != "app"
    log = ["--log_target", "stderr"] if new else []
    mvs = MVS_NEW if new else MVS_APP
    mounts = [f"{BENCH}/projects/{P}/photos:/photos:ro", f"{d}/rest/rest2:/model:ro", f"{o}:/o"]
    t = threads(cond, "--max-threads")
    steps = [
        ("undistort", sh(["colmap", "image_undistorter", *log, "--image_path", "/photos", "--input_path", "/model",
                          "--output_path", "/o/dense", "--output_type", "COLMAP"])),
        # the folder in full, as the app gives it: OpenMVS 2.4 takes a relative one from the input folder, 2.3 from -w
        ("import", sh([f"{mvs}/InterfaceCOLMAP", "-i", "dense", "-o", "scene.mvs", "--image-folder", "/o/dense/images", "-w", "/o"])),
        # without an interrupted attempt's depth maps, which it would take up rather than make
        ("dense", "rm -f depth*.dmap scene_dense*\n"
         + sh([f"{mvs}/DensifyPointCloud", "scene.mvs", "-w", "/o", "--resolution-level", "2", *t])),
        ("mesh", sh([f"{mvs}/ReconstructMesh", "scene_dense.mvs", "-w", "/o", *t])),
        ("texture", sh([f"{mvs}/TextureMesh", "scene_dense.mvs", "-m", "scene_dense_mesh.ply", "-o",
                        "scene_dense_mesh_texture.mvs", "-w", "/o", "--export-type", "glb", "--empty-color", str(0x808080),
                        "--max-texture-size", "4096", *t])),
    ]
    for name, script in steps:      # the newer OpenMVS is built with CUDA; the app's, and undistorting, are CPU only
        if step(o, name, tools, "cd /o\n" + script, mounts, cond, timeout=4 * 3600, gpu=new and name != "undistort")["rc"] != 0:
            break


def agree(only: str | None = None) -> None:
    """Every pair of models of each boulder laid over each other (stats.agreement): this benchmark's runs (after the
    app's rest step where it was run, else the mapper's largest model), the first scripts' runs (runs/) and the
    pycolmap probes (probe/). One row a pair: how many cameras each places and both, and how far those both place
    are apart. The baseline against itself on other seeds is the spread any other method has to be judged by."""
    print("boulder\ta\tb\tplaced_a\tplaced_b\tshared\tpos_median\tpos_p90\tdeg_median\tdeg_p90")
    for proj in sorted((BENCH / "projects").iterdir()):
        P = proj.name
        if only and P != only:
            continue
        models = {}
        for d in sorted(R.glob(f"{P}/*/*")):
            if ".stale" in d.name:
                continue
            if (d / "rest" / "rest2" / "images.bin").exists():
                models[f"{d.parent.name}/{d.name}+rest"] = d / "rest" / "rest2"
            elif (d / "sparse").is_dir() and stats.largest(d / "sparse")[0]:
                models[f"{d.parent.name}/{d.name}"] = d / "sparse" / stats.largest(d / "sparse")[0]
        for d in sorted((BENCH / "runs" / P).glob("*-s*")):
            if not d.name.startswith(("failed", "old")) and (d / "sparse").is_dir() and stats.largest(d / "sparse")[0]:
                models[f"runs/{d.name}"] = d / "sparse" / stats.largest(d / "sparse")[0]
        for d in sorted((BENCH / "probe").glob(f"{P}-*")):
            if d.is_dir() and stats.largest(d)[0]:
                models[f"probe/{d.name[len(P) + 1:]}"] = d / stats.largest(d)[0]
        poses = {k: stats.poses(v) for k, v in models.items()}
        names = sorted(poses)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ag = stats.agreement(poses[a], poses[b]) or {}
                print("\t".join(str(x) for x in [P, a, b, len(poses[a]), len(poses[b]), len(set(poses[a]) & set(poses[b])),
                                                 ag.get("pos_median", ""), ag.get("pos_p90", ""), ag.get("deg_median", ""),
                                                 ag.get("deg_p90", "")]))


def table() -> None:
    """Every step recorded so far, one row each."""
    cols = ["boulder", "photos", "feat", "run", "sub", "step", "tools", "cond", "rc", "wall_s", "cpu_s", "cores",
            "started", "flags", "own_gpu_mib", "gpu_util_own", "gpu_share", "load", "ba_cuda", "ba_cpu", "gp_cuda", "gp_cpu",
            "ba_fallbacks", "models", "placed", "after_rest", "points", "track", "vs_base_cams", "vs_base_pos", "vs_base_deg",
            "lock_wait", "blas"]
    print("\t".join(cols))
    for rec_file in sorted(R.glob("*/*/**/*.json")):
        if rec_file.name in ("models.json", "placed.json") or ".stale" in str(rec_file):
            continue
        rec = json.loads(rec_file.read_text())
        if "wall" not in rec:
            continue
        parts = rec_file.relative_to(R).parts
        P, feat = parts[0], parts[1]
        run = parts[2] if len(parts) > 3 else ""
        sub = parts[3] if len(parts) > 4 else ""
        host = rec.get("host", {})
        row = {"boulder": P, "photos": len(list((BENCH / "projects" / P / "photos").iterdir())), "feat": feat, "run": run,
               "sub": sub, "step": rec["step"], "tools": rec["tools"], "cond": rec["cond"], "rc": rec["rc"],
               "wall_s": round(rec["wall"], 1), "cpu_s": rec["cpu_s"],
               "cores": round(rec["cpu_s"] / rec["wall"], 1) if rec["wall"] else "", "started": rec["started"],
               "flags": flags(host), "own_gpu_mib": host.get("own_mib_max", ""), "gpu_util_own": host.get("util_mean_own", ""),
               "gpu_share": host.get("own_gpu_share", ""), "load": host.get("load_mean", ""),
               "lock_wait": rec.get("lock_wait", ""),
               "blas": "+".join(Path(b).name for b in rec.get("blas", []))}
        log = rec_file.with_suffix(".log")
        if rec["step"] in ("map", "rest") and log.exists():
            text = log.read_text(errors="replace")
            for key, pat in (("ba_cuda", r"BENCH_BA .*device=cuda"), ("ba_cpu", r"BENCH_BA .*device=cpu"),
                             ("gp_cuda", r"BENCH_GP .*device=cuda"), ("gp_cpu", r"BENCH_GP .*device=cpu"),
                             ("ba_fallbacks", r"retrying with CPU|Falling back to CPU")):
                row[key] = len(re.findall(pat, text))
        if rec["step"] == "map":
            d = rec_file.parent
            if (d / "models.json").exists():
                models = json.loads((d / "models.json").read_text())
                row["models"] = "+".join(str(n) for n in sorted((len(v) for v in models.values()), reverse=True))
                row["placed"] = max((len(v) for v in models.values()), default=0)
            if (d / "rest" / "placed.json").exists():
                row["after_rest"] = len(json.loads((d / "rest" / "placed.json").read_text()))
            if (d / "rest" / "rest2" / "points3D.bin").exists():
                n, track = stats.points(d / "rest" / "rest2")
                row["points"], row["track"] = n, round(track, 2)
                # against the baseline's model for the same seed and condition, after the app's rest step in both
                ref = d.parent / re.sub(r"\+[a-z]+", "", re.sub(r"^\w+?-s", "base-s", run)) / "rest" / "rest2"
                if ref != d / "rest" / "rest2" and (ref / "images.bin").exists():
                    ag = stats.agreement(stats.poses(d / "rest" / "rest2"), stats.poses(ref)) or {}
                    row["vs_base_cams"], row["vs_base_pos"], row["vs_base_deg"] = (ag.get("cameras", ""), ag.get("pos_median", ""),
                                                                                  ag.get("deg_median", ""))
        print("\t".join(str(row.get(c, "")) for c in cols))


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["feat", "map", "rest", "post", "all", "held", "heldstats", "queue", "table", "agree"])
    p.add_argument("args", nargs="*")
    p.add_argument("--feat", default="sift391")
    p.add_argument("--cond", default="dev")
    p.add_argument("--build", default="plain")
    p.add_argument("--tools", default="app")
    p.add_argument("--post", default="")
    p.add_argument("--rep", type=int, default=1)
    a = p.parse_args(argv)
    if a.command == "feat":
        features(a.args[0], a.feat, a.cond, a.build)
    elif a.command == "map":
        mapper(a.args[0], a.args[1], int(a.args[2]), a.feat, a.cond, a.build, a.rep)
    elif a.command == "rest":
        rest(a.args[0], a.args[1], a.feat)
    elif a.command == "post":
        post(a.args[0], a.args[1], a.feat, a.tools, a.cond)
    elif a.command == "all":
        run = mapper(a.args[0], a.args[1], int(a.args[2]), a.feat, a.cond, a.build, a.rep)
        rest(a.args[0], run, a.feat)
        for tools in filter(None, a.post.split(",")):
            post(a.args[0], run, a.feat, tools, a.cond)
    elif a.command == "held":
        held(a.args[0], a.args[1], int(a.args[2]), int(a.args[3]), a.feat, a.cond, a.build)
    elif a.command == "heldstats":
        heldstats()
    elif a.command == "table":
        table()
    elif a.command == "agree":
        agree(a.args[0] if a.args else None)
    elif a.command == "queue":      # read again before each job, so that jobs can be added to the file while it runs
        done, status = 0, Path(a.args[0]).with_suffix(".status")      # the job it is on, for anyone looking
        while True:
            lines = [l for l in Path(a.args[0]).read_text().split("\n") if l.strip() and not l.startswith("#")]
            if done >= len(lines):
                status.write_text(f"finished {time.strftime('%F %T')} ({len(lines)} jobs)\n")
                break
            room = pool_room()
            if room is not None and room < POOL_MIN_GB:
                print(f"{time.strftime('%T')} STOPPED before \"{lines[done]}\": {room:.0f} GB free on rpool", flush=True)
                status.write_text(f"stopped {time.strftime('%F %T')} at job {done + 1} of {len(lines)}: rpool nearly full\n")
                sys.exit(3)
            job, n = shlex.split(lines[done]), cap()
            job = capped(job, n) if n else job
            done += 1
            if job is None:
                print(f"{time.strftime('%T')} SKIPPED under the {n}-core cap: {lines[done - 1]}", flush=True)
                continue
            status.write_text(f"job {done} of {len(lines)} since {time.strftime('%F %T')}: {sh(job)}\n")
            print(f"{time.strftime('%T')} JOB {done}/{len(lines)} of {Path(a.args[0]).name}: {sh(job)}", flush=True)
            try:
                main(job)
            except Exception as e:      # one failing job doesn't stop the rest
                print(f"{time.strftime('%T')} FAILED {sh(job)}: {e!r}", flush=True)
            except SystemExit as e:
                if e.code != 2:         # 2: a line argparse couldn't read
                    raise
                print(f"{time.strftime('%T')} FAILED {sh(job)}: not a job bench.py knows", flush=True)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))     # so a killed step still stops its container
    main(sys.argv[1:])
