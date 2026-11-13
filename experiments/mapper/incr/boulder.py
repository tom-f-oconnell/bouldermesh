"""A boulder's reconstruction as a state that photos are added to: building it and adding to it are one call.

    boulder.py add <state dir> <photos: files or a folder> [--masks dir] [--init auto|vggt|global|incremental]
                   [--ba auto|cpu|gpu|caspar] [--feat sift|siftgpu|aliked] [--cond dev|prod|tN] [--seed N]
                   [--no-mesh] [--no-vggt] [--adopt database.db]
    boulder.py mesh <state dir> [--cond ..]                 the mesh again, of the model as it is
    boulder.py bootstrap <state dir> <mesh dir> <photos...> [--masks dir]   a state from a finished mesh's cameras.json
    boulder.py cameras <model dir> <out.json>                a model's cameras as a mesh's cameras.json
    boulder.py boot <project> <state dir>                   the test: a state's cameras as a mesh's, and a state from them
    boulder.py status <state dir>
    boulder.py first <project> <init> <seed> [...]          a first build of a benchmark boulder (every photo)
    boulder.py held <project> <init> <seed> <percent> [--batches N] [...]   one of a random <percent>%, the rest added
    boulder.py scenario <project> <name> [...]              the add tests: nothing, duplicates, another rock, interrupted
    boulder.py queue <file>                                 a file of the three above, one a line, one at a time
    boulder.py table                                        every step of every state under <BENCH>/incr/r
    boulder.py compare [project]                            first builds and adds laid over each other and the baseline
    boulder.py heldstats [project]                          held-out states against the from-scratch build of every photo

add() copies in the photos the state doesn't have (by content), then runs the steps, each in its own container, timed
and with the machine sampled as the mapper benchmark's are (bench.py's launch() and gpusample.py): features for the
new photos, matches for the new pairs, VGGT's cameras when a model has to be started, placement (core.py), VGGT again
for photos that are linked to the model yet could not be registered, and, if the new photos show more rock, the dense
steps, mesh and texture. A state with no model gets a first build; one with a model has photos added to it with its
cameras held fixed. Steps that use the GPU hold <BENCH>/gpu.lock, as the benchmark's timed steps do.

Tools: "incr" is the tree build-env.sh makes (pycolmap on the benchmark's patched COLMAP 4.2.1, OpenMVS 2.4 with
CUDA); "app" is the app's -gpu image, for VGGT. States of the tests live under <BENCH>/incr/r/<project>/<run>.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = Path(os.environ.get("BENCH", "/data/tom/mapperbench"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BENCH / "scripts" if (BENCH / "scripts" / "bench.py").exists() else HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "backend"))       # meshclean, for stats.mesh_stats
import bench  # noqa: E402
import gpusample  # noqa: E402
import stats  # noqa: E402
import state as st  # noqa: E402

R = BENCH / "incr" / "r"
GPU_LOCK = BENCH / "gpu.lock"
MODELS = BENCH / "cache" / "models"        # VGGT's weights, as bench.py keeps them
TOOLS = os.environ.get("INCR_TOOLS", "incr-full")      # the tree (build-<TOOLS>, build-env.sh) the steps run in


def verified(tools: str) -> str:
    """What the benchmark's verify_build.py found of a tree, as of the binaries there now: "verified" with the BLAS and
    sparse libraries its run loaded, or what it lacks."""
    if tools == "app":
        return "the app's image: torch only (on CUDA, checked by the step)"
    import verify_build
    v = verify_build.current(tools, BENCH)
    if not v:
        return "not verified (no verify.json of these binaries)"
    rows = v.get("rows", [])
    loaded = sorted({r["got"] for r in rows if "loaded" in r.get("item", "") and r.get("ok")})
    bad = [f"{r['part']}: {r['item']}" for r in rows if r.get("ok") is False]
    return ("verified" if v["ok"] else "NOT verified: " + "; ".join(bad)) + (f" (loaded: {', '.join(loaded)})" if loaded else "")


def run_step(logdir: Path, name: str, tools: str, script: str, mounts: list[str], cond: str, env: dict | None = None,
             gpu: bool = False, timeout: float = 6 * 3600) -> dict:
    """One timed step in a container, as bench.py's step(): its log, a record (seconds, CPU seconds, exit code, what
    else ran on the machine, and which device each bundle adjustment in it used) and the machine's samples."""
    logdir.mkdir(parents=True, exist_ok=True)
    cpus, _, nice = bench.condition(cond)
    cname = f"bench-incr-{name}-{os.getpid()}-{int(time.time()) % 100000}"
    own = f"{os.getuid()}:{os.getgid()}"       # the container is root: hand what it made back
    script = f"trap 'chown -R {own} /s 2>/dev/null || true' EXIT\n{script}"
    backend = HERE.parents[2] / "backend"       # captureplan.py, read only
    mounts = mounts + [f"{HERE}:/incr:ro", f"{backend}:/backend:ro"]
    if tools == "app":      # VGGT: only the image's torch is used, which the step checks is on CUDA (vggt_cams.py)
        cmd, stop, really = launch_app(cname, script, mounts, env or {}, cpus, nice), ["docker", "stop", cname], "app"
    else:       # the tree, which bench.launch refuses unless verify_build.py passed it
        cmd, stop, really = bench.launch(cname, tools, script, mounts, env or {}, cpus, nice)
    lock, asked = None, time.time()
    if gpu:
        lock = open(GPU_LOCK, "a")
        fcntl.flock(lock, fcntl.LOCK_EX)
    lock_wait = round(time.time() - asked, 1)
    log, samples = logdir / f"{name}.log", logdir / f"{name}.gpu.jsonl"
    samples.unlink(missing_ok=True)
    sampler = subprocess.Popen([sys.executable, gpusample.__file__, str(samples), cname])
    started = time.time()
    try:
        with open(log, "w") as f:
            f.write(f"# {bench.sh(cmd)}\n")
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
    t = re.findall(r"BENCH_TIME ([\d.]+) ([\d.]+) ([\d.]+)", text)
    wall, user, system = (float(x) for x in t[-1]) if t else (time.time() - started, 0.0, 0.0)
    count = lambda pat: len(re.findall(pat, text))      # noqa: E731
    rec = {"step": name, "tools": really, "build": verified(really), "cond": cond, "rc": rc, "wall": wall, "cpu_s": round(user + system, 1),
           "started": time.strftime("%F %T", time.localtime(started)), "gpu_locked": gpu, "lock_wait": lock_wait,
           "marks": {k: float(v) for k, v in re.findall(r"BENCH_MARK (\w+) ([\d.]+)", text)},
           "ba": {"cuda": count(r"BENCH_BA .*device=cuda"), "cpu": count(r"BENCH_BA .*device=cpu"),
                  "caspar": count(r"BENCH_BA .*solver=CASPAR"), "gp_cuda": count(r"BENCH_GP .*device=cuda"),
                  "gp_cpu": count(r"BENCH_GP .*device=cpu"), "fallbacks": count(r"retrying with CPU|Falling back to CPU")},
           "host": gpusample.summary(str(samples)) if samples.exists() else {}}
    (logdir / f"{name}.json").write_text(json.dumps(rec, indent=1))
    print(f"{time.strftime('%T')} {logdir.parent.parent.name}/{logdir.name} {name}: {wall:.0f} s, rc {rc}, {rec['cpu_s']:.0f} CPU-s, "
          f"{bench.flags(rec['host']) or 'quiet'}", flush=True)
    return rec


