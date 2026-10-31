# BoulderMesh with the real pipeline: COLMAP (apt) + OpenMVS (built from source, CPU).
# Build:  docker build -t bouldermesh .      (first build ~5-30 min depending on cores)
# Run:    docker compose up                 (see docker-compose.yml)
#
# For NVIDIA GPU acceleration use an nvidia/cuda base image, install COLMAP with
# CUDA, run with --gpus all and set USE_GPU=1. CPU-only works, just slower.

FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates \
    libboost-iostreams-dev libboost-program-options-dev libboost-system-dev libboost-serialization-dev \
    libopencv-dev libcgal-dev libcgal-qt5-dev libeigen3-dev libglu1-mesa-dev libglew-dev libglfw3-dev \
    libjpeg-dev libpng-dev libtiff-dev \
 && rm -rf /var/lib/apt/lists/*
# VCGLib is a header-only dependency of OpenMVS.
# v2.3.0's Util::formatTime() passes snprintf the whole buffer size at an offset, which
# Ubuntu's _FORTIFY_SOURCE=3 aborts on ("buffer overflow detected") as soon as a duration has
# two units, killing DensifyPointCloud; patched as upstream did in v2.4.0 (which needs OpenCV >= 4.11).
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /opt/vcglib \
 && git clone --depth 1 --branch v2.3.0 https://github.com/cdcseacave/openMVS.git /opt/openMVS \
 && sed -i 's/_sntprintf(buf+len, 128,/_sntprintf(buf+len, 128-len,/' /opt/openMVS/libs/Common/Util.h \
 && grep -q '_sntprintf(buf+len, 128-len,' /opt/openMVS/libs/Common/Util.h
RUN cmake -S /opt/openMVS -B /opt/openMVS/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DVCG_ROOT=/opt/vcglib -DOpenMVS_USE_CUDA=OFF -DOpenMVS_USE_PYTHON=OFF \
 && cmake --build /opt/openMVS/build && cmake --install /opt/openMVS/build

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil python3-numpy python3-opencv python3-pip ffmpeg colmap \
    libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 \
    libopencv-core4.6 libopencv-imgproc4.6 libopencv-imgcodecs4.6 libopencv-calib3d4.6 libopencv-features2d4.6 \
    libcgal-dev libglew2.2 libglfw3 libjpeg8 libpng16-16 libtiff6 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages yt-dlp \
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY --from=build /usr/local/bin/OpenMVS /usr/local/bin/OpenMVS
ENV OPENMVS_BIN=/usr/local/bin/OpenMVS
# COLMAP's binary needs a display for some commands; run headless via Qt offscreen
ENV QT_QPA_PLATFORM=offscreen

WORKDIR /app
COPY backend ./backend
COPY frontend ./frontend
COPY cli.py ./cli.py
ENV DATA_DIR=/data PORT=8470
VOLUME /data
EXPOSE 8470
CMD ["python3", "backend/server.py"]
