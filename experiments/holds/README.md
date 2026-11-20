# The apps' hold-finding model

Approach B of gym climbs (app/shared `Gym.kt`, `HoldModel.kt`): a small segmenter says which pixels of a photo are a
hold, so that colour only has to say whose. Approach A (colour alone) can't tell a hold from a wall painted its colour.
The design and how the two compare are in [design.md](../../design.md#the-phone-apps-app) under gym climbs.

## The model

It's torchvision's LR-ASPP on MobileNetV3-Large (BSD), from ImageNet weights; `--small` trains MobileNetV3-Small,
which scored worse (design.md has how each did). Each pixel is background, hold or volume.
The apps run it on the photo made 640 pixels on its longer side (`HoldModel.SIDE`). Android uses ONNX Runtime on
`holds.onnx`, in the APK's assets. The iPhone uses Core ML on `holds.mlpackage`, in `app/ios/xcode/Models`, at a fixed
640 x 640 with the picture padded into it.

## The data

Its training data comes from Roboflow Universe, fetched by `fetch.py` with the key in
`~/.config/bouldermesh/roboflow_key`. It's kept in `/data/tom/holdmodel/data`, not here.

| Name | Project | Licence | Pictures | Labels |
|---|---|---|---|---|
| pwals | [bouldering-holds](https://universe.roboflow.com/pwals/bouldering-holds-9wavr) v4 | CC BY 4.0 | 48 | masks, by colour |
| johns | [climbing-holds](https://universe.roboflow.com/johns-workspace-rfgwn/climbing-holds-hsnzv) v1 | Public Domain | 168 | boxes and some masks, by colour |
| blackcreed | [climbing-holds-and-volumes](https://universe.roboflow.com/blackcreed-xpgxh/climbing-holds-and-volumes) v14 | CC BY 4.0 | 4,301 | boxes: holds, volumes |

Where only a box is given, the target is the ellipse inside it. The model is made from CC BY data: the apps credit those
datasets at the bottom of Settings (HoldModel.CREDITS).

## Running

    uv venv venv --python 3.12
    VIRTUAL_ENV=venv uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
    VIRTUAL_ENV=venv uv pip install onnx onnxruntime coremltools pillow numpy opencv-python-headless

    venv/bin/python fetch.py /data/tom/holdmodel/data
    venv/bin/python train.py /data/tom/holdmodel/data /data/tom/holdmodel/large2     # about 40 min on hal's 3090
    venv/bin/python export.py /data/tom/holdmodel/large2/best.pt /data/tom/holdmodel/large2/export
    cp /data/tom/holdmodel/large2/export/holds.onnx ../../app/android/src/main/assets/
    cp -r /data/tom/holdmodel/large2/export/holds.mlpackage ../../app/ios/xcode/Models/

`export.py` checks that ONNX gives what PyTorch does. Core ML can only be run on Apple's systems, so nothing checks
`holds.mlpackage` here. To see both finders on a real capture's photos, run app/shared's GymCheck:
`BM_GYM_CHECK=...` (A), and with `BM_GYM_MODEL=<holds.onnx>` added (B), as its comment says.
