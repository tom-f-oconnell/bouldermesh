#!/usr/bin/env python3
"""One reconstruction on a compute node (compute.py): a job directory's photos/ and masks/ in, its
out/ (the mesh, its textures, cameras.json, orientation.json) and new masks out.

usage: remote_job.py <job dir> [--internet] [--gpu]

Prints the pipeline's log, with its progress as "@@progress <step> <fraction> <detail as JSON>" lines
and the result as "@@result <JSON>", which the server reads back.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402
from progress import Tracker  # noqa: E402


class PrintedTracker(Tracker):
    """A tracker that prints its progress for the server to follow."""

    def update(self, key: str, frac: float = 0.0, detail: str | None = None) -> None:
        super().update(key, frac, detail)
        print(f"@@progress {key} {frac:.4f} {json.dumps(detail) if detail is not None else ''}".rstrip(), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("--internet", action="store_true")
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args()
    log = lambda line: print(line, flush=True)  # noqa: E731
    try:
        mesh, demo = pipeline.reconstruct(a.job / "photos", a.job / "work", a.job / "out", log, a.gpu, internet=a.internet,
                                          tracker=PrintedTracker(pipeline.steps()))
    except Exception as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    print("@@result " + json.dumps({"mesh": mesh.name, "demo": demo}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
