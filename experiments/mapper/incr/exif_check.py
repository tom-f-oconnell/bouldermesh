"""Whether each reader of a photo in the pipeline sees the same pixels COLMAP does when the photo carries an EXIF
Orientation tag (web photos often do; the phone apps' always do). COLMAP 4.2.1 reads pixels as stored, and only
turns the tag into a gravity prior (image_reader.cc). A reader that applies the tag sees the photo a quarter turn
from COLMAP's, and its cameras or masks are then wrong.

    exif_check.py make <photo> <out dir>        a copy with Orientation 6 (pixels untouched) and one with 1
    exif_check.py app <dir>                     in the app's image: PIL as occlusion.py and vggt_place use it,
                                                VGGT's loader, cv2.imread as other backend steps use it
    exif_check.py colmap <dir>                  in the pipeline's tools: the size COLMAP extracts features at, and
                                                what its undistorter writes (pixels and tag)
Prints one line a reader: the size it sees, and whether that is the stored size (consistent with COLMAP).
"""
import sys
from pathlib import Path


def make(photo: Path, out: Path) -> None:
    from PIL import Image
    out.mkdir(parents=True, exist_ok=True)
    im = Image.open(photo)
    for o in (1, 6):
        exif = im.getexif()
        exif[0x0112] = o
        im.save(out / f"o{o}.jpg", exif=exif.tobytes(), quality=95)
    print("stored size", im.size)


def report(name: str, size, stored) -> None:
    same = tuple(size) == tuple(stored)
    print(f"{name}: sees {tuple(size)}; {'as stored, like COLMAP' if same else 'TURNED from what COLMAP sees'}")


def app(d: Path) -> None:
    import cv2
    from PIL import Image
    for o in (1, 6):
        p = d / f"o{o}.jpg"
        stored = Image.open(p).size
        print(f"-- Orientation {o}, stored {stored}")
        with Image.open(p) as im:
            report("PIL Image.open (occlusion.person_mask, vggt_place.focal)", im.convert("RGB").size, stored)
        from vggt.utils.load_fn import load_and_preprocess_images
        t = load_and_preprocess_images([str(p)])
        h, w = t.shape[-2:]
        landscape = stored[0] >= stored[1]
        print(f"VGGT load_and_preprocess_images: {w}x{h}: {'as stored' if (w >= h) == landscape else 'TURNED'}")
        im = cv2.imread(str(p))
        report("cv2.imread default (beta.py, scale.py, splat.py)", (im.shape[1], im.shape[0]), stored)
        im = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        report("cv2.imread IMREAD_UNCHANGED (OpenMVS's reader)", (im.shape[1], im.shape[0]), stored)


def colmap(d: Path) -> None:
    import shutil
    import pycolmap
    from PIL import Image
    work = Path("/tmp/exif")
    shutil.rmtree(work, ignore_errors=True)
    (work / "photos").mkdir(parents=True)
    for o in (1, 6):
        shutil.copy(d / f"o{o}.jpg", work / "photos" / f"o{o}.jpg")
    ext = pycolmap.FeatureExtractionOptions()
    ext.use_gpu, ext.max_image_size = False, 800
    pycolmap.extract_features(str(work / "db.db"), str(work / "photos"), extraction_options=ext, device=pycolmap.Device.cpu)
    db = pycolmap.Database.open(str(work / "db.db"))
    for im in db.read_all_images():
        cam = db.read_camera(im.camera_id)
        stored = Image.open(work / "photos" / im.name).size
        report(f"COLMAP feature extraction, {im.name}", (cam.width, cam.height), stored)
    # the undistorter: a model with one camera per photo, placed anywhere
    rec = pycolmap.Reconstruction()
    for im in db.read_all_images():
        cam = db.read_camera(im.camera_id)
        rec.add_camera_with_trivial_rig(cam)
        im2 = pycolmap.Image(name=im.name, camera_id=cam.camera_id, image_id=im.image_id)
        rec.add_image_with_trivial_frame(im2, pycolmap.Rigid3d())
    db.close()
    rec.write(str(work / "model")) if (work / "model").mkdir() is None else None
    pycolmap.undistort_images(str(work / "dense"), str(work / "model"), str(work / "photos"))
    for p in sorted((work / "dense" / "images").iterdir()):
        im = Image.open(p)
        print(f"COLMAP undistorter wrote {p.name}: {im.size}, Orientation tag {im.getexif().get(0x0112)}")


if __name__ == "__main__":
    {"make": lambda: make(Path(sys.argv[2]), Path(sys.argv[3])), "app": lambda: app(Path(sys.argv[2])),
     "colmap": lambda: colmap(Path(sys.argv[2]))}[sys.argv[1]]()
