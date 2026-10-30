#!/usr/bin/env python3
"""Command-line access to the climb adapters and photo harvester.

  python cli.py fetch  <url> [--no-children]          metadata for an MP / OpenBeta / Kaya URL -> JSON
  python cli.py search "<area name>" [--near LAT LNG --radius M]   OpenBeta search -> JSON
  python cli.py mp-csv <mp_area_id> [--type boulder]  Mountain Project route-finder CSV export -> JSON
  python cli.py harvest <url> <out_dir> [--sources climb,google,commons,flickr,youtube,ddg] [--no-filter]
                                                       download public photos + relevance + diversity reports
  python cli.py diversity <photos_dir>                 score an existing photo folder
  python cli.py relevance <photos_dir> [--anchors DIR] [--move]
                                                       which photos are substantially of the boulder? (report-only
                                                       unless --move; anchors default to climb page / video photos)

Environment: GOOGLE_CSE_KEY/GOOGLE_CSE_CX, FLICKR_KEY, HARVEST_DDG=1, DATA_DIR (cache location),
             BOULDERMESH_UA (please set a contact address), HTTP_MIN_INTERVAL (min. seconds between requests
             to one host; a longer robots.txt Crawl-delay wins).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
import climbs  # noqa: E402
from climbs import diversity, harvest, mountainproject, relevance  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("url"); f.add_argument("--no-children", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query", nargs="?", default="")
    s.add_argument("--near", nargs=2, type=float, metavar=("LAT", "LNG")); s.add_argument("--radius", type=int, default=2000)
    c = sub.add_parser("mp-csv"); c.add_argument("area_id"); c.add_argument("--type", default="boulder")
    h = sub.add_parser("harvest"); h.add_argument("url"); h.add_argument("out_dir")
    h.add_argument("--sources", default="climb,google,commons,flickr,youtube"); h.add_argument("--no-filter", action="store_true")
    d = sub.add_parser("diversity"); d.add_argument("photos_dir")
    r = sub.add_parser("relevance"); r.add_argument("photos_dir"); r.add_argument("--anchors"); r.add_argument("--move", action="store_true")
    a = ap.parse_args()

    if a.cmd == "fetch":
        recs = climbs.fetch_climb_url(a.url, include_children=not a.no_children)
        print(json.dumps([r.to_dict() for r in recs], indent=1))
    elif a.cmd == "search":
        lat, lng = (a.near or (None, None))
        print(json.dumps([r.to_dict() for r in climbs.search(a.query, lat, lng, a.radius)], indent=1))
    elif a.cmd == "mp-csv":
        print(json.dumps([r.to_dict() for r in mountainproject.export_area_csv(a.area_id, a.type)], indent=1))
    elif a.cmd == "harvest":
        recs = climbs.fetch_climb_url(a.url, include_children=False)
        out = Path(a.out_dir)
        summary = harvest.harvest(recs[0], out, print, set(a.sources.split(",")), relevance_filter=not a.no_filter)
        print(json.dumps(summary, indent=1))
        print(f"\nphotos in {out / 'photos'}; provenance in {out / 'sources.json'}")
    elif a.cmd == "diversity":
        print(json.dumps(diversity.assess(Path(a.photos_dir), print), indent=1))
    elif a.cmd == "relevance":
        d = Path(a.photos_dir)
        anchors = sorted(p for p in Path(a.anchors).iterdir() if p.suffix.lower() in IMAGE_EXT) if a.anchors else None
        photos = sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)
        print(json.dumps(relevance.filter_photos(photos + (anchors or []), d.parent / "rejected" if a.move else None, print,
                                                 anchors=anchors), indent=1))


if __name__ == "__main__":
    main()
