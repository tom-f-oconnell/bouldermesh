"""What visitors' browsers and the server said of the site over a day, in a few lines: for a daily notification
(server.site_health_loop()) and `python cli.py site-health`.

From the report files server.py keeps in its data folder: mesh-views.jsonl (each mesh shown, given up on while
loading, or turned slowly), client-errors.jsonl (errors in the page, meshes that wouldn't load, the view's graphics
lost) and slow-requests.jsonl (the site's requests that took seconds). Each line has its time, in UTC.
"""
from __future__ import annotations

import collections
import json
import re
import time
from pathlib import Path

FILES = ("mesh-views.jsonl", "client-errors.jsonl", "slow-requests.jsonl")
# errors that aren't the page's: browser extensions and in-app browsers' own scripts, and the browser's word for an
# error in a script from elsewhere, which says nothing more
NOISE = re.compile(r"^Script error\.?$|window\.ethereum|__firefox__|WKWebView|ResizeObserver loop")
SLOW_REQUEST = 3.0      # seconds: a request the server took longer over is kept (server.note_slow_request())
SLOW_LOAD = 10          # seconds: a mesh slower than this to show is counted
SLOW_FRAMES_MS = 50     # a view whose median frame took longer (under 20 a second) moved badly


def read(data: Path, name: str) -> list[dict]:
    """A report file and the one before it (<name>.1), oldest first; lines that aren't JSON skipped."""
    out = []
    for f in (data / f"{name}.1", data / name):
        try:
            text = f.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def summary(views: list[dict], errors: list[dict], slow: list[dict], since: str, until: str | None = None) -> dict:
    """Counts of what happened from since to until (UTC times as the files write them, "2026-11-19T16:00:00Z"): views
    by visitors (not editors), how long meshes took, those given up on, views that moved badly, errors by message
    (new: not reported in the files before since), and slow requests by path."""
    within = lambda r: since <= r.get("time", "") and (until is None or r["time"] < until)  # noqa: E731
    vs = [v for v in views if within(v) and not v.get("editor")]
    shown = [v for v in vs if v.get("event", "shown") == "shown"]
    loads = [v["seconds"] for v in shown if isinstance(v.get("seconds"), (int, float))]
    firsts = [v["page_seconds"] for v in shown if isinstance(v.get("page_seconds"), (int, float))]
    lists = [v["list_seconds"] for v in shown if isinstance(v.get("list_seconds"), (int, float))]
    left = [v for v in vs if v.get("event") == "left"]
    frames = [v for v in vs if v.get("event") == "frames" and isinstance(v.get("frames"), dict)]
    jerky = [v for v in frames if (v["frames"].get("median_ms") or 0) > SLOW_FRAMES_MS]
    errs = [e for e in errors if within(e) and not NOISE.search(e.get("message") or "")]
    before = {(e.get("kind"), e.get("message")) for e in errors if e.get("time", "") < since}
    by_err = collections.Counter((e.get("kind"), e.get("message")) for e in errs)
    gone = [e for e in errs if e.get("kind") == "webgl-lost"]
    sl = [r for r in slow if within(r)]
    by_path = collections.defaultdict(list)
    for r in sl:
        by_path[f"{r.get('method', 'GET')} {r.get('path')}"].append(r.get("seconds") or 0)
    return {
        "views": len(shown), "meshes": len({v["mesh"] for v in shown}),
        "load": {"median": _pct(loads, .5), "p90": _pct(loads, .9), "slow": sum(x > SLOW_LOAD for x in loads),
                 "worst": max(((v["seconds"], v.get("name") or v["mesh"]) for v in shown if v.get("seconds") is not None), default=None)} if loads else None,
        "first": {"median": _pct(firsts, .5), "p90": _pct(firsts, .9)} if firsts else None,
        "list": {"median": _pct(lists, .5), "p90": _pct(lists, .9)} if lists else None,
        "left": {"n": len(left), "page": sum(v.get("left") == "page" for v in left),
                 "median": _pct([v.get("seconds") or 0 for v in left], .5)} if left else None,
        "frames": {"n": len(frames), "jerky": len(jerky),
                   "gpus": collections.Counter(v.get("gpu") or "?" for v in jerky).most_common(3)} if frames else None,
        "errors": {"n": len(errs), "editor": sum(bool(e.get("editor")) for e in errs), "lost_view": len(gone),
                   "top": [{"kind": k, "message": m, "n": n, "new": (k, m) not in before} for (k, m), n in by_err.most_common(6)]},
        "slow": {"n": len(sl), "top": sorted(((p, len(xs), max(xs)) for p, xs in by_path.items()), key=lambda t: -t[1])[:5]},
    }


