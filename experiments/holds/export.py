#!/usr/bin/env python3
"""Exports a trained hold-finding model (train.py) for the apps, and checks the export gives what the model does.

    venv/bin/python export.py /data/tom/holdmodel/large/best.pt /data/tom/holdmodel/large/export

Writes holds.onnx (the Android app's asset: app/android/src/main/assets/) and holds.mlpackage (the iPhone app's:
app/ios/xcode/Models/), each with input "image" (1 x 3 x h x w) and output "logits" (1 x 3 x h x w: background, hold,
volume), as app/shared HoldModel.kt has them. The ONNX model takes any h and w; the Core ML one only 640 x 640 (Core
ML can't resize to a size known only when it runs), into which the iPhone app puts the picture, padded.
"""
import pathlib
import sys

import numpy as np
import torch

from train import model


COREML_SIDE = 640       # the Core ML model's fixed input, h and w (HoldModel.SIDE; HoldFinder.kt on the iPhone pads to it)


class Logits(torch.nn.Module):
    """The model with its output as a plain tensor, which both exporters want."""
    def __init__(self, net):
        super().__init__()
        self.net = net

    def forward(self, image):
        return self.net(image)["out"]


def main(ckpt: pathlib.Path, out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    c = torch.load(ckpt, map_location="cpu")
    net = model(c["small"]); net.load_state_dict(c["model"]); net.eval()
    m = Logits(net).eval()
    x = torch.randn(1, 3, 180, 320)
    onnx_path = out / "holds.onnx"
    torch.onnx.export(m, (x,), onnx_path, input_names=["image"], output_names=["logits"], opset_version=17, dynamo=False,
                      dynamic_axes={"image": {2: "h", 3: "w"}, "logits": {2: "h", 3: "w"}})
    import onnxruntime as ort
    s = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    for shape in ((1, 3, 180, 320), (1, 3, 320, 180), (1, 3, 232, 310)):
        xi = torch.randn(*shape)
        with torch.no_grad():
            ref = m(xi).numpy()
        got = s.run(None, {"image": xi.numpy()})[0]
        print(f"onnx {shape}: max difference {np.abs(got - ref).max():.2e}, same argmax {(got.argmax(1) == ref.argmax(1)).mean():.4f}")
    print(f"holds.onnx: {onnx_path.stat().st_size / 1e6:.1f} MB (epoch {c['epoch']}, hold IoU {c['iou']:.3f}, holds half covered {c['covered']:.3f})")
    try:
        import coremltools as ct
        traced = torch.jit.trace(m, torch.zeros(1, 3, COREML_SIDE, COREML_SIDE))
        ml = ct.convert(traced, inputs=[ct.TensorType(name="image", shape=(1, 3, COREML_SIDE, COREML_SIDE), dtype=np.float32)],
                        outputs=[ct.TensorType(name="logits", dtype=np.float32)], convert_to="mlprogram",
                        minimum_deployment_target=ct.target.iOS16)
        ml.short_description = "Which pixels are a hold: background, hold, volume (BoulderMesh experiments/holds)"
        ml.save(str(out / "holds.mlpackage"))
        print("holds.mlpackage written (not run: Core ML runs only on Apple's systems)")
    except Exception as e:
        print(f"Core ML: not converted ({type(e).__name__}: {e})")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
