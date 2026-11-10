#!/usr/bin/env python3
"""Notifications through ntfy (backend/notify.py), against a stand-in for an ntfy server here: what's published, the
Approve and Deny buttons' signed replies, following the reply topic and acting on each reply once, batching many
registrations into a summary, and new topics turning old buttons off. Offline.

    python3 tests/test_notify.py
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import notify  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="bouldermesh-notify-"))
published: list[dict] = []
replies: list[dict] = []        # what the reply topic holds, as ntfy would send them
asked: list[str] = []           # the reply topic's requests, with their since


class Ntfy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        published.append(json.loads(body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        asked.append(u.path + "?" + u.query)
        since = parse_qs(u.query).get("since", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        lines = [{"event": "open"}, {"event": "keepalive"}]
        ids = [r["id"] for r in replies]
        lines += replies[ids.index(since) + 1:] if since in ids else [r for r in replies if not since.isdigit() or r["time"] > int(since)]
        for line in lines:
            self.wfile.write(json.dumps(line).encode() + b"\n")
            self.wfile.flush()
        time.sleep(0.3)     # then the connection ends, as after a dropped one, and the follower comes back


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


try:
    port = free_port()
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), Ntfy).serve_forever, daemon=True).start()
    conf = {"notify.enabled": False, "notify.server": f"http://127.0.0.1:{port}/", "notify.topic": "", "notify.reply_topic": ""}
    acted: list[tuple] = []
    n = notify.Notifier(lambda: conf, TMP / "notify.key", TMP / "notify.state",
                        lambda verb, kind, rid: acted.append((verb, kind, rid)) or f"{verb}d {rid}", lambda line: None)

    # off, or without topics: nothing goes
    assert not n.send("t", "m") and not published
    try:
        n.test()
        raise AssertionError("not on yet")
    except ValueError:
        pass
    conf.update(n.new_topics(), **{"notify.enabled": True})
    assert conf["notify.topic"].startswith("bm-") and conf["notify.topic"] != conf["notify.reply_topic"] and len(conf["notify.topic"]) > 30
    assert oct((TMP / "notify.key").stat().st_mode)[-3:] == "600"

    # signed replies: good until they expire, and nothing else
    body = n.signed("approve", "volunteer", "ab12cd34")
    assert n.verify(body) == ("approve", "volunteer", "ab12cd34")
    assert n.verify(body.replace("approve", "deny", 1)) is None, "what it does is signed"
    assert n.verify(body.replace("ab12cd34", "ab12cd35")) is None and n.verify(body[:-1] + "0") is None
    assert n.verify(body, now=time.time() + 8 * 86400) is None, "only for a week"
    assert n.verify("approve volunteer x") is None and n.verify("") is None

    # a registration's notification: Approve and Deny, which post their signed replies to the reply topic
    n.registered("volunteer", {"id": "ab12cd34", "name": "Jane-Q-Public"}, " (script 2)")
    for _ in range(50):
        if published:
            break
        time.sleep(0.05)
    msg = published[0]
    assert msg["topic"] == conf["notify.topic"] and msg["title"] == "New volunteer: Jane-Q-Public" and "(script 2)" in msg["message"]
    assert [a["label"] for a in msg["actions"]] == ["Approve", "Deny"]
    for a, verb in zip(msg["actions"], ("approve", "deny")):
        assert a["action"] == "http" and a["method"] == "POST" and a["url"] == f"http://127.0.0.1:{port}/{conf['notify.reply_topic']}"
        assert n.verify(a["body"]) == (verb, "volunteer", "ab12cd34")
    assert "ab12cd34" not in json.dumps(msg).replace(msg["actions"][0]["body"], "").replace(msg["actions"][1]["body"], ""), \
        "an id only in the signed replies"

    # following the reply topic: each signed reply acted on once, others not, and the last one read kept
    now = int(time.time())
    replies += [{"id": "r1", "time": now + 1, "event": "message", "message": msg["actions"][0]["body"]},
                {"id": "r2", "time": now + 2, "event": "message", "message": "approve volunteer ab12cd34 9999999999 forged"}]
    threading.Thread(target=n.follow, daemon=True).start()
    for _ in range(100):
        if acted and n.listening:
            break
        time.sleep(0.05)
    time.sleep(1.0)     # through a reconnection or two
    assert acted == [("approve", "volunteer", "ab12cd34")], acted
    assert n.reply(replies[0]) is None, "one sent again isn't acted on twice"
    assert (TMP / "notify.state").read_text() == "r2", "the last read, so a restart doesn't act on one again"
    assert asked[0].endswith(f"since={now}") or asked[0].split("since=")[1].isdigit(), asked[0]
    assert any(a.endswith("since=r2") for a in asked[1:]), asked
    assert any(p["title"] == "Done" and "approved ab12cd34" in p["message"] for p in published), "and says what it did"
    assert n.state()["listening"] and n.state()["subscribe"] == f"http://127.0.0.1:{port}/{conf['notify.topic']}"

    # the test, whose Reply button pings the server
    published.clear()
    assert "follows the replies" in n.test()
    ping = published[0]["actions"][0]["body"]
    assert n.reply({"id": "r3", "event": "message", "message": ping}) and acted[-1] == ("ping", "test", "0")

    # many registering at once: a few notifications with buttons, then one summary of the rest
    notify.BATCH = (3, 1.0)
    n2 = notify.Notifier(lambda: conf, TMP / "notify.key", TMP / "n2.state", lambda *a: "", lambda line: None)
    published.clear()
    for i in range(7):
        n2.registered("machine", {"id": f"m{i}", "name": f"box-{i}"})
    time.sleep(2.5)
    singles = [p for p in published if p["title"].startswith("New machine")]
    summary = [p for p in published if p["title"].endswith("more registered")]
    assert all(p in singles + summary or p["title"] == "Replies reach the server" for p in published), published
    assert len(singles) == 3 and len(summary) == 1, published
    assert summary[0]["title"] == "4 more registered" and "machine box-6" in summary[0]["message"], summary

    # new topics: buttons already sent stop working
    old = singles[0]["actions"][0]["body"]
    conf.update(n.new_topics())
    assert n.verify(old) is None and not (TMP / "notify.state").exists()

    # the admin page's settings take only an https server, and topics ntfy would take
    import settings
    s = settings.Settings(TMP / "s.sqlite", TMP)
    for bad in ({"notify.server": "http://ntfy.sh"}, {"notify.server": "ntfy.sh"}, {"notify.topic": "has spaces"},
                {"notify.reply_topic": "x" * 65}):
        try:
            s.update(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert s.update({"notify.server": "https://ntfy.example.org", "notify.topic": "bm-abc"})["notify.topic"] == "bm-abc"
    print("ALL NOTIFY TESTS PASSED")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
