"""Step-by-step progress of a background job, for the UI's progress bars."""
from __future__ import annotations

import json
import time
from typing import Callable

from climbs.common import jobctl


class Tracker:
    """Named steps of one job, each with a 0-1 fraction that only ever goes up.

    Args:
        steps: (key, label, weight) per step, in order. Weights are rough relative durations,
            used only for the overall percentage.
        save: called with the JSON snapshot when something changes, at most every 0.5 s
            except when a step starts or ends.
    """

    def __init__(self, steps: list[tuple[str, str, float]], save: Callable[[str], None] | None = None):
        self.steps = [{"key": k, "label": l, "weight": w, "frac": 0.0, "detail": "", "state": "pending"}
                      for k, l, w in steps]
        self._save = save
        self._last = 0.0

    def _step(self, key: str) -> dict | None:
        return next((s for s in self.steps if s["key"] == key), None)

    def update(self, key: str, frac: float = 0.0, detail: str | None = None) -> None:
        """Report progress on a step, which also ends any step before it. Where the steps that
        don't run a tool get stopped: raises Cancelled if the job has been."""
        jobctl.check()
        s = self._step(key)
        if s is None:
            return
        changed = s["state"] != "running"
        for other in self.steps:
            if other is s:
                break
            if other["state"] in ("pending", "running"):
                self._end(other, "done")
        s["state"] = "running"
        s["frac"] = max(s["frac"], min(float(frac), 1.0))
        if detail is not None:
            s["detail"] = detail
        self._flush(changed)

    def callback(self, key: str) -> Callable[..., None]:
        """A progress(frac, detail) function for code that doesn't need to know about steps."""
        return lambda frac=0.0, detail=None: self.update(key, frac, detail)

    def finish(self, state: str = "done") -> None:
        """End the job: running steps get `state`; pending ones are done, or skipped if it failed."""
        for s in self.steps:
            if s["state"] == "running":
                self._end(s, state)
            elif s["state"] == "pending":
                self._end(s, "done" if state == "done" else "skipped")
        self._flush(True)

    def skip_rest(self) -> None:
        """Later steps won't run (e.g. no reconstruction after a poor harvest)."""
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

    def snapshot(self) -> dict:
        counted = [s for s in self.steps if s["state"] != "skipped"]
        total = sum(s["weight"] for s in counted) or 1
        return {"overall": round(100 * sum(s["weight"] * s["frac"] for s in counted) / total, 1),
                "steps": [{k: s[k] for k in ("key", "label", "frac", "detail", "state")} for s in self.steps]}

    def _flush(self, force: bool) -> None:
        if self._save and (force or time.time() - self._last >= 0.5):
            self._last = time.time()
            self._save(json.dumps(self.snapshot()))