def launch_app(cname: str, script: str, mounts: list[str], env: dict, cpus: int | None, nice: bool) -> list[str]:
    """A step in the app's -gpu image, as bench.launch runs one. bench.launch refuses the image for timing, as
    verify_build.py fails its Ceres (the reference BLAS); the VGGT steps use none of COLMAP or Ceres, only torch,
    and vggt_cams.py fails rather than run VGGT anywhere but on CUDA."""
    inner = 'TIMEFORMAT="BENCH_TIME %R %U %S"; time ' + ("nice -n 10 " if nice else "nice -n 19 ") + 'bash -ec "$1"'
    cmd = ["docker", "run", "--rm", "--name", cname, "--log-driver", "none", "--tmpfs", "/tmp:exec,size=8g", "--memory", bench.MEM,
           "--memory-swap", bench.MEM, "--pids-limit", "512", "--gpus", "all", "-w", "/tmp", "-e", "HOME=/tmp",
           "-v", f"{bench.HERE}:/bench:ro"]
    if cpus:
        cmd += ["--cpus", str(cpus)]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    for m in mounts:
        cmd += ["-v", m]
    return cmd + ["--entrypoint", "bash", bench.APP, "-c", inner, "_", script]


def expand(photos: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, photos):
        out += sorted(q for q in p.iterdir() if q.suffix.lower() in st.PHOTO_TYPES) if p.is_dir() else [p]
    return out


