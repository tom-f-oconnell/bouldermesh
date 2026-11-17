#!/usr/bin/env python3
"""Make (or show) the iPhone app's external TestFlight group, with its public link, through the App Store Connect API,
for when App Store Connect's own page doesn't offer one: and if Apple refuses, its reason, which the page doesn't give.

    python3 app/ci/testflight-group.py <issuer id>               the groups there are, and their links
    python3 app/ci/testflight-group.py <issuer id> --make        an external group "Testers", with a public link,
                                                                 and the latest build added (sent for beta review)

The API key is the one app/ci/ios-signing.sh keeps (~/.bouldermesh-ios-signing/AuthKey_<id>.p8; SIGNING_DIR changes
where); the issuer id is on App Store Connect's Users and Access, Integrations, App Store Connect API page. Needs PyJWT
and cryptography (pip install pyjwt cryptography).
"""
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

BUNDLE = "sh.boulderme.app"
API = "https://api.appstoreconnect.apple.com/v1"


def token(issuer: str) -> str:
    import jwt
    d = os.environ.get("SIGNING_DIR") or os.path.expanduser("~/.bouldermesh-ios-signing")
    keys = sorted(glob.glob(os.path.join(d, "AuthKey_*.p8")))
    if not keys:
        sys.exit(f"no AuthKey_*.p8 in {d}")
    kid = os.path.basename(keys[-1])[len("AuthKey_"):-len(".p8")]
    now = int(time.time())
    return jwt.encode({"iss": issuer, "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
                      open(keys[-1]).read(), algorithm="ES256", headers={"kid": kid, "typ": "JWT"})


def call(tok: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        got = e.read().decode(errors="replace")
        try:
            errs = json.loads(got).get("errors") or []
            said = "; ".join(f"{x.get('title')}: {x.get('detail')}" for x in errs) or got
        except ValueError:
            said = got
        sys.exit(f"App Store Connect refused {method} {path} ({e.code}): {said}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tok = token(sys.argv[1])
    apps = call(tok, "GET", f"/apps?filter[bundleId]={BUNDLE}")["data"]
    if not apps:
        sys.exit(f"no app with bundle id {BUNDLE} on this account")
    app = apps[0]["id"]
    groups = call(tok, "GET", f"/apps/{app}/betaGroups?limit=50")["data"]
    for g in groups:
        a = g["attributes"]
        print(f"{'internal' if a.get('isInternalGroup') else 'external'} group {a.get('name')!r}: "
              f"public link {a.get('publicLink') if a.get('publicLinkEnabled') else 'off'}")
    if "--make" not in sys.argv:
        return
    ext = next((g for g in groups if not g["attributes"].get("isInternalGroup")), None)
    if ext is None:
        ext = call(tok, "POST", "/betaGroups", {"data": {"type": "betaGroups", "attributes": {
            "name": "Testers", "isInternalGroup": False, "publicLinkEnabled": True, "publicLinkLimitEnabled": False,
            "feedbackEnabled": True},
            "relationships": {"app": {"data": {"type": "apps", "id": app}}}}})["data"]
        print(f"made the external group 'Testers'")
    builds = call(tok, "GET", f"/builds?filter[app]={app}&sort=-uploadedDate&limit=1")["data"]
    if builds:
        call(tok, "POST", f"/betaGroups/{ext['id']}/relationships/builds", {"data": [{"type": "builds", "id": builds[0]["id"]}]})
        print(f"build {builds[0]['attributes'].get('version')} added to it")
        try:
            call(tok, "POST", "/betaAppReviewSubmissions", {"data": {"type": "betaAppReviewSubmissions",
                 "relationships": {"build": {"data": {"type": "builds", "id": builds[0]["id"]}}}}})
            print("sent for beta review (usually within a day)")
        except SystemExit as e:
            print(f"not sent for beta review: {e}")
    ext = call(tok, "GET", f"/betaGroups/{ext['id']}")["data"]["attributes"]
    print(f"public link: {ext.get('publicLink') or '(App Store Connect makes it once the group has a build)'}: put it in the "
          "admin page's 'The iPhone app's TestFlight public link'")


if __name__ == "__main__":
    main()
