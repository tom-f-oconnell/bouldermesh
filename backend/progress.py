"""Step-by-step progress of a background job, for the UI's progress bars."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from climbs.common import jobctl


class Tracker:
    """Named steps of one job, each with a 0-1 fraction that only ever goes up, and once begun, when it began
    ("started") and once ended, how long it ran ("seconds"), for timings.py.

    Args:
        steps: (key, label, weight) per step, in order, or (key, label, weight, True) for one that runs alongside
            the others, on a thread of its own: it neither ends the steps before it nor is ended by those after, but
            by end(). Weights are rough relative durations, used only for the overall percentage.
        save: called with the JSON snapshot when something changes, at most every 0.5 s
            except when a step starts or ends.
        where: where steps that start are run, for the UI (at()): "server", "machine" (one that takes work from the
            server), "waiting" (for a machine to take it), "volunteers", or "server+volunteers" (the server, with what
            volunteers fetch for it). None to leave it unsaid.
    """

    def __init__(self, steps: list[tuple], save: Callable[[str], None] | None = None, where: str | None = None):
        self.steps = [{"key": k, "label": l, "weight": w, "frac": 0.0, "detail": "", "state": "pending", "parallel": bool(rest and rest[0])}
                      for k, l, w, *rest in steps]
        self.where = where
        self._save = save
        self._last = 0.0
        self._lock = threading.RLock()     # steps alongside others report from their own threads

    def _step(self, key: str) -> dict | None:
        return next((s for s in self.steps if s["key"] == key), None)

    def update(self, key: str, frac: float = 0.0, detail: str | None = None) -> None:
        """Report progress on a step, which also ends any step before it. Where the steps that
        don't run a tool get stopped: raises Cancelled if the job has been."""
        jobctl.check()
        with self._lock:
            s = self._step(key)
            if s is None:
                return
            changed = s["state"] != "running"
            if changed and "started" not in s:
                s["started"] = time.time()
            if changed and "where" not in s and self.where:
                s["where"] = self.where
            for other in [] if s["parallel"] else self.steps:
                if other is s:
                    break
                if other["state"] in ("pending", "running") and not other["parallel"]:
                    self._end(other, "done")
            s["state"] = "running"
            s["frac"] = max(s["frac"], min(float(frac), 1.0))
            if detail is not None:
                s["detail"] = detail
            self._flush(changed)

    def end(self, key: str, state: str = "done", detail: str | None = None) -> None:
        """End a step, as one alongside the others is ended when its thread is done."""
        with self._lock:
            s = self._step(key)
            if s is None or s["state"] not in ("pending", "running"):
                return
            if detail is not None:
                s["detail"] = detail
            self._end(s, state)
            self._flush(True)

    def at(self, where: str | None, key: str | None = None) -> None:
        """Say where the work is done (as `where` in Tracker()): of the step with this key, now, or without one, of the
        steps that start from now on, as when meshing goes to a machine (the snapshot's own "where")."""
        with self._lock:
            s = self._step(key) if key is not None else None
            if key is None and self.where != where:
                self.where = where
            elif s is not None and s.get("where") != where:
                s["where"] = where
            else:
                return
            self._flush(True)

    def units(self, key: str, n: int) -> None:
        """Say how much the step with this key has to do, when that isn't its project's photos (a boulder's climbs to
        harvest): kept with its time (steptimes.py), to tell from it how long one like it takes."""
        with self._lock:
            s = self._step(key)
            if s is not None:
                s["units"] = n

    def callback(self, key: str) -> Callable[..., None]:
        """A progress(frac, detail) function for code that doesn't need to know about steps. Its .at(where) says where
        that step's work is done (at())."""
        def report(frac: float = 0.0, detail: str | None = None) -> None:
            self.update(key, frac, detail)
        report.at = lambda where: self.at(where, key)
        return report

    def finish(self, state: str = "done") -> None:
        """End the job: running steps get `state`; pending ones are done, or skipped if it failed."""
        with self._lock:
            for s in self.steps:
                if s["state"] == "running":
                    self._end(s, state)
                elif s["state"] == "pending":
                    self._end(s, "done" if state == "done" else "skipped")
            self._flush(True)

    def skip_rest(self) -> None:
        """Later steps won't run (e.g. no reconstruction after a poor harvest)."""
        with self._lock:
            for s in self.steps:
                if s["state"] == "running":
                    self._end(s, "done")
                elif s["state"] == "pending":
                    self._end(s, "skipped")
            self._flush(True)

    @staticmethod
    def _end(s: dict, state: str) -> None:
        s["state"] = state
        if state == "done":
            s["frac"] = 1.0
        if "started" in s:
            s["ended"] = time.time()

    def snapshot(self) -> dict:
        counted = [s for s in self.steps if s["state"] != "skipped"]
        total = sum(s["weight"] for s in counted) or 1
        return {"overall": round(100 * sum(s["weight"] * s["frac"] for s in counted) / total, 1),
                **({"where": self.where} if self.where else {}),
                "steps": [{**{k: s[k] for k in ("key", "label", "frac", "detail", "state")},
                           **({"parallel": True} if s["parallel"] else {}),
                           **({"where": s["where"]} if s.get("where") else {}),
                           **({"units": s["units"]} if "units" in s else {}),
                           **({"started": round(s["started"], 1)} if "started" in s else {}),
                           **({"seconds": round(s["ended"] - s["started"], 1)} if "ended" in s else {})} for s in self.steps]}

    def _flush(self, force: bool) -> None:
        if self._save and (force or time.time() - self._last >= 0.5):
            self._last = time.time()
            self._save(json.dumps(self.snapshot()))
