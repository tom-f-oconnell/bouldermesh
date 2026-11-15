# BoulderMesh with the real pipeline: COLMAP (built with CUDA) + OpenMVS (built from source, CPU; --build-arg MVS=24: see below).
# Build:  docker build -t bouldermesh .      (first build ~10-40 min depending on cores)
# Run:    docker compose up                 (see docker-compose.yml)
#
# The image works with or without a GPU: photo matching uses one if it's given to the container
# (docker-compose.gpu.yml) and has room, and the CPU otherwise. torch is its CUDA build (TORCH=cu126, 10.4 GB in all),
# for VGGT placing photos where COLMAP's mapper can't (backend/vggt_place.py), which needs a GPU; everything else of
# torch's runs on the CPU. --build-arg TORCH=cpu makes the 2.5 GB image a server that meshes nothing needs (build.sh).

# Which OpenMVS. 23: v2.3.0 for the CPU, which every mesh so far was made with. 24 (--build-arg MVS=24): v2.4.0, built
# twice: for the CPU, and with CUDA for depth maps on a GPU (5 times faster in experiments/mapper), which pipeline.py
# uses for that one step when a GPU has room. Not the default until an image built with it has made meshes.
ARG MVS=23

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
# what the last stage takes from whichever OpenMVS stage: here no CUDA build, and no libraries of its own
RUN mkdir -p /usr/local/bin/OpenMVS-cuda /opt/mvs-libs

FROM build AS mvs23

# OpenMVS v2.4.0 needs OpenCV >= 4.11 (Ubuntu 24.04 has 4.6) and CGAL 6 (it has 5.6; header-only), so both are built
# here, OpenCV with only what OpenMVS uses and nothing that would need more libraries in the last stage. The CUDA
# build's tools load the NVIDIA driver's own library (libcuda.so.1), so they only start in a container given a GPU:
# hence the CPU build beside it, which does every step but depth maps, and those too where there's no GPU.
FROM ubuntu:24.04 AS mvs24
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates curl xz-utils nvidia-cuda-toolkit nvidia-cuda-toolkit-gcc \
    libboost-iostreams-dev libboost-program-options-dev libboost-system-dev libboost-serialization-dev \
    libeigen3-dev libgmp-dev libmpfr-dev libnanoflann-dev libjpeg-dev libpng-dev libtiff-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 4.14.0 https://github.com/opencv/opencv.git /opt/opencv \
 && cmake -S /opt/opencv -B /opt/opencv/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_LIST=core,imgproc,imgcodecs,calib3d,features2d,flann,highgui -DWITH_CUDA=OFF -DBUILD_TESTS=OFF \
      -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DWITH_GTK=OFF -DWITH_QT=OFF -DWITH_FFMPEG=OFF \
      -DWITH_GSTREAMER=OFF -DWITH_V4L=OFF -DWITH_OPENCL=OFF -DWITH_VA=OFF -DWITH_VTK=OFF -DWITH_LAPACK=OFF -DWITH_ITT=OFF \
      -DWITH_WEBP=OFF -DWITH_OPENEXR=OFF -DWITH_OPENJPEG=OFF -DWITH_JASPER=OFF -DWITH_AVIF=OFF -DWITH_JPEGXL=OFF \
      -DWITH_PROTOBUF=OFF -DWITH_ADE=OFF -DWITH_OBSENSOR=OFF \
 && cmake --build /opt/opencv/build && cmake --install /opt/opencv/build \
 && mkdir -p /opt/mvs-libs && cp -a /usr/local/lib/libopencv_*.so.* /opt/mvs-libs/
RUN curl -fsSL https://github.com/CGAL/cgal/releases/download/v6.0.1/CGAL-6.0.1.tar.xz | tar -xJ --no-same-owner -C /opt \
 && cmake -S /opt/CGAL-6.0.1 -B /opt/CGAL-6.0.1/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/cgal6 \
 && cmake --install /opt/CGAL-6.0.1/build
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /opt/vcglib \
 && git clone --depth 1 --branch v2.4.0 https://github.com/cdcseacave/openMVS.git /opt/openMVS
# the CPU build, then the CUDA one (for every NVIDIA generation, as COLMAP's is), each into its own folder
RUN for cuda in OFF ON; do \
      cmake -S /opt/openMVS -B /opt/openMVS/build-$cuda -G Ninja -DCMAKE_BUILD_TYPE=Release -DVCG_ROOT=/opt/vcglib \
        -DCGAL_DIR=/opt/cgal6/lib/cmake/CGAL -DOpenCV_DIR=/usr/local/lib/cmake/opencv4 -DOpenMVS_USE_CUDA=$cuda \
        -DCMAKE_CUDA_ARCHITECTURES=all-major -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF -DOpenMVS_USE_BREAKPAD=OFF \
        -DOpenMVS_ENABLE_TESTS=OFF -DOpenMVS_USE_CERES=OFF -DCMAKE_INSTALL_PREFIX=/opt/mvs-$cuda \
   && cmake --build /opt/openMVS/build-$cuda && cmake --install /opt/openMVS/build-$cuda || exit 1; \
    done \
 && rm -rf /usr/local/bin/OpenMVS && cp -a /opt/mvs-OFF/bin/OpenMVS /usr/local/bin/OpenMVS \
 && cp -a /opt/mvs-ON/bin/OpenMVS /usr/local/bin/OpenMVS-cuda \
 && test -x /usr/local/bin/OpenMVS/DensifyPointCloud && test -x /usr/local/bin/OpenMVS-cuda/DensifyPointCloud \
 && ! (ldd /usr/local/bin/OpenMVS/TextureMesh | grep -q libcuda)

