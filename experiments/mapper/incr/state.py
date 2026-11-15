"""What is kept of one boulder between adds, and the decisions that need no COLMAP (standard library only).

A state folder holds:
    manifest.json   photos (hash, when added, whether placed and how), the history of adds, the last full solve and mesh
    journal.json    an add in progress: there only between begin() and commit()
    photos/         the photos, by name; masks/ their people masks, if given (<name>.png)
    database.db     COLMAP's features and verified matches for every photo
    model/          the sparse model, in the frame the first build set; model.prev/ the one before the last add
    cache/          VGGT's cameras by request
    dense/, mesh/   the undistorted photos and depth maps, and the mesh made from them
    log/add-NNNN/   each step's log, record and machine samples

An add only becomes the state when commit() swaps the new model in and rewrites the manifest (both renames), so one
that dies midway leaves the last committed state usable. Its photos, features and matches stay (features and matches
are only ever added to), and the next add takes them up again.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

VERSION = 1
PHOTO_TYPES = (".jpg", ".jpeg", ".png")

# The gates' thresholds. None is tuned yet: they are first guesses, reported with every decision so that they can be.
SETTINGS = {
    # a full re-solve, in place of adding to the model: the placed photos have grown by this share since the last one,
    "resolve_growth": 0.5,
    # or under this share of at least resolve_min_tried new photos that are linked to placed ones could be placed
    "resolve_min_placed_share": 0.5,
    "resolve_min_tried": 6,
    # or this many photos (and this share of those placed) are joined to the model by verified matches but left out
    "resolve_min_joined": 3,
    "resolve_joined_share": 0.1,
    # a new mesh: the add made at least this share more 3D points, or the placed photos have grown by this share
    # since the last mesh (more views of the same rock still sharpen it)
    "remesh_new_points": 0.03,
    "remesh_growth": 0.25,
    # with the mesh measured (core.surface_gain): new points off the mesh, as a share of the model's points; or the
    # share of the mesh's area covered as backend/captureplan.py's NEED asks, up by this much
    "remesh_off_mesh": 0.02,
    "remesh_coverage": 0.05,
    # A model's check of itself (model_check(); the numbers are core.self_check()'s). From the four benchmark boulders:
    # every COLMAP model's points are 0.94-1.71 px off in the mean (90th percentile 1.75-2.5) and this pipeline's
    # 0.66-0.77 (1.6-1.7), where the three its own VGGT assist had spoilt were at 2.45-5.7 (3.1-15);
    "check_err_mean": 2.0,
    "check_err_p90": 3.0,
    # cameras are 1.3-3.0 degrees (median) from their pairs' two-view geometry in the models other methods agree
    # with (31de, Drifter; this pipeline's e6de), and 11-16 in every method's model of 9f3b, which no two methods
    # agree on (the baseline's too: there this check fails every one of them, which is the truth of it);
    "check_pair_med_deg": 5.0,
    "check_min_pairs": 20,          # fewer pairs than this say nothing
    # lenses that can't be real were 0-6% of a model's in those, and 5-21% on 9f3b: a fifth is past any of the good.
    "check_odd_lens_share": 0.2,
    "check_min_cameras": 10,
    # VGGT starts a model only if it has room for this share of the photos its skeleton is chosen from (those linked by
    # verified matches): with less, most photos are registered one by one against VGGT's few, which is the slow part
    # it was to spare, on a base no full solve has checked. Atlas's 12 GB took 42: of 72 linked on 31de (58%) the
    # model was looser than the global mapper's (rays within 1 degree of the baseline's for 65% of cameras against
    # 89%), of 9f3b's 150 it was the furthest of any method's from the rest; hal's 24 GB took all 72 on 31de and came
    # 0.12 degrees (median) from the baseline. Two thirds sits between; it is a first guess from three boulders.
    "vggt_min_share": 0.66,
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    """Written whole or not at all: to a file beside it, then renamed over it."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(value, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class State:
    """One boulder's state folder."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        for d in ("photos", "masks", "cache", "log"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.journal_path = self.root / "journal.json"
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text())
        else:
            self.manifest = {"version": VERSION, "settings": {}, "photos": {}, "adds": [], "solve": None, "mesh": None}

    # -- what it holds

    @property
    def photos(self) -> dict[str, dict]:
        return self.manifest["photos"]

    def placed(self) -> list[str]:
        return sorted(n for n, p in self.photos.items() if p.get("placed"))

    def unplaced(self) -> list[str]:
        return sorted(n for n, p in self.photos.items() if not p.get("placed"))

    def journal(self) -> dict | None:
        return json.loads(self.journal_path.read_text()) if self.journal_path.exists() else None

    def has_model(self) -> bool:
        return (self.root / "model" / "images.bin").exists()

    def next_id(self) -> int:
        return len(self.manifest["adds"])

    # -- an add

    def plan(self, paths: list[Path]) -> tuple[list[tuple[Path, str, str]], list[str]]:
        """Which of the photos offered are new to this boulder.

        Returns:
            ([(source, the name it will have here, its hash)], [names offered that it has already, by content]).
            A new photo whose name another photo here has gets its hash's start put before its type.
        """
        have = {p["sha"]: n for n, p in self.photos.items()}
        j = self.journal()
        if j:       # those of an add that died are here already, though not in the manifest yet
            have.update({p["sha"]: n for n, p in j["photos"].items()})
        names = set(self.photos) | set(j["photos"] if j else ())
        new, same = [], []
        for src in paths:
            src = Path(src)
            if src.suffix.lower() not in PHOTO_TYPES:
                continue
            h = sha256(src)
            if h in have:
                same.append(src.name)
                continue
            name = src.name
            if name in names:
                name = f"{src.stem}-{h[:8]}{src.suffix}"
            have[h] = name
            names.add(name)
            new.append((src, name, h))
        return new, same

    def begin(self, new: list[tuple[Path, str, str]], masks: Path | None = None) -> dict:
        """Starts an add: the new photos (and their masks, <source name>.png under masks) are copied in and the
        journal written. Photos of an add that died before commit() are part of this one. Returns the journal."""
        j = self.journal() or {"id": self.next_id(), "photos": {}, "started": time.time()}
        j["id"] = self.next_id()
        for left in ("model.new", "scenes.new", "dense.new"):       # what an add that died left half made
            shutil.rmtree(self.root / left, ignore_errors=True)
        for src, name, h in new:
            dst = self.root / "photos" / name
            if not dst.exists():
                tmp = dst.with_name(dst.name + ".part")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
            if masks and (masks / f"{src.name}.png").exists():
                shutil.copyfile(masks / f"{src.name}.png", self.root / "masks" / f"{name}.png")
            j["photos"][name] = {"sha": h}
        write_json(self.journal_path, j)
        return j

    def commit(self, report: dict) -> dict:
        """Makes an add the state: model.new (if the add made one) takes model's place, which is kept as model.prev,
        and the manifest gains the add's photos and its record.

        Args:
            report: {"kind": "first" | "add" | "resolve" | "none", "placed": {name: how}, "tried": [names], ...}:
                the photos placed by this add (new ones, and older ones that had been left out), and what else is
                to be kept of it.
        """
        j = self.journal() or {"id": self.next_id(), "photos": {}, "started": time.time()}
        k = self.next_id()
        new_model, model, prev = self.root / "model.new", self.root / "model", self.root / "model.prev"
        if (new_model / "images.bin").exists():
            shutil.rmtree(prev, ignore_errors=True)
            if model.exists():
                os.replace(model, prev)
            os.replace(new_model, model)
            if (self.root / "scenes.new").exists():     # the other models a full solve found
                shutil.rmtree(self.root / "scenes", ignore_errors=True)
                os.replace(self.root / "scenes.new", self.root / "scenes")
        m = self.manifest
        for name, rec in j["photos"].items():
            m["photos"].setdefault(name, {"sha": rec["sha"], "add": k, "placed": False, "tries": 0})
        if report.get("kind") in ("first", "resolve", "bootstrap"):      # a full solve decides afresh which photos are placed
            for p in m["photos"].values():
                p["placed"] = False
        for name in report.get("tried", []):
            if name in m["photos"]:
                m["photos"][name]["tries"] = m["photos"][name].get("tries", 0) + 1
        for name, how in report.get("placed", {}).items():
            p = m["photos"][name]
            if not p.get("placed") or "placed_add" not in p:
                p["placed_add"] = k
            p["placed"], p["by"] = True, how
        record = {"id": k, "started": j.get("started"), "ended": time.time(), "new": sorted(j["photos"]),
                  **{key: v for key, v in report.items() if key not in ("placed", "tried")},
                  "placed_new": sorted(report.get("placed", {})), "placed_total": len(self.placed())}
        m["adds"].append(record)
        if report.get("kind") in ("first", "resolve", "bootstrap"):
            m["solve"] = {"add": k, "placed": len(self.placed())}
        write_json(self.manifest_path, m)
        self.journal_path.unlink(missing_ok=True)
        return record

    def meshed(self, info: dict) -> None:
        """Records that the mesh is of the state as it is now."""
        self.manifest["mesh"] = {"add": self.next_id() - 1, "placed": len(self.placed()), **info}
        write_json(self.manifest_path, self.manifest)


def resolve_due(placed_before: int, placed_at_solve: int | None, tried_linked: int, placed_linked: int,
                left_joined: int = 0, placed_now: int = 0, settings: dict = SETTINGS) -> tuple[bool, str]:
    """Whether to solve everything again, not add to the model, and why. Before the add: placed_before photos placed,
    placed_at_solve of them at the last full solve. Of the photos the add tried, tried_linked share verified matches
    with placed ones, and placed_linked of those could be placed. left_joined: photos left out although verified
    matches now join them to the model (through photos of this add, often), of placed_now placed."""
    if placed_at_solve and placed_before + placed_linked > placed_at_solve * (1 + settings["resolve_growth"]):
        return True, (f"{placed_before + placed_linked} photos placed against {placed_at_solve} at the last full solve: "
                      f"more than {settings['resolve_growth']:.0%} grown")
    if tried_linked >= settings["resolve_min_tried"] and placed_linked < tried_linked * settings["resolve_min_placed_share"]:
        return True, (f"only {placed_linked} of {tried_linked} photos linked to placed ones could be placed "
                      f"(under {settings['resolve_min_placed_share']:.0%})")
    if left_joined >= max(settings["resolve_min_joined"], settings["resolve_joined_share"] * placed_now):
        return True, (f"{left_joined} photos joined to the model by verified matches are left out ({placed_now} placed): "
                      f"a full solve takes them together")
    return False, "the model takes the new photos as it is"


def model_check(n: dict, settings: dict = SETTINGS) -> dict:
    """Whether a model passes its own check, from core.self_check()'s numbers: its points' reprojection errors (mean
    and 90th percentile), its cameras against their photos' pairwise geometry (the median angle), and its lenses.

    Returns:
        {"ok", "failed": [which of "error", "pairs", "lenses"], "why": the numbers in words, what failed first}.
    """
    failed, said = [], []
    mean, p90 = n.get("err_mean"), n.get("err_p90")
    if mean is None:
        failed.append("error")
        said.append("it has no points")
    else:
        bad = mean > settings["check_err_mean"] or p90 > settings["check_err_p90"]
        if bad:
            failed.append("error")
        said.append(f"its points are {mean:.2f} px off (90th percentile {p90:.2f})"
                    + (f", over the {settings['check_err_mean']:g} ({settings['check_err_p90']:g}) a sound model stays under" if bad else ""))
    pairs, med = n.get("pairs") or 0, n.get("pair_med_deg")
    if pairs >= settings["check_min_pairs"] and med is not None:
        bad = med > settings["check_pair_med_deg"]
        if bad:
            failed.append("pairs")
        said.append(f"its cameras are {med:.1f} degrees (median) from their photos' pairwise geometry over {pairs} pairs"
                    + (f", over {settings['check_pair_med_deg']:g}" if bad else ""))
    else:
        said.append(f"only {pairs} pairs to hold its cameras against")
    placed, odd = n.get("placed") or 0, n.get("odd_lenses") or 0
    if placed >= settings["check_min_cameras"] and odd > settings["check_odd_lens_share"] * placed:
        failed.append("lenses")
        said.append(f"{odd} of its {placed} lenses can't be real")
    elif odd:
        said.append(f"{odd} odd lens{'es' if odd != 1 else ''}")
    order = {"error": 0, "pairs": 1, "lenses": 2}
    said.sort(key=lambda t: 0 if any(w in t for w in ("over the", ", over ", "can't be real", "no points")) else 1)
    return {"ok": not failed, "failed": sorted(failed, key=order.get), "why": "; ".join(said)}


def check_rank(check: dict) -> tuple:
    """For choosing between models of the same photos by their own checks (the larger the better): one that passes
    before one that doesn't; then the fewer checks failed; then cameras nearer their pairs' geometry; then more
    photos placed; then points less far off."""
    med = check.get("pair_med_deg")
    return (bool(check.get("ok")), -len(check.get("failed") or []), -(med if med is not None else 1e9), check.get("placed") or 0,
            -(check.get("err_mean") if check.get("err_mean") is not None else 1e9))


def vggt_start_worth(room: int, linked: int, settings: dict = SETTINGS) -> tuple[bool, str]:
    """Whether VGGT should start a model, by how many photos it has room for of those linked by verified matches."""
    if linked <= 0:
        return False, "no photos are linked by verified matches"
    if room >= linked or room >= settings["vggt_min_share"] * linked:
        return True, f"VGGT has room for {min(room, linked)} of the {linked} linked photos"
    return False, (f"VGGT has room for {room} of the {linked} linked photos ({room / linked:.0%}), under the "
                   f"{settings['vggt_min_share']:.0%} it starts a model from")


def remesh_due(mesh: dict | None, placed_now: int, points_before: int, points_new: int, gain: dict | None = None,
               settings: dict = SETTINGS) -> tuple[bool, str]:
    """Whether the add calls for a new mesh, and why: mesh is the manifest's record of the last one (None: there is
    none), points_before the model's 3D points before the add and points_new those the add made. gain, where the
    mesh could be measured (core.surface_gain: the app's capture-plan coverage of the mesh before and after, and the
    add's new points off the mesh), decides; without it, the count of new points does."""
    if placed_now == 0:
        return False, "no photo is placed"
    if not mesh:
        return True, "there is no mesh yet"
    if gain:
        off = gain["new_points_off_mesh"] / max(points_before, 1)
        if off >= settings["remesh_off_mesh"]:
            return True, f"{gain['new_points_off_mesh']} new 3D points off the mesh ({off:.1%} of the model's): rock it doesn't have"
        more = gain["covered_after"] - gain["covered_before"]
        if more >= settings["remesh_coverage"]:
            return True, (f"the share of the mesh covered as the capture plan asks went from {gain['covered_before']:.1%} to "
                          f"{gain['covered_after']:.1%}")
        return False, (f"coverage {gain['covered_before']:.1%} -> {gain['covered_after']:.1%} and {gain['new_points_off_mesh']} "
                       f"new points off the mesh: cameras only")
    if points_before and points_new >= points_before * settings["remesh_new_points"]:
        return True, f"{points_new} new 3D points, {points_new / points_before:.1%} more: the new photos show more surface"
    if placed_now >= mesh["placed"] * (1 + settings["remesh_growth"]):
        return True, f"{placed_now} photos placed against {mesh['placed']} in the mesh: more than {settings['remesh_growth']:.0%} grown"
    share = f"{points_new / points_before:.1%}" if points_before else "no"
    return False, f"{points_new} new 3D points ({share} more) and {placed_now - mesh['placed']} more photos than the mesh has: cameras only"
