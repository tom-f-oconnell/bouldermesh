#!/usr/bin/env bash
# Runs inside the nvidia/cuda devel image, with a tree's /usr, /var, /etc, /opt and /tmp mounted over its own
# (build.sh): build-inside.sh <plain|native> <jobs>. Each part is skipped once built in that tree for that variant.
set -ex
export DEBIAN_FRONTEND=noninteractive
V=$1 JOBS=$2
F="-O3" IPO=OFF
[ "$V" = native ] && F="-O3 -march=native" IPO=ON
CUDSS=/usr/lib/x86_64-linux-gnu/libcudss/12/cmake/cudss EIGEN=/usr/local/share/eigen3/cmake
cd /opt
if [ ! -e apt.done ]; then
  apt-get update -qq
  # openimageio-tools: OpenImageIO's cmake targets name its binaries. The static cuDSS: its cmake config names it.
  apt-get install -y -qq --no-install-recommends git cmake build-essential ninja-build ca-certificates pkg-config \
    libcudss1-dev-cuda-12 libcudss1-static-cuda-12 libcudnn9-dev-cuda-12 libopenblas-dev liblapack-dev \
    libboost-program-options-dev libboost-filesystem-dev libboost-graph-dev libboost-system-dev \
    libboost-iostreams-dev libboost-serialization-dev libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev \
    libgoogle-glog-dev libgflags-dev libsuitesparse-dev libsqlite3-dev libglew-dev libcgal-dev libopenimageio-dev \
    openimageio-tools libssl-dev libcurl4-openssl-dev libgmock-dev libgtest-dev libjpeg-dev libpng-dev libtiff-dev \
    libwebp-dev libjxl-dev libglu1-mesa-dev libglfw3-dev libnanoflann-dev python3
  touch apt.done
