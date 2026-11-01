# BoulderMesh with the real pipeline: COLMAP (built with CUDA) + OpenMVS (built from source, CPU).
# Build:  docker build -t bouldermesh .      (first build ~10-40 min depending on cores)
# Run:    docker compose up                 (see docker-compose.yml)
#
# The image works with or without a GPU: photo matching uses one if it's given to the container
# (docker-compose.gpu.yml) and has room, and the CPU otherwise.

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

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pil python3-numpy python3-opencv python3-pip ffmpeg colmap libcudart12 \
    libboost-iostreams1.83.0 libboost-program-options1.83.0 libboost-serialization1.83.0 \
    libopencv-core4.6 libopencv-imgproc4.6 libopencv-imgcodecs4.6 libopencv-calib3d4.6 libopencv-features2d4.6 \
    libcgal-dev libglew2.2 libglfw3 libjpeg8 libpng16-16 libtiff6 \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages yt-dlp \
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/cpu
# Ubuntu's colmap package is only there for the libraries this one links to: /usr/local/bin comes first
COPY --from=colmap /opt/colmap-cuda/bin/colmap /usr/local/bin/colmap
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
