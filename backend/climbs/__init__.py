"""Climb metadata adapters.

    from climbs import fetch_climb_url, search
    rec = fetch_climb_url("https://www.mountainproject.com/route/105835104/midnight-lightning")
    rec = fetch_climb_url("https://openbeta.io/climbs/<uuid>")
    rec = fetch_climb_url("https://kaya-app.kayaclimb.com/...")   # link-only record
    rec = fetch_climb_url("https://climbing-history.org/climb/826/burden-of-dreams")

Sources (see each module's docstring for what is and isn't accessible):
  openbeta        open-licensed GraphQL API; primary programmatic source
  mountainproject public HTML pages, parsed one URL at a time (cached, rate limited, robots-aware)
  kaya            no public API / server-rendered pages and ToS forbids scraping -> link + manual import only
  climbinghistory climbing-history.org's public pages, a climb at a time, and its lists of the hardest climbs (cached,
                  rate limited); its videos are the links in a climb's ascents' references
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import climbinghistory, kaya, mountainproject, openbeta
from .common import Climb, Media

ADAPTERS = {"openbeta": openbeta, "mountainproject": mountainproject, "kaya": kaya, "climbinghistory": climbinghistory}


def adapter_for_url(url: str):
    host = urlparse(url if "://" in url else "https://" + url.strip()).netloc.lower()     # a link pasted without its scheme too
    if "mountainproject.com" in host:
        return mountainproject
    if "openbeta.io" in host:
        return openbeta
    if "kayaclimb.com" in host:
        return kaya
    if host.removeprefix("www.") == "climbing-history.org":
        return climbinghistory
    return None


def fetch_climb_url(url: str, **kw) -> list[Climb]:
    """Fetch one page/entity and return the climbs it describes.

    A route/climb URL returns one record. An area/boulder URL returns a record per
    child climb (each tagged with the boulder/area name and coordinates) plus one
    record for the area itself (type 'area')."""
    ad = adapter_for_url(url)
    if ad is None:
        raise ValueError("unsupported URL; use a mountainproject.com, climbing-history.org, openbeta.io or kayaclimb.com link")
    return ad.fetch(url, **kw)


def search(query: str = "", lat: float | None = None, lng: float | None = None,
           radius_m: int = 2000, limit: int = 50) -> list[Climb]:
    """Name / proximity search. Only OpenBeta supports this programmatically."""
    return openbeta.search(query=query, lat=lat, lng=lng, radius_m=radius_m, limit=limit)


__all__ = ["Climb", "Media", "fetch_climb_url", "search", "adapter_for_url", "ADAPTERS"]