def note(s: dict, hours: float = 24) -> tuple[str, str, bool] | None:
    """The summary as a notification: (title, text, whether anything in it wants looking at), or None if nothing at
    all happened. Kept under ntfy's 2000 characters."""
    errs, slow = s["errors"], s["slow"]
    if not s["views"] and not errs["n"] and not slow["n"] and not s["left"]:
        return None
    lines = [f"{s['views']} view(s) of {s['meshes']} mesh(es) in {hours:g} h."]
    if s["load"]:
        ld = s["load"]
        lines.append(f"Loading a mesh: median {ld['median']:g} s, 90% under {ld['p90']:g} s; {ld['slow']} over {SLOW_LOAD} s"
                     + (f", slowest {ld['worst'][1]} at {ld['worst'][0]:g} s." if ld["worst"] and ld["slow"] else "."))
    if s["first"]:
        lines.append(f"Page open to first mesh: median {s['first']['median']:g} s, 90% under {s['first']['p90']:g} s"
                     + (f" (the list of meshes: median {s['list']['median']:g} s)." if s["list"] else "."))
    if s["left"]:
        lf = s["left"]
        lines.append(f"Given up on while loading: {lf['n']} ({lf['page']} by leaving or hiding the page), median after {lf['median']:g} s.")
    if s["frames"] and s["frames"]["jerky"]:
        fr = s["frames"]
        lines.append(f"Moved badly (under {1000 // SLOW_FRAMES_MS} frames a second): {fr['jerky']} of {fr['n']} views; "
                     + ", ".join(f"{g[:50]} {n}" for g, n in fr["gpus"]) + ".")
    if errs["lost_view"]:
        lines.append(f"The 3D view lost to the graphics card (out of memory): {errs['lost_view']}.")
    if errs["n"]:
        lines.append(f"Errors: {errs['n']}" + (f" ({errs['editor']} editors')" if errs["editor"] else "") + ":")
        lines += [f"  {e['n']}x {'NEW ' if e['new'] else ''}{e['kind']}: {(e['message'] or '')[:110]}" for e in errs["top"]]
    if slow["n"]:
        lines.append(f"Requests over {SLOW_REQUEST:g} s: {slow['n']}: " + "; ".join(f"{p} {n}x (up to {m:g} s)" for p, n, m in slow["top"]))
    look = bool(any(e["new"] for e in errs["top"]) or slow["n"] or errs["lost_view"] or (s["load"] and s["load"]["slow"])
                or (s["frames"] and s["frames"]["jerky"]))
    title = f"BoulderMesh's {'day' if hours == 24 else f'last {hours:g} h'}: {s['views']} views, {errs['n']} errors" + (f", {slow['n']} slow requests" if slow["n"] else "")
    return title, "\n".join(lines)[:1990], look


def since_iso(hours: float, now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - hours * 3600))


def for_data(data: Path, hours: float = 24, now: float | None = None) -> tuple[str, str, bool] | None:
    """note() of the last `hours` from a data folder's report files."""
    views, errors, slow = (read(data, f) for f in FILES)
    return note(summary(views, errors, slow, since_iso(hours, now)), hours)
