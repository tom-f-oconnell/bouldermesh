#!/usr/bin/env python3
"""Real app captures' frames through the server's placing, for the apps' LocateCheck (shared jvmTest): per mesh, 8
photos of one spell of tracking left out of its pack, the server's answers for them (locateapi.answer(), 4 at a time),
their world poses from the capture's record, and the true mesh-to-world fit from all of that spell's photos.

    venv/bin/python tests/locate_check_prep.py <out folder>
    BM_LOCATE_CHECK=<out>/29daeac05fdf:<out>/46240a79be12 ./gradlew :shared:jvmTest --tests '*LocateCheck' -i

Reads hal's copy of the server's data (the two gym captures in it); MESHES lists (mesh, project) pairs."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "backend"))
import json, struct, shutil, pathlib, base64, collections
import numpy as np, cv2
import locate, reconstruction, locateapi as la
from modelcheck import read_model
B = pathlib.Path("/data/tom/bouldermesh-backups/current/data")
OUT = pathlib.Path(sys.argv[1]); OUT.mkdir(exist_ok=True)

def qmat(q):
    w,x,y,z = q
    return np.array([[1-2*(y*y+z*z),2*(x*y-w*z),2*(x*z+w*y)],[2*(x*y+w*z),1-2*(x*x+z*z),2*(y*z-w*x)],[2*(x*z-w*y),2*(y*z+w*x),1-2*(x*x+y*y)]])

MESHES = (("29daeac05fdf", "11456db92134"), ("46240a79be12", "490d38b46c43"))

for mid, pid in MESHES:
    md, pdir = B/"meshes"/mid, B/"projects"/pid
    w = OUT/mid; shutil.rmtree(w, ignore_errors=True); (w/"mesh").mkdir(parents=True); (w/"frames").mkdir()
    shutil.copy(md/"reconstruction.tar.gz", w/"mesh"); reconstruction.unpack(w/"mesh", w/"rec")
    tvec = {}
    with open(w/"rec/model/images.bin","rb") as f:
        for _ in range(struct.unpack("<Q", f.read(8))[0]):
            v=struct.unpack("<I7dI", f.read(64)); name=b""
            while (c:=f.read(1))!=b"\0": name+=c
            f.seek(24*struct.unpack("<Q", f.read(8))[0],1); tvec[name.decode()]=np.array(v[5:8])
    cams,_,_ = read_model(w/"rec/model")
    shots = {s["name"]: s["blob"] for s in json.loads((pdir/"capture.json").read_text())["shots"] if s.get("blob", {}).get("ar")}
    spells = collections.Counter(shots[n]["ar"]["s"] for n in cams if n in shots)
    spell = spells.most_common(1)[0][0]
    names = sorted(n for n in cams if n in shots and shots[n]["ar"]["s"] == spell)
    # truth: mesh -> world from every photo of that spell (centres, Umeyama), and how well turns agree with it
    M = np.array([-cams[n]["R"].T @ tvec[n] for n in names]); Wc = np.array([shots[n]["ar"]["p"] for n in names])
    mm, mw = M.mean(0), Wc.mean(0); A, Bm = M-mm, Wc-mw
    U,S,Vt = np.linalg.svd(Bm.T@A); D=np.eye(3); D[2,2]=np.sign(np.linalg.det(U@Vt)); R=U@D@Vt
    s = (S*np.diag(D)).sum()/(A**2).sum(); t = mw - s*R@mm
    res = np.linalg.norm((s*(R@M.T)).T + t - Wc, axis=1)
    turn = [np.degrees(np.arccos(np.clip((np.trace(qmat(shots[n]["ar"]["q"]).T @ R @ cams[n]["R"].T)-1)/2,-1,1))) for n in names]
    print(f"{mid}: spell {spell}, {len(names)} of {len(cams)} photos; truth scale {s:.4f} m/unit, centres off {np.median(res)*100:.1f} cm median, "
          f"{res.max()*100:.1f} max; turns off {np.median(turn):.2f}° median, {max(turn):.2f}° max")
    held = names[1::max(1, len(names)//8)][:8]
    locate.build_pack(w/"mesh", [pdir/"photos"], "locate", exclude=set(held))
    pack = locate.load_pack(w/"mesh/locate.bin")
    frames = []
    for i, n in enumerate(held):
        shutil.copy(pdir/"photos"/n, w/"frames"/n)
        img = cv2.imread(str(pdir/"photos"/n), cv2.IMREAD_GRAYSCALE)
        k = shots[n]["k"]; assert (img.shape[1], img.shape[0]) == (k["w"], k["h"]), (img.shape, k)
        f = 1280 / max(img.shape)
        small = cv2.resize(img, (int(img.shape[1]*f), int(img.shape[0]*f)), interpolation=cv2.INTER_AREA)
        sx = small.shape[1] / k["w"]
        frames.append({"id": i, "jpeg_b64": base64.b64encode(cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()).decode(),
                       "lens": {"fx": k["fx"]*sx, "fy": k["fy"]*sx, "cx": k["cx"]*sx, "cy": k["cy"]*sx, "w": small.shape[1], "h": small.shape[0]}})
    got = {"located": [], "ms": 0}
    for b in range(0, len(frames), 4):
        one, code = la.answer(mid, {"frames": frames[b:b + 4]}, "t", type("P", (), {"get": lambda self, *a: pack})(), la.Limits(), locate.place)
        assert code == 200, one
        got["located"] += one["located"]; got["ms"] += one["ms"]
    print(f"  server: {len(got['located'])} of {len(held)} placed in {got['ms']} ms; inliers {[a['inliers'] for a in got['located']]}")
    json.dump({"mesh": mid, "answer": got, "scale_json": json.loads((md/"scale.json").read_text())["metres_per_unit"],
               "frames": [{"id": i, "file": n, "ar": shots[n]["ar"], "k": shots[n]["k"]} for i, n in enumerate(held)],
               "truth": {"s": s, "R": R.tolist(), "t": t.tolist(), "probe": mm.tolist()}}, open(w/"fixture.json", "w"), indent=1)
