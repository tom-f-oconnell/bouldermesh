"""Accounts: invites, tokens, roles, the terms, pairing a second device, and what an account's token may change on a
running server (its own sets of beta, and nothing else).

    OMP_NUM_THREADS=4 venv/bin/python tests/test_accounts.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import accounts  # noqa: E402
import beta  # noqa: E402
from test_public import AUTH, Server, req  # noqa: E402


def refused(call, *args, **kw) -> str:
    try:
        call(*args, **kw)
    except accounts.Refused as e:
        return str(e)
    raise AssertionError("wasn't refused")


# --- the module ----------------------------------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    a = accounts.Accounts(Path(tmp) / "accounts.sqlite")
    inv = a.invite(note="for Ana", now=1000.0)
    assert len(inv["code"]) >= 20 and a.invites(now=1001.0)[0]["note"] == "for Ana" and "code" not in a.invites(now=1001.0)[0]
    assert "at least two" in refused(a.join, inv["code"], " ", now=1001.0) and "isn't good" in refused(a.join, "nope", "Ana", now=1001.0)
    ana = a.join(inv["code"], "  Ana   B ", "a browser", now=1002.0)
    assert ana["user"]["name"] == "Ana B" and ana["user"]["role"] == "user" and not ana["user"]["accepted"] and a.invites(now=1003.0) == []
    assert "isn't good" in refused(a.join, inv["code"], "Someone else", now=1003.0), "an invite works once"
    late = a.invite(now=1000.0, days=1)
    assert "isn't good" in refused(a.join, late["code"], "Late", now=1000.0 + 86401), "and not after it runs out"
    assert "taken" in refused(a.join, a.invite(now=1000.0)["code"], "ana b", now=1004.0)
    assert a.user_for(ana["token"])["id"] == ana["user"]["id"] and a.user_for("x" + ana["token"]) is None and a.user_for(None) is None
    # the terms: this version or none
    assert "changed" in refused(a.accept, ana["user"]["id"], accounts.TERMS_VERSION + 1)
    assert a.accept(ana["user"]["id"], accounts.TERMS_VERSION)["accepted"] and a.user_for(ana["token"])["accepted"]
    # another device: a code good once, for ten minutes
    code = a.pair_code(ana["user"]["id"], now=2000.0)["code"]
    assert "isn't good" in refused(a.take_pair, code, now=2000.0 + accounts.PAIR_SECONDS + 1)
    code = a.pair_code(ana["user"]["id"], now=3000.0)["code"]
    phone = a.take_pair(code, "a phone", now=3001.0)
    assert phone["user"]["id"] == ana["user"]["id"] and phone["token"] != ana["token"] and "isn't good" in refused(a.take_pair, code, now=3002.0)
    assert a.users()[0]["devices"] == 2
    # signing out one device, then all; a closed account's tokens are no use
    assert a.sign_out(ana["user"]["id"], phone["token"]) == 1 and a.user_for(phone["token"]) is None and a.user_for(ana["token"])
    mod = a.join(a.invite("moderator", ["c1"], now=1000.0)["code"], "Mo", now=1005.0)["user"]
    assert accounts.may_moderate(mod, "c1") and not accounts.may_moderate(mod, "c2") and not accounts.may_moderate(a.get(ana["user"]["id"]), "c1")
    assert accounts.may_moderate(a.change(mod["id"], {"areas": ["*"]}), "c2") and accounts.may_moderate({"role": "owner", "areas": []}, None)
    assert a.change(ana["user"]["id"], {"quota_mb": 500})["quota_mb"] == 500 and "megabytes" in refused(a.change, ana["user"]["id"], {"quota_mb": -1})
    assert "role" in refused(a.change, ana["user"]["id"], {"role": "king"}) and "moderator" in refused(a.invite, "owner")
    a.change(ana["user"]["id"], {"disabled": True})
    assert a.user_for(ana["token"]) is None and a.get(ana["user"]["id"])["disabled"]
    assert accounts.RANK["owner"] < accounts.RANK["moderator"] < accounts.RANK["user"] and "CC BY 4.0" in accounts.TERMS
print("accounts OK")

# --- a set of beta's owner, and who sees it --------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    site = beta.new_set(d, name="Main")
    mine = [beta.new_set(d, owner="u1", author="Ana") for _ in range(beta.SETS_PER_USER)]
    assert site["owner"] is None and site["shared"] and mine[0]["owner"] == "u1" and mine[1]["name"] == "Beta 2"
    try:
        beta.new_set(d, owner="u1")
        raise AssertionError("an eleventh")
    except ValueError as e:
        assert "delete one first" in str(e)
    assert beta.new_set(d, owner="u2", shared=False)["shared"] is False, "another account has its own ten"
    seen = lambda *args, **kw: [s["name"] for s in beta.visible(beta.load_sets(d), *args, **kw)]       # noqa: E731
    assert len(seen()) == 11 and len(seen("u2")) == 12 and len(seen(every=True)) == 12
    try:
        beta.update_set(d, mine[0]["id"], {"holds": [{"p": [i, 0, 0], "role": "hand"} for i in range(beta.SET_HOLDS_MOST + 1)]})
        raise AssertionError("a hundred and one holds")
    except ValueError as e:
        assert "at most 100" in str(e)
    assert beta.update_set(d, mine[0]["id"], {"shared": False})["shared"] is False
print("sets' owners OK")

# --- on a running server ---------------------------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    mid = "ab" * 6
    (tmp / "data" / "meshes" / mid).mkdir(parents=True)
    srv = Server(tmp, EDIT_TOKEN="an-editing-token", READ_ONLY="auto", SITE_URL="https://rocks.example/")
    try:
        B, A = srv.url, f"http://127.0.0.1:{srv.admin_port}"
        admin = lambda name, body: req(A, "POST", f"/api/admin/actions/{name}", body)       # noqa: E731
        st, _, me = req(B, "GET", "/api/account")
        assert st == 200 and me["user"] is None and me["accounts"] and me["terms"]["version"] == accounts.TERMS_VERSION and me["limits"] == {"sets": 10, "holds": 100}
        st, _, made = admin("user-invite", {"note": "Ana"})
        assert st == 200 and made["invite"]["url"].startswith("https://rocks.example/?invite=") and made["users"]["invites"][0]["note"] == "Ana", made
        assert req(B, "POST", "/api/account/join", {"invite": "wrong", "name": "Ana"})[0] == 400
        st, _, ana = req(B, "POST", "/api/account/join", {"invite": made["invite"]["code"], "name": "Ana"})
        assert st == 201 and ana["user"]["name"] == "Ana" and ana["user"]["role"] == "user"
        ANA = {"Authorization": f"Bearer {ana['token']}"}
        assert req(B, "GET", "/api/account", headers=ANA)[2]["user"]["id"] == ana["user"]["id"] and req(B, "GET", "/api/account", headers=AUTH)[2]["owner"]
        # nothing before the terms are accepted; then its own sets of beta, and nothing else
        st, _, r = req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"name": "Mine"}, ANA)
        assert st == 403 and "terms" in r["error"], r
        assert req(B, "POST", "/api/account/terms", {"version": 99}, ANA)[0] == 400
        assert req(B, "POST", "/api/account/terms", {"version": accounts.TERMS_VERSION}, ANA)[2]["user"]["accepted"]
        st, _, mine = req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"name": "Mine", "author": "Somebody Else"}, ANA)
        assert st == 201 and mine["owner"] == ana["user"]["id"] and mine["author"] == "Ana" and mine["shared"], mine
        st, _, site = req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"name": "The site's"}, AUTH)
        assert st == 201 and site["owner"] is None
        hold = [{"p": [0, 1, 2], "role": "start"}]
        assert req(B, "PATCH", f"/api/meshes/{mid}/beta/sets/{mine['id']}", {"holds": hold, "author": "X"}, ANA)[2]["author"] == "Ana"
        assert req(B, "PATCH", f"/api/meshes/{mid}/beta/sets/{site['id']}", {"holds": hold}, ANA)[0] == 403
        assert req(B, "DELETE", f"/api/meshes/{mid}/beta/sets/{site['id']}", headers=ANA)[0] == 403
        for method, path in (("POST", f"/api/meshes/{mid}/holds"), ("POST", "/api/projects"), ("DELETE", f"/api/meshes/{mid}"), ("PATCH", f"/api/meshes/{mid}")):
            assert req(B, method, path, {"holds": []}, ANA)[0] == 401, f"{method} {path} is the owner's"
        assert req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"name": "x"})[0] == 401, "and nobody's without a token"
        # a set not shared: its owner's and the site's owner's to see; and not to copy by anyone else
        st, _, quiet = req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"name": "Quiet", "shared": False}, AUTH)
        names = lambda h=None: [s["name"] for s in req(B, "GET", f"/api/meshes/{mid}/beta", headers=h)[2]["sets"]]     # noqa: E731
        assert names() == ["Mine", "The site's"] == names(ANA) and names(AUTH) == ["Mine", "The site's", "Quiet"]
        assert req(B, "POST", f"/api/meshes/{mid}/beta/sets", {"from": {"set": quiet["id"]}}, ANA)[0] == 400
        assert req(B, "PATCH", f"/api/meshes/{mid}/beta/sets/{mine['id']}", {"shared": False}, ANA)[2]["shared"] is False
        assert names() == ["The site's"] and names(ANA) == ["Mine", "The site's"]
        # another device, by a code; signing out; an account closed from the admin page
        code = req(B, "POST", "/api/account/pair", {}, ANA)[2]["code"]
        st, _, phone = req(B, "POST", "/api/account/pair/take", {"code": code, "label": "a phone"})
        PHONE = {"Authorization": f"Bearer {phone['token']}"}
        assert st == 201 and req(B, "GET", "/api/account", headers=PHONE)[2]["user"]["name"] == "Ana"
        assert req(B, "POST", "/api/account/pair/take", {"code": code})[0] == 400
        assert req(B, "POST", "/api/account/signout", {}, PHONE)[0] == 200 and req(B, "GET", "/api/account", headers=PHONE)[2]["user"] is None
        st, _, state = admin("user-change", {"id": ana["user"]["id"], "role": "moderator", "areas": ["*"], "quota_mb": 4000})
        assert st == 200 and state["users"]["list"][0]["role"] == "moderator" and state["users"]["list"][0]["quota_mb"] == 4000
        admin("user-change", {"id": ana["user"]["id"], "disabled": True})
        assert req(B, "GET", "/api/account", headers=ANA)[2]["user"] is None and req(B, "DELETE", f"/api/meshes/{mid}/beta/sets/{mine['id']}", headers=ANA)[0] == 401
        # guessing invites is slowed
        codes = [req(B, "POST", "/api/account/join", {"invite": f"guess{i}", "name": "Guess"})[0] for i in range(12)]
        assert codes[-1] == 429 and 400 in codes, codes
    finally:
        out = srv.stop()
    assert "account made: Ana (user)" in out
print("the server's accounts OK")
print("ALL ACCOUNTS TESTS PASSED")