fi
update-alternatives --display libblas.so.3-x86_64-linux-gnu | sed -n 2p     # OpenBLAS, not the reference BLAS
# The cuDSS package's cmake config looks for its headers only here, and OpenImageIO's names a folder of OpenCV's
ln -sf /usr/include/libcudss/12/*.h /usr/include/; mkdir -p /usr/include/opencv4

# Ceres master logs through Abseil, newer than Ubuntu's
[ -d absl ] || git clone -q --depth 1 --branch 20250512.1 https://github.com/abseil/abseil-cpp.git absl
if [ ! -e absl/build/done ]; then
  cmake -S absl -B absl/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_STANDARD=17 -DABSL_PROPAGATE_CXX_STD=ON \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=ON -DABSL_BUILD_TESTING=OFF
  cmake --build absl/build -j "$JOBS" && touch absl/build/done
fi
cmake --install absl/build > /dev/null

[ -d eigen ] || git clone -q --depth 1 --branch 5.0.1 https://gitlab.com/libeigen/eigen.git eigen
cmake -S eigen -B eigen/build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DEIGEN_BUILD_DOC=OFF \
  -DEIGEN_BUILD_BLAS=OFF -DEIGEN_BUILD_LAPACK=OFF > /dev/null
cmake --install eigen/build > /dev/null
# CGAL's FindEigen3 (which COLMAP's PoseLib ends up using) can't read Eigen 5's version: defer to Eigen's own config
cat > /usr/lib/x86_64-linux-gnu/cmake/CGAL/FindEigen3.cmake <<'EOF'
find_package(Eigen3 CONFIG REQUIRED PATHS /usr/local/share/eigen3/cmake NO_DEFAULT_PATH)
set(EIGEN3_FOUND TRUE)
get_target_property(EIGEN3_INCLUDE_DIR Eigen3::Eigen INTERFACE_INCLUDE_DIRECTORIES)
set(EIGEN3_INCLUDE_DIRS ${EIGEN3_INCLUDE_DIR})
EOF

# Ceres: an unreleased commit of master, as no release has cuDSS (CUDA sparse Cholesky) yet
[ -d ceres ] || { git clone -q https://github.com/ceres-solver/ceres-solver.git ceres &&
                  git -C ceres checkout -q e831bfef06ebd7a8daac7d76a1ca8bd33b47fa72; }
if [ ! -e ceres/build-$V/done ]; then
  rm -rf "ceres/build-$V"
  cmake -S ceres -B ceres/build-$V -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
    -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=$IPO -DUSE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -Dcudss_DIR=$CUDSS \
    -DEigen3_DIR=$EIGEN -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DBUILD_SHARED_LIBS=ON
  cmake --build ceres/build-$V -j "$JOBS" && touch ceres/build-$V/done
fi
cmake --install ceres/build-$V > /dev/null

[ -d colmap ] || git clone -q --depth 1 --branch 4.2.1 https://github.com/colmap/colmap.git colmap
python3 /bench/patch_colmap.py /opt/colmap
if [ ! -e colmap/build-$V/done ]; then
  rm -rf "colmap/build-$V"
  cmake -S colmap -B colmap/build-$V -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
    -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=$IPO -DCUDA_ENABLED=ON -DCMAKE_CUDA_ARCHITECTURES=86 -Dcudss_DIR=$CUDSS \
    -DEigen3_DIR=$EIGEN -DGUI_ENABLED=OFF -DTESTS_ENABLED=OFF -DOPENGL_ENABLED=OFF -DCASPAR_ENABLED=ON
  cmake --build colmap/build-$V -j "$JOBS" && touch colmap/build-$V/done
fi
cmake --install colmap/build-$V > /dev/null

# OpenMVS v2.4.0 needs OpenCV >= 4.11, and Ubuntu 24.04 has 4.6
[ -d opencv ] || git clone -q --depth 1 --branch 4.14.0 https://github.com/opencv/opencv.git opencv
if [ ! -e opencv/build-$V/done ]; then
  rm -rf "opencv/build-$V"
  cmake -S opencv -B opencv/build-$V -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
    -DBUILD_LIST=core,imgproc,imgcodecs,calib3d,features2d,flann,highgui -DWITH_CUDA=OFF -DBUILD_TESTS=OFF \
    -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DWITH_GTK=OFF -DWITH_QT=OFF -DWITH_FFMPEG=OFF \
    -DWITH_LAPACK=ON -DOPENCV_GENERATE_PKGCONFIG=ON
  cmake --build opencv/build-$V -j "$JOBS" && touch opencv/build-$V/done
fi
cmake --install opencv/build-$V > /dev/null

# OpenMVS v2.4.0 also needs CGAL 6 (Ubuntu 24.04 has 5.6, which COLMAP keeps): header-only, in its own prefix
if [ ! -d cgal6 ]; then
  apt-get -o APT::Sandbox::User=root install -y -qq --no-install-recommends curl xz-utils
  curl -sL https://github.com/CGAL/cgal/releases/download/v6.0.1/CGAL-6.0.1.tar.xz | tar -xJ --no-same-owner
  cmake -S CGAL-6.0.1 -B CGAL-6.0.1/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/cgal6 > /dev/null
  cmake --install CGAL-6.0.1/build > /dev/null
  cp /usr/lib/x86_64-linux-gnu/cmake/CGAL/FindEigen3.cmake "$(dirname "$(find /opt/cgal6 -name FindEigen3.cmake | head -1)")"/
fi
[ -d vcglib ] || git clone -q --depth 1 https://github.com/cdcseacave/VCG.git vcglib
[ -d openMVS ] || git clone -q --depth 1 --branch v2.4.0 https://github.com/cdcseacave/openMVS.git openMVS
if [ ! -e openMVS/build-$V/done ]; then
  rm -rf "openMVS/build-$V"
  cmake -S openMVS -B openMVS/build-$V -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
    -DVCG_ROOT=/opt/vcglib -DCGAL_DIR=/opt/cgal6/lib/cmake/CGAL -DOpenCV_DIR=/usr/local/lib/cmake/opencv4 -DEigen3_DIR=$EIGEN -DOpenMVS_USE_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=86 -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF -DOpenMVS_USE_BREAKPAD=OFF \
    -DOpenMVS_ENABLE_TESTS=OFF -DOpenMVS_ENABLE_IPO=$IPO -DOpenMVS_USE_CERES=OFF
  cmake --build openMVS/build-$V -j "$JOBS" && touch openMVS/build-$V/done
fi
cmake --install openMVS/build-$V > /dev/null

ldconfig
{ echo "variant $V: CXX flags '$F', link-time optimization $IPO"
  LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libcudss/12 colmap -h 2>&1 | head -1
  grep -hE "define CERES_(NO|USE)_" /usr/local/include/ceres/internal/config.h
  update-alternatives --display libblas.so.3-x86_64-linux-gnu | sed -n 2p
  grep -E "^(Eigen3|SuiteSparse|METIS|CUDAToolkit|cudss|OpenMP|BLAS|LAPACK)[A-Za-z_]*(_DIR|_VERSION|_LIBRARIES|_LIBRARY)" ceres/build-$V/CMakeCache.txt | head -30
  ls /usr/local/bin/OpenMVS; } > /opt/build-info-$V.txt 2>&1
cat /opt/build-info-$V.txt
