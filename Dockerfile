# BoulderMesh with the real pipeline: COLMAP (built with CUDA) + OpenMVS (built from source, CPU; --build-arg MVS=24: see below).
# Build:  docker build -t bouldermesh .      (first build ~10-40 min depending on cores)
# Run:    docker compose up                 (see docker-compose.yml)
#
# The image works with or without a GPU: photo matching uses one if it's given to the container
# (docker-compose.gpu.yml) and has room, and the CPU otherwise. torch is its CUDA build (TORCH=cu126, 10.4 GB in all),
# for VGGT placing photos where COLMAP's mapper can't (backend/vggt_place.py), which needs a GPU; everything else of
# torch's runs on the CPU. --build-arg TORCH=cpu makes the 2.5 GB image a server that meshes nothing needs (build.sh).

# Which COLMAP 4, for the global mapper (the colmap4 stages below). cuda: with CUDA and cuDSS, as the mapper benchmark
# measured it, so its solves run on a GPU where one has room, for machines with one. cpu: on Ubuntu's Ceres, without
# CUDA, about 2 GB smaller, for the server's image (deploy/build.sh), which never has a GPU.
ARG COLMAP4=cuda

# Which OpenMVS. 23: v2.3.0 for the CPU, which every mesh so far was made with. 24 (--build-arg MVS=24): v2.4.0, built
# twice: for the CPU, and with CUDA for depth maps on a GPU (5 times faster in experiments/mapper), which pipeline.py
# uses for that one step when a GPU has room. Not the default until an image built with it has made meshes.
ARG MVS=23
# torch's build (cu126 for machines with a GPU, cpu for the server: deploy/build.sh), and its version, the same in the
# image and in the stage that builds gsplat against it (gsplat's CUDA code has to match the torch it runs with)
ARG TORCH=cu126
ARG TORCH_VERSION=2.14.1

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

# COLMAP 4.2.1, for its global mapper only (pipeline.py, MAPPER): on the mapper benchmark's boulders it placed the cameras
# 6-14 times faster than 3.9.1's incremental mapper, with as many photos placed (design.md, "The global mapper").
# Everything else (features, matching, the rest step, levelling, undistorting) stays on 3.9.1 above. Either stage has
# experiments/mapper/patch_colmap.py, whose BENCH_BA / BENCH_GP lines in a job's log say which device and solver each
# solve used (pipeline.py counts them, and says so loudly when the GPU was chosen and a large solve ran on the CPU).
# Its binary and every library it loads but glibc's, libstdc++'s, libgcc's and the GPU driver's go to /opt/colmap4, run
# through a wrapper (colmap4) that points at them, so that 3.9.1's colmap and its libraries are left as they are.
#
# COLMAP4=cuda: built as the benchmark built the COLMAP 4 every timing behind the switch came from
# (experiments/mapper/build-inside.sh, "complete"): Ceres from an unreleased commit of master, as no release has cuDSS
# (CUDA's sparse Cholesky) yet, with CUDA, cuDSS (1.0.0, as there) and SuiteSparse, on the Abseil and Eigen 5 it was
# built with; COLMAP with CUDA, for every NVIDIA generation. Global positioning and the bundle adjustments of 50 photos
# or more then solve on the GPU. The stage fails unless Ceres says it has CUDA, cuDSS and SuiteSparse and COLMAP says it
# has CUDA and links cuDSS, and the image's last stage checks that it starts without a GPU. CUDA's libraries it needs
# (cuBLAS, cuSPARSE, cuSOLVER, cuDSS: about 2 GB) come with it; the driver's comes from the machine.
FROM nvidia/cuda:12.9.1-devel-ubuntu24.04 AS colmap4-cuda
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates pkg-config python3 \
    libcudss1-cuda-12=1.0.0.35-1 libcudss1-dev-cuda-12=1.0.0.35-1 libcudss1-static-cuda-12=1.0.0.35-1 \
    libopenblas-dev liblapack-dev libsuitesparse-dev \
    libboost-program-options-dev libboost-filesystem-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev libgoogle-glog-dev libgflags-dev libsqlite3-dev \
    libglew-dev libcgal-dev libopenimageio-dev openimageio-tools libjpeg-dev libpng-dev libtiff-dev libwebp-dev \
    libgtest-dev libgmock-dev libssl-dev libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*
