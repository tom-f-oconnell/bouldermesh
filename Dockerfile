# BoulderMesh with the real pipeline: COLMAP (built with CUDA) + OpenMVS (built from source, CPU).
# Build:  docker build -t bouldermesh .      (first build ~10-40 min depending on cores)
# Run:    docker compose up                 (see docker-compose.yml)
#
# The image works with or without a GPU: photo matching uses one if it's given to the container
# (docker-compose.gpu.yml) and has room, and the CPU otherwise. torch is its CUDA build, for VGGT placing photos where
# COLMAP's mapper can't (backend/vggt_place.py), which needs a GPU; everything else of torch's runs on the CPU.

# The same COLMAP version as Ubuntu's package, which has no CUDA, so that only the device changes.
# It predates this compiler, which no longer brings in those two headers by itself.
FROM ubuntu:24.04 AS colmap
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates nvidia-cuda-toolkit nvidia-cuda-toolkit-gcc \
    libboost-program-options-dev libboost-filesystem-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev libgoogle-glog-dev libgflags-dev \
    libsqlite3-dev libglew-dev qtbase5-dev libqt5opengl5-dev libcgal-dev libceres-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 3.9.1 https://github.com/colmap/colmap.git /opt/colmap \
 && cmake -S /opt/colmap -B /opt/colmap/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCUDA_ENABLED=ON \
      -DCMAKE_CUDA_ARCHITECTURES=all-major -DGUI_ENABLED=OFF -DTESTS_ENABLED=OFF -DCMAKE_INSTALL_PREFIX=/opt/colmap-cuda \
      "-DCMAKE_CXX_FLAGS=-include memory -include cstdint" \
 && cmake --build /opt/colmap/build -j "$(( $(nproc) / 2 + 1 ))" -- -k 0 && cmake --install /opt/colmap/build

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
COPY backend/signal_note.c /opt/signal_note.c
RUN gcc -shared -fPIC -O2 -Wall -o /usr/local/lib/signal_note.so /opt/signal_note.c

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# rtmlib (the pose model finding climbers' hands and feet) is installed without its declared dependencies, which
# would replace Ubuntu's numpy and OpenCV with pip's; it works with Ubuntu's, and onnxruntime's are listed after
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil python3-numpy python3-opencv python3-pip ffmpeg colmap libcudart12 \
    openssh-client python3-boto3 python3-cryptography \
    libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 \
    libopencv-core4.6 libopencv-imgproc4.6 libopencv-imgcodecs4.6 libopencv-calib3d4.6 libopencv-features2d4.6 \
    libcgal-dev libglew2.2 libglfw3 libjpeg8 libpng16-16 libtiff6 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages yt-dlp \
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/cu126 \
 && pip install --no-cache-dir --break-system-packages --no-deps rtmlib onnxruntime \
 && pip install --no-cache-dir --break-system-packages flatbuffers protobuf coloredlogs humanfriendly sympy packaging tqdm \
 && pip install --no-cache-dir --break-system-packages --ignore-installed idna \
 && pip install --no-cache-dir --break-system-packages open_clip_torch
# VGGT at the commit tried in experiments/depth; its weights (4.7 GB) download into the models cache when first needed.
# Without its dependencies, as it pins numpy below 2 and pip's OpenCV, where Ubuntu's work.
RUN pip install --no-cache-dir --break-system-packages einops safetensors huggingface_hub \
 && pip install --no-cache-dir --break-system-packages --no-deps \
      https://github.com/facebookresearch/vggt/archive/a288dd0f14786c93483e45524328726ab7b1b4ce.zip \
 && python3 -c "import vggt.models.vggt"
# YouTube needs a JavaScript runtime and yt-dlp's challenge scripts (yt-dlp-ejs, in its "default" extras) for full
# support: without them yt-dlp warns that formats may be missing. Deno is the runtime it looks for on PATH.
RUN pip install --no-cache-dir --break-system-packages --upgrade "yt-dlp[default]" deno \
 && deno --version | head -1 && python3 -c "import yt_dlp_ejs"
# Ubuntu's colmap package is only there for the libraries this one links to: /usr/local/bin comes first
COPY --from=colmap /opt/colmap-cuda/bin/colmap /usr/local/bin/colmap
COPY --from=build /usr/local/bin/OpenMVS /usr/local/bin/OpenMVS
COPY --from=build /usr/local/lib/signal_note.so /usr/local/lib/signal_note.so
ENV OPENMVS_BIN=/usr/local/bin/OpenMVS
# COLMAP's binary needs a display for some commands; run headless via Qt offscreen
ENV QT_QPA_PLATFORM=offscreen

WORKDIR /app
COPY backend ./backend
COPY frontend ./frontend
COPY cli.py ./cli.py
COPY volunteer ./volunteer
COPY worker ./worker
# Which build this is, given by deploy/build.sh, and the name it's pushed under: shown on the admin page, and the image
# remote machines run unless the admin page names another. Here at the end, so that the steps above stay cached.
ARG BOULDERMESH_COMMIT=
ARG BOULDERMESH_IMAGE=
ENV BOULDERMESH_COMMIT=$BOULDERMESH_COMMIT BOULDERMESH_IMAGE=$BOULDERMESH_IMAGE
ENV DATA_DIR=/data PORT=8470
VOLUME /data
EXPOSE 8470
CMD ["python3", "backend/server.py"]
