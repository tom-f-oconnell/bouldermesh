"""The whole mapper benchmark as one chain that needs nobody watching it: chain.py

Run it detached, so that it outlives whoever started it, and again the same way to carry on after a stop (a job
whose results exist is skipped, a step cut short is made again):

    systemd-run --user --unit bm-bench --collect -p WorkingDirectory=/data/tom/mapperbench/scripts \\
        -E PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin python3 chain.py
    systemctl --user stop bm-bench          # stops its running containers too

chain.py short runs the SHORT plan in place of the whole benchmark (PLANS): the smallest set of jobs that answers the
main questions, small boulders first, so that a method that is clearly worse can be taken out of the later phases'
queue files before they start. CORES_FREE and MEM (bench.py) in the environment fit it to a machine smaller than hal.

PHASES run in order. A timed phase is one queue (bench.py queue): a job at a time, with nothing else of the benchmark
going. An untimed phase runs several things side by side, each given some cores, as many at once as fit in CORES_FREE,
or in what <BENCH>/cores-cap.txt says while that holds a number (timed jobs then run as their "c<N>" variants:
bench.py's capped()). A job that fails is recorded and the chain goes on. Everything is appended to <BENCH>/chain.log
(a build's own output to <BENCH>/build-<variant>.log), and <BENCH>/STATE.txt is rewritten as things change: where
the chain is, then the notes kept in STATE.notes.txt. The chain stops, saying so in both, when hal's root pool is
nearly full (a queue's exit code 3: bench.POOL_MIN_GB).
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402

BENCH = bench.BENCH
LOG, STATE, NOTES = BENCH / "chain.log", BENCH / "STATE.txt", BENCH / "STATE.notes.txt"
CORES_FREE = int(os.environ.get("CORES_FREE", 36))      # what an untimed phase may use in all while there is no cap
PLAN = sys.argv[1] if len(sys.argv) > 1 else "full"
START = ("systemd-run --user --unit bm-bench --collect -p WorkingDirectory=" + str(HERE)
         + " -E PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
         + "".join(f" -E {k}={os.environ[k]}" for k in ("BENCH", "CORES_FREE", "MEM") if k in os.environ)
         + f" {sys.executable} chain.py {PLAN}")


def queue(name: str, cores: int | None = None, script: str = "bench.py") -> dict:
    """A queue file as a thing to run: alone when cores is None (a timed queue), else beside others on that many.
    script is whose queue it is: bench.py's, or incr/boulder.py's (the incremental pipeline)."""
    return {"name": name, "cmd": [sys.executable, str(HERE / script), "queue", str(BENCH / name)], "cores": cores,
            "status": (BENCH / name).with_suffix(".status")}


def build(variant: str, cores: int = 10) -> dict:
    """A tree of the newer tools (build.sh), built on that many cores."""
    return {"name": f"build-{variant}", "cmd": ["bash", str(HERE / "build.sh"), str(BENCH / f"build-{variant}"), variant],
            "cores": cores, "log": BENCH / f"build-{variant}.log", "jobs": True}


def build_incr(cores: int = 6) -> dict:
    """The incremental pipeline's tree (incr/build-env.sh: build-incr-full), from build-plain's sources."""
    return {"name": "build-incr-full", "cmd": ["bash", str(HERE / "incr/build-env.sh"), str(BENCH), "full"],
            "cores": cores, "log": BENCH / "build-incr-full.log", "jobs": True}


FULL = [
    ("preflight: the smallest boulder, every kind of step", [queue("queue-preflight.txt")]),
    ("timed: the 3.9.1 baselines (the app's image), Drifter first", [queue("queue-prod-a.txt")]),
    ("untimed: the complete, v3 and native builds (each verified), the baseline's and global mapper's other seeds",
     [build("complete"), build("v3"), build("native"), queue("queue-dev1.txt", 6), queue("queue-dev2.txt", 6),
      queue("queue-dev3.txt", 6), queue("queue-dev4.txt", 6), queue("queue-dev5.txt", 6), queue("queue-dev6.txt", 6)]),
    ("timed: plain against complete, the small boulder then two larger", [queue("queue-prod-c.txt")]),
    ("timed: every 4.2.1 method on the complete build, and complete against v3 and native", [queue("queue-prod-b.txt")]),
    ("untimed: the long baseline runs on other seeds", [queue("queue-late1.txt", 6), queue("queue-late2.txt", 6)]),
]
# The short plan: per boulder the baseline twice (its spread is the yardstick), global, caspar, cpu421 and vggt on
# the complete build, the app's image with OpenBLAS, then the pipeline's first build. A phase's two queues run one
# after the other. Lines can be taken out of a later phase's files while an earlier phase runs.
SHORT = [
    ("untimed: the complete build and the pipeline's tree (each verified)", [build("complete", 6), build_incr(6)]),
    ("timed: the 33-photo boulder (a check of every kind of step), then the 82-photo one",
     [queue("queue-short-1.txt"), queue("queue-short-1-incr.txt", script="incr/boulder.py")]),
    ("timed: the 150-photo boulder", [queue("queue-short-2.txt"), queue("queue-short-2-incr.txt", script="incr/boulder.py")]),
    ("timed: Drifter (234 photos)", [queue("queue-short-3.txt"), queue("queue-short-3-incr.txt", script="incr/boulder.py")]),
]
PLANS = {"full": FULL, "short": SHORT}
PHASES = PLANS[PLAN]
stopping = False


def say(text: str) -> None:
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%F %T')} CHAIN {text}\n")


def write_state(lines: list[str], last: list[str]) -> None:
    """Rewrites STATE.txt (if it would change): these lines about where the chain is, then the notes."""
    room = bench.pool_room()
    n = bench.cap()
    text = ("WHERE THE CHAIN IS (scripts/chain.py rewrites this part as things change; the notes under it are STATE.notes.txt)\n"
            + "".join(f"  {l}\n" for l in lines)
            + f"  cores: {f'capped at {n} in all (cores-cap.txt)' if n else f'no cap (untimed work within {CORES_FREE})'}"
            + (f"; rpool has {room:.0f} GB free (the chain stops under {bench.POOL_MIN_GB})\n" if room is not None else "\n")
            + f"  log: {LOG}    unit: bm-bench (systemctl --user status bm-bench)\n"
            + f"  to carry on after a stop, from where it stopped: {START}\n\n"
            + (NOTES.read_text() if NOTES.exists() else ""))
    if [text] != last:
        STATE.write_text(text)
        last[:] = [text]


def run_phase(index: int, name: str, items: list[dict], done: list[str], last: list[str]) -> bool:
    """Runs a phase's items, as many at once as the cores allowed fit. False if the chain has to stop."""
    waiting, running = list(items), []         # running: (process, item, cores taken, when started)
    since = time.strftime("%F %T")
    say(f"PHASE {index + 1}/{len(PHASES)} START: {name}")
    while waiting or running:
        budget = bench.cap() or CORES_FREE
        used = sum(r[2] for r in running)
        for item in list(waiting):
            need = min(item["cores"] or budget, budget)
            if running and used + need > budget:
                continue
            env = dict(os.environ, RUNNER="docker")       # never bubblewrap in place of Docker without anyone knowing
            if item.get("jobs"):
                env["JOBS"] = str(need)
            with open(item.get("log", LOG), "a") as out:
                p = subprocess.Popen(item["cmd"], stdout=out, stderr=subprocess.STDOUT, env=env, cwd=HERE)
            running.append((p, item, need, time.time()))
            waiting.remove(item)
            used += need
            say(f"START {item['name']} on {'every core' if item['cores'] is None else f'{need} cores'} (pid {p.pid})")
        lines = [f"phase {index + 1} of {len(PHASES)} since {since}: {name}"]
        for p, item, need, started in running:
            where = (item["status"].read_text().strip() if item.get("status") and item["status"].exists()
                     else f"since {time.strftime('%T', time.localtime(started))}, output in {item.get('log', LOG)}")
            lines.append(f"  running {item['name']}: {where}")
        lines += [f"  waiting for cores: {item['name']}" for item in waiting]
        lines += [f"done: {d}" for d in done]
        write_state(lines, last)
        time.sleep(10)
        failed = False
        for r in list(running):
            rc = r[0].poll()
            if rc is None:
                continue
            running.remove(r)
            say(f"END {r[1]['name']}: exit code {rc} after {(time.time() - r[3]) / 60:.0f} min")
            failed |= rc == 3
        if failed or stopping:
            for r in running:
                r[0].terminate()
            for r in running:
                try:
                    r[0].wait(timeout=60)
                except subprocess.TimeoutExpired:
                    r[0].kill()
            why = "by a signal" if stopping else f"hal's root pool has under {bench.POOL_MIN_GB} GB free"
            say(f"STOPPED in phase {index + 1} ({name}): {why}")
            write_state([f"STOPPED {time.strftime('%F %T')} in phase {index + 1} of {len(PHASES)} ({name}): {why}"]
                        + [f"done: {d}" for d in done], last)
            return False
    say(f"PHASE {index + 1}/{len(PHASES)} END: {name}")
    done.append(f"phase {index + 1} ({name}) {since} to {time.strftime('%F %T')}")
    return True


def main() -> None:
    def stop(*_):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    say(f"START (pid {os.getpid()})")
    done, last = [], []
    for i, (name, items) in enumerate(PHASES):
        if not run_phase(i, name, items, done, last):
            sys.exit(143 if stopping else 3)
    say("FINISHED: every phase has run")
    write_state([f"FINISHED {time.strftime('%F %T')}: every phase has run (failed jobs, if any, are in the log: grep FAILED)"]
                + [f"done: {d}" for d in done], last)


if __name__ == "__main__":
    main()