def add(state_dir: Path | str, photos: list[Path], masks: Path | None = None, init: str = "auto", ba: str = "auto",
        feat: str = "sift", cond: str = "dev", seed: int = 0, mesh: bool = True, vggt: bool = True,
        adopt: Path | None = None, die_after: str | None = None, poses: Path | None = None) -> dict:
    """Adds photos to a boulder's state: a first build if it has no model yet.

    Args:
        state_dir: the state's folder (made if it isn't there).
        photos: the photos offered; those it has already, by content, are left out.
        masks: a folder of their people masks (<photo's name>.png), for the features.
        init: how a model is started: "vggt" (VGGT's cameras, then COLMAP's triangulation and bundle adjustment),
            "global" (COLMAP's global mapper), "incremental" (COLMAP's own, from two photos), or "auto": VGGT's if it
            can run and passes its checks, else the global mapper, else the incremental one.
        ba: where bundle adjustment runs: "cpu", "gpu" (Ceres with CUDA, always), "caspar", or "auto" (the GPU from
            the size where it pays).
        feat: the features, for a new state (a state keeps to its own).
        cond: bench.py's conditions: "dev" (6 cores, as many threads, niced), "prod" (as the live worker runs a job).
        mesh: False to stop after placing.
        vggt: False to never ask VGGT.
        adopt: for a new state, a COLMAP database that has these photos' features and matches already (the
            benchmark's), so that only placement differs from the runs made from it.
        die_after: for the tests: stop, as if killed, after the step named.
        poses: with init "poses", the starting cameras in vggt_cams.py's format ({chosen, placed}).

    Returns:
        The add's record, as kept in the manifest ({"kind": "none"} if there was nothing to do).
    """
    s = st.State(state_dir)
    root = s.root
    new, same = s.plan(photos)
    if not new and not s.journal():
        print(f"nothing new: {len(same)} of the photos offered are here already", flush=True)
        return {"kind": "none", "duplicates": same, "why": "nothing new"}
    feat = s.manifest["settings"].setdefault("features", feat)
    j = s.begin(new, masks)
    k = j["id"]
    logdir = root / "log" / f"add-{k:04d}"
    logdir.mkdir(parents=True, exist_ok=True)
    cur = root / "log" / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(logdir.name)
    if adopt and not (root / "database.db").exists():
        shutil.copyfile(adopt, root / "database.db")
    threads = bench.condition(cond)[1] or -1
    mounts = [f"{root}:/s"]
    core = lambda *a: "python3 /incr/core.py " + bench.sh(a)        # noqa: E731
    steps: dict[str, dict] = {}

    def step(name: str, tools: str, script: str, gpu: bool, env: dict | None = None, more: list[str] | None = None) -> dict:
        steps[name] = run_step(logdir, name, tools, script, mounts + (more or []), cond, env, gpu)
        if die_after == name:
            raise KeyboardInterrupt(f"stopped after {name}, as asked")
        return steps[name]

    def report(name: str) -> dict:
        f = logdir / f"{name}.report.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def vggt_step(name: str, mode: str, out: Path, request: Path | None = None) -> bool:
        if not vggt:
            return False
        r = step(name, "app", "python3 /incr/vggt_cams.py " + bench.sh([mode, "/s/database.db", "/s/photos",
                 f"/s/{out.relative_to(root)}", *([f"/s/{request.relative_to(root)}"] if request else [])]), True,
                 {"DATA_DIR": "/data", "HF_HUB_OFFLINE": "1"}, [f"{MODELS}:/data/cache/models"])
        return r["rc"] == 0 and out.exists()

    step("features", TOOLS, core("features", "/s", "--feat", feat, "--threads", threads), feat != "sift")
    step("match", TOOLS, core("match", "/s", "--feat", feat, "--threads", threads), True)
    place_args = ["place", "/s", "--init", init, "--ba", ba, "--seed", seed, "--threads", threads]
    cams = root / "cache" / f"vggt-skeleton-{k:04d}.json"
    if not s.has_model() and init == "poses" and poses:        # starting cameras from elsewhere (a phone's tracking)
        shutil.copyfile(poses, root / "cache" / f"poses-{k:04d}.json")
        place_args += ["--cameras", f"/s/cache/poses-{k:04d}.json"]
    elif not s.has_model() and init in ("auto", "vggt") and vggt_step("vggt", "skeleton", cams):
        place_args += ["--vggt", f"/s/{cams.relative_to(root)}"]
    step("place", TOOLS, core(*place_args), ba != "cpu")
    rep = report("place")
    if rep.get("resolve", {}).get("due"):       # the model doesn't take the new photos well: solve everything again
        first = rep
        args = ["place", "/s", "--init", init, "--ba", ba, "--seed", seed, "--threads", threads, "--resolve"]
        if init in ("auto", "vggt") and vggt_step("vggt", "skeleton", cams):
            args += ["--vggt", f"/s/{cams.relative_to(root)}"]
        shutil.copyfile(logdir / "place.report.json", logdir / "place-add.report.json")
        step("resolve", TOOLS, core(*args), ba != "cpu")
        rep = report("place")
        rep["resolve"] = {"due": True, "why": first["resolve"]["why"], "added_first": len(first.get("placed", {}))}
    placed, assist_rep = dict(rep.get("placed", {})), {}
    if rep.get("assist_request") and (root / "model.new" / "images.bin").exists():
        req = root / "cache" / f"assist-request-{k:04d}.json"
        st.write_json(req, rep["assist_request"])
        out = root / "cache" / f"vggt-assist-{k:04d}.json"
        if vggt_step("vggt_assist", "assist", out, req):
            step("assist", TOOLS, core("assist", "/s", f"/s/{out.relative_to(root)}", "--ba", ba, "--threads", threads), False)
            assist_rep = report("assist")
            placed.update(assist_rep.get("placed", {}))
    ok = rep.get("kind") in ("first", "add", "resolve") and steps.get("resolve", steps["place"])["rc"] == 0
    if not ok:      # nothing half made becomes the state: the photos are kept as unplaced, for the next add to try
        shutil.rmtree(root / "model.new", ignore_errors=True)
    brief = lambda r: {"wall": round(r["wall"], 1), "cpu_s": r["cpu_s"], "rc": r["rc"], "ba": r["ba"],      # noqa: E731
                       "gpu_mib": r["host"].get("own_mib_max"), "lock_wait": r["lock_wait"]}
    record = s.commit({
        "kind": rep.get("kind", "none") if ok else "failed", "placed": placed if ok else {}, "tried": rep.get("tried", []),
        "duplicates": same, "tools": TOOLS, "build": verified(TOOLS), "init": init, "start": rep.get("start"), "starts": rep.get("starts"), "ba": ba, "cond": cond,
        "seed": seed, "model": (assist_rep or rep).get("model"), "left": sorted(set(rep.get("left", [])) - set(placed)),
        "points_new": rep.get("points_new"), "other_models": rep.get("other_models"), "resolve": rep.get("resolve"),
        "remesh": rep.get("remesh"), "old_cameras_moved": rep.get("old_cameras_moved"),
        "assist": {"asked": len(rep["assist_request"]["names"]), "placed": len(assist_rep.get("placed", {})),
                   "anchors_off": assist_rep.get("anchors_off")} if rep.get("assist_request") else None,
        "steps": {n: brief(r) for n, r in steps.items()}})
    if mesh and ok and rep.get("remesh", {}).get("due") and s.placed():
        made = remesh(s, logdir, cond, steps)
        s.manifest["adds"][-1]["steps"] = {n: brief(r) for n, r in steps.items()}
        s.manifest["adds"][-1]["meshed"] = made
        st.write_json(s.manifest_path, s.manifest)
        record = s.manifest["adds"][-1]
    print(f"{record['kind']}: {len(record['placed_new'])} placed by this add, {record['placed_total']} in all, "
          f"{len(record.get('left') or [])} left out; steps {sum(r['wall'] for r in steps.values()):.0f} s", flush=True)
    return record


