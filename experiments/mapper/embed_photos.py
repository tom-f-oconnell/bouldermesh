"""CLIP image embeddings of a boulder's photos, for the pair lists of variants.py's pairs<K> feature sets:

    embed_photos.py <photos> <out>

Run in the app's image (bench.py's "app" tools), with the off-topic filter's own CLIP model (backend/climbs/
relevance.py), on the GPU where there is one. Writes <out>/embed.npy, one unit-length row a photo (float32), and
<out>/names.json, their file names in that order.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/app/backend")
import torch  # noqa: E402
from climbs import relevance  # noqa: E402
from PIL import Image  # noqa: E402

photos, out = Path(sys.argv[1]), Path(sys.argv[2])
names = sorted(p.name for p in photos.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
model, prep, _, device = relevance._load_clip(print)
start, rows = time.time(), []
with torch.no_grad():
    for i in range(0, len(names), 32):
        batch = torch.stack([prep(Image.open(photos / n).convert("RGB")) for n in names[i:i + 32]]).to(device)
        e = model.encode_image(batch).float()
        rows.append((e / e.norm(dim=-1, keepdim=True)).cpu().numpy())
np.save(out / "embed.npy", np.concatenate(rows).astype(np.float32) if rows else np.zeros((0, 512), np.float32))
(out / "names.json").write_text(json.dumps(names))
print(f"BENCH_EMBED photos={len(names)} device={device} seconds={time.time() - start:.1f}", flush=True)
