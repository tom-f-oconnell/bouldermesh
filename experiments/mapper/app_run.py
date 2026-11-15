"""The app's own pipeline, once, inside an image of the app, called as its worker calls it for a mesh of photos from
the internet (backend/remote_job.py): app_run.py <photos> <work> <out> <records>

For bench.py app. Into <records>: steps.json, the pipeline's own tracker as its steps begin and end (each step's
"seconds" once it has ended), and metrics.json, the mesh's numbers (backend/meshmetrics.py). Works with the backend of
whichever image it is run in, so only what both the live image and newer ones have is used.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/backend")
import meshmetrics  # noqa: E402
import pipeline  # noqa: E402
from progress import Tracker  # noqa: E402

photos, work, out, records = (Path(a) for a in sys.argv[1:5])
tracker = Tracker(pipeline.steps(), save=(records / "steps.json").write_text)
began = time.time()
mesh, demo = pipeline.reconstruct(photos, work, out, lambda line: print(line, flush=True), True, internet=True, tracker=tracker)
tracker.finish()
print(f"BENCH_APP mesh={mesh.name} demo={demo} seconds={time.time() - began:.1f}", flush=True)
(records / "metrics.json").write_text(json.dumps(meshmetrics.compute(out)))
