#!/usr/bin/env python3
"""Trains the apps' hold-finding model (approach B of gym climbs: app/shared Gym.kt): which pixels of a photo are a hold.

    venv/bin/python train.py /data/tom/holdmodel/data /data/tom/holdmodel/run [--small] [--epochs 40]

A semantic segmenter, torchvision's LR-ASPP on MobileNetV3 (BSD; its backbone's ImageNet weights downloaded once): each
pixel is background (0), a hold (1) or a volume (2). Targets are the datasets' masks where they have them (pwals, some of
johns), else the ellipse inside each box: holds are roughly round, and the apps only ask whether a patch of one colour
is mostly on a hold. Trained on crops of pictures made 320-1024 pixels on their longer side, as a phone's photo of a
wall from a few metres, made 640 (HoldModel.SIDE), shows holds no bigger than a close-up of a few holds does.
pwals' validation photos have true masks: hold IoU against them is what's reported and what picks the checkpoint.
"""
import argparse
import json
import math
import pathlib
import random
import time

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset
from torchvision.models import MobileNet_V3_Large_Weights, MobileNet_V3_Small_Weights
from torchvision.models.segmentation import lraspp_mobilenet_v3_large
from torchvision.models.segmentation.lraspp import LRASPP

MEAN = np.array([0.485, 0.456, 0.406], np.float32); STD = np.array([0.229, 0.224, 0.225], np.float32)
LONG_MAX = 800          # pictures are kept in memory this big on their longer side (4,400 of them: 6 GB)
CROP = 384
SCALES = (320, 1024)    # trained on them made this big on their longer side: their holds as big as a phone's photos show them
EVAL_SIDE = 640         # and checked at the apps' size (HoldModel.SIDE)


def is_volume(dataset: str, name: str) -> bool:
    return name == "volume" or (dataset == "blackcreed" and name == "1")


def load(root: pathlib.Path, split: str) -> list[tuple[np.ndarray, np.ndarray]]:
    """Every picture of a split of every dataset, made at most LONG_MAX on its longer side, with its target."""
    out = []
    for ds in sorted(p for p in root.iterdir() if p.is_dir()):
        f = ds / split / "_annotations.coco.json"
        if not f.exists():
            continue
        j = json.loads(f.read_text())
        cats = {c["id"]: c["name"] for c in j["categories"]}
        anns: dict[int, list] = {}
        for a in j["annotations"]:
            anns.setdefault(a["image_id"], []).append(a)
        for im in j["images"]:
            img = cv2.imread(str(ds / split / im["file_name"]))
            if img is None:
                continue
            k = min(1.0, LONG_MAX / max(img.shape[:2]))
            img = cv2.resize(img, (round(img.shape[1] * k), round(img.shape[0] * k)), interpolation=cv2.INTER_AREA)
            target = np.zeros(img.shape[:2], np.uint8)
            # volumes first, holds over them (a hold on a volume is a hold)
            for a in sorted(anns.get(im["id"], []), key=lambda a: not is_volume(ds.name, cats[a["category_id"]])):
                cls = 2 if is_volume(ds.name, cats[a["category_id"]]) else 1
                seg = a.get("segmentation")
                if isinstance(seg, list) and seg and len(seg[0]) >= 6:
                    for poly in seg:
                        cv2.fillPoly(target, [np.round(np.array(poly).reshape(-1, 2) * k).astype(np.int32)], cls)
                else:
                    x, y, w, h = [v * k for v in a["bbox"]]
                    if w < 1 or h < 1:
                        continue
                    cv2.ellipse(target, (round(x + w / 2), round(y + h / 2)), (max(1, round(w / 2)), max(1, round(h / 2))), 0, 0, 360, cls, -1)
            out.append((cv2.cvtColor(img, cv2.COLOR_BGR2RGB), target, ds.name))
    return out


class Crops(Dataset):
    """Random scales (320-512 on the longer side), crops, flips and light changes of colour and brightness."""
    def __init__(self, items, train: bool):
        self.items = items; self.train = train

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        img, target, _ = self.items[i]
        if self.train:
            long = random.randint(*SCALES)
            k = long / max(img.shape[:2])
            size = (max(CROP, round(img.shape[1] * k)), max(CROP, round(img.shape[0] * k)))
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA); target = cv2.resize(target, size, interpolation=cv2.INTER_NEAREST)
            y = random.randint(0, img.shape[0] - CROP); x = random.randint(0, img.shape[1] - CROP)
            img = img[y:y + CROP, x:x + CROP]; target = target[y:y + CROP, x:x + CROP]
            if random.random() < 0.5:
                img = img[:, ::-1]; target = target[:, ::-1]
            img = img.astype(np.float32) / 255
            img = img * random.uniform(0.6, 1.4) + random.uniform(-0.1, 0.1)                       # brightness, contrast
            grey = img.mean(2, keepdims=True)
            img = grey + (img - grey) * random.uniform(0.6, 1.3)                                    # saturation
            img = img * np.random.uniform(0.9, 1.1, 3).astype(np.float32)                          # white balance
            img = np.clip(img, 0, 1)
        else:
            k = EVAL_SIDE / max(img.shape[:2])
            size = (round(img.shape[1] * k / 32) * 32, round(img.shape[0] * k / 32) * 32)
            img = cv2.resize(img, size, interpolation=cv2.INTER_AREA).astype(np.float32) / 255
            target = cv2.resize(target, size, interpolation=cv2.INTER_NEAREST)
        x = torch.from_numpy(((img - MEAN) / STD).transpose(2, 0, 1).copy())
        return x, torch.from_numpy(target.astype(np.int64).copy())


