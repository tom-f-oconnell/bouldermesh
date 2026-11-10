"""Notifications on the admin's phone through ntfy (ntfy.sh, or another ntfy server), with buttons that act on them.

ntfy passes a message published to a topic on to every phone subscribed to it; its app is free, for Android and iOS.
The admin page makes two topics with long random names: one this server publishes to, which the phone subscribes to,
and one for replies. A notification of a registration has Approve and Deny buttons, and tapping one has the phone post a
reply to the reply topic. This server follows that topic over a connection of its own, so nothing new has to reach it:
the admin page stays on its private port.

A reply is signed (HMAC-SHA256, with a key only this server has, in DATA_DIR/notify.key) over what it does and until
when, so one made up by anyone else who learns the reply topic is refused. Whoever can read the notifications' topic
could still copy a reply from them, so that topic's name is the secret: on ntfy.sh, anyone who knows a topic's name can
read it. Its own ntfy server, with accounts, would close that (design.md, "Approving from a phone").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

SIGNED_DAYS = 7         # how long a notification's buttons work
BATCH = (3, 600)        # at most this many notifications with buttons in this many seconds; the rest in one summary after
RECONNECT = (5, 300)    # seconds before following the reply topic again after it fails, doubling up to the second
USER_AGENT = "BoulderMesh"


class Notifier:
    """Sends notifications, and acts on the replies their buttons send.

    Args:
        settings: the admin page's settings now (notify.* among them).
        key_file: where the signing key is kept, made when it's first needed.
        state_file: the last reply read, so that a restart doesn't act on one twice.
        act: does what a reply asks, (verb, kind, id) -> what it did, as text: "approve" or "deny" a "volunteer" or a
            "machine" by its id, or "ping" (the test's button).
    """

    def __init__(self, settings: Callable[[], dict], key_file: Path, state_file: Path, act: Callable[[str, str, str], str],
                 log: Callable[[str], None] = print):
        self.settings, self.key_file, self.state_file, self.act, self.log = settings, key_file, state_file, act, log
        self.lock = threading.Lock()
        self.sent: list[float] = []         # when each notification with buttons went, within BATCH's span
        self.held: list[str] = []           # registrations kept for the summary
        self.summary_due: float | None = None
        self.last_error: str | None = None
        self.last_sent: float | None = None
        self.listening = False
        self.generation = 0                 # bumped when the topics change, so the follower starts again on the new one
        self.seen: list[str] = []           # the last replies acted on, should ntfy send one again

    # -- configuration ------------------------------------------------------------------------------------------------
    def conf(self) -> dict:
        s = self.settings()
        return {"enabled": bool(s.get("notify.enabled")) and bool(s.get("notify.topic")) and bool(s.get("notify.reply_topic")),
                "server": str(s.get("notify.server") or "https://ntfy.sh").rstrip("/"), "topic": s.get("notify.topic") or "",
                "reply_topic": s.get("notify.reply_topic") or ""}

    def new_topics(self) -> dict:
        """New topics (as settings to save), and a new signing key, so that buttons on earlier notifications stop working."""
        self.key_file.write_bytes(secrets.token_bytes(32))
        os.chmod(self.key_file, 0o600)
        self.state_file.unlink(missing_ok=True)     # the last reply read was on the old topic
        self.generation += 1
        return {"notify.topic": "bm-" + secrets.token_urlsafe(24).replace("_", "-"),
                "notify.reply_topic": "bm-" + secrets.token_urlsafe(24).replace("_", "-")}

    def reconfigured(self) -> None:
        self.generation += 1

    def key(self) -> bytes:
        try:
            return self.key_file.read_bytes()
        except FileNotFoundError:
            self.key_file.write_bytes(secrets.token_bytes(32))
            os.chmod(self.key_file, 0o600)
            return self.key_file.read_bytes()

    # -- signing ------------------------------------------------------------------------------------------------------
    def signed(self, verb: str, kind: str, rid: str, now: float | None = None) -> str:
        """A reply's body: what to do, until when, and the signature over both."""
        until = int((now or time.time()) + SIGNED_DAYS * 86400)
        msg = f"{verb} {kind} {rid} {until}"
        return f"{msg} {hmac.new(self.key(), msg.encode(), hashlib.sha256).hexdigest()}"

    def verify(self, body: str, now: float | None = None) -> tuple[str, str, str] | None:
        """(verb, kind, id) of a reply this server signed and that's still good, else None."""
        parts = (body or "").strip().split(" ")
        if len(parts) != 5 or not parts[3].isdigit():
            return None
        msg = " ".join(parts[:4])
        good = hmac.new(self.key(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, parts[4]) or int(parts[3]) < (now or time.time()):
            return None
        return parts[0], parts[1], parts[2]

    # -- sending ------------------------------------------------------------------------------------------------------
    def send(self, title: str, message: str, actions: list[dict] | None = None, priority: int = 3, tags=None) -> bool:
        """Publish a notification now; whether ntfy took it."""
        c = self.conf()
        if not c["enabled"]:
            return False
        body = {"topic": c["topic"], "title": title[:200], "message": message[:2000], "priority": priority}
        if tags:
            body["tags"] = tags
        if actions:
            body["actions"] = actions
        req = urllib.request.Request(c["server"], data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            self.last_error, self.last_sent = None, time.time()
            return True
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.last_error = f"{time.strftime('%Y-%m-%d %H:%M')}: couldn't publish to {c['server']}: {e}"
            self.log(f"notify: {self.last_error}")
            return False

    def buttons(self, kind: str, rid: str) -> list[dict]:
        c = self.conf()
        url = f"{c['server']}/{c['reply_topic']}"
        return [{"action": "http", "label": label, "url": url, "method": "POST", "body": self.signed(verb, kind, rid), "clear": True}
                for verb, label in (("approve", "Approve"), ("deny", "Deny"))]

    def registered(self, kind: str, row: dict, detail: str = "") -> None:
        """A client registered itself: a notification with Approve and Deny, or, past BATCH's few, a place in a summary
        sent once BATCH's span is over. In the background, so that registering doesn't wait for ntfy."""
        if not self.conf()["enabled"]:
            return
        now = time.time()
        with self.lock:
            self.sent = [t for t in self.sent if t > now - BATCH[1]]
            if len(self.sent) >= BATCH[0]:
                self.held.append(f"{kind} {row['name']}")
                if self.summary_due is None:
                    self.summary_due = self.sent[0] + BATCH[1]
                    threading.Thread(target=self._summary, daemon=True).start()
                return
            self.sent.append(now)
        threading.Thread(target=self.send, args=(f"New {kind}: {row['name']}", f"It registered itself{detail}, and waits for "
                                                 "your approval. Approve or Deny here, or on the admin page.",
                                                 self.buttons(kind, row["id"]), 4, ["bust_in_silhouette"]), daemon=True).start()

    def _summary(self) -> None:
        while True:
            with self.lock:
                due = self.summary_due or time.time()
            if time.time() >= due:
                break
            time.sleep(min(30, due - time.time()))
        with self.lock:
            names, self.held, self.summary_due = self.held, [], None
            self.sent = []
        if names:
            self.send(f"{len(names)} more registered", "More clients registered than to notify of one by one: "
                      + ", ".join(names[:20]) + (" and more" if len(names) > 20 else "") + ". Approve them on the admin page.",
                      priority=4, tags=["bust_in_silhouette"])

    def test(self) -> str:
        c = self.conf()
        if not c["enabled"]:
            raise ValueError("turn notifications on, with topics, first")
        ok = self.send("BoulderMesh: a test", "This phone gets this server's notifications. Tap Reply to check that its "
                       "buttons reach the server too: a second notification says so.",
                       [{"action": "http", "label": "Reply", "url": f"{c['server']}/{c['reply_topic']}", "method": "POST",
                         "body": self.signed("ping", "test", "0"), "clear": True}], tags=["white_check_mark"])
        if not ok:
            raise ValueError(self.last_error or "ntfy didn't take it")
        return (f"sent to {c['server']}/{c['topic']}" + ("; this server follows the replies" if self.listening else
                "; this server isn't following the replies yet, so its buttons won't work"))

    # -- following replies --------------------------------------------------------------------------------------------
    def follow(self) -> None:
        """Follow the reply topic for good (a thread of its own), acting on each signed reply once."""
        wait = RECONNECT[0]
        while True:
            c, generation = self.conf(), self.generation
            if not c["enabled"]:
                self.listening = False
                time.sleep(10)
                continue
            try:
                since = self.state_file.read_text().strip() or None
            except FileNotFoundError:
                since = None
            since = since or str(int(time.time()))      # a first start acts only on what comes after it
            url = f"{c['server']}/{c['reply_topic']}/json?since={since}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=90) as r:      # ntfy sends a keepalive every 45 s
                    self.listening, wait = True, RECONNECT[0]
                    for line in r:
                        if self.generation != generation:
                            break
                        self.reply(json.loads(line or b"{}"))
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.listening = False
                self.log(f"notify: following replies on {c['server']} failed ({e}); again in {wait} s")
                time.sleep(wait)
                wait = min(wait * 2, RECONNECT[1])

    def reply(self, event: dict) -> str | None:
        """Act on one event from the reply topic: what was done, or None for one that isn't a signed reply."""
        if event.get("event") != "message" or (event.get("id") and event["id"] in self.seen):
            return None
        if event.get("id"):
            self.state_file.write_text(str(event["id"]))
            self.seen = self.seen[-199:] + [event["id"]]
        what = self.verify(event.get("message") or "")
        if not what:
            self.log("notify: a reply without a good signature, ignored")
            return None
        verb, kind, rid = what
        try:
            done = self.act(verb, kind, rid)
        except Exception as e:
            done = f"couldn't {verb} it: {e}"
        self.log(f"notify: {verb} {kind} {rid} from a phone: {done}")
        threading.Thread(target=self.send, args=("Done" if verb != "ping" else "Replies reach the server", done),
                         kwargs={"priority": 2, "tags": ["ok"]}, daemon=True).start()
        return done

    def state(self) -> dict:
        c = self.conf()
        link = f"{c['server']}/{c['topic']}" if c["topic"] else None
        # the Android app subscribes from an ntfy:// link (docs.ntfy.sh, "Subscribe from your phone"); the https one opens
        # the topic's page, from which an iPhone's app can be given the topic
        app = f"ntfy://{urlparse(c['server']).netloc}/{c['topic']}?display=BoulderMesh" if c["topic"] else None
        return {"enabled": c["enabled"], "server": c["server"], "topic": c["topic"], "reply_topic": c["reply_topic"],
                "subscribe": link, "subscribe_app": app, "qr_link": qr_svg(link), "qr_app": qr_svg(app), "listening": self.listening,
                "last_error": self.last_error, "last_sent": self.last_sent, "waiting_summary": len(self.held)}


_qr: dict[str, str | None] = {}


def qr_svg(text: str | None) -> str | None:
    """A QR code of text, as SVG to put in a page, for a phone's camera; None without text, or without segno (pip)."""
    if not text:
        return None
    if text not in _qr:
        try:
            import segno
        except ImportError:
            return None
        _qr.clear()     # only the topics now are wanted
        _qr[text] = segno.make(text, error="m").svg_inline(scale=4, border=3, dark="#000", light="#fff")
    return _qr[text]
