"""Samples hal's GPU and CPU every 2 s until killed, one JSON line a sample: gpusample.py <out.jsonl> [own container]

Each sample has the GPU's utilization and memory, each process on the GPU with whose it is, the load average, and
the CPU in use (% of one core, over the last interval) by whom. "Whose" is "own" (the container named), "bench"
(the benchmark's other containers, e.g. a build), "bmw" (the BoulderMesh worker's job containers), "c:<name>" (any
other container) or the user's name. summary() boils a step's samples down, so that a timing taken while someone
else's work shared the machine can be flagged.
"""
import json
import re
import subprocess
import sys
import time


def run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def containers() -> dict[str, str]:
    """Container ID (its first 12 characters) -> name, for the running containers."""
    return dict(l.split(None, 1) for l in run("docker", "ps", "--format", "{{.ID}} {{.Names}}").split("\n") if l)


def whose(pid: str, user: str, names: dict[str, str], own: str) -> str:
    """Whose a process is: by its container, or (for steps run without Docker) its systemd scope."""
    try:
        cgroup = open(f"/proc/{pid}/cgroup").read()
    except OSError:
        return user
    m = re.search(r"([0-9a-f]{64})", cgroup)
    scope = re.search(r"/(bench-[\w-]+)\.scope", cgroup)
    if not m and not scope:
        return user
    name = names.get(m.group(1)[:12], m.group(1)[:12]) if m else scope.group(1)
    return "own" if name == own else "bmw" if name.startswith("bmw-") else "bench" if name.startswith("bench-") else f"c:{name}"


def main(out: str, own: str) -> None:
    prev, prev_t = {}, time.time()
    with open(out, "a") as f:
        while True:
            names = containers()        # every time: a short step's own container must be known from its first sample
            try:
                util, mem = (int(x) for x in run("nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                                 "--format=csv,noheader,nounits").split(","))
            except ValueError:      # nvidia-smi failed this once
                time.sleep(2)
                continue
            users, procs = {}, []
            for l in run("ps", "-eo", "pid=,user=,times=").split("\n"):     # CPU seconds so far, per process
                if l.strip():
                    pid, user, secs = l.split()
                    users[pid] = (user, int(secs))
            for l in run("nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits").split("\n"):
                if l.strip():
                    pid, used = (x.strip() for x in l.split(","))
                    args = run("ps", "-o", "args=", "-p", pid).strip()
                    procs.append({"pid": int(pid), "mib": int(used), "args": args[:100],
                                  "who": whose(pid, users.get(pid, ("?", 0))[0], names, own)})
            cpu, now = {}, time.time()
            for pid, (user, secs) in users.items():
                pc = 100 * (secs - prev.get(pid, (user, secs))[1]) / max(now - prev_t, 1)
                if pc >= 5:
                    who = whose(pid, user, names, own)
                    cpu[who] = round(cpu.get(who, 0) + pc)
            prev, prev_t = users, now
            f.write(json.dumps({"t": round(now, 1), "util": util, "mib": mem, "procs": procs, "cpu": cpu,
                                "load": float(open("/proc/loadavg").read().split()[0])}) + "\n")
            f.flush()
            time.sleep(2)


def summary(path: str) -> dict:
    """A step's samples as: GPU utilization (mean, max, and mean while the step itself had a process on it), the most
    GPU memory the step's own processes and anyone else's held, who else was on the GPU, mean cores in use by whom,
    and the mean load average."""
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = rows[1:] or rows         # the first sample has no interval to measure CPU over
    if not rows:
        return {}
    own_mib = [sum(p["mib"] for p in r["procs"] if p["who"] == "own") for r in rows]
    other_mib = [sum(p["mib"] for p in r["procs"] if p["who"] != "own") for r in rows]
    on_gpu = [r["util"] for r, m in zip(rows, own_mib) if m > 0]
    cores = {}
    for r in rows:
        for who, pc in r["cpu"].items():
            cores[who] = cores.get(who, 0) + pc / 100 / len(rows)
    return {"samples": len(rows), "util_mean": round(sum(r["util"] for r in rows) / len(rows), 1),
            "util_max": max(r["util"] for r in rows), "own_gpu_share": round(len(on_gpu) / len(rows), 3),
            "util_mean_own": round(sum(on_gpu) / len(on_gpu), 1) if on_gpu else None,
            "own_mib_max": max(own_mib), "other_mib_max": max(other_mib),
            "others_on_gpu": sorted({p["who"] for r in rows for p in r["procs"] if p["who"] != "own"}),
            "cores": {k: round(v, 1) for k, v in sorted(cores.items()) if v >= 0.3},
            "load_mean": round(sum(r["load"] for r in rows) / len(rows), 1)}


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
