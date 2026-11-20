#!/usr/bin/env python3
"""Downloads the hold datasets from Roboflow Universe, each as COCO JSON, into <out>/<name>/ (train/valid/test).

    venv/bin/python fetch.py /data/tom/holdmodel/data

The API key is read from ~/.config/bouldermesh/roboflow_key (never printed). An export not made yet is asked for and
waited on.
"""
import io
import json
import pathlib
import sys
import time
import urllib.request
import zipfile

# name: (workspace/project, version, format, licence); masks where the project has them, else boxes
DATASETS = {
    "pwals": ("pwals/bouldering-holds-9wavr", 4, "coco-segmentation", "CC BY 4.0"),
    "johns": ("johns-workspace-rfgwn/climbing-holds-hsnzv", 1, "coco", "Public Domain"),
    "blackcreed": ("blackcreed-xpgxh/climbing-holds-and-volumes", 14, "coco", "CC BY 4.0"),
}


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read())


def main(out: pathlib.Path) -> None:
    key = (pathlib.Path.home() / ".config/bouldermesh/roboflow_key").read_text().strip()
    for name, (project, version, fmt, licence) in DATASETS.items():
        dest = out / name
        if (dest / "train").exists():
            print(f"{name}: already here")
            continue
        url = f"https://api.roboflow.com/{project}/{version}/{fmt}?api_key={key}"
        for _ in range(60):
            d = get(url)
            link = (d.get("export") or {}).get("link")
            if link:
                break
            print(f"{name}: waiting for the export ({d.get('progress', '?')})")
            time.sleep(10)
        else:
            sys.exit(f"{name}: no export after 10 minutes")
        with urllib.request.urlopen(link, timeout=600) as r:
            zipfile.ZipFile(io.BytesIO(r.read())).extractall(dest)
        (dest / "SOURCE.txt").write_text(f"https://universe.roboflow.com/{project}/dataset/{version}\n{licence}\n")
        n = sum(len(json.loads(f.read_text())["images"]) for f in dest.glob("*/_annotations.coco.json"))
        print(f"{name}: {n} images ({licence})")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]))
