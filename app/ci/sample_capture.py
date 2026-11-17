#!/usr/bin/env python3
"""A capture as the apps keep one on the phone, already sent, for trying their upload screens with no camera: photos for
each purpose a capture's photos have (a hold on a climb, rock on it, tying to the old photos, other rock, the ground, a
card), the scenery's frames, its record, points and log, and the plan it went by, from a test server
(tests/capture_fixture.py). Its upload names that server's project (capture_fixture.PROJECT), which the mesh came of.

    sample_capture.py <the test server> <folder to put it in>

Prints the capture's id; the capture is in <folder>/<id>.
"""
from __future__ import annotations

import json
import pathlib
import struct
import sys
import urllib.request

from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tests"))
from capture_fixture import MESH, PROJECT  # noqa: E402

CAPTURE = "cisample"


def picture(path: pathlib.Path, label: str, shade: int, size=(960, 540)) -> None:
    img = Image.new("RGB", size, (shade, 110, 255 - shade))
    d = ImageDraw.Draw(img)
    for x in range(0, size[0], 60):
        d.line([(x, 0), (x + 200, size[1])], fill=(200, 190, 170), width=6)
    d.text((30, 30), label, fill=(255, 255, 255))
    img.save(path, quality=80)


def main(server: str, out: pathlib.Path) -> str:
    plan_text = urllib.request.urlopen(f"{server}/api/capture/plan/{MESH}").read().decode()
    plan = json.loads(plan_text)
    targets = plan["targets"]
    pick = lambda f: next((t["id"] for t in targets if f(t)), None)      # noqa: E731
    wanted = [("hold", [pick(lambda t: t["kind"] == "hold")]),
              ("rock on its climb", [pick(lambda t: t["kind"] == "patch" and t["prio"] == 2)]),
              ("tying", [pick(lambda t: t["kind"] == "view")]),
              ("other rock", [pick(lambda t: t["kind"] in ("patch", "edge") and t["prio"] == 1)]),
              ("ground", [pick(lambda t: t["kind"] == "patch" and t["prio"] == 0)]),
              ("card", []), ("nothing new", [])]
    d = out / CAPTURE
    (d / "photos").mkdir(parents=True, exist_ok=True)
    (d / "scenery").mkdir(exist_ok=True)
    shots = []
    for i, (label, helped) in enumerate(wanted * 2, 1):
        name = f"cap_{CAPTURE}_{i:03d}.jpg"
        picture(d / "photos" / name, f"{i}: {label}", (i * 37) % 255)
        blob = {"v": 1, "app": "ci", "device": "a test", "cap": CAPTURE, "i": i, "t": 1.7e9 + i, "down": [0.0, 1.0, 0.0], "down_src": "turn",
                "heading": 120.0 + i, "gps": {"lat": 34.0001, "lng": -116.0002, "acc": 3.0, "age": 0.5},
                "q": {"blur_px": 0.4, "ok": True}}
        if label == "card":
            blob["ref"] = "id1-card"
        shots.append({"name": name, "blob": blob, "kept": True, "helped": [h for h in helped if h]})
    scenery = [f"scn_{CAPTURE}_{i:03d}.jpg" for i in (1, 2, 3)] + [f"pano_{CAPTURE}_{i:02d}.jpg" for i in (1, 2)]
    for i, name in enumerate(scenery):
        picture(d / "scenery" / name, name, 40 * i)
    record = {"v": 4, "id": CAPTURE, "app": "ci", "device": "a test", "mesh": MESH, "plan": {"key": plan.get("key", ""), "made": plan.get("made", 0), "guide": ""},
              "started": 1.7e9, "ended": 1.7e9 + 60, "mode": "plan", "shots": shots, "licence": "CC-BY-4.0", "credit": "CI"}
    (d / "capture.json").write_text(json.dumps(record))
    (d / "plan.json").write_text(plan_text)
    (d / "points.bin").write_bytes(b"BMP1" + struct.pack("<I", 2) + struct.pack("<fffH", 0, 1, 2, 1) + struct.pack("<fffH", 0.1, 1, 2, 1))
    (d / "capture-log.jsonl").write_text('{"t":0.0,"e":"start"}\n')
    names = [s["name"] for s in shots]
    (d / "upload.json").write_text(json.dumps({"name": "A Wall (capture)", "mesh": MESH, "project": PROJECT, "sent": names, "recordSent": True,
                                               "pointsSent": True, "logSent": True, "scenerySent": scenery[:3], "sceneryDone": False,
                                               "meshAsked": True, "done": False, "photos": len(names), "note": ""}))
    return CAPTURE


if __name__ == "__main__":
    print(main(sys.argv[1].rstrip("/"), pathlib.Path(sys.argv[2])))