FROM mvs${MVS} AS mvs

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ARG TORCH=cu126
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
 && pip install --no-cache-dir --break-system-packages torch torchvision --index-url https://download.pytorch.org/whl/$TORCH \
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
# segno: QR codes on the admin page, of the topic a phone subscribes to for notifications (notify.py)
RUN pip install --no-cache-dir --break-system-packages segno
# YouTube needs a JavaScript runtime and yt-dlp's challenge scripts (yt-dlp-ejs, in its "default" extras) for full
# support: without them yt-dlp warns that formats may be missing. Deno is the runtime it looks for on PATH.
RUN pip install --no-cache-dir --break-system-packages --upgrade "yt-dlp[default]" deno \
 && deno --version | head -1 && python3 -c "import yt_dlp_ejs"
# py-spy: what a hung process's threads are doing, native frames too (diagnostics.dump_threads(); README, "When it hangs")
RUN pip install --no-cache-dir --break-system-packages py-spy && py-spy --version
# Pictures of meshes (backend/thumbs.py): the site's own viewer in a browser with no screen, Chromium as playwright-core
# downloads it (with the libraries it needs), run by Ubuntu's node. The server's build only: the machines' (deploy/build.sh)
# passes THUMBS=0, as they don't draw them.
ARG THUMBS=1
ENV PLAYWRIGHT_CORE=/opt/playwright-core PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN if [ "$THUMBS" = 1 ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs curl ca-certificates \
   && mkdir -p $PLAYWRIGHT_CORE \
   && curl -fsSL https://registry.npmjs.org/playwright-core/-/playwright-core-1.48.2.tgz | tar -xz -C $PLAYWRIGHT_CORE --strip-components=1 \
   && node $PLAYWRIGHT_CORE/cli.js install --with-deps chromium \
   && chmod -R a+rX $PLAYWRIGHT_BROWSERS_PATH && rm -rf /var/lib/apt/lists/*; fi
# Ubuntu's colmap package is only there for the libraries this one links to: /usr/local/bin comes first
COPY --from=colmap /opt/colmap-cuda/bin/colmap /usr/local/bin/colmap
COPY --from=mvs /usr/local/bin/OpenMVS /usr/local/bin/OpenMVS
# with MVS=24: its CUDA build for depth maps, and the OpenCV both its builds link (empty folders otherwise)
COPY --from=mvs /usr/local/bin/OpenMVS-cuda /usr/local/bin/OpenMVS-cuda
COPY --from=mvs /opt/mvs-libs/ /usr/local/lib/
RUN ldconfig
COPY --from=build /usr/local/lib/signal_note.so /usr/local/lib/signal_note.so
ENV OPENMVS_BIN=/usr/local/bin/OpenMVS OPENMVS_CUDA_BIN=/usr/local/bin/OpenMVS-cuda
# COLMAP's binary needs a display for some commands; run headless via Qt offscreen
ENV QT_QPA_PLATFORM=offscreen

WORKDIR /app
COPY backend ./backend
COPY frontend ./frontend
COPY cli.py ./cli.py
COPY volunteer ./volunteer
COPY worker ./worker
# Which build this is, given by deploy/build.sh, and the name it's pushed under, both shown on the admin page; and the
# image remote machines run unless the admin page names another, the commit's GPU build. Here at the end, so that the
# steps above stay cached.
ARG BOULDERMESH_COMMIT=
ARG BOULDERMESH_IMAGE=
ARG BOULDERMESH_WORKER_IMAGE=
# and the image machines without a GPU run, which only filter photos (deploy/worker-cpu.Dockerfile), if it was built
ARG BOULDERMESH_WORKER_CPU_IMAGE=
ENV BOULDERMESH_COMMIT=$BOULDERMESH_COMMIT BOULDERMESH_IMAGE=$BOULDERMESH_IMAGE BOULDERMESH_WORKER_IMAGE=$BOULDERMESH_WORKER_IMAGE \
    BOULDERMESH_WORKER_CPU_IMAGE=$BOULDERMESH_WORKER_CPU_IMAGE
ENV DATA_DIR=/data PORT=8470
VOLUME /data
EXPOSE 8470
CMD ["python3", "backend/server.py"]