# cuDSS's cmake config looks for its headers only in /usr/include, and OpenImageIO's names a folder of OpenCV's headers;
# Ceres master logs through Abseil, newer than Ubuntu's; CGAL's FindEigen3 (which COLMAP's PoseLib ends up using) can't
# read Eigen 5's version, so it defers to Eigen's own config
RUN ln -sf /usr/include/libcudss/12/*.h /usr/include/ && mkdir -p /usr/include/opencv4 \
 && echo /usr/lib/x86_64-linux-gnu/libcudss/12 > /etc/ld.so.conf.d/cudss.conf && ldconfig \
 && git clone -q --depth 1 --branch 20250512.1 https://github.com/abseil/abseil-cpp.git /opt/absl \
 && cmake -S /opt/absl -B /opt/absl/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=17 -DABSL_PROPAGATE_CXX_STD=ON \
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=ON -DABSL_BUILD_TESTING=OFF \
 && cmake --build /opt/absl/build -j "$(( $(nproc) / 2 + 1 ))" && cmake --install /opt/absl/build > /dev/null \
 && git clone -q --depth 1 --branch 5.0.1 https://gitlab.com/libeigen/eigen.git /opt/eigen \
 && cmake -S /opt/eigen -B /opt/eigen/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DEIGEN_BUILD_DOC=OFF \
      -DEIGEN_BUILD_BLAS=OFF -DEIGEN_BUILD_LAPACK=OFF > /dev/null && cmake --install /opt/eigen/build > /dev/null \
 && printf '%s\n' 'find_package(Eigen3 CONFIG REQUIRED PATHS /usr/local/share/eigen3/cmake NO_DEFAULT_PATH)' 'set(EIGEN3_FOUND TRUE)' \
      'get_target_property(EIGEN3_INCLUDE_DIR Eigen3::Eigen INTERFACE_INCLUDE_DIRECTORIES)' 'set(EIGEN3_INCLUDE_DIRS ${EIGEN3_INCLUDE_DIR})' \
      > /usr/lib/x86_64-linux-gnu/cmake/CGAL/FindEigen3.cmake
RUN mkdir /opt/ceres && cd /opt/ceres && git init -q && git remote add origin https://github.com/ceres-solver/ceres-solver.git \
 && git fetch -q --depth 1 origin e831bfef06ebd7a8daac7d76a1ca8bd33b47fa72 && git checkout -q FETCH_HEAD \
 && cmake -S /opt/ceres -B /opt/ceres/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-O3 -DUSE_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=all-major -Dcudss_DIR=/usr/lib/x86_64-linux-gnu/libcudss/12/cmake/cudss -DWITH_SUITESPARSE=ON \
      -DEigen3_DIR=/usr/local/share/eigen3/cmake -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DBUILD_SHARED_LIBS=ON \
 && cmake --build /opt/ceres/build -j "$(( $(nproc) / 2 + 1 ))" && cmake --install /opt/ceres/build > /dev/null && ldconfig \
 && grep -E "define CERES_(NO|USE)_" /usr/local/include/ceres/internal/config.h \
 && for f in CUDA CUDSS SUITESPARSE; do grep -qx "// #define CERES_NO_$f" /usr/local/include/ceres/internal/config.h \
      || { echo "Ceres was built without $f" >&2; exit 1; }; done
COPY experiments/mapper/patch_colmap.py /opt/patch_colmap.py
RUN git clone -q --depth 1 --branch 4.2.1 https://github.com/colmap/colmap.git /opt/colmap \
 && python3 /opt/patch_colmap.py /opt/colmap \
 && cmake -S /opt/colmap -B /opt/colmap/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS=-O3 \
      -DCMAKE_INSTALL_PREFIX=/opt/colmap4-built -DCUDA_ENABLED=ON -DCMAKE_CUDA_ARCHITECTURES=all-major \
      -Dcudss_DIR=/usr/lib/x86_64-linux-gnu/libcudss/12/cmake/cudss -DEigen3_DIR=/usr/local/share/eigen3/cmake \
      -DONNX_ENABLED=OFF -DGUI_ENABLED=OFF -DOPENGL_ENABLED=OFF -DMVS_ENABLED=OFF -DTESTS_ENABLED=OFF -DCASPAR_ENABLED=OFF \
      -DDOWNLOAD_ENABLED=OFF \
 && cmake --build /opt/colmap/build -j "$(( $(nproc) / 2 + 1 ))" && cmake --install /opt/colmap/build
# only the binary goes on (its static libraries and headers stay here), with the shared libraries it loads, and NVRTC's
# builtins, which NVRTC opens by name when it's used
RUN mkdir -p /opt/colmap4/bin /opt/colmap4/lib && cp /opt/colmap4-built/bin/colmap /opt/colmap4/bin/colmap \
 && ! ldd /opt/colmap4/bin/colmap | grep "not found" \
 && ldd /opt/colmap4/bin/colmap | grep -q libcudss \
 && ldd /opt/colmap4/bin/colmap | awk '/=> \// {print $3}' \
  | grep -vE '/(libc|libm|libmvec|libpthread|libdl|librt|libresolv|libutil|ld-linux[^/]*|libstdc\+\+|libgcc_s|libcuda|libnvidia[^/]*)\.so' \
  | xargs -r cp -L -t /opt/colmap4/lib \
 && if ls /opt/colmap4/lib/libnvrtc.so* > /dev/null 2>&1; then cp -L /usr/local/cuda/lib64/libnvrtc-builtins.so.12* /opt/colmap4/lib/; fi \
 && printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/colmap4/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} exec /opt/colmap4/bin/colmap "$@"\n' \
      > /opt/colmap4/colmap4 \
 && chmod +x /opt/colmap4/colmap4 \
 && /opt/colmap4/colmap4 -h | head -1 | grep "COLMAP 4.2.1" | grep -q "with CUDA" \
 && /opt/colmap4/colmap4 global_mapper -h 2>&1 | grep -q GlobalMapper.gp_use_gpu \
 && du -sh /opt/colmap4

# COLMAP4=cpu: on Ubuntu's Ceres (2.2: SuiteSparse, no CUDA), so its solves are on the CPU
FROM ubuntu:24.04 AS colmap4-cpu
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates pkg-config python3 \
    libboost-program-options-dev libboost-filesystem-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev libgoogle-glog-dev libgflags-dev libsqlite3-dev \
    libglew-dev libcgal-dev libceres-dev libsuitesparse-dev libopenblas-dev liblapack-dev \
    libopenimageio-dev openimageio-tools libjpeg-dev libpng-dev libtiff-dev libwebp-dev \
    libgtest-dev libgmock-dev libssl-dev libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*
COPY experiments/mapper/patch_colmap.py /opt/patch_colmap.py
# OpenImageIO's cmake config names a folder of OpenCV's headers, which nothing here installs
RUN mkdir -p /usr/include/opencv4 \
 && git clone --depth 1 --branch 4.2.1 https://github.com/colmap/colmap.git /opt/colmap \
 && python3 /opt/patch_colmap.py /opt/colmap \
 && cmake -S /opt/colmap -B /opt/colmap/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/colmap4-built \
      -DCUDA_ENABLED=OFF -DONNX_ENABLED=OFF -DGUI_ENABLED=OFF -DOPENGL_ENABLED=OFF -DMVS_ENABLED=OFF -DTESTS_ENABLED=OFF \
      -DCASPAR_ENABLED=OFF -DDOWNLOAD_ENABLED=OFF \
 && cmake --build /opt/colmap/build -j "$(( $(nproc) / 2 + 1 ))" && cmake --install /opt/colmap/build
# only the binary goes on (its static libraries and headers stay here), with the shared libraries it loads
RUN mkdir -p /opt/colmap4/bin /opt/colmap4/lib && cp /opt/colmap4-built/bin/colmap /opt/colmap4/bin/colmap \
 && ldd /opt/colmap4/bin/colmap | awk '/=> \// {print $3}' \
  | grep -vE '/(libc|libm|libmvec|libpthread|libdl|librt|libresolv|libutil|ld-linux[^/]*|libstdc\+\+|libgcc_s|libcuda|libnvidia[^/]*)\.so' \
  | xargs -r cp -L -t /opt/colmap4/lib \
 && printf '#!/bin/sh\nLD_LIBRARY_PATH=/opt/colmap4/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH} exec /opt/colmap4/bin/colmap "$@"\n' \
      > /opt/colmap4/colmap4 \
 && chmod +x /opt/colmap4/colmap4 \
 && /opt/colmap4/colmap4 -h | head -1 | grep -q "COLMAP 4.2.1" \
 && /opt/colmap4/colmap4 global_mapper -h 2>&1 | grep -q GlobalMapper.min_model_size

FROM colmap4-${COLMAP4} AS colmap4

# pycolmap 4.2.1, built from the same patched COLMAP, Ceres and libraries as colmap4 above (COLMAP4=cuda only): for adding
# photos to a mesh's kept model (backend/incremental.py), as experiments/mapper/incr builds it. Its extension finds its
# libraries in /opt/colmap4/lib (its RPATH, which unlike a RUNPATH also finds theirs), so that nothing else in the
# image loads them. COLMAP4=cpu has none: photos are then never added to a model, and every photo is placed.
FROM colmap4-cuda AS pycolmap-cuda
RUN apt-get update && apt-get install -y --no-install-recommends python3-dev python3-pip patchelf && rm -rf /var/lib/apt/lists/* \
 && pip install --break-system-packages --no-cache-dir scikit-build-core "pybind11==3.0.4" numpy \
 && cd /opt/colmap && CMAKE_BUILD_PARALLEL_LEVEL="$(( $(nproc) / 2 + 1 ))" pip install --break-system-packages --no-cache-dir \
      --no-build-isolation --no-deps --target /opt/pycolmap . -Ccmake.define.GENERATE_STUBS=OFF -Ccmake.define.CCACHE_ENABLED=OFF \
      -Cbuild-dir=/tmp/pycolmap-build -Ccmake.define.CUDA_ENABLED=ON -Ccmake.define.CMAKE_CUDA_ARCHITECTURES=all-major \
      -Ccmake.define.cudss_DIR=/usr/lib/x86_64-linux-gnu/libcudss/12/cmake/cudss -Ccmake.define.Eigen3_DIR=/usr/local/share/eigen3/cmake \
      -Ccmake.define.ONNX_ENABLED=OFF -Ccmake.define.CASPAR_ENABLED=OFF -Ccmake.define.DOWNLOAD_ENABLED=OFF \
      -Ccmake.define.GUI_ENABLED=OFF -Ccmake.define.OPENGL_ENABLED=OFF -Ccmake.define.CMAKE_BUILD_TYPE=Release -Ccmake.build-type=Release \
 && rm -rf /tmp/pycolmap-build \
 && patchelf --force-rpath --set-rpath /opt/colmap4/lib /opt/pycolmap/pycolmap/_core*.so

FROM ubuntu:24.04 AS pycolmap-cpu
RUN mkdir -p /opt/pycolmap

FROM pycolmap-${COLMAP4} AS pycolmap

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
# here, OpenCV with only what OpenMVS uses and nothing that would need more libraries in the last stage. It also requires
# JPEG XL (libjxl, found by pkg-config), whose libraries go to the last stage with OpenCV's, so that the default image's
# packages stay as they are. The CUDA build's tools load the NVIDIA driver's own library (libcuda.so.1), so they only
# start in a container given a GPU: hence the CPU build beside it, which does every step but depth maps, and those too
# where there's no GPU.
FROM ubuntu:24.04 AS mvs24
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential ninja-build ca-certificates curl xz-utils nvidia-cuda-toolkit nvidia-cuda-toolkit-gcc \
    libboost-iostreams-dev libboost-program-options-dev libboost-system-dev libboost-serialization-dev \
    libeigen3-dev libgmp-dev libmpfr-dev libnanoflann-dev libjpeg-dev libpng-dev libtiff-dev zlib1g-dev \
    pkg-config libjxl-dev \
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
# v2.4.0's TImage::sample() with a sampler reads the image as if it were already of the type it interpolates in, so
# TextureMesh's seam levelling sampled garbage and painted a sixth to a quarter of every atlas black (upstream issue
# #1251); patched as upstream fixed it after 2.4.0 (eeedab7, Feb 27, 2026)
RUN git clone --depth 1 https://github.com/cdcseacave/VCG.git /opt/vcglib \
 && git clone --depth 1 --branch v2.4.0 https://github.com/cdcseacave/openMVS.git /opt/openMVS \
 && sed -i 's/return Sampler::Sample< INTERTYPE, INTERTYPE, TImage<TYPE>, SAMPLER,/return Sampler::Sample< TYPE, INTERTYPE, TImage<TYPE>, SAMPLER,/' \
      /opt/openMVS/libs/Common/Types.inl \
 && grep -q 'return Sampler::Sample< TYPE, INTERTYPE, TImage<TYPE>, SAMPLER,' /opt/openMVS/libs/Common/Types.inl
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
 && ! (ldd /usr/local/bin/OpenMVS/TextureMesh | grep -q libcuda) \
 && cp -a /usr/lib/x86_64-linux-gnu/libjxl.so.0.7* /usr/lib/x86_64-linux-gnu/libhwy.so.1* /opt/mvs-libs/

FROM mvs${MVS} AS mvs

# gsplat, for fitting captures' scenery as splats (backend/splat.py) on machines with a GPU: built here against the
# image's torch, with CUDA's compiler, which the image itself doesn't have. The server's (TORCH=cpu) has none of it.
# From its source, as its wheel on PyPI compiles its CUDA code when first used, and in C++20, as this torch's headers
# need it (gsplat 1.5.3 asks for C++17).
FROM nvidia/cuda:12.9.1-devel-ubuntu24.04 AS gsplat-cu126
ARG TORCH_VERSION
ENV DEBIAN_FRONTEND=noninteractive TORCH_CUDA_ARCH_LIST="7.5;8.0;8.6;8.9;9.0+PTX" MAX_JOBS=6
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-dev git ninja-build \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --break-system-packages torch==$TORCH_VERSION --index-url https://download.pytorch.org/whl/cu126 \
 && pip install --no-cache-dir --break-system-packages numpy jaxtyping rich setuptools wheel \
 && mkdir /wheels /src && cd /src \
 && pip download --no-deps --no-binary :all: --no-build-isolation --no-cache-dir -d /src gsplat==1.5.3 \
 && tar xzf gsplat-1.5.3.tar.gz \
 && sed -i 's/"-std=c++17"/"-std=c++20"/' gsplat-1.5.3/setup.py \
 && pip wheel --no-deps --no-build-isolation --no-cache-dir -w /wheels ./gsplat-1.5.3 \
 && ls -la /wheels
FROM ubuntu:24.04 AS gsplat-cpu
RUN mkdir /wheels
FROM gsplat-${TORCH} AS gsplat

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ARG TORCH
ARG TORCH_VERSION
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
 && pip install --no-cache-dir --break-system-packages torch==$TORCH_VERSION torchvision --index-url https://download.pytorch.org/whl/$TORCH \
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
# gsplat (built above against this torch), on machines with a GPU only; it checks it has its compiled CUDA code
COPY --from=gsplat /wheels /tmp/gsplat
RUN if ls /tmp/gsplat/*.whl > /dev/null 2>&1; then \
      pip install --no-cache-dir --break-system-packages jaxtyping rich \
   && pip install --no-cache-dir --break-system-packages --no-deps /tmp/gsplat/*.whl \
   && python3 -c "import gsplat, gsplat.cuda._backend as b; assert b._C is not None, 'gsplat has no compiled CUDA code'; print('gsplat', gsplat.__version__, 'compiled')"; fi \
 && rm -rf /tmp/gsplat
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
# COLMAP 4.2.1 beside it, for the global mapper (the colmap4 stages): checked here to start in this image, without a GPU
# and without its libraries reaching anything else (they are found only through the wrapper), and to have CUDA when
# it's the cuda one
ARG COLMAP4
COPY --from=colmap4 /opt/colmap4 /opt/colmap4
RUN ln -s /opt/colmap4/colmap4 /usr/local/bin/colmap4 && colmap4 -h | head -1 | grep -q "COLMAP 4.2.1" \
 && { [ "$COLMAP4" != cuda ] || colmap4 -h | head -1 | grep -q "with CUDA"; }
ENV COLMAP4_BIN=/usr/local/bin/colmap4
COPY --from=pycolmap /opt/pycolmap /opt/pycolmap
RUN echo /opt/pycolmap > /usr/lib/python3/dist-packages/pycolmap-opt.pth \
 && { [ "$COLMAP4" != cuda ] || python3 -c "import pycolmap; assert pycolmap.__version__.startswith('4.2.1'), pycolmap.__version__"; }
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