def mesh_stats(ply: Path) -> dict:
    """Faces, and the share of the surface in the largest connected piece, of OpenMVS's mesh (stats.mesh_stats reads
    only the type names OpenMVS 2.3 writes)."""
    import numpy as np
    import meshclean
    from core import read_ply
    if not ply.exists():
        return {}
    V, F = read_ply(ply)
    roots = meshclean._components(F, len(V))[F[:, 0]]
    _, labels = np.unique(roots, return_inverse=True)
    area = np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1) / 2
    areas = np.bincount(labels.ravel(), weights=area)
    return {"faces": int(len(F)), "one_piece": round(float(areas.max() / areas.sum()), 4), "pieces": int(len(areas))}


def bootstrap(state_dir: Path | str, mesh_dir: Path, photos: list[Path], masks: Path | None = None, cond: str = "dev",
              feat: str = "sift") -> dict:
    """A state for a mesh made before states were kept, from its folder's cameras.json and its project's photos:
    features and matches as for any add, then the model from the mesh's own cameras (core.bootstrap), in its frame.
    The mesh itself isn't made again; the manifest names it as the one later adds are measured against and tied to."""
    s = st.State(state_dir)
    if s.has_model():
        raise SystemExit(f"{s.root} has a model already")
    root = s.root
    new, same = s.plan(photos)
    s.manifest["settings"].setdefault("features", feat)
    j = s.begin(new, masks)
    logdir = root / "log" / f"add-{j['id']:04d}"
    logdir.mkdir(parents=True, exist_ok=True)
    cur = root / "log" / "current"
    if cur.is_symlink() or cur.exists():
        cur.unlink()
    cur.symlink_to(logdir.name)
    shutil.copyfile(mesh_dir / "cameras.json", root / "cache" / "former-cameras.json")
    threads = bench.condition(cond)[1] or -1
    core = lambda *a: "python3 /incr/core.py " + bench.sh(a)        # noqa: E731
    steps = {}
    for name, script, gpu in (("features", core("features", "/s", "--feat", feat, "--threads", threads), feat != "sift"),
                              ("match", core("match", "/s", "--feat", feat, "--threads", threads), True),
                              ("bootstrap", core("bootstrap", "/s", "/s/cache/former-cameras.json", "--threads", threads), False)):
        steps[name] = run_step(logdir, name, TOOLS, script, [f"{root}:/s"], cond, None, gpu)
        if steps[name]["rc"] != 0:
            break
    rep = json.loads((logdir / "bootstrap.report.json").read_text()) if (logdir / "bootstrap.report.json").exists() else {}
    ok = rep.get("kind") == "bootstrap"
    if not ok:
        shutil.rmtree(root / "model.new", ignore_errors=True)
    record = s.commit({"kind": "bootstrap" if ok else "failed", "placed": rep.get("placed", {}) if ok else {}, "tried": rep.get("tried", []),
                       "duplicates": same, "tools": TOOLS, "build": verified(TOOLS), "cond": cond, "model": rep.get("model"),
                       "former": {"mesh": str(mesh_dir), "cameras": len(json.loads((root / "cache" / "former-cameras.json").read_text()))},
                       "skipped": rep.get("skipped"), "moved_max": rep.get("moved_max"), "turned_max_deg": rep.get("turned_max_deg"),
                       "steps": {n: {"wall": round(r["wall"], 1), "cpu_s": r["cpu_s"], "rc": r["rc"], "ba": r["ba"]} for n, r in steps.items()}})
    s.manifest["former"] = {"mesh": str(mesh_dir), "frame": "the state's model is in this mesh's frame"}
    st.write_json(s.manifest_path, s.manifest)
    print(f"bootstrap: {record['placed_total']} cameras of {record['former']['cameras']} from {mesh_dir}; "
          f"{(rep.get('model') or {}).get('points')} points; steps {sum(r['wall'] for r in steps.values()):.0f} s", flush=True)
    return record


