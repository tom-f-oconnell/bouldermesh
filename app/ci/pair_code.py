#!/usr/bin/env python3
"""For CI: an account on a test server with accounts (EDIT_TOKEN set), made from an invite of its admin page's, with the
terms accepted, and a pairing code of that account's, printed: what the site's "Sign the app in" hands the app.

    pair_code.py <the server> <its admin page> <the account's name>
"""
import json
import sys
import urllib.request


def post(base: str, path: str, body: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json",
                                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


site, admin, name = sys.argv[1:4]
invite = post(admin, "/api/admin/actions/user-invite", {})["invite"]["code"]
token = post(site, "/api/account/join", {"invite": invite, "name": name})["token"]
post(site, "/api/account/terms", {"version": 1}, token)
print(post(site, "/api/account/pair", {}, token)["code"])
