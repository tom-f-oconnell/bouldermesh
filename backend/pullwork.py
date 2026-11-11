"""Machines that connect to this server for work: reconstructions on machines the server can't reach.

compute.py reaches its machines over SSH, so they must be reachable from the server. A machine behind a firewall or
NAT, such as a lab's workstation reached only through the lab's VPN, isn't. It runs worker/bouldermesh_worker.py
instead, which makes its own token and registers it here (registration.py), or uses one from the admin page, and asks
this server for work over HTTPS: it fetches a climb's photos and
people masks, runs the same job on them in the same Docker image as an SSH machine would (remote_job.py), sends its log
as it goes, and then the mesh, its cameras and the masks. The climb's worker thread here waits for that as it would for
an SSH machine, and a climb no machine is there to take, or whose machine goes quiet, waits (ComputeUnavailable).

Files go in parts, under Cloudflare's 100 MB a request, and carry on after a dropped connection. Like volunteers.py,
it's served on a port of its own (WORKER_PORT) that answers only /worker/..., for the proxy in front to pass on.
Machines are kept in a database of their own, readable only by the server's user: their tokens' hashes, to know them by,
and for those made on the admin page the tokens themselves, so that it can show a machine's command again.

A machine that registered itself is provisional until approved, and gets no work meanwhile unless the admin page says
it may. Then it's given only reconstructions, and what it makes is held (in held/, beside the database) rather than
becoming a mesh: its climb waits, and takes the held mesh once the machine is approved, or is meshed again elsewhere if
it's turned down. Jobs are kept only in memory: a restart here
ends them anyway, the climbs carry on after it as new jobs, and a machine still running an old one is told it's gone.

A machine whose owner's work keeps its GPU busy, or whose memory is short, says it's held back, and gets no job; one
whose GPU hasn't memory free for the next job's (gpu_need_mb()) is held back too. Each machine's state as it asks
(working, idle, held back, awaiting approval, or offline once it stops asking) is kept as it changes, in machine_states,
and logged, for the admin page's share of time in each.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import compute
import meshopts
import presence
import registration
import vggt_place
from volunteers import OffsetError, _client_ip, script_fetch

PART_MAX = 64 * 2**20       # the largest part of a file either way, under Cloudflare's 100 MB a request
MAX_OUTPUT = 4 * 2**30      # what a job may send back: a mesh, its textures, cameras and masks
CONNECTED = 120             # seconds since a machine last asked or reported for it to count as there
QUIET = 15 * 60             # a job whose machine hasn't been heard from for this long is lost
POLL = 15                   # seconds a machine with nothing to do waits before asking again
GONE_AFTER = 600            # a machine not heard from for this long has left (presence.py), unless it said goodbye before
GPU_NEED_PHOTOS = 20        # a reconstruction's GPU memory is reckoned for VGGT placing this many (it places fewer in less)
OTHER_GPU_MB = 4000         # finding a mesh's beta or size
STATES = ("working", "idle", "held back", "awaiting approval", "offline")
SEND_EVERY = 5              # seconds between a running job's reports, which are also how it's known to be alive
MAX_LINES = 5000            # lines in one report
CLIENT_VERSION = 1          # the oldest script that speaks to this
SCRIPT = Path(__file__).resolve().parents[1] / "worker" / "bouldermesh_worker.py"
TOP = ("out", "masks")      # what a job's results may hold
# the admin page's settings by default (machines.* there): whether a provisional machine gets work, and how many may register
RULES = {"provisional_work": False, "registrations_per_address": 5, "max_waiting": 30}


@dataclass
class Job:
    id: str
    label: str
    input: Path
    size: int
    internet: bool
    image: str
    created: float = field(default_factory=time.time)
    state: str = "queued"           # queued | running | done | failed
    machine: str | None = None
    machine_name: str | None = None
    taken: float | None = None
    touched: float = 0.0            # when its machine last fetched, reported or sent
    lines: list[str] = field(default_factory=list)   # what its machine sent that the waiting thread hasn't followed yet
    out_size: int | None = None
    error: str | None = None
    task: str = "reconstruct"       # or one of remote_job.py's other tasks: "beta", "scale"
    version: int = 1                # the oldest script that does it
    gpu_mb: int = 0                 # the GPU memory it may need (gpu_need_mb())

    @property
    def output(self) -> Path:
        return self.input.with_name(f"{self.id}.out.tar")


class Board:
    """The machines that connect here, and the reconstructions waiting for them.

    Args:
        db_path: its database of machines.
        files: where jobs' photos wait to be fetched and their results come in.
        image: the Docker image a job runs, asked for each job (the admin page's, or this server's own build).
        jobctl: the waiting thread's job control, to stop waiting when its climb is stopped.
        rules: the admin page's settings, as RULES.
        registered: called with a new machine's row when one registers itself.
        log: for each machine's state as it changes.
        came_went: called with (a machine's row, "here" or "gone", why) when it starts being heard from or stops, not
            as it's busy or held back (presence.py).
    """

    def __init__(self, db_path: Path, files: Path, image: Callable[[], str], jobctl, rules: Callable[[], dict] = lambda: RULES,
                 registered: Callable[[dict], None] = lambda m: None, log: Callable[[str], None] = print,
                 came_went: Callable[[dict, str, str], None] = lambda m, state, why: None,
                 downloaded: Callable[[dict], None] = lambda info: None,
                 idle_why: Callable[[], str | None] = lambda: None):
        self.db_path, self.files, self.image, self.jobctl = db_path, files, image, jobctl
        self.rules, self.registered, self.came_went, self.idle_why = rules, registered, came_went, idle_why
        self.downloaded = downloaded
        self._said: dict[str, tuple[str, float]] = {}   # each machine's reason for its state last logged, and when
        self.paused: str | None = None      # why no new job is given out (the server about to restart); those running go on
        self._hour = int(time.time() // 3600)
        self.log = lambda line: log(line) if log is not print else print(line, flush=True)
        self.held = db_path.parent / "held"     # what provisional machines made, for climbs to take once they're approved
        self.jobs: dict[str, Job] = {}
        self.cond = threading.Condition(threading.RLock())
        self.seen: dict[str, float] = {}
        shutil.rmtree(files, ignore_errors=True)    # jobs from before a restart, which ended with it
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS machines(id TEXT PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created REAL,
                  revoked REAL, last_seen REAL, last_ip TEXT, client TEXT, gpu INTEGER, cpus REAL, done INTEGER DEFAULT 0,
                  failed INTEGER DEFAULT 0, token TEXT);
                CREATE TABLE IF NOT EXISTS machine_states(machine_id TEXT, state TEXT, why TEXT, start REAL, end REAL);
                CREATE INDEX IF NOT EXISTS machine_states_by ON machine_states(machine_id, start);
                CREATE TABLE IF NOT EXISTS machine_jobs(id TEXT PRIMARY KEY, task TEXT, label TEXT, machine_id TEXT, machine TEXT,
                  created REAL, taken REAL, ended REAL, outcome TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS machine_jobs_by ON machine_jobs(created);
            """)
            if "token" not in {r[1] for r in con.execute("PRAGMA table_info(machines)")}:   # kept since 2026-11-10
                con.execute("ALTER TABLE machines ADD COLUMN token TEXT")
            # the GPU a machine has (its script's GpuWatch, from version 10), and each job's share of its use, in seconds
            if "gpu_name" not in {r[1] for r in con.execute("PRAGMA table_info(machines)")}:
                con.execute("ALTER TABLE machines ADD COLUMN gpu_name TEXT")
            for col, kind in (("gpu_busy", "REAL"), ("gpu_name", "TEXT")):
                if col not in {r[1] for r in con.execute("PRAGMA table_info(machine_jobs)")}:
                    con.execute(f"ALTER TABLE machine_jobs ADD COLUMN {col} {kind}")
            registration.migrate(con, "machines")
            presence.migrate(con, "machines", GONE_AFTER)
        os.chmod(db_path, 0o600)

    def db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def q(self, sql: str, *args, one: bool = False) -> list[dict] | dict | None:
        with self.db() as con:
            rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        return (rows[0] if rows else None) if one else rows

    # -- machines -------------------------------------------------------------------------------------------------
    def add(self, name: str) -> dict:
        """A new machine: {id, name, token}. The token is shown this once; only its hash is kept."""
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("give the machine a name")
        name = registration.unique_name(name, (r["name"] for r in self.q("SELECT name FROM machines")))
        mid, token = secrets.token_hex(4), "bmw_" + secrets.token_urlsafe(32)
        self.q("INSERT INTO machines(id,name,token_hash,created,token,status,approved) VALUES(?,?,?,?,?,?,?)", mid, name,
               registration.token_hash(token), time.time(), token, "approved", time.time())
        return {"id": mid, "name": name, "token": token}

    def register(self, token: str, name, ip: str, client: str = "") -> dict:
        """A machine's script registering the token it made: provisional until approved (registration.register()).
        The new machine goes to registered()."""
        rules = {**RULES, **self.rules()}
        before = self.machine(token)
        m = registration.register(self.q, "machines", token, name, ip, "machine", rules["registrations_per_address"],
                                  rules["max_waiting"], {"client": str(client)[:120]})
        if not before:
            self.registered(m)
        return m

    def approve(self, mid: str) -> dict | None:
        """A provisional machine approved: it gets work, and its climbs take what it made. None for one unknown or revoked."""
        return registration.approve(self.q, "machines", mid)

    def get(self, mid: str) -> dict | None:
        return self.q("SELECT * FROM machines WHERE id=?", mid, one=True)

    def token(self, mid: str) -> dict | None:
        """A machine's name and token, to show its command again: None for one unknown, and token None for one revoked,
        or added before tokens were kept."""
        m = self.q("SELECT id, name, token, revoked FROM machines WHERE id=?", mid, one=True)
        return m and {"id": m["id"], "name": m["name"], "token": None if m["revoked"] else m["token"]}

    def forget_gone(self, now: float | None = None) -> int:
        """Forget machines revoked or denied that never finished a job and registered over a day ago (registrations from
        an address are counted for a day by their rows), with their record of states and jobs; those that finished one
        stay, for the leaderboard and the record. How many went."""
        now = now or time.time()
        gone = [r["id"] for r in self.q("SELECT id FROM machines WHERE revoked IS NOT NULL AND COALESCE(done, 0)=0 AND "
                                        "created<? AND id NOT IN (SELECT machine_id FROM machine_jobs WHERE outcome='done' "
                                        "AND machine_id IS NOT NULL)", now - 86400)]
        for mid in gone:
            for table, column in (("machine_states", "machine_id"), ("machine_jobs", "machine_id"), ("machines", "id")):
                self.q(f"DELETE FROM {table} WHERE {column}=?", mid)
        return len(gone)

    def revoke(self, mid: str, status: str | None = None) -> None:
        """Its token stops working at once, and what it was doing goes to another machine; what it made and is held
        goes. status "denied" for one never approved."""
        self.q("UPDATE machines SET revoked=?, token=NULL, status=COALESCE(?, status) WHERE id=?", time.time(), status, mid)
        for f in self.held.glob("*.json") if self.held.is_dir() else []:
            try:
                if json.loads(f.read_text()).get("machine") == mid:
                    f.with_suffix(".tar").unlink(missing_ok=True)
                    f.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        with self.cond:
            self.seen.pop(mid, None)
            for j in self.jobs.values():
                if j.machine == mid and j.state == "running":
                    self._requeue(j, f"remote: {j.machine_name} was revoked: waiting for another machine")

    def machine(self, token: str) -> dict | None:
        return self.q("SELECT * FROM machines WHERE token_hash=?", registration.token_hash(token), one=True)

    # -- what each machine is doing, as it changes --------------------------------------------------------------------
    def _state(self, m: dict, state: str, why: str | None = None) -> None:
        """Record a machine's state, if it changed (a new reason for the same state only replaces the reason), and log
        the change. A machine not heard from for more than CONNECTED was offline from then until now."""
        now = time.time()
        with self.cond:
            cur = self.q("SELECT rowid, state, start FROM machine_states WHERE machine_id=? AND end IS NULL ORDER BY start DESC LIMIT 1",
                         m["id"], one=True)
            last = m.get("last_seen")
            if cur and cur["state"] != "offline" and last and now - last > CONNECTED:     # it stopped asking a while
                self.q("UPDATE machine_states SET end=? WHERE rowid=?", last + CONNECTED, cur["rowid"])
                self.q("INSERT INTO machine_states VALUES(?,?,?,?,?)", m["id"], "offline", None, last + CONNECTED, None)
                cur = {"rowid": None, "state": "offline"}
            if cur and cur["state"] == state:
                if cur["rowid"]:
                    self.q("UPDATE machine_states SET why=? WHERE rowid=?", why, cur["rowid"])
                kind, (said, at) = re.sub(r"[\d.]+", "#", why or ""), self._said.get(m["id"], ("", 0.0))
                if why and kind != said and now - at > 600:
                    self._said[m["id"]] = (kind, now)
                    self.log(f"machines: {m['name']} still {state}: {why}")
                return
            self._said[m["id"]] = (re.sub(r"[\d.]+", "#", why or ""), now)
            self.q("UPDATE machine_states SET end=? WHERE machine_id=? AND end IS NULL", now, m["id"])
            self.q("INSERT INTO machine_states VALUES(?,?,?,?,?)", m["id"], state, why, now, None)
        self.log(f"machines: {m['name']} {state}" + (f": {why}" if why else ""))

    def sweep(self) -> None:
        """Mark machines that stopped asking as offline, from when they were last heard from (CONNECTED after it)."""
        now = time.time()
        for r in self.q("SELECT s.rowid, s.machine_id, m.name, m.last_seen FROM machine_states s JOIN machines m ON m.id=s.machine_id "
                        "WHERE s.end IS NULL AND s.state!='offline' AND COALESCE(m.last_seen, 0) < ?", now - CONNECTED):
            with self.cond:
                if self.seen.get(r["machine_id"], 0) > now - CONNECTED:
                    continue
                at = (r["last_seen"] or now) + CONNECTED
                self.q("UPDATE machine_states SET end=? WHERE rowid=?", at, r["rowid"])
                self.q("INSERT INTO machine_states VALUES(?,?,?,?,?)", r["machine_id"], "offline", None, at, None)
            self.log(f"machines: {r['name']} offline (not heard from since {time.strftime('%H:%M', time.localtime(r['last_seen'] or now))})")

    def shares(self, mid: str, since: float) -> dict:
        """The share of the time since `since` a machine spent in each state, {state: fraction}, with "hours", how much of
        it was recorded (from its first record on)."""
        took = self.spent(mid, since)
        total = sum(took.values())
        return {"hours": round(total / 3600, 1), **{k: round(v / total, 3) for k, v in took.items()}} if total else {"hours": 0}

    def spent(self, mid: str, since: float) -> dict[str, float]:
        """The seconds since `since` a machine spent in each state, as recorded; an open record of a machine since gone
        quiet counts as offline from CONNECTED after it was last heard from."""
        now = time.time()
        last = (self.q("SELECT last_seen FROM machines WHERE id=?", mid, one=True) or {}).get("last_seen") or 0
        took: dict[str, float] = {}
        for r in self.q("SELECT state, start, end FROM machine_states WHERE machine_id=? AND (end IS NULL OR end>?)", mid, since):
            start, end = max(r["start"], since), r["end"] or now
            if r["end"] is None and r["state"] != "offline" and now - last > CONNECTED:
                took["offline"] = took.get("offline", 0) + max(0.0, end - max(start, last + CONNECTED))
                end = max(start, last + CONNECTED)
            took[r["state"]] = took.get(r["state"], 0) + max(0.0, end - start)
        return took

    def changes(self, limit: int = 30) -> list[dict]:
        """The latest state changes, of all machines, newest first."""
        return self.q("SELECT s.machine_id AS machine, m.name, s.state, s.why, s.start FROM machine_states s "
                      "LEFT JOIN machines m ON m.id=s.machine_id ORDER BY s.start DESC LIMIT ?", limit)

    def current(self, mid: str) -> dict | None:
        return self.q("SELECT state, why, start FROM machine_states WHERE machine_id=? AND end IS NULL ORDER BY start DESC LIMIT 1",
                      mid, one=True)

    def held_why(self) -> str | None:
        """Why the machines there now aren't taking a job: each one's reason, if none of them is idle or working; None
        otherwise (or with none there)."""
        cut = time.time() - CONNECTED
        with self.cond:
            there = [mid for mid, t in self.seen.items() if t > cut]
        states = [(self.q("SELECT name FROM machines WHERE id=?", mid, one=True) or {}, self.current(mid)) for mid in there]
        if not states or any(not c or c["state"] != "held back" for _, c in states):
            return None
        return "; ".join(f"{m.get('name')} is held back ({c['why']})" for m, c in states)

    def connected(self) -> bool:
        """Whether a machine has asked for work or reported lately."""
        cut = time.time() - CONNECTED
        with self.cond:
            return any(t > cut for t in self.seen.values())

    def _touch(self, m: dict, ip: str | None = None, client: dict | None = None) -> None:
        """A machine was heard from: asking for work (client), or about a job; a token's check doesn't count as being here."""
        now = time.time()
        with self.cond:
            self.seen[m["id"]] = now
        active = None if (client or {}).get("check") else now
        if client is not None:
            self.q("UPDATE machines SET last_seen=?, active_at=COALESCE(?, active_at), last_ip=?, client=?, gpu=?, cpus=?, "
                   "gpu_name=COALESCE(?, gpu_name) WHERE id=?", now, active, ip, str(client.get("client") or "")[:120], int(bool(client.get("gpu"))),
                   _number(client.get("cpus")), str(client["gpu_name"])[:80] if client.get("gpu_name") else None, m["id"])
        else:
            self.q("UPDATE machines SET last_seen=?, active_at=? WHERE id=?", now, now, m["id"])

    def bye(self, m: dict) -> None:
        """A machine's script stopped: it's gone, at once, for telling of it (presence.py). It still counts as there
        for CONNECTED, as one run again at once (--once, or restarted) takes the next job without waiting."""
        self.q("UPDATE machines SET bye_at=? WHERE id=?", time.time(), m["id"])

    def presence(self) -> list:
        """Tell of machines that came or went since (presence.sweep())."""
        return presence.sweep(self.q, "machines", lambda m: GONE_AFTER, self.came_went)

    # -- the machine's side ---------------------------------------------------------------------------------------
    def work(self, m: dict, ip: str, client: dict) -> dict:
        """What a machine asking for work is to do: {"job": ...}, the job it had if it lost track of it (it starts
        again), or the oldest waiting; or {"wait": seconds}. With "check" in client, only says whose token it is."""
        self._touch(m, ip, client)
        waiting = registration.provisional(m)
        allowed = not waiting or {**RULES, **self.rules()}["provisional_work"]
        if client.get("check"):
            return {"name": m["name"], "wait": POLL, "status": "provisional" if waiting else "approved", "may_work": allowed}
        if not allowed:
            self._state(m, "awaiting approval")
            return {"wait": 300, "why": "waiting for approval by whoever runs this site", "provisional": True}
        held = str(client.get("held") or "")[:200]
        if held:    # its owner's work, or memory short: as its script says
            self._state(m, "held back", held)
            return {"wait": POLL, "why": f"held back: {held}", "held": True}
        free = _number(client.get("gpu_free_mb")) if client.get("gpu") else None
        now = time.time()
        with self.cond:
            mine = next((j for j in self.jobs.values() if j.machine == m["id"] and j.state == "running"), None)
            if mine:
                mine.lines.append(f"remote: {m['name']} started the job again")
                mine.out_size = None
                mine.output.unlink(missing_ok=True)
            elif self.paused:
                self._state(m, "idle", self.paused)
                return {"wait": POLL, "why": f"nothing for it: {self.paused}"}
            else:
                version = int(_number(client.get("version")) or 1)
                could = [j for j in self.jobs.values() if j.state == "queued" and j.version <= version
                         and (not waiting or j.task == "reconstruct")]
                mine = min((j for j in could if free is None or j.gpu_mb <= free),        # meshing first, then beta and size
                           key=lambda j: (j.task != "reconstruct", j.created), default=None)
                if not mine and could:      # none fits in the GPU memory free there now
                    why = (f"its GPU has {free / 1024:.1f} GB free, and the next climb may need "
                           f"{min(j.gpu_mb for j in could) / 1024:.1f} GB")
                    self._state(m, "held back", why)
                    return {"wait": POLL, "why": f"held back: {why}", "held": True}
                if not mine:
                    try:
                        why = self.idle_why()
                    except Exception as e:      # only the reason is lost
                        why = f"(couldn't say why: {type(e).__name__})"
                    self._state(m, "idle", why)
                    return {"wait": POLL, **({"why": f"nothing for it: {why}"} if why else {})}
                gpu = " (GPU)" if client.get("gpu") else ""
                mine.state, mine.machine, mine.machine_name, mine.taken = "running", m["id"], m["name"], now
                mine.lines.append(f"remote: {m['name']}{gpu} took it after {now - mine.created:.0f} s")
            mine.touched = now
            self.cond.notify_all()
            self.q("UPDATE machine_jobs SET machine_id=?, machine=?, taken=?, outcome=NULL WHERE id=?", m["id"], m["name"], now, mine.id)
            self._state(m, "working", mine.label)
            return {"job": {"id": mine.id, "image": mine.image, "internet": mine.internet, "size": mine.size, "part": PART_MAX,
                            "task": mine.task}}

    def _mine(self, m: dict, job_id: str) -> Job:
        j = self.jobs.get(job_id)
        if not j or j.machine != m["id"] or j.state != "running":
            raise LookupError("that job isn't yours now: it was stopped, or given to another machine")
        return j

    def input(self, m: dict, job_id: str, offset: int) -> bytes:
        """A part of a job's photos and masks (a tar), from offset."""
        with self.cond:
            j = self._mine(m, job_id)
            j.touched = time.time()
        self._touch(m)
        if not 0 <= offset <= j.size:
            raise ValueError(f"offset {offset} is outside the {j.size} bytes")
        try:
            with open(j.input, "rb") as f:
                f.seek(offset)
                return f.read(PART_MAX)
        except FileNotFoundError:   # the climb was stopped meanwhile
            raise LookupError("that job isn't yours now: it was stopped, or given to another machine") from None

    def report(self, m: dict, job_id: str, lines: list) -> dict:
        """Lines a job printed, which the climb's log follows; also how a machine says it's still at it."""
        if not isinstance(lines, list) or len(lines) > MAX_LINES:
            raise ValueError(f"a list of at most {MAX_LINES} lines")
        with self.cond:
            j = self._mine(m, job_id)
            j.lines.extend(str(line)[:2000] for line in lines)
            j.touched = time.time()
            self.cond.notify_all()
        self._touch(m)
        return {"ok": True}

    def received(self, m: dict, job_id: str) -> int:
        with self.cond:
            j = self._mine(m, job_id)
            return j.output.stat().st_size if j.out_size is not None and j.output.exists() else 0

    def part(self, m: dict, job_id: str, offset: int, size: int, body: bytes) -> int:
        """Add a part of a job's results (a tar of out/ and masks/) at offset, of size bytes in all; how much has come.

        Raises:
            LookupError: not this machine's job now.
            ValueError: too big, or more than size.
            OffsetError: not where the file has got to.
        """
        with self.cond:
            j = self._mine(m, job_id)
            if not 0 < size <= MAX_OUTPUT:
                raise ValueError(f"results of up to {MAX_OUTPUT // 2**30} GB")
            if j.out_size is not None and j.out_size != size:
                raise ValueError("that's another file than the one being sent")
            have = j.output.stat().st_size if j.out_size is not None and j.output.exists() else 0
            if offset != have:
                raise OffsetError(have)
            if have + len(body) > size:
                raise ValueError("more than the size it was started with")
            if j.out_size is None:
                j.output.unlink(missing_ok=True)
            j.out_size = size
            with open(j.output, "ab") as out:
                out.write(body)
            j.touched = time.time()
        self._touch(m)
        return have + len(body)

    def finish(self, m: dict, job_id: str, report: dict) -> dict:
        """How a job ended: {"status": "ok"} once its results have all come, "failed" with "error" (its last line),
        or "released" when the machine stopped before finishing, for another machine to take it."""
        status = report.get("status")
        with self.cond:
            j = self._mine(m, job_id)
            if status == "released":
                self._ended(j, "released")
                self._requeue(j, f"remote: {j.machine_name} stopped before finishing: waiting for another machine")
                return {"ok": True}
            if status == "ok":
                if j.out_size is None or not j.output.exists() or j.output.stat().st_size != j.out_size:
                    raise ValueError("the results haven't all come: send the rest first")
                j.state = "done"
            else:
                j.state, j.error = "failed", str(report.get("error") or "failed on the machine")[:500]
            self.cond.notify_all()
            self._ended(j, j.state, j.error)
            gpu = report.get("gpu") if isinstance(report.get("gpu"), dict) else {}
            busy = _number(gpu.get("busy_seconds"))
            if busy is not None:    # its share of the machine's GPU while it ran (the script's GpuWatch, from version 10)
                self.q("UPDATE machine_jobs SET gpu_busy=?, gpu_name=? WHERE id=?", max(0.0, busy), str(gpu.get("name") or "")[:80] or None, j.id)
        col = "done" if status == "ok" else "failed"
        self.q(f"UPDATE machines SET {col}={col}+1 WHERE id=?", m["id"])
        return {"ok": True}

    def _ended(self, j: Job, outcome: str, error: str | None = None) -> None:
        """Keep how a job ended (done, failed, released by its machine, or gone: stopped here, or its machine lost),
        and log it with how long it took on its machine and how long it waited for one."""
        now = time.time()
        self.q("UPDATE machine_jobs SET ended=?, outcome=?, error=? WHERE id=? AND outcome IS NULL", now, outcome, error, j.id)
        if j.taken:
            self.log(f"machines: {j.machine_name} {outcome} {j.task} {j.label} in {(now - j.taken) / 60:.1f} min, "
                     f"after waiting {j.taken - j.created:.0f} s for a machine")

    def running(self) -> list[dict]:
        """The jobs machines are on now, and those waiting for one: {id, task, label, state, machine, created, taken}."""
        with self.cond:
            return [{"id": j.id, "task": j.task, "label": j.label, "state": j.state, "machine": j.machine_name,
                     "created": j.created, "taken": j.taken} for j in self.jobs.values() if j.state in ("queued", "running")]

    def typical(self, task: str) -> float | None:
        """How long, in seconds, a machine has taken over the last 20 jobs of this task it finished (the median)."""
        rows = self.q("SELECT ended - taken AS s FROM machine_jobs WHERE task=? AND outcome='done' AND taken IS NOT NULL "
                      "ORDER BY ended DESC LIMIT 20", task)
        took = sorted(r["s"] for r in rows if r["s"] and r["s"] > 0)
        return took[len(took) // 2] if took else None

    def jobs_since(self, since: float) -> list[dict]:
        """The jobs made since `since`, oldest first: {id, task, label, machine, created, taken, ended, outcome, error}."""
        return self.q("SELECT * FROM machine_jobs WHERE created>? ORDER BY created", since)

    def hourly(self, force: bool = False) -> None:
        """Once an hour (or now, with force): a line for each machine heard from in it, of its time in each state and
        the jobs it ended."""
        hour = int(time.time() // 3600)
        if hour == self._hour and not force:
            return
        self._hour, now = hour, time.time()
        for m in self.q("SELECT id, name FROM machines WHERE revoked IS NULL AND COALESCE(last_seen, 0) > ?", now - 3600):
            mins = {k: v / 60 for k, v in self.spent(m["id"], now - 3600).items() if v > 0}
            if not mins:
                continue
            ended = self.q("SELECT outcome, COUNT(*) n FROM machine_jobs WHERE machine_id=? AND ended>? GROUP BY outcome", m["id"], now - 3600)
            jobs = ", ".join(f"{r['n']} {r['outcome']}" for r in ended)
            self.log(f"machines: {m['name']} in the last hour: " + ", ".join(f"{k} {v:.0f} min" if v >= 1 else f"{k} <1 min"
                                                                                   for k, v in mins.items() if v > 0)
                     + (f"; jobs {jobs}" if ended else "; no jobs"))

    def _requeue(self, j: Job, note: str) -> None:
        j.state, j.machine, j.machine_name, j.taken, j.out_size = "queued", None, None, None, None
        j.output.unlink(missing_ok=True)
        j.lines.append(note)
        self.cond.notify_all()

    # -- the climb's side: what compute.RemoteExecutor does, for these machines ---------------------------------------
    def reconstruct(self, images: Path, work: Path, out_dir: Path, log, use_gpu: bool = True, internet: bool = False,
                    tracker=None) -> tuple[Path, bool]:
        """Reconstruct a project's photos on a machine that connects here: what pipeline.reconstruct does, there.

        Args:
            images: the project's photos/, next to its masks/, which go too and come back with new ones.

        Returns:
            The mesh file and whether it's a demo, as pipeline.reconstruct does.

        Raises:
            compute.ComputeUnavailable: no machine has been in touch lately, or the one doing it went quiet.
        """
        project = images.parent
        files = {part: project / part for part in ("photos", "masks") if (project / part).is_dir()}
        made = self._take_held(project, files, log)
        if made:
            result, tar = made
            back = Path(tempfile.mkdtemp(prefix="bm-remote-", dir=project))
            try:
                safe_extract(tar, back, TOP)
                mesh = compute.take_results(back, out_dir, project, result)
            finally:
                shutil.rmtree(back, ignore_errors=True)
                tar.unlink(missing_ok=True)
                tar.with_suffix(".json").unlink(missing_ok=True)
            return mesh, bool(result.get("demo"))
        mapper = meshopts.given()     # an area's settings for COLMAP's mapper, sent as task.json
        result, back, job = self._run("reconstruct", files, {"internet": internet, **({"mapper": mapper} if mapper else {})},
                                      project.name, project, log, tracker)
        m = self.get(job.machine or "")
        if registration.provisional(m):     # held until its machine is approved
            shutil.rmtree(back, ignore_errors=True)
            self.held.mkdir(parents=True, exist_ok=True)
            os.replace(job.output, self.held / f"{project.name}.tar")
            (self.held / f"{project.name}.json").write_text(json.dumps({"machine": m["id"], "name": m["name"], "inputs": _inputs(files),
                                                                       "result": result, "made": time.time()}))
            raise compute.ComputeUnavailable(f"meshed on {m['name']}, which waits for approval: once it's approved on the admin page, "
                                             "this climb takes that mesh")
        try:
            safe_extract(job.output, back, TOP)
            mesh = compute.take_results(back, out_dir, project, result)
        finally:
            shutil.rmtree(back, ignore_errors=True)
            job.output.unlink(missing_ok=True)
        return mesh, bool(result.get("demo"))

    def _take_held(self, project: Path, files: dict[str, Path], log) -> tuple[dict, Path] | None:
        """What a provisional machine made for this project, once it's approved: (its result, the tar of it). None
        if there's none, or it was made from other photos, or its machine was turned down (then it goes).

        Raises:
            compute.ComputeUnavailable: its machine still waits for approval.
        """
        note, tar = self.held / f"{project.name}.json", self.held / f"{project.name}.tar"
        try:
            held = json.loads(note.read_text())
        except (OSError, ValueError):
            return None
        m = self.get(held.get("machine") or "")
        if not tar.exists() or not m or m.get("revoked") or held.get("inputs") != _inputs(files):
            tar.unlink(missing_ok=True)
            note.unlink(missing_ok=True)
            return None
        if registration.provisional(m):
            raise compute.ComputeUnavailable(f"meshed on {m['name']}, which waits for approval: once it's approved on the admin page, "
                                             "this climb takes that mesh")
        log(f"remote: taking what {m['name']} made before it was approved")
        return held["result"], tar

    def task(self, kind: str, files: dict[str, Path], args: dict, label: str, log, tracker=None) -> tuple[dict, Path]:
        """Run another kind of job (remote_job.py's --task: "beta" or "scale") on a machine that connects here.

        Args:
            files: what to send, by the name it has in the job's folder there (a file, or a folder and all in it).
            args: given to the job as task.json.
            label: what the admin page calls it.

        Returns:
            What the job said it made, and a new folder holding its out/, which is the caller's to delete.

        Raises:
            compute.ComputeUnavailable: no machine has been in touch lately, or the one doing it went quiet.
        """
        self.files.mkdir(parents=True, exist_ok=True)
        result, back, job = self._run(kind, files, args, label, self.files, log, tracker)
        try:
            safe_extract(job.output, back, ("out",))
        except BaseException:
            shutil.rmtree(back, ignore_errors=True)
            raise
        finally:
            job.output.unlink(missing_ok=True)
        return result, back

    def _run(self, kind: str, files: dict[str, Path], args: dict, label: str, near: Path, log, tracker) -> tuple[dict, Path, "Job"]:
        """Hand a job to a machine and follow it until it's done: its result, a new folder in near for its results,
        and the job, whose output (a tar) is the caller's to unpack and delete."""
        from progress import Tracker
        tracker = tracker or Tracker([])
        until = time.time() + 3 * POLL     # as just after a restart here, before machines have asked again
        while not self.connected():
            if time.time() > until:
                raise compute.ComputeUnavailable(f"no machine has asked this server for work in the last {CONNECTED // 60} minutes")
            self.jobctl.check()
            time.sleep(1)
        image = self.image()
        if not image:
            raise compute.ComputeUnavailable("set the Docker image the machines run on the admin page")
        job = self._new_job(kind, files, args, label, image)
        log(f"remote: {job.size / 1e6:.0f} MB ready for the machines that connect here, job {job.id}")
        result, last, said, said_at = None, "", None, 0.0
        try:
            while True:
                with self.cond:
                    if not job.lines and job.state in ("queued", "running"):
                        self.cond.wait(1.0)
                    lines, job.lines = job.lines, []
                    state, touched = job.state, job.touched
                for line in lines:
                    result = compute.follow(line, tracker, log) or result
                    if not line.startswith(("@@", "remote: ")):
                        last = line or last
                self.jobctl.check()
                if state == "queued" and time.time() - said_at > 30:
                    said_at, why = time.time(), self.held_why()
                    if why and why != said:
                        log(f"remote: waiting: {why}")
                    said = why
                if state == "queued" and not self.connected():
                    raise compute.ComputeUnavailable(f"no machine has asked this server for work in the last {CONNECTED // 60} minutes")
                if state == "running" and time.time() - touched > QUIET:
                    raise compute.ComputeUnavailable(f"lost {job.machine_name}: not heard from in {QUIET // 60} minutes")
                if state == "failed":
                    raise compute.job_failed(job.error or last)
                if state == "done":
                    break
            if not result:
                raise compute.job_failed(last)
            log(f"remote: {job.machine_name} finished in {(time.time() - job.taken) / 60:.0f} min")
            return result, Path(tempfile.mkdtemp(prefix="bm-remote-", dir=near)), job
        except BaseException:
            job.output.unlink(missing_ok=True)
            raise
        finally:
            with self.cond:     # a machine still on it hears it's gone the next time it reports
                self.jobs.pop(job.id, None)
                if job.state not in ("done", "failed"):
                    self._ended(job, "gone")
            job.input.unlink(missing_ok=True)

    def _new_job(self, kind: str, files: dict[str, Path], args: dict, label: str, image: str) -> Job:
        jid = secrets.token_hex(6)
        self.files.mkdir(parents=True, exist_ok=True)
        path = self.files / f"{jid}.in.tar"
        with tarfile.open(path, "w") as tar:
            for name, src in files.items():
                tar.add(src, arcname=name)
            told = kind != "reconstruct" or bool(args.get("mapper"))
            if told:
                data = json.dumps(args).encode()
                info = tarfile.TarInfo("task.json")
                info.size, info.mtime = len(data), time.time()
                tar.addfile(info, io.BytesIO(data))
        # a reconstruction sent task.json only goes to scripts that take one (version 2)
        photos = sum(1 for _ in files["photos"].iterdir()) if "photos" in files and files["photos"].is_dir() else 0
        job = Job(id=jid, label=label, input=path, size=path.stat().st_size, internet=bool(args.get("internet")), image=image,
                  task=kind, version=2 if told else 1, gpu_mb=gpu_need_mb(kind, photos))
        with self.cond:
            self.jobs[jid] = job
        self.q("INSERT OR REPLACE INTO machine_jobs(id, task, label, created) VALUES(?,?,?,?)", jid, kind, label, job.created)
        return job

    # -- for the admin page ---------------------------------------------------------------------------------------
    def state(self) -> dict:
        now = time.time()
        with self.cond:
            jobs = [{"id": j.id, "project": j.label, "task": j.task, "state": j.state, "machine": j.machine_name, "machine_id": j.machine,
                     "minutes": round((now - (j.taken or j.created)) / 60, 1)} for j in self.jobs.values()]
            seen = dict(self.seen)
        machines, rules = [], {**RULES, **self.rules()}
        held = {}
        for f in self.held.glob("*.json") if self.held.is_dir() else []:
            try:
                mid = json.loads(f.read_text()).get("machine")
                held[mid] = held.get(mid, 0) + 1
            except (OSError, ValueError):
                pass
        for m in self.q("SELECT * FROM machines ORDER BY created"):
            doing = next((j for j in jobs if j["state"] == "running" and j["machine_id"] == m["id"]), None)
            waiting = registration.provisional(m)
            if m["revoked"]:
                status = "denied" if m.get("status") == "denied" else "revoked"
            elif waiting and not rules["provisional_work"]:
                status = "awaiting approval"
            elif seen.get(m["id"], 0) > now - CONNECTED:
                cur = self.current(m["id"])
                status = "working" if doing else "held back" if cur and cur["state"] == "held back" else "waiting for work"
            else:
                status = "not connected"
            machines.append({"id": m["id"], "name": m["name"], "status": status, "last_seen": m["last_seen"], "address": m["last_ip"],
                             "token_kept": bool(m["token"]),
                             "client": m["client"], "gpu": bool(m["gpu"]), "cpus": m["cpus"], "done": m["done"], "failed": m["failed"],
                             "job": doing, "provisional": waiting, "held": held.get(m["id"], 0), "created": m["created"],
                             "now": self.current(m["id"]), "day": self.shares(m["id"], now - 86400),
                             "week": self.shares(m["id"], now - 7 * 86400)})
        return {"machines": machines, "jobs": jobs, "changes": self.changes(),
                "connected": sum(m["status"] in ("working", "waiting for work", "held back") for m in machines)}


def _inputs(files: dict[str, Path]) -> list:
    """What a reconstruction is made from, to know a held one is still what its climb would make: each file's name and size."""
    out = []
    for name, src in sorted(files.items()):
        for f in sorted(src.rglob("*")) if src.is_dir() else [src]:
            if f.is_file():
                out.append([name + "/" + str(f.relative_to(src)) if src.is_dir() else name, f.stat().st_size])
    return out


def gpu_need_mb(kind: str, photos: int) -> int:
    """The GPU memory a job may need: for a reconstruction, VGGT placing GPU_NEED_PHOTOS of its photos (or all, if
    fewer), its largest use, should COLMAP not place them; for the others, OTHER_GPU_MB."""
    if kind != "reconstruct":
        return OTHER_GPU_MB
    n = max(vggt_place.MIN_PHOTOS, min(photos, GPU_NEED_PHOTOS))
    return vggt_place.GPU_BASE_MB + vggt_place.GPU_SPARE_MB + vggt_place.GPU_PER_PHOTO_MB * n


def _number(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def safe_extract(tar_path: Path, dest: Path, top: tuple[str, ...]) -> None:
    """Unpack a tar from elsewhere: only files and folders, all under the given top-level folders."""
    with tarfile.open(tar_path) as tar:
        members = tar.getmembers()
        for t in members:
            parts = Path(t.name).parts
            if not (t.isfile() or t.isdir()) or not parts or parts[0] not in top or ".." in parts or Path(t.name).is_absolute():
                raise ValueError(f"the results hold something they shouldn't: {t.name!r}")
        tar.extractall(dest, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))


# ------------------------------------------------------------------------------------------- serving it
PAGE = """BoulderMesh: machines lent for reconstructions connect here: the script makes a token and registers it.
The script: {base}/worker/bouldermesh_worker.py
"""


def handler_for(board: Board, log: Callable[[str], None]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BoulderMeshWorkers"
        sys_version = ""
        timeout = 120

        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, body: bytes, kind: str = "application/json", headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code: int = 200) -> None:
            self.send(code, json.dumps(obj).encode())

        def who(self) -> dict | None:
            auth = self.headers.get("Authorization", "")
            m = board.machine(auth[7:].strip()) if auth.startswith("Bearer ") else None
            if not m:
                self.json({"error": "unknown token: run the script without --token, and it makes one and registers it"}, 401)
                return None
            if m["revoked"]:
                self.json({"error": "this machine was turned down by whoever runs this site" if m.get("status") == "denied" else
                           "this token no longer works"}, 403)
                return None
            return m

        def body(self, most: int) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= most:
                raise ValueError(f"a request of at most {most // 2**20} MB")
            return self.rfile.read(n)

        def handle_errors(self, fn) -> None:
            try:
                fn()
            except OffsetError as e:
                self.json({"error": str(e), "received": e.have}, 409)
            except LookupError as e:
                self.json({"error": str(e), "gone": True}, 409)
            except ValueError as e:
                self.json({"error": str(e)}, 400)
            except Exception as e:
                log(f"machines: error on {urlparse(self.path).path}: {type(e).__name__}: {e}")
                self.json({"error": "the server failed at that: try again later"}, 500)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            base = f"https://{self.headers.get('X-Forwarded-Host') or self.headers.get('Host') or 'this-site'}"
            if u.path in ("/worker", "/worker/"):
                return self.send(200, PAGE.replace("{base}", base).encode(), "text/plain; charset=utf-8")
            if u.path == "/worker/bouldermesh_worker.py" and SCRIPT.exists():
                try:    # someone fetching the script (or a copy updating itself, which the server tells apart)
                    board.downloaded(script_fetch(self))
                except Exception as e:  # noqa: BLE001 - telling of it mustn't stop the download
                    log(f"machines: telling of a download failed: {type(e).__name__}: {e}")
                return self.send(200, SCRIPT.read_bytes(), "text/x-python; charset=utf-8",
                                 {"Content-Disposition": 'attachment; filename="bouldermesh_worker.py"'})
            m = re.fullmatch(r"/worker/api/jobs/([0-9a-f]{12})/(input|output)", u.path)
            if not m:
                return self.json({"error": "not found"}, 404)
            who = self.who()
            if not who:
                return

            def get():
                if m[2] == "input":
                    return self.send(200, board.input(who, m[1], int((qs.get("offset") or ["0"])[0] or 0)), "application/octet-stream")
                self.json({"received": board.received(who, m[1])})

            self.handle_errors(get)

        def register(self) -> None:
            """A script registering the token it made (the Authorization header's), with its name if it gave one."""
            auth = self.headers.get("Authorization", "")
            try:
                body = json.loads(self.body(2**16) or b"{}")
                if int(body.get("version") or 0) < CLIENT_VERSION:
                    return self.json(registration.too_old(SCRIPT, "/worker/"), 426)
                m = board.register(auth[7:].strip() if auth.startswith("Bearer ") else "", body.get("name"), _client_ip(self),
                                   str(body.get("client") or ""))
            except registration.Refused as e:
                return self.json({"error": str(e)}, e.code)
            except ValueError as e:
                return self.json({"error": str(e)}, 400)
            if m["revoked"]:
                return self.json({"error": "this token no longer works"}, 403)
            waiting = registration.provisional(m)
            log(f"machines: {m['name']} registered from {_client_ip(self)}" if waiting else f"machines: {m['name']} registered again")
            self.json({"id": m["id"], "name": m["name"], "status": "provisional" if waiting else "approved",
                       "may_work": not waiting or {**RULES, **board.rules()}["provisional_work"]})

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/worker/api/register":
                return self.register()
            who = self.who()
            if not who:
                return

            def post():
                if u.path == "/worker/api/bye":     # its script stopped
                    self.body(2**16)
                    board.bye(who)
                    log(f"machines: {who['name']} said goodbye")
                    return self.json({"ok": True})
                if u.path == "/worker/api/work":
                    body = json.loads(self.body(2**20) or b"{}")
                    if int(body.get("version") or 0) < CLIENT_VERSION:
                        return self.json(registration.too_old(SCRIPT, "/worker/"), 426)
                    news = registration.script_news(SCRIPT, body) if not body.get("check") else {}
                    if "update" in news:    # asked between jobs: it updates itself and asks again
                        log(f"machines: {who['name']} told to update its script to version {news['update']['version']}")
                        return self.json(news)
                    out = {**board.work(who, _client_ip(self), body), **news}
                    if "job" in out:
                        log(f"machines: {who['name']} took job {out['job']['id']}")
                    return self.json(out)
                m = re.fullmatch(r"/worker/api/jobs/([0-9a-f]{12})/(log|output|done)", u.path)
                if not m:
                    return self.json({"error": "not found"}, 404)
                if m[2] == "log":
                    return self.json(board.report(who, m[1], (json.loads(self.body(8 * 2**20) or b"{}")).get("lines") or []))
                if m[2] == "output":
                    one = lambda k: (qs.get(k) or [""])[0]     # noqa: E731
                    got = board.part(who, m[1], int(one("offset") or -1), int(one("size") or 0), self.body(PART_MAX))
                    return self.json({"received": got})
                report = json.loads(self.body(2**20) or b"{}")
                out = board.finish(who, m[1], report)
                log(f"machines: {who['name']}: job {m[1]} {report.get('status')}" + (f": {report.get('error')}" if report.get("error") else ""))
                return self.json(out)

            self.handle_errors(post)

    return Handler


def serve(board: Board, host: str, port: int, log: Callable[[str], None] = print) -> ThreadingHTTPServer | None:
    """Serve machines on host:port in the background (None if it can't), only /worker/..."""
    try:
        httpd = ThreadingHTTPServer((host, port), handler_for(board, log))
    except OSError as e:
        log(f"machines: NOT served on {host}:{port}: {e}")
        return None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"machines: served on http://{host}:{port}/worker/")
    return httpd