def model(small: bool) -> torch.nn.Module:
    if small:
        from torchvision.models import mobilenet_v3_small
        backbone = mobilenet_v3_small(weights=MobileNet_V3_Small_Weights.IMAGENET1K_V1, dilated=True).features
        stage = [i for i, b in enumerate(backbone) if getattr(b, "_is_cn", False)] + [len(backbone) - 1]
        low, high = stage[-4], stage[-1]
        from torchvision.models._utils import IntermediateLayerGetter
        body = IntermediateLayerGetter(backbone, return_layers={str(low): "low", str(high): "high"})
        return LRASPP(body, backbone[low].out_channels, backbone[high].out_channels, 3, 128)
    return lraspp_mobilenet_v3_large(weights=None, weights_backbone=MobileNet_V3_Large_Weights.IMAGENET1K_V1, num_classes=3)


def iou(net, items, device) -> tuple[float, float]:
    """Hold IoU, and the share of true holds (each a mask) at least half covered, over pwals' validation photos."""
    net.eval(); inter = union = 0; covered = total = 0
    with torch.no_grad():
        for x, t in DataLoader(Crops(items, False), batch_size=1):
            p = net(x.to(device))["out"].argmax(1).cpu()[0]
            hold = p == 1; truth = t[0] == 1
            inter += (hold & truth).sum().item(); union += (hold | truth).sum().item()
            n, lab = cv2.connectedComponents(truth.numpy().astype(np.uint8))
            for k in range(1, n):
                m = torch.from_numpy(lab == k)
                total += 1; covered += (hold[m].float().mean() >= 0.5).item()
    net.train()
    return inter / max(union, 1), covered / max(total, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=pathlib.Path); ap.add_argument("run", type=pathlib.Path)
    ap.add_argument("--small", action="store_true"); ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.run.mkdir(parents=True, exist_ok=True)
    random.seed(0); torch.manual_seed(0)
    t0 = time.time()
    train = load(a.data, "train") + [x for x in load(a.data, "valid") if x[2] != "pwals"]
    val = [x for x in load(a.data, "valid") + load(a.data, "test") if x[2] == "pwals"]
    print(f"{len(train)} pictures to train on, {len(val)} to check by ({time.time() - t0:.0f} s to load)", flush=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    net = model(a.small).to(device)
    # pwals has true masks, few pictures: seen 8 times as often
    weights = [8.0 if x[2] == "pwals" else 1.0 for x in train]
    sampler = torch.utils.data.WeightedRandomSampler(weights, len(train), replacement=True)
    dl = DataLoader(Crops(train, True), batch_size=32, sampler=sampler, num_workers=a.workers, drop_last=True, persistent_workers=True)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    steps = a.epochs * len(dl)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=1e-3, total_steps=steps, pct_start=0.1)
    cw = torch.tensor([1.0, 2.0, 1.0], device=device)
    best = -1.0
    for epoch in range(a.epochs):
        total = 0.0
        for x, t in dl:
            x, t = x.to(device, non_blocking=True), t.to(device, non_blocking=True)
            with torch.autocast(device, dtype=torch.bfloat16, enabled=device == "cuda"):
                loss = F.cross_entropy(net(x)["out"].float(), t, weight=cw)
            opt.zero_grad(set_to_none=True); loss.backward(); opt.step(); sched.step()
            total += loss.item()
        hold_iou, covered = iou(net, val, device)
        print(f"epoch {epoch + 1}/{a.epochs}: loss {total / len(dl):.3f}, hold IoU {hold_iou:.3f}, holds half covered {covered:.3f} "
              f"({time.time() - t0:.0f} s)", flush=True)
        if hold_iou > best:
            best = hold_iou
            torch.save({"model": net.state_dict(), "small": a.small, "epoch": epoch + 1, "iou": hold_iou, "covered": covered}, a.run / "best.pt")
    print(f"best hold IoU {best:.3f}", flush=True)


if __name__ == "__main__":
    main()