def write_cameras(model: Path, out: Path) -> int:
    """A model's cameras as the app's meshes keep them (backend/orientation.read_cameras: name, q, t, lens, seen)."""
    import orientation
    cams = orientation.read_cameras(model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cams))
    return len(cams)


def remesh(s: st.State, logdir: Path, cond: str, steps: dict | None = None) -> bool:
    """The dense steps on a state's model as it is: undistorted photos, OpenMVS's scene, depth maps and their fusion
    (on the GPU), mesh and texture, in dense.new, which takes dense's place if every step worked. Without the people
    masks the app gives OpenMVS, as the benchmark's own dense steps are."""
    root, threads = s.root, bench.condition(cond)[1] or -1
    steps = {} if steps is None else steps
    for name, gpu in (("undistort", False), ("import", False), ("dense", True), ("mesh", False), ("texture", False)):
        steps[name] = run_step(logdir, name, TOOLS, "python3 /incr/core.py " + bench.sh([name, "/s", "--threads", threads]),
                               [f"{root}:/s"], cond, None, gpu)
        if steps[name]["rc"] != 0:
            return False
    shutil.rmtree(root / "dense", ignore_errors=True)
    os.replace(root / "dense.new", root / "dense")
    # not kept: depth maps (6 MB a photo at resolution level 2) and the undistorted photos. OpenMVS reuses depth maps
    # only with its geometric pass off, an add changes which photos are each one's neighbours, and densifying on the
    # GPU takes seconds: they're cheaper made again than stored.
    kept_mb = sum(f.stat().st_size for f in (root / "dense").glob("*.dmap")) / 2 ** 20
    for f in (root / "dense").glob("*.dmap"):
        f.unlink()
    shutil.rmtree(root / "dense" / "dense", ignore_errors=True)
    m = mesh_stats(root / "dense" / "scene_dense_mesh.ply")
    m["dmap_mb_dropped"] = round(kept_mb, 1)
    s.meshed({"faces": m.get("faces"), "one_piece": m.get("one_piece"), "pieces": m.get("pieces"), "dmap_mb_dropped": m["dmap_mb_dropped"],
              "seconds": {n: round(steps[n]["wall"], 1) for n in ("undistort", "import", "dense", "mesh", "texture")}})
    return True


# ---------------------------------------------------------------- the tests and the benchmark's runs

def project(P: str) -> tuple[list[Path], Path | None]:
    d = BENCH / "projects" / P
    return expand([str(d / "photos")]), d / "masks" if (d / "masks").exists() else None


def run_dir(P: str, name: str) -> Path:
    return R / P / name


def common(a) -> dict:
    return {"init": a.init or "auto", "ba": a.ba, "feat": a.feat, "cond": a.cond, "mesh": not a.no_mesh, "vggt": not a.no_vggt}


def suffix(a) -> str:
    return ("~plain" if TOOLS == "incr" else "" if TOOLS == "incr-full" else f"~{TOOLS}") + (f"-{a.ba}" if a.ba != "auto" else "") + (f"-{a.feat}" if a.feat != "sift" else "") + (f"@{a.cond}" if a.cond != "dev" else "") \
        + ("-adopt" if a.adopt else "") + (f"-{a.tag}" if a.tag else "")


def adopt_db(P: str, a) -> Path | None:
    return BENCH / "r2" / P / "sift391" / "database.db" if a.adopt else None


def first(P: str, init: str, seed: int, a) -> Path:
    """A first build of every photo of a benchmark boulder."""
    d = run_dir(P, f"first-{init}-s{seed}{suffix(a)}")
    if (d / "manifest.json").exists() and st.State(d).manifest["adds"]:
        return d
    shutil.rmtree(d, ignore_errors=True)
    photos, masks = project(P)
    add(d, photos, masks, **{**common(a), "init": init}, seed=seed, adopt=adopt_db(P, a))
    return d


