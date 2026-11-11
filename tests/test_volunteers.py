#!/usr/bin/env python3
"""Volunteers fetching YouTube videos: the broker's queue, limits and rests; the volunteer script against it, with a
stand-in for yt-dlp; the video cache, and a harvest asking volunteers for what YouTube refused; and the whole way
through the server, from the admin page's buttons to a project harvested again. Offline; needs ffmpeg for the videos.

    python3 tests/test_volunteers.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-volunteers-"))
os.environ["DATA_DIR"] = str(TMP / "data")      # before the climbs package reads it
sys.path.insert(0, str(ROOT / "backend"))
import volunteers  # noqa: E402
from climbs import harvest, youtube  # noqa: E402
from climbs.common import Climb, Media  # noqa: E402

SCRIPT = ROOT / "volunteer" / "bouldermesh_volunteer.py"
FAKE_YTDLP = '''#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
if "--version" in args:
    print("2099.01.01"); sys.exit(0)
url = args[-1]
vid = url.rsplit("=", 1)[-1]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
if url.startswith("ytsearch"):
    print(json.dumps({"entries": [{"id": "sssssssssss", "title": "Relic V9 Joshua Tree", "duration": 90.0, "channel": "a climber",
                                   "channel_id": "UC" + "b" * 22, "url": "https://www.youtube.com/watch?v=sssssssssss", "extra": "x"}]}))
    sys.exit(0)
if "/channel/" in url:
    print(json.dumps({"channel": "a climber", "description": "I'm 6 ft tall"})); sys.exit(0)
if vid.startswith("refused"):
    sys.stderr.write("ERROR: [youtube] " + vid + ": Sign in to confirm you\\u2019re not a bot. This helps protect our community.\\n"); sys.exit(1)
if vid.startswith("slow"):
    import time; time.sleep(30)
if vid.startswith("private"):
    sys.stderr.write("ERROR: [youtube] " + vid + ": Private video. Sign in if you've been granted access to this video\\n"); sys.exit(1)
info = {"id": vid, "title": "Relic V9 Joshua Tree", "uploader": "a climber", "duration": 4.0, "description": "I'm 180 cm tall",
        "webpage_url": url, "formats": [{"url": "https://rr1---sn.googlevideo.com/videoplayback?ip=203.0.113.9"}]}
if "-J" in args:
    print(json.dumps(info)); sys.exit(0)
out = args[args.index("-o") + 1]
shutil.copy(os.environ["FAKE_VIDEO"], out.replace("%(ext)s", "mp4"))
json.dump(info, open(out.replace("%(ext)s", "info.json"), "w"))
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(url: str, body=None, token: str | None = None, method: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def make_video(path: Path, seconds: int = 4) -> Path:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


# --- the broker on its own ---------------------------------------------------------------------------------------
def broker_alone() -> None:
    got, ready = [], []
    b = volunteers.Broker(TMP / "b1" / "v.sqlite", TMP / "b1" / "files", lambda t, p, i: got.append((t, p, i)),
                          lambda purpose, ref: ready.append((purpose, ref)), probe=lambda p: 4.0)
    assert not b.any() and not b.want("video", "not-an-id", "harvest", "p1")
    v = b.add("Alice", per_hour=3)
    assert v["token"].startswith("bmv_") and b.any()
    alice = b.volunteer(v["token"])
    assert alice and alice["per_hour"] == 3 and b.volunteer("bmv_wrong") is None
    assert b.add("Bob", per_hour=99)["id"] and b.volunteer(b.add("Carol")["token"])["per_hour"] == volunteers.PER_HOUR, "capped"

    assert b.work(alice, "198.51.100.1")["why"] == "nothing wanted now"
    for vid, ref in (("aaaaaaaaaaa", "p1"), ("bbbbbbbbbbb", "p1"), ("ccccccccccc", "p2")):
        assert b.want("video", vid, "harvest", ref)
    assert b.want("video", "aaaaaaaaaaa", "beta", "m1"), "a second purpose for the same video"
    assert len(b.q("SELECT * FROM tasks")) == 3

    t = b.work(alice, "198.51.100.1")["task"]
    assert t["video_id"] == "aaaaaaaaaaa" and t["format"] == volunteers.VIDEO_FORMAT
    assert b.work(alice, "198.51.100.1")["task"]["id"] == t["id"], "one at a time: what it has, again"
    try:
        b.part(alice, t["id"], 5, 10, "mp4", b"x")
        raise AssertionError("not where it's got to")
    except volunteers.OffsetError as e:
        assert e.have == 0
    for bad in ((0, 10, "exe", b"x"), (0, 2 * volunteers.MAX_VIDEO, "mp4", b"x"), (0, 3, "mp4", b"toolong")):
        try:
            b.part(alice, t["id"], *bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert b.part(alice, t["id"], 0, 10, "mp4", b"01234") == 5 and b.received(alice, t["id"]) == 5
    try:
        b.finish(alice, t["id"], {"status": "ok", "info": {}})
        raise AssertionError("not all of it")
    except ValueError:
        pass
    assert b.part(alice, t["id"], 5, 10, "mp4", b"56789") == 10
    assert b.finish(alice, t["id"], {"status": "ok", "info": {"duration": 4, "title": "x" * 1000, "formats": ["ip=1"]}})["ok"]
    task, path, info = got[0]
    assert task["video_id"] == "aaaaaaaaaaa" and path.name.endswith(".mp4") and set(info) <= set(volunteers.DETAILS) | {"url", "duration"}
    assert info["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa" and len(info["title"]) == 500
    assert ready == [("beta", "m1")], "p1 still wants bbbbbbbbbbb; m1 wanted only this"
    try:
        b.part(alice, t["id"], 0, 10, "mp4", b"x")
        raise AssertionError("done: not Alice's any more")
    except LookupError:
        pass

    # a refusal: Alice rests an hour, and the video goes back for someone else
    t = b.work(alice, "198.51.100.1")["task"]
    assert b.finish(alice, t["id"], {"status": "refused", "error": "not a bot"})["rest_until"] > time.time() + 3500
    alice = b.volunteer(v["token"])
    assert b.work(alice, "198.51.100.1")["why"].startswith("YouTube refused")
    bob = b.volunteer(b.add("Bob2")["token"])
    t2 = b.work(bob, "198.51.100.2")["task"]
    assert t2["id"] == t["id"], "the refused one, to the next volunteer"
    assert b.finish(bob, t2["id"], {"status": "failed", "error": "Private video", "permanent": True})["ok"]
    assert ready == [("beta", "m1"), ("harvest", "p1")], "p1: one came, one failed for good"
    assert not b.want("video", "bbbbbbbbbbb", "harvest", "p1"), "failed for good: not asked for again"
    t3 = b.work(bob, "198.51.100.2")["task"]
    assert b.finish(bob, t3["id"], {"status": "released"})["ok"] and b.work(bob, "198.51.100.2")["task"]["id"] == t3["id"]
    b.finish(bob, t3["id"], {"status": "failed", "error": "network"})
    assert b.q("SELECT state, tries FROM tasks WHERE id=?", t3["id"], one=True) == {"state": "queued", "tries": 1}, "might pass"

    # limits: Alice's 3 an hour are spent (1 done, 1 refused, plus this); and her address's, for anyone there
    b.q("UPDATE volunteers SET refused_until=NULL")
    alice = b.volunteer(v["token"])
    b.want("video", "ddddddddddd", "harvest", "p3")
    b.want("video", "eeeeeeeeeee", "harvest", "p3")
    assert "task" in b.work(alice, "198.51.100.1")
    b.finish(alice, b.work(alice, "198.51.100.1")["task"]["id"], {"status": "released"})
    w = b.work(alice, "198.51.100.1")
    assert "an hour is the most for you" in w["why"] and 3500 < w["wait"] < 3700, w
    carol = b.volunteer(b.add("Carol2", per_hour=2)["token"])
    assert "your address" in b.work(carol, "198.51.100.1")["why"]
    assert "task" in b.work(carol, "198.51.100.9")

    # work given out that never came back goes to someone else; revoking hands back what it had
    b.q("UPDATE tasks SET touched=?, assigned=? WHERE state='assigned'", time.time() - 7200, time.time() - 7200)
    b.requeue_stale()
    assert not b.q("SELECT 1 FROM tasks WHERE state='assigned'")
    t = b.work(carol, "198.51.100.9")["task"]
    b.revoke(carol["id"])
    assert b.q("SELECT state FROM tasks WHERE id=?", t["id"], one=True)["state"] == "queued"
    st = b.state()
    assert {x["name"]: x["status"] for x in st["volunteers"]}["Carol2"] == "revoked" and st["counts"]["done_video"] == 1, st
    # searches and channels: by their words and id, and only to a script that says it does them
    assert not b.want("search", "x\nrm -rf /", "harvest", "p9") and not b.want("channel", "not-a-channel", "channel", "x")
    b.q("UPDATE tasks SET state='cancelled' WHERE state IN ('queued','assigned')")
    assert b.want("search", "Relic V9 Joshua Tree", "harvest", "p9") and b.pending("harvest", "p9") == 1
    dave = b.volunteer(b.add("Dave")["token"])
    kinds = ["video", "details", "search", "channel"]
    assert b.work(dave, "203.0.113.20")["why"] == "nothing wanted now", "an old script gets no searches"
    t = b.work(dave, "203.0.113.20", kinds=kinds)["task"]
    assert t == {"id": t["id"], "kind": "search", "query": "Relic V9 Joshua Tree", "max_results": volunteers.SEARCH_RESULTS}, t
    b.finish(dave, t["id"], {"status": "ok", "results": [{"id": "rrrrrrrrrrr", "title": "Relic V9", "duration": 60, "url": "https://x.example"},
                                                          {"id": "../../etc"}, "nonsense"]})
    task, path, info = got[-1]
    assert task["kind"] == "search" and path is None and info["query"] == "Relic V9 Joshua Tree", info
    assert info["results"] == [{"id": "rrrrrrrrrrr", "title": "Relic V9", "description": None, "duration": 60, "channel": None,
                                "channel_id": None, "uploader": None}], "only results with a video's id, and no addresses"
    assert ready[-1] == ("harvest", "p9") and b.pending("harvest", "p9") == 0
    cid = "UC" + "a" * 22
    assert b.want("channel", cid, "channel", cid)
    t = b.work(dave, "203.0.113.20", kinds=kinds)["task"]
    assert t == {"id": t["id"], "kind": "channel", "channel_id": cid}, t
    b.finish(dave, t["id"], {"status": "ok", "info": {"name": "A climber", "description": "I'm 180 cm tall", "url": "https://x.example"}})
    assert got[-1][2] == {"channel_id": cid, "url": f"https://www.youtube.com/channel/{cid}", "name": "A climber",
                          "description": "I'm 180 cm tall"}, got[-1]
    b.want("video", "fffffffffff", "harvest", "p10")
    assert b.cancel() >= 1 and not b.q("SELECT 1 FROM tasks WHERE state='queued'")
    print("broker OK")


# --- the script against the broker --------------------------------------------------------------------------------
def script_and_broker() -> None:
    video = make_video(TMP / "fake.mp4")
    fake = TMP / "fake-yt-dlp"
    fake.write_text(FAKE_YTDLP)
    fake.chmod(0o755)
    got, ready = [], []
    b = volunteers.Broker(TMP / "b2" / "v.sqlite", TMP / "b2" / "files",
                          lambda t, p, i: got.append((t["video_id"], p.read_bytes() if p else None, i)),
                          lambda purpose, ref: ready.append((purpose, ref)))
    port = free_port()
    httpd = volunteers.serve(b, "127.0.0.1", port, log=lambda line: None)
    base = f"http://127.0.0.1:{port}"
    token = b.add("tester")["token"]
    env = {**os.environ, "FAKE_VIDEO": str(video), "FAKE_LOG": str(TMP / "fake.log")}

    def volunteer(*extra) -> str:
        r = subprocess.run([sys.executable, str(SCRIPT), "--server", base, "--token", token, "--agree", "--once",
                            "--yt-dlp", str(fake), "--home", str(TMP / "home"), *extra], env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    try:
        st, page = call(base + "/volunteer/")
        assert st == 200 and b"bouldermesh_volunteer.py" in page
        assert b"Help compute meshes" in page and b"/worker/bouldermesh_worker.py" in page, "both ways to help, on one page"
        assert b'data-show="data">Data collection' in page and b"Coming soon" in page and b">Help collect videos<" not in page.split(b"<section")[0]
        st, script = call(base + "/volunteer/bouldermesh_volunteer.py")
        assert st == 200 and script == SCRIPT.read_bytes()
        assert call(base + "/volunteer/api/work", {"version": 1})[0] == 401
        assert call(base + "/volunteer/api/work", {"version": 0}, token)[0] == 426, "too old a script"
        assert call(base + "/api/status")[0] == 404, "only /volunteer/"

        assert "nothing to do" in volunteer()
        b.want("video", "vvvvvvvvvvv", "harvest", "p1")
        b.want("details", "ddddddddddd", "details", "https://youtu.be/ddddddddddd")
        out = volunteer()
        assert "sent" in out, out
        vid, data, info = got[0]
        assert vid == "vvvvvvvvvvv" and data == video.read_bytes() and info["uploader"] == "a climber", info
        assert "formats" not in info and "203.0.113.9" not in json.dumps(info), "nothing of the volunteer's own"
        assert ready == [("harvest", "p1")]
        cmd = (TMP / "fake.log").read_text().splitlines()[-1]
        assert "--sleep-requests 0.75" in cmd and "bv*[height<=1080]/b" in cmd and cmd.endswith("https://www.youtube.com/watch?v=vvvvvvvvvvv")

        conf = json.loads((TMP / "home" / "config.json").read_text())
        assert conf["token"] == token and oct((TMP / "home" / "config.json").stat().st_mode)[-3:] == "600"
        r = subprocess.run([sys.executable, str(SCRIPT), "--once", "--yt-dlp", str(fake), "--home", str(TMP / "home")],
                           env=env, capture_output=True, text=True, timeout=120)    # remembered, and the gap kept
        assert r.returncode == 0 and "sent" in r.stdout and got[1][0] == "ddddddddddd" and got[1][1] is None, r.stdout + r.stderr
        state = json.loads((TMP / "home" / "state.json").read_text())
        gaps = sorted(state["fetched"])
        assert gaps[1] - gaps[0] >= 8, "8-15 s between fetches"

        # a search, and a channel's description: only words and an id from the server, paced as videos are
        b.want("search", "Relic V9 Joshua Tree", "harvest", "p4")
        out = volunteer()
        assert "sent 1 result(s)" in out, out
        assert (TMP / "fake.log").read_text().splitlines()[-1].endswith("--flat-playlist ytsearch15:Relic V9 Joshua Tree")
        assert got[-1][2]["results"][0]["id"] == "sssssssssss" and "extra" not in got[-1][2]["results"][0] and ready[-1] == ("harvest", "p4")
        b.want("channel", "UC" + "b" * 22, "channel", "UC" + "b" * 22)
        out = volunteer()
        assert "sent" in out and got[-1][2]["description"] == "I'm 6 ft tall", out
        assert (TMP / "fake.log").read_text().splitlines()[-1].endswith("https://www.youtube.com/channel/UC" + "b" * 22)

        b.want("video", "refused0000", "harvest", "p2")
        assert "YouTube refused: resting 1 h" in volunteer()
        assert b.q("SELECT refused_until FROM volunteers", one=True)["refused_until"] > time.time() + 3500
        assert json.loads((TMP / "home" / "state.json").read_text())["rest_until"] > time.time() + 3500, "and it rests itself"

        # stopped partway (a kill, as by pkill): what it had goes back for someone else
        b.q("UPDATE volunteers SET refused_until=NULL")
        b.want("video", "slow0000000", "harvest", "p3")
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--server", base, "--token", token, "--agree", "--yt-dlp", str(fake),
                                 "--home", str(TMP / "home3")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            if "slow0000000" in (TMP / "fake.log").read_text():     # yt-dlp has started on it
                break
            time.sleep(0.1)
        proc.terminate()
        out = proc.communicate(timeout=30)[0]
        assert "stopped" in out and b.q("SELECT state, volunteer FROM tasks WHERE video_id='slow0000000'", one=True) == \
            {"state": "queued", "volunteer": None}, out
        print("script OK")
    finally:
        httpd.shutdown()


# --- the script registering a token of its own, and its approval -----------------------------------------------------
def registering() -> None:
    import registration
    assert registration.clean_name("  Jane   Q\tPublic\n ", "volunteer") == "Jane-Q-Public"
    assert registration.clean_name(" \x07 ", "volunteer") == "volunteer" and len(registration.clean_name("x" * 99, "v")) == 40
    assert registration.unique_name("Jane", ["jane", "Jane-2", "bob"]) == "Jane-3" and registration.unique_name("Al", ["Alan"]) == "Al"

    video = make_video(TMP / "fake-r.mp4")
    fake = TMP / "fake-yt-dlp"
    rules = dict(volunteers.RULES, provisional_work=False, registrations_per_address=3, max_waiting=30)
    news, got = [], []
    b = volunteers.Broker(TMP / "b5" / "v.sqlite", TMP / "b5" / "files", lambda t, p, i: got.append(t["video_id"]), lambda *_: None,
                          rules=lambda: rules, registered=news.append)
    port = free_port()
    httpd = volunteers.serve(b, "127.0.0.1", port, log=lambda line: None)
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "FAKE_VIDEO": str(video), "FAKE_LOG": str(TMP / "fake-r.log")}

    def volunteer(home: str, *extra, ok: bool = True) -> str:
        r = subprocess.run([sys.executable, str(SCRIPT), "--server", base, "--agree", "--once", "--yt-dlp", str(fake),
                            "--home", str(TMP / home), *extra], env=env, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        assert (r.returncode == 0) is ok, r.stdout + r.stderr
        return r.stdout + r.stderr

    def token(home: str) -> str:
        return json.loads((TMP / home / "config.json").read_text())["token"]

    try:
        out = volunteer("r1", "--name", "  Jane   Q  Public ")
        assert "as 'Jane-Q-Public': waiting for approval" in out and "Until then, it waits" in out, out
        v1 = b.volunteer(token("r1"))
        assert token("r1").startswith("bmv_") and v1["status"] == "provisional" and v1["name"] == "Jane-Q-Public"
        assert [v["id"] for v in news] == [v1["id"]], "the server hears of it, once"
        b.want("video", "vvvvvvvvvvv", "harvest", "p1")
        assert "waiting for approval by whoever runs this site" in volunteer("r1") and not got, "no work until approved"
        assert not b.any(), "no one to ask, while the only volunteer waits and mayn't work"
        assert "'jane-q-public-2'" in volunteer("r2", "--name", "jane q public"), "a name taken: numbered"
        assert "as 'volunteer'" in volunteer("r3"), "no name given, and no one to ask: the default"
        assert "3 a day is the most" in volunteer("r4", ok=False), "a fourth from one address in a day"
        assert not (TMP / "r4" / "config.json").exists() or not json.loads((TMP / "r4" / "config.json").read_text()).get("token")
        st, j = call(base + "/volunteer/api/register", {"version": 2, "name": "someone else"}, token("r1"))
        assert st == 200 and j["name"] == "Jane-Q-Public" and j["status"] == "provisional" and len(news) == 3, "registering again is harmless"
        assert call(base + "/volunteer/api/register", {"version": 2}, "short")[0] == 400
        assert call(base + "/volunteer/api/register", {"version": 0}, "bmv_" + "a" * 40)[0] == 426

        # while waiting, with the admin page's leave: within its own limits
        rules.update(provisional_work=True, provisional_per_hour=1)
        assert b.any()
        assert "sent" in volunteer("r1") and got == ["vvvvvvvvvvv"]
        b.want("video", "wwwwwwwwwww", "harvest", "p1")
        assert "1 an hour is the most for you until you're approved" in volunteer("r1")
        assert b.approve(v1["id"])["status"] == "approved" and b.get(v1["id"])["approved"]
        b.q("UPDATE volunteers SET refused_until=NULL")
        assert "sent" in volunteer("r1") and got[-1] == "wwwwwwwwwww", "approved: everyone's limits"
        st = {v["name"]: v for v in b.state()["volunteers"]}
        assert not st["Jane-Q-Public"]["provisional"] and st["jane-q-public-2"]["provisional"] and st["volunteer"]["registered"]

        # turned down: the script forgets that token, says so, and registers a new one, as with none
        rules.update(registrations_per_address=100)
        old = token("r2")
        b.revoke(b.volunteer(old)["id"], "denied")
        out = volunteer("r2")
        assert "warning: " in out and "no longer takes this token (this token was turned down by whoever runs this site)" in out, out
        assert "Forgot it, and registering a new one" in out and "as 'jane-q-public-3'" in out and "Traceback" not in out, out
        assert token("r2") != old and b.volunteer(token("r2"))["status"] == "provisional", "the name given before, numbered"
        assert {v["name"]: v["status"] for v in b.state()["volunteers"]}["jane-q-public-2"] == "denied"

        # so many waiting that more must wait; and those never heard from again go
        rules.update(max_waiting=1)
        assert "too many are waiting" in volunteer("r5", ok=False)
        b.q("UPDATE volunteers SET created=?, last_seen=NULL WHERE status='provisional'", time.time() - 15 * 86400)
        assert "as 'someone'" in volunteer("r5", "--name", "someone"), "the old one dropped, making room"
        assert b.volunteer(token("r3")) is None
        rules.update(max_waiting=30)
        old = token("r3")       # one the server no longer knows: forgotten, and another registered
        r = subprocess.run([sys.executable, str(SCRIPT), "--agree", "--once", "--yt-dlp", str(fake), "--home", str(TMP / "r3")],
                           env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert r.returncode == 0 and "no longer takes this token (unknown token" in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr
        assert token("r3") != old and b.volunteer(token("r3"))["name"] == "volunteer", "no name given before: none now"
        r = subprocess.run([sys.executable, str(SCRIPT), "--forget-token", "--home", str(TMP / "r3")], env=env, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert r.returncode == 0 and "forgot the token" in r.stdout and "token" not in json.loads((TMP / "r3" / "config.json").read_text())
        assert "server" in json.loads((TMP / "r3" / "config.json").read_text()), "the rest remembered"
        assert "no token was remembered" in subprocess.run([sys.executable, str(SCRIPT), "--forget-token", "--home", str(TMP / "r3")],
                                                           env=env, capture_output=True, text=True, timeout=60).stdout
        assert "registered with" in volunteer("r3"), "the next run registers a new one"
        out = volunteer("r2", "--register", "--name", "jane again")
        assert "as 'jane-again': waiting for approval" in out and b.volunteer(token("r2"))["status"] == "provisional", out
    finally:
        httpd.shutdown()

    # a video's length, by someone else's search: one sending a video can't vouch for it alone
    b = volunteers.Broker(TMP / "b6" / "v.sqlite", TMP / "b6" / "files", lambda *a: None, lambda *_: None, probe=lambda p: 4.0)
    a, c = (b.volunteer(b.add(n)["token"]) for n in ("a", "c"))
    b.want("search", "Relic V9 Joshua Tree", "harvest", "p")
    t = b.work(a, "10.0.0.1", kinds=["search"])["task"]
    b.finish(a, t["id"], {"status": "ok", "results": [{"id": "xxxxxxxxxxx", "duration": 100}]})
    for who, ok in ((c, False), (a, True)):
        b.want("video", "xxxxxxxxxxx", "harvest", "p")
        t = b.work(b.get(who["id"]), "10.0.0.2")["task"]
        b.part(who, t["id"], 0, 3, "mp4", b"abc")
        r = b.finish(who, t["id"], {"status": "ok", "info": {"duration": 4}})
        assert r["ok"] is ok, (who["name"], r)
        if not ok:
            assert "100 s by another's search" in b.q("SELECT error FROM tasks WHERE id=?", t["id"], one=True)["error"]
    print("registering OK")


# --- the cache, and a harvest asking volunteers -----------------------------------------------------------------------
def cache_and_harvest() -> None:
    video = make_video(TMP / "cached.mp4", 6)
    youtube.cache_put("https://www.youtube.com/watch?v=CCCCCCCCCCC", Path(shutil.copy(video, TMP / "c1.mp4")),
                      {"title": "cached", "license": "CC BY"})
    assert youtube.cached("https://youtu.be/CCCCCCCCCCC") and youtube.cached_details("CCCCCCCCCCC")["license"] == "CC BY"
    assert youtube.cached("https://www.youtube.com/watch?v=NNNNNNNNNNN") is None
    vimeo = Path(shutil.copy(video, TMP / "v1.mp4"))
    assert youtube.cache_put("https://vimeo.com/1234", vimeo) == vimeo and vimeo.exists(), "not YouTube's: left where it is"

    calls, wanted = [], []
    real_call = youtube.call

    def refusing(cmd, **kw):
        calls.append(cmd)
        raise youtube.Refused({"since": 0, "until": time.time() + 60, "level": 0, "error": "not a bot"})
    youtube.call = refusing
    try:
        out = TMP / "project" / "photos"
        out.mkdir(parents=True)
        lines = []
        videos = [Media(url="https://www.youtube.com/watch?v=NNNNNNNNNNN", kind="video", page=None, source="youtube"),
                  Media(url="https://www.youtube.com/watch?v=CCCCCCCCCCC", kind="video", page=None, source="youtube"),
                  Media(url="https://www.youtube.com/watch?v=MMMMMMMMMMM", kind="video", page=None, source="youtube")]
        infos = {}
        frames = harvest.video_frames(videos, out, lines.append, infos=infos, want=wanted.append)
        assert len(calls) <= 1, "after a refusal, YouTube isn't asked again in this harvest"
        assert wanted == ["https://www.youtube.com/watch?v=NNNNNNNNNNN", "https://www.youtube.com/watch?v=MMMMMMMMMMM"], wanted
        assert frames and all(f.name.startswith("yt_") for f in frames), "the cached one's frames"
        assert infos["https://www.youtube.com/watch?v=CCCCCCCCCCC"]["license"] == "CC BY"
        assert youtube.cached("CCCCCCCCCCC"), "kept for beta"
        assert any("asked of volunteers" in line for line in lines), lines
        lines.clear()
        harvest.video_frames(videos[:1], out, lines.append)
        assert any("skipping 1 video" in line for line in lines), lines

        youtube.via = "volunteers"
        youtube.call = real_call
        try:
            youtube.call(["yt-dlp", "x"])
            raise AssertionError("never here")
        except youtube.Refused as e:
            assert "volunteers" in str(e)
        try:
            youtube.call(["true"], video=False, stdout=subprocess.DEVNULL)
            raise AssertionError("nor searches")
        except youtube.Refused as e:
            assert "volunteers" in str(e)
    finally:
        youtube.call, youtube.via = real_call, "volunteers_when_refused"

    big = TMP / "big.bin"
    big.write_bytes(b"\0" * 2**20)
    youtube.CACHE_BYTES = 2**20     # room for the new one only
    youtube.cache_put("OOOOOOOOOOO", Path(shutil.copy(big, TMP / "o.mp4")))
    assert youtube.cached("OOOOOOOOOOO") and not youtube.cached("CCCCCCCCCCC"), "the least recently used goes first"

    # a search this server leaves to volunteers: asked of them, and their results used once they've come
    youtube.via = "volunteers"
    try:
        relic = Climb(source="mountainproject", source_id="9", url="https://example.com/relic", name="Relic", kind="boulder", grade="V9",
                      area_path=["California", "Joshua Tree National Park", "Real Hidden Valley"])
        lines, asked, words, state = [], [], [], {}
        assert youtube.search(relic, lines.append, want=lambda w: words.append(w) or True, asked=asked, state=state) == []
        assert state == {"checked": False, "words": youtube.query(relic)}, state
        assert words == asked == [youtube.query(relic)] and "asked volunteers to search" in lines[-1], lines
        assert youtube.search(relic, lines.append) == [] and "skipped" in lines[-1], "without volunteers, none"
        youtube.save_search(youtube.query(relic), [{"id": "sssssssssss", "title": "Relic V9 Joshua Tree", "duration": 90},
                                                   {"id": "ttttttttttt", "title": "Another climb", "duration": 90}], "volunteer")
        found = youtube.search(relic, lines.append, want=lambda w: words.append(w) or True, state=state)
        assert state["checked"], "searched, by a volunteer"
        assert [v.url for v in found] == ["https://www.youtube.com/watch?v=sssssssssss"] and len(words) == 1, (found, words)
        assert "as a volunteer searched 0 h ago" in lines[-2], lines
        assert youtube.cached_search(youtube.query(relic), days=0) is None, "and searched again once it's old"
    finally:
        youtube.via = "volunteers_when_refused"
    print("cache and harvest OK")


# --- through the server ---------------------------------------------------------------------------------------------
def through_the_server() -> None:
    data = TMP / "server"
    port, admin_port, vport = free_port(), free_port(), free_port()
    env = {**os.environ, "DATA_DIR": str(data), "PORT": str(port), "HOST": "127.0.0.1", "ADMIN_PORT": str(admin_port),
           "VOLUNTEER_PORT": str(vport), "VOLUNTEER_URL": "https://rocks.example", "HTTP_MIN_INTERVAL": "0", "STOP_WAIT_MINUTES": "0.1"}
    for k in ("EDIT_TOKEN", "READ_ONLY", "YOUTUBE_VIA"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "backend" / "server.py")], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    admin, vbase = f"http://127.0.0.1:{admin_port}", f"http://127.0.0.1:{vport}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        st = call(admin + "/api/admin/state")[1]
        assert st["volunteers"]["served"] and st["settings"]["youtube.via"] == "volunteers_when_refused" and not st["volunteers"]["volunteers"]
        assert call(vbase + "/volunteer/")[0] == 200

        # a harvest that went without a video, as the database would have it
        con = sqlite3.connect(data / "bouldermesh.sqlite")
        con.execute("INSERT INTO projects(id,name,created,kind,status,harvest) VALUES('abcdef012345','Relic',?, 'harvest','harvested',?)",
                    (time.time(), json.dumps({"images": 3, "videos": [{"url": "https://www.youtube.com/watch?v=vvvvvvvvvvv", "frames": 0},
                                                                    {"url": "https://www.youtube.com/watch?v=wwwwwwwwwww", "frames": 40}]})))
        con.execute("INSERT INTO videos(url) VALUES('https://youtu.be/vvvvvvvvvvv')")
        con.commit()
        con.close()
        (data / "projects" / "abcdef012345" / "photos").mkdir(parents=True)

        r = call(admin + "/api/admin/actions/volunteer-add", {"name": "Dana", "per_hour": 5})[1]
        assert r["command"] == f"python3 bouldermesh_volunteer.py --server https://rocks.example --token {r['token']}", r
        r2 = call(admin + "/api/admin/actions/volunteers-skipped", {})[1]
        assert "1 video(s) and search(es) harvests went without" in r2["result"], r2
        assert "1 video(s)" in call(admin + "/api/admin/actions/volunteers-details", {})[1]["result"]

        fake = TMP / "fake-yt-dlp"
        renv = {**os.environ, "FAKE_VIDEO": str(TMP / "fake.mp4"), "FAKE_LOG": str(TMP / "fake2.log")}
        out = subprocess.run([sys.executable, str(SCRIPT), "--server", vbase, "--token", r["token"], "--agree", "--once",
                              "--yt-dlp", str(fake), "--home", str(TMP / "home2")], env=renv, capture_output=True, text=True, timeout=120)
        assert "sent" in out.stdout, out.stdout + out.stderr
        assert (data / "cache" / "videos" / "vvvvvvvvvvv.mp4").exists(), "in the cache, for the harvest"
        con = sqlite3.connect(data / "bouldermesh.sqlite")
        row = con.execute("SELECT uploader, license, height_cm FROM videos WHERE url='https://youtu.be/vvvvvvvvvvv'").fetchone()
        assert row == ("a climber", "YouTube standard licence (all rights reserved)", 180.0), row
        con.close()
        for _ in range(50):
            log_file = data / "projects" / "abcdef012345" / "job.log"
            if log_file.exists() and "volunteers fetched" in log_file.read_text():
                break
            time.sleep(0.2)
        assert "volunteers fetched" in log_file.read_text(), "harvested again with it"
        st = call(admin + "/api/admin/state")[1]["volunteers"]
        assert st["volunteers"][0]["name"] == "Dana" and st["volunteers"][0]["done"] == 1 and st["counts"]["queued_details"] == 1, st

        # a search a harvest asked for that never came: asked again, done by the script, and kept for the harvest
        con = sqlite3.connect(data / "bouldermesh.sqlite")
        con.execute("INSERT INTO projects(id,name,created,kind,status,harvest) VALUES('bcdef0123456','Relic 2',?, 'harvest','harvested',?)",
                    (time.time(), json.dumps({"images": 3, "videos": [], "asked": {"searches": ["Relic V9 Joshua Tree"], "videos": []}})))
        con.commit()
        con.close()
        (data / "projects" / "bcdef0123456" / "photos").mkdir(parents=True)
        assert "1 video(s) and search(es) harvests went without" in call(admin + "/api/admin/actions/volunteers-skipped", {})[1]["result"]
        out = subprocess.run([sys.executable, str(SCRIPT), "--once", "--yt-dlp", str(fake), "--home", str(TMP / "home2")], env=renv,
                             capture_output=True, text=True, timeout=120)
        assert "sent 1 result(s)" in out.stdout, out.stdout + out.stderr
        assert len(list((data / "cache" / "youtube-searches").glob("*.json"))) == 1
        for _ in range(50):
            log_file = data / "projects" / "bcdef0123456" / "job.log"
            if log_file.exists() and "volunteers fetched" in log_file.read_text():
                break
            time.sleep(0.2)
        assert "volunteers fetched" in log_file.read_text(), "harvested again with what it found"
        call(admin + "/api/admin/actions/volunteer-revoke", {"id": st["volunteers"][0]["id"]})
        assert call(vbase + "/volunteer/api/work", {"version": 1}, r["token"])[0] == 403

        # one registering itself: waiting, approved on the admin page, and the admin page's limits
        out = subprocess.run([sys.executable, str(SCRIPT), "--server", vbase, "--agree", "--once", "--name", "Sam", "--yt-dlp", str(fake),
                              "--home", str(TMP / "home4")], env=renv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert "as 'Sam'" in out.stdout and "at most 3 an hour and 10 a day" in out.stdout, out.stdout + out.stderr
        st = call(admin + "/api/admin/state")[1]
        sam = next(v for v in st["volunteers"]["volunteers"] if v["name"] == "Sam")
        assert sam["provisional"] and (sam["per_hour"], sam["per_day"]) == (3, 10) and st["notify"]["enabled"] is False, sam
        assert call(admin + "/api/admin/settings", {"volunteers.provisional_per_hour": 99})[0] == 400
        assert call(admin + "/api/admin/settings", {"volunteers.provisional_work": False, "volunteers.per_hour": 7})[0] == 200
        sam = next(v for v in call(admin + "/api/admin/state")[1]["volunteers"]["volunteers"] if v["name"] == "Sam")
        assert sam["status"] == "awaiting approval", sam
        assert "approved Sam" in call(admin + "/api/admin/actions/volunteer-approve", {"id": sam["id"]})[1]["result"]
        sam = next(v for v in call(admin + "/api/admin/state")[1]["volunteers"]["volunteers"] if v["name"] == "Sam")
        assert not sam["provisional"] and sam["per_hour"] == 7, sam
        assert call(admin + "/api/admin/actions/volunteer-approve", {"id": sam["id"]})[0] == 200, "again: no harm"
        assert call(admin + "/api/admin/actions/volunteer-approve", {"id": "nope"})[0] == 400
        assert call(admin + "/api/admin/settings", {"notify.server": "http://ntfy.example"})[0] == 400, "https only"
        r3 = call(admin + "/api/admin/actions/notify-topics", {})[1]
        nt = call(admin + "/api/admin/state")[1]
        assert "new topics" in r3["result"] and nt["settings"]["notify.enabled"] and nt["notify"]["subscribe"].startswith("https://ntfy.sh/bm-"), nt["notify"]
        assert call(admin + "/api/admin/settings", {"notify.enabled": False})[0] == 200
        assert call(admin + "/api/admin/settings", {"youtube.via": "nowhere"})[0] == 400
    finally:
        proc.terminate()
        out = proc.communicate(timeout=30)[0]
    assert "volunteers: served on" in out and "Traceback" not in out, out[-3000:]
    print("through the server OK")


def tools_python():
    """Which Python the script's venv for yt-dlp and Deno is made with, with the commands stood in for: this one if it's
    new enough, else a newer one on the PATH, else one uv fetches (installed from PyPI first, if it isn't there)."""
    sys.path.insert(0, str(SCRIPT.parent))
    import bouldermesh_volunteer as bv
    real = (bv.subprocess.run, bv.shutil.which, bv.python_version, bv.sys.executable)
    ran, versions, on_path = [], {}, {}

    def run(cmd, **kw):
        ran.append([str(c) for c in cmd])
        if cmd[1:3] == ["-m", "venv"] or cmd[1] == "venv":     # a venv: its python, of the Python that made it
            made = Path(cmd[-1]) / "bin" / "python"
            made.parent.mkdir(parents=True, exist_ok=True)
            versions[str(made)] = versions.get(str(cmd[0]), (3, 12))
        if "yt-dlp[default]" in cmd:
            (Path(cmd[0]).parent / "yt-dlp").touch()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    bv.subprocess.run, bv.shutil.which = run, lambda name: on_path.get(name)
    bv.python_version = lambda exe: versions.get(str(exe), (0, 0))
    try:
        def tools(home, here, path=None):
            ran.clear()
            versions.clear()
            on_path.clear()
            on_path.update(path or {})
            bv.sys.executable = "/usr/bin/python-here"
            versions["/usr/bin/python-here"] = here
            for name, exe in (path or {}).items():
                versions[exe] = (3, int(name.split(".")[1])) if "python" in name else (0, 0)
            args = type("Args", (), {"home": str(TMP / home), "yt_dlp": None, "server": "https://x.example", "token": "t",
                                     "per_hour": 1, "per_day": 1})()
            v = bv.Volunteer(args)
            v.tools()
            return v, [" ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd)) for cmd in ran]

        _, cmds = tools("t1", (3, 12))
        assert cmds[0].startswith("python-here -m venv") and len(cmds) == 3, cmds
        _, cmds = tools("t2", (3, 8), {"python3.13": "/opt/py/python3.13"})
        assert cmds[0].startswith("python3.13 -m venv"), cmds
        _, cmds = tools("t3", (3, 8))
        assert [c.split()[:5] for c in cmds[:4]] == [["python-here", "-m", "venv", str(TMP / "t3" / "uv")],
                                                     ["python", "-m", "pip", "install", "-q"], ["python", "-m", "pip", "install", "-q"],
                                                     ["uv", "venv", "-q", "--seed", "--python"]], cmds
        assert "uv" in cmds[2] and "3.12" in cmds[3], cmds
        _, cmds = tools("t4", (3, 8), {"uv": "/home/me/.local/bin/uv"})
        assert cmds[0].startswith("uv venv"), "uv there already: not installed again"
        # a venv made before, with a Python too old for today's yt-dlp: made again
        old = TMP / "t5" / "venv" / "bin"
        old.mkdir(parents=True)
        (old / "yt-dlp").touch()
        versions_before = {str(old / "python"): (3, 8)}
        bv.python_version = lambda exe: {**versions, **versions_before}.get(str(exe), (0, 0))
        _, cmds = tools("t5", (3, 8), {"python3.11": "/usr/bin/python3.11"})
        assert cmds[0].startswith("python3.11 -m venv"), cmds
    finally:
        bv.subprocess.run, bv.shutil.which, bv.python_version, bv.sys.executable = real
    print("tools' Python OK")


# --- the script updating itself when the server serves a newer one -----------------------------------------------------
NEWER = """#!/usr/bin/env python3
VERSION = 99
import sys, pathlib
pathlib.Path(__file__).with_name("ran.json").write_text(__import__("json").dumps(sys.argv[1:]))
"""


def updating() -> None:
    import registration
    fake = TMP / "fake-yt-dlp-u"
    fake.write_text(FAKE_YTDLP)
    fake.chmod(0o755)
    newer = TMP / "served" / "bouldermesh_volunteer.py"
    newer.parent.mkdir()
    newer.write_text(NEWER)
    served = volunteers.SCRIPT
    volunteers.SCRIPT = newer       # what the server serves: a newer one
    b = volunteers.Broker(TMP / "bu" / "v.sqlite", TMP / "bu" / "files", lambda *a: None, lambda *a: None)
    port = free_port()
    httpd = volunteers.serve(b, "127.0.0.1", port, log=lambda line: None)
    base = f"http://127.0.0.1:{port}"
    token = b.add("tester")["token"]
    env = {**os.environ, "FAKE_LOG": str(TMP / "fake-u.log")}
    here = TMP / "run" / "bouldermesh_volunteer.py"     # a copy, outside a git checkout, as a volunteer has it
    here.parent.mkdir()
    shutil.copy2(SCRIPT, here)

    def run(*extra, timeout=60):
        return subprocess.run([sys.executable, str(here), "--server", base, "--token", token, "--agree", "--yt-dlp", str(fake),
                               "--home", str(TMP / "hu"), *extra], env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    try:
        # what the server tells a script asking for work: an update only to one that does them, and not to one as new
        assert registration.served_script(newer) == {"version": 99, "sha256": hashlib.sha256(newer.read_bytes()).hexdigest()}
        assert registration.script_news(newer, {"version": 3, "updates": True}) == {"update": registration.served_script(newer)}
        assert registration.script_news(newer, {"version": 2}) == {"newer": 99}, "one from before updates: told, and given work"
        assert registration.script_news(newer, {"version": 99, "updates": True}) == {}
        assert registration.script_news(TMP / "nonesuch.py", {"version": 1, "updates": True}) == {}
        # told not to, or only once: it says so, and carries on as it is
        for extra in (["--once"], ["--once", "--no-auto-update"]):
            r = run(*extra)
            assert r.returncode == 0 and "the server has a newer version of this script (99)" in r.stdout, r.stdout + r.stderr
            assert here.read_bytes() == SCRIPT.read_bytes()
        # between fetches, it replaces itself with the newer one, checked, and runs it with the same arguments
        r = run("--name", "Sam")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "updated itself from version 3 to 99" in r.stdout and here.read_text() == NEWER, r.stdout
        assert json.loads((here.parent / "ran.json").read_text()) == ["--server", base, "--token", token, "--agree", "--yt-dlp",
                                                                      str(fake), "--home", str(TMP / "hu"), "--name", "Sam"], \
            "the same arguments"
        assert (TMP / "hu" / "previous.py").read_bytes() == SCRIPT.read_bytes(), "the one before kept"
        assert oct(here.stat().st_mode & 0o777) == oct(SCRIPT.stat().st_mode & 0o777)
        # one that isn't what the server said: kept as it is, and it carries on, given work again
        shutil.copy2(SCRIPT, here)
        real = registration.served_script
        registration.served_script = lambda path: {**real(path), "sha256": "0" * 64}
        b.want("details", "aaaaaaaaaaa", "harvest", "p1")
        proc = subprocess.Popen([sys.executable, str(here), "--server", base, "--token", token, "--agree", "--yt-dlp", str(fake),
                                 "--home", str(TMP / "hu")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(300):
                if b.q("SELECT 1 FROM tasks WHERE state='done'"):
                    break
                time.sleep(0.1)
            assert b.q("SELECT 1 FROM tasks WHERE state='done'"), "given work after all"
        finally:
            proc.terminate()
            out = proc.communicate(timeout=30)[0]
            registration.served_script = real
        assert "wasn't what the server said it would be: carrying on with this one" in out and here.read_bytes() == SCRIPT.read_bytes(), out
        # in a git checkout, as the repository's own: never replaced, but told
        r = subprocess.run([sys.executable, str(SCRIPT), "--server", base, "--token", token, "--agree", "--once", "--yt-dlp", str(fake),
                            "--home", str(TMP / "hu")], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert "it doesn't update itself (it's in a git checkout" in r.stdout and "newer version of this script (99)" in r.stdout, r.stdout
        # a script too old to be served at all is told how to update, and does
        shutil.copy2(SCRIPT, here)
        volunteers.CLIENT_VERSION, old_min = 50, volunteers.CLIENT_VERSION
        r = run()
        volunteers.CLIENT_VERSION = old_min
        assert r.returncode == 0 and "updated itself from version 3 to 99" in r.stdout and here.read_text() == NEWER, r.stdout + r.stderr
    finally:
        volunteers.SCRIPT = served
        httpd.shutdown()
    print("updating itself OK")


try:
    broker_alone()
    tools_python()
    updating()
    if shutil.which("ffmpeg"):
        script_and_broker()
        registering()
        cache_and_harvest()
        through_the_server()
    else:
        print("ffmpeg not installed: skipping the script, the cache and the server")
    print("ALL VOLUNTEER TESTS PASSED")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
