"""The whole mapper benchmark as one chain that needs nobody watching it: chain.py

Run it detached, so that it outlives whoever started it, and again the same way to carry on after a stop (a job
whose results exist is skipped, a step cut short is made again):

    systemd-run --user --unit bm-bench --collect -p WorkingDirectory=/data/tom/mapperbench/scripts \\
        -E PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin python3 chain.py
    systemctl --user stop bm-bench          # stops its running containers too

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
CORES_FREE = 36         # what an untimed phase may use in all while there is no cap
START = ("systemd-run --user --unit bm-bench --collect -p WorkingDirectory=" + str(HERE)
         + " -E PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin python3 chain.py")


def queue(name: str, cores: int | None = None) -> dict:
    """A queue file as a thing to run: alone when cores is None (a timed queue), else beside others on that many."""
    return {"name": name, "cmd": [sys.executable, str(HERE / "bench.py"), "queue", str(BENCH / name)], "cores": cores,
            "status": (BENCH / name).with_suffix(".status")}


def build(variant: str, cores: int = 12) -> dict:
    """A tree of the newer tools (build.sh), built on that many cores."""
    return {"name": f"build-{variant}", "cmd": ["bash", str(HERE / "build.sh"), str(BENCH / f"build-{variant}"), variant],
            "cores": cores, "log": BENCH / f"build-{variant}.log", "jobs": True}


PHASES = [
    ("preflight: the smallest boulder, every kind of step", [queue("queue-preflight.txt")]),
    ("timed: Drifter with every method, and the baselines", [queue("queue-prod-a.txt")]),
    ("untimed: the native and SuiteSparse builds, the baseline's and the global mapper's other seeds",
     [build("native"), build("ss"), queue("queue-dev1.txt", 6), queue("queue-dev2.txt", 6), queue("queue-dev3.txt", 6),
      queue("queue-dev4.txt", 6), queue("queue-dev5.txt", 6), queue("queue-dev6.txt", 6)]),
    ("timed: everything else", [queue("queue-prod-b.txt")]),
    ("untimed: the long baseline runs on other seeds", [queue("queue-late1.txt", 6), queue("queue-late2.txt", 6)]),
]
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