def held(P: str, init: str, seed: int, percent: int, batches: int, a) -> Path:
    """A first build of a random percent% of a boulder's photos (the same ones bench.py's held test keeps), then the
    rest added in so many batches."""
    d = run_dir(P, f"held{percent}-b{batches}-{init}-s{seed}{suffix(a)}")
    s = st.State(d) if (d / "manifest.json").exists() else None
    if s and len(s.manifest["adds"]) > batches:
        return d
    shutil.rmtree(d, ignore_errors=True)
    photos, masks = project(P)
    names = sorted(p.name for p in photos)
    kept = set(random.Random(f"{P}-{seed}-{percent}").sample(names, round(len(names) * percent / 100)))
    rest = [p for p in photos if p.name not in kept]
    random.Random(f"{P}-{seed}-{percent}-order").shuffle(rest)
    kw = {**common(a), "init": init}
    add(d, [p for p in photos if p.name in kept], masks, **kw, seed=seed, adopt=None)
    for b in range(batches):
        add(d, rest[b * len(rest) // batches:(b + 1) * len(rest) // batches], masks, **kw, seed=seed)
    return d


def scenario(P: str, name: str, a) -> None:
    """The add tests that aren't timings, each on a first build of a boulder: prints what happened and whether it is
    what should."""
    base = first(P, a.init or "auto", 0, a)
    photos, masks = project(P)
    d = run_dir(P, f"scenario-{name}{suffix(a)}")
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(base, d, symlinks=True)
    s = st.State(d)
    before = json.loads(json.dumps(s.manifest))
    model = lambda: {n: (R_.tolist(), t.tolist()) for n, (R_, t) in stats.poses(d / "model").items()}     # noqa: E731
    was = model()
    kw = common(a)
    if name == "nothing":
        r = add(d, [], masks, **kw)
        ok = r["kind"] == "none" and st.State(d).manifest == before and model() == was
    elif name == "duplicates":
        r = add(d, photos[:5], masks, **kw)
        ok = r["kind"] == "none" and len(r["duplicates"]) == 5 and st.State(d).manifest == before and model() == was
    elif name == "other-rock":      # photos of another boulder: they should stay unplaced and the model as it was
        other = next(q.name for q in sorted((BENCH / "projects").iterdir()) if q.name != P)
        theirs, their_masks = project(other)
        r = add(d, theirs[:12], their_masks, **kw)
        now = model()
        moved = max((abs(now[n][1][i] - was[n][1][i]) for n in was for i in range(3)), default=0)
        ok = not r["placed_new"] and set(now) == set(was) and moved < 1e-9
        r = {**r, "old_cameras_moved": moved}
    else:
        raise SystemExit(f"unknown scenario {name}")
    print(f"SCENARIO {P} {name}: {'OK' if ok else 'WRONG'} {json.dumps({k: v for k, v in r.items() if k != 'steps'}, default=str)[:600]}", flush=True)


def interrupted(P: str, a) -> None:
    """A state of 70% of a boulder's photos; an add of the rest killed after matching; then the same add again."""
    photos, masks = project(P)
    names = sorted(p.name for p in photos)
    kept = set(random.Random(f"{P}-0-70").sample(names, round(len(names) * 0.7)))
    d = run_dir(P, f"scenario-interrupted{suffix(a)}")
    shutil.rmtree(d, ignore_errors=True)
    kw = common(a)
    add(d, [p for p in photos if p.name in kept], masks, **kw)
    before = json.loads((d / "manifest.json").read_text())
    was = {n: t.tolist() for n, (_, t) in stats.poses(d / "model").items()}
    rest = [p for p in photos if p.name not in kept]
    try:
        add(d, rest, masks, **kw, die_after="match")
    except KeyboardInterrupt:
        pass
    same = json.loads((d / "manifest.json").read_text()) == before and \
        {n: t.tolist() for n, (_, t) in stats.poses(d / "model").items()} == was
    pending = len((st.State(d).journal() or {}).get("photos", {}))
    r = add(d, rest, masks, **kw)       # the same photos offered again: the add that died is taken up
    done = len(r["new"]) == len(rest) and r["kind"] in ("add", "resolve")
    print(f"SCENARIO {P} interrupted: {'OK' if same and done else 'WRONG'} state unchanged after the kill: {same}; "
          f"{pending} photos pending; then {r['kind']} of {len(r['new'])} placed {len(r['placed_new'])}", flush=True)


def table() -> None:
    """Every step of every state under R, one row each."""
    cols = ["boulder", "run", "add", "kind", "photos_new", "placed_new", "placed_total", "step", "tools", "cond", "rc", "wall_s",
            "cpu_s", "flags", "own_gpu_mib", "ba_cuda", "ba_cpu", "ba_caspar", "gp_cuda", "lock_wait"]
    print("\t".join(cols))
    for mf in sorted(R.glob("*/*/manifest.json")):
        m = json.loads(mf.read_text())
        for rec in m["adds"]:
            for f in sorted((mf.parent / "log" / f"add-{rec['id']:04d}").glob("*.json")):
                if f.name.endswith(".report.json"):
                    continue
                r = json.loads(f.read_text())
                row = {"boulder": mf.parts[-3], "run": mf.parts[-2], "add": rec["id"], "kind": rec["kind"], "photos_new": len(rec["new"]),
                       "placed_new": len(rec["placed_new"]), "placed_total": rec["placed_total"], "step": r["step"], "tools": r["tools"],
                       "cond": r["cond"], "rc": r["rc"], "wall_s": round(r["wall"], 1), "cpu_s": r["cpu_s"],
                       "flags": bench.flags(r.get("host", {})), "own_gpu_mib": r.get("host", {}).get("own_mib_max", ""),
                       "ba_cuda": r["ba"]["cuda"], "ba_cpu": r["ba"]["cpu"], "ba_caspar": r["ba"]["caspar"],
                       "gp_cuda": r["ba"]["gp_cuda"], "lock_wait": r["lock_wait"]}
                print("\t".join(str(row.get(c, "")) for c in cols))


def compare(only: str | None = None) -> None:
    """Each state's model against the others of its boulder and the benchmark's baseline models: photos placed, those
    both place, and how far apart their cameras are once laid over each other (stats.agreement)."""
    print("boulder\ta\tb\tplaced_a\tplaced_b\tshared\tonly_a\tonly_b\tpos_median\tpos_p90\tdeg_median\tdeg_p90")
    for proj in sorted(R.iterdir()) if R.exists() else []:
        P = proj.name
        if only and P != only:
            continue
        models = {d.name: d / "model" for d in sorted(proj.iterdir()) if (d / "model" / "images.bin").exists()}
        for d in sorted((BENCH / "r2" / P).glob("*/base-s*")):       # the baseline's, after the app's rest step
            if (d / "rest" / "rest2" / "images.bin").exists():
                models[f"base:{d.name}+rest"] = d / "rest" / "rest2"
        poses = {k: stats.poses(v) for k, v in models.items()}
        names = sorted(poses)
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                ag = stats.agreement(poses[x], poses[y]) or {}
                both = set(poses[x]) & set(poses[y])
                print("\t".join(str(v) for v in [P, x, y, len(poses[x]), len(poses[y]), len(both), len(set(poses[x]) - both),
                                                 len(set(poses[y]) - both), ag.get("pos_median", ""), ag.get("pos_p90", ""),
                                                 ag.get("deg_median", ""), ag.get("deg_p90", "")]))


def heldstats(only: str | None = None) -> None:
    """Each held-out state against the first build of every photo with the same start and seed: time (the first
    build, each add after it, and the from-scratch build), photos placed (by each, and which), whether the cameras
    placed before an add moved, how far the added cameras are from the from-scratch build's once laid over it
    (fitted on the cameras both place), and the meshes."""
    import numpy as np
    cols = ["boulder", "run", "adds", "kinds", "first_s", "adds_s", "scratch_s", "placed", "scratch_placed", "both", "only_held",
            "only_scratch", "old_moved_max", "vs_scratch_pos_median", "vs_scratch_pos_p90", "vs_scratch_deg_median",
            "vs_scratch_deg_p90", "remeshed", "faces", "scratch_faces", "assist_placed", "resolves"]
    print("\t".join(cols))
    for d in sorted(R.glob("*/held*")):
        if only and d.parent.name != only or not (d / "manifest.json").exists():
            continue
        m = re.fullmatch(r"held(\d+)-b(\d+)-(\w+)-s(\d+)(.*)", d.name)
        scratch = d.parent / f"first-{m.group(3)}-s{m.group(4)}{m.group(5)}"
        man = json.loads((d / "manifest.json").read_text())
        adds = man["adds"]
        secs = lambda a: sum(r["wall"] for r in a.get("steps", {}).values())       # noqa: E731
        row = {"boulder": d.parent.name, "run": d.name, "adds": len(adds), "kinds": "+".join(a["kind"] for a in adds),
               "first_s": round(secs(adds[0])) if adds else "", "adds_s": "+".join(str(round(secs(a))) for a in adds[1:]),
               "placed": adds[-1]["placed_total"] if adds else 0,
               "remeshed": "".join("y" if a.get("meshed") else "n" for a in adds),
               "faces": (man.get("mesh") or {}).get("faces", ""),
               "assist_placed": sum((a.get("assist") or {}).get("placed", 0) for a in adds),
               "resolves": sum(1 for a in adds if a["kind"] == "resolve")}
        moved = [a.get("old_cameras_moved") for a in adds[1:] if a.get("old_cameras_moved") is not None]
        row["old_moved_max"] = max(moved) if moved else ""
        mine = stats.poses(d / "model") if (d / "model" / "images.bin").exists() else {}
        if (scratch / "manifest.json").exists() and (scratch / "model" / "images.bin").exists():
            sm = json.loads((scratch / "manifest.json").read_text())
            theirs = stats.poses(scratch / "model")
            row.update(scratch_s=round(sum(secs(a) for a in sm["adds"])), scratch_placed=len(theirs), both=len(set(mine) & set(theirs)),
                       only_held=len(set(mine) - set(theirs)), only_scratch=len(set(theirs) - set(mine)),
                       scratch_faces=(sm.get("mesh") or {}).get("faces", ""))
            ag = stats.agreement(mine, theirs) or {}
            row.update(vs_scratch_pos_median=ag.get("pos_median", ""), vs_scratch_pos_p90=ag.get("pos_p90", ""),
                       vs_scratch_deg_median=ag.get("deg_median", ""), vs_scratch_deg_p90=ag.get("deg_p90", ""))
        print("\t".join(str(row.get(c, "")) for c in cols))


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["add", "mesh", "status", "first", "held", "scenario", "queue", "table", "compare", "heldstats",
                                        "bootstrap", "cameras", "boot"])
    p.add_argument("args", nargs="*")
    p.add_argument("--masks", type=Path)
    p.add_argument("--init")
    p.add_argument("--ba", default="auto")
    p.add_argument("--feat", default="sift")
    p.add_argument("--cond", default="dev")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--batches", type=int, default=1)
    p.add_argument("--no-mesh", action="store_true")
    p.add_argument("--no-vggt", action="store_true")
    p.add_argument("--adopt", nargs="?", const="bench", default=None)
    p.add_argument("--tag", default="")
    a = p.parse_args(argv)
    if a.command == "add":
        adopt = Path(a.adopt) if a.adopt and a.adopt != "bench" else None
        add(Path(a.args[0]), expand(a.args[1:]), a.masks, a.init or "auto", a.ba, a.feat, a.cond, a.seed, not a.no_mesh,
            not a.no_vggt, adopt)
    elif a.command == "bootstrap":      # <state> <mesh dir> <photos...>
        bootstrap(Path(a.args[0]), Path(a.args[1]), expand(a.args[2:]), a.masks, a.cond, a.feat)
    elif a.command == "cameras":        # <model dir> <out.json>
        print(write_cameras(Path(a.args[0]), Path(a.args[1])), "cameras")
    elif a.command == "boot":           # <project> <state with a model>: its cameras as a mesh's, and a state from them
        P, src = a.args[0], Path(a.args[1])
        mesh = R / P / f"mesh-of-{src.name}"
        write_cameras(src / "model", mesh / "cameras.json")
        d = R / P / f"boot-{src.name}{suffix(a)}"
        shutil.rmtree(d, ignore_errors=True)
        photos, masks = project(P)
        bootstrap(d, mesh, photos, masks, a.cond, a.feat)
    elif a.command == "mesh":       # the mesh again, of the model as it is
        s = st.State(a.args[0])
        remesh(s, s.root / "log" / f"add-{max(0, s.next_id() - 1):04d}", a.cond)
    elif a.command == "status":
        s = st.State(a.args[0])
        print(json.dumps({"photos": len(s.photos), "placed": len(s.placed()), "unplaced": s.unplaced(), "solve": s.manifest["solve"],
                          "mesh": s.manifest["mesh"], "adds": [{k: r.get(k) for k in ("id", "kind", "placed_total", "remesh", "resolve")}
                                                               for r in s.manifest["adds"]], "pending": s.journal()}, indent=1))
    elif a.command == "first":
        first(a.args[0], a.args[1], int(a.args[2]), a)
    elif a.command == "held":
        held(a.args[0], a.args[1], int(a.args[2]), int(a.args[3]), a.batches, a)
    elif a.command == "scenario":
        interrupted(a.args[0], a) if a.args[1] == "interrupted" else scenario(a.args[0], a.args[1], a)
    elif a.command == "table":
        table()
    elif a.command == "compare":
        compare(a.args[0] if a.args else None)
    elif a.command == "heldstats":
        heldstats(a.args[0] if a.args else None)
    elif a.command == "queue":      # read again before each job, so that jobs can be added while it runs
        done = 0
        while True:
            lines = [l for l in Path(a.args[0]).read_text().split("\n") if l.strip() and not l.startswith("#")]
            if done >= len(lines):
                break
            room = bench.pool_room()
            if room is not None and room < bench.POOL_MIN_GB:
                print(f"{time.strftime('%T')} STOPPED before \"{lines[done]}\": {room:.0f} GB free on rpool", flush=True)
                sys.exit(3)
            try:
                main(shlex.split(lines[done]))
            except Exception as e:      # one failing job doesn't stop the rest
                print(f"{time.strftime('%T')} FAILED {lines[done]}: {e!r}", flush=True)
            done += 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))     # so a killed step still stops its container
    main(sys.argv[1:])
