"""Whether the proxy addresses deploy/Caddyfile believes are still Cloudflare's.

Behind Cloudflare, Caddy takes a visitor's address from CF-Connecting-IP only on connections from the ranges in its
trusted_proxies, which are Cloudflare's published ones, and the firewall in front lets only those reach ports 80 and
443. Cloudflare changes that list rarely. A range it adds that isn't in both has its visitors logged as Cloudflare, and
if the firewall misses it, gives them Cloudflare's 522 error; a range it drops stays trusted and open. So once a day the
server compares Cloudflare's list with the Caddyfile's, and the admin page says when they differ. The firewall can't be
seen from here: whoever updates the Caddyfile updates it too.

Only where CADDYFILE names the Caddyfile (docker-compose.public.yml mounts deploy/ for it), and it trusts some ranges.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

URL = "https://api.cloudflare.com/client/v4/ips"
EVERY = 24 * 3600       # seconds between checks
RETRY = 3600            # after a failed one


def _cidr(text: str) -> str:
    return str(ipaddress.ip_network(text, strict=False))


def trusted(caddyfile: str) -> list[str]:
    """The ranges a Caddyfile's `trusted_proxies static` lines name (private_ranges aside), as CIDRs."""
    out = []
    for line in caddyfile.splitlines():
        words = line.split("#", 1)[0].split()
        if words[:2] == ["trusted_proxies", "static"]:
            out += [_cidr(w) for w in words[2:] if w != "private_ranges"]
    return out


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "BoulderMesh"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def published(fetch: Callable[[str], bytes] = _get) -> list[str]:
    """Cloudflare's IPv4 and IPv6 ranges, from its API (which needs no account)."""
    body = json.loads(fetch(URL))
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare's API answered {body.get('errors')}")
    return [_cidr(c) for c in body["result"]["ipv4_cidrs"] + body["result"]["ipv6_cidrs"]]


class Watch:
    """Compares the two lists, now and once a day after (run()), and says in the log when the difference changes."""

    def __init__(self, caddyfile: Path, fetch: Callable[[str], bytes] = _get, log: Callable[[str], None] = print):
        self.caddyfile, self.fetch, self.log = caddyfile, fetch, log
        self.lock = threading.Lock()
        self.result = {"caddyfile": "/".join(caddyfile.parts[-2:]), "trusted": 0, "published": 0, "missing": [], "extra": [],
                       "checked": None, "error": None, "failing_since": None}

    def check(self) -> dict | None:
        try:
            mine = trusted(self.caddyfile.read_text())
            theirs = published(self.fetch) if mine else []
        except Exception as e:      # the last comparison stands, with when checking started failing
            with self.lock:
                self.result.update(error=f"{type(e).__name__}: {e}", failing_since=self.result["failing_since"] or time.time())
            return self.state()
        missing, extra = sorted(set(theirs) - set(mine)), sorted(set(mine) - set(theirs))
        with self.lock:
            changed = (missing, extra) != (self.result["missing"], self.result["extra"])
            self.result.update(trusted=len(mine), published=len(theirs), missing=missing, extra=extra,
                               checked=time.time(), error=None, failing_since=None)
        if changed and (missing or extra):
            self.log(f"cloudflare: its published ranges no longer match trusted_proxies in {self.caddyfile}: "
                     f"not trusted {', '.join(missing) or 'none'}; trusted but no longer Cloudflare's {', '.join(extra) or 'none'}. "
                     "Update the Caddyfile and the firewall (see the admin page)")
        elif changed:
            self.log(f"cloudflare: trusted_proxies in {self.caddyfile} matches Cloudflare's ranges again")
        return self.state()

    def state(self) -> dict | None:
        """What the admin page shows, or None if the Caddyfile trusts no ranges, so there's nothing to compare."""
        with self.lock:
            return dict(self.result) if self.result["trusted"] or self.result["error"] else None

    def run(self) -> None:
        while True:
            self.check()
            time.sleep(RETRY if self.result["error"] else EVERY)
