# The image machines without a GPU run jobs in: filtering a harvest's photos (remote_job.py --task filter), which needs
# only Python, OpenCV and CLIP (torch's CPU build), not COLMAP, OpenMVS, CUDA or VGGT. Built for x86-64 and ARM64
# (Apple Silicon Macs, ARM Windows laptops and Linux boards, under Docker), as one name, by deploy/build.sh:
#     docker buildx build --platform linux/amd64,linux/arm64 -f deploy/worker-cpu.Dockerfile .
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil python3-numpy python3-opencv python3-pip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir --break-system-packages open_clip_torch \
 && python3 -c "import cv2, torch, open_clip; print('opencv', cv2.__version__, 'torch', torch.__version__)"
WORKDIR /app
COPY backend ./backend
ARG BOULDERMESH_COMMIT=
ENV BOULDERMESH_COMMIT=$BOULDERMESH_COMMIT DATA_DIR=/data
VOLUME /data
CMD ["python3", "backend/remote_job.py", "--help"]
