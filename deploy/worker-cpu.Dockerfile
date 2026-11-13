# The image machines without a GPU run jobs in: filtering a harvest's photos (remote_job.py --task filter) and finding a
# mesh's beta (--task beta), which need only Python, OpenCV, CLIP (torch's CPU build), ffmpeg and rtmlib's pose model
# on onnxruntime, not COLMAP, OpenMVS, CUDA or VGGT. rtmlib goes in without its declared dependencies, as in the main
# Dockerfile, so that Ubuntu's numpy and OpenCV stay; onnxruntime's are listed after. Built for x86-64 and ARM64
# (Apple Silicon Macs, ARM Windows laptops and Linux boards, under Docker), as one name, by deploy/build.sh:
#     docker buildx build --platform linux/amd64,linux/arm64 -f deploy/worker-cpu.Dockerfile .
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil python3-numpy python3-opencv python3-pip ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir --break-system-packages open_clip_torch \
 && pip install --no-cache-dir --break-system-packages --no-deps rtmlib onnxruntime \
 && pip install --no-cache-dir --break-system-packages flatbuffers protobuf coloredlogs humanfriendly sympy packaging tqdm \
 && python3 -c "import cv2, torch, open_clip, rtmlib, onnxruntime; print('opencv', cv2.__version__, 'torch', torch.__version__, 'onnxruntime', onnxruntime.__version__)"
WORKDIR /app
COPY backend ./backend
ARG BOULDERMESH_COMMIT=
ENV BOULDERMESH_COMMIT=$BOULDERMESH_COMMIT DATA_DIR=/data
VOLUME /data
CMD ["python3", "backend/remote_job.py", "--help"]
