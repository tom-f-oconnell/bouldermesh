#!/usr/bin/env bash
# Runs inside the nvidia/cuda devel image with a tree's /usr, /var, /etc, /opt and /tmp mounted over its own
# (build-env.sh): V=<full|plain> JOBS=<cores>. Each part is skipped once built. The cmake lines are the benchmark's
# build-inside.sh's, with SuiteSparse on in Ceres, -march=x86-64-v3 throughout and OpenMP asked for by name.
set -ex
CUDSS=/usr/lib/x86_64-linux-gnu/libcudss/12/cmake/cudss EIGEN=/usr/local/share/eigen3/cmake
F="-O3"; [ "$V" = full ] && F="-O3 -march=x86-64-v3"
cd /opt
if [ "$V" = full ]; then
  if [ ! -e ceres/build-full/done ]; then
    cmake -S ceres -B ceres/build-full -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" -DUSE_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=86 -Dcudss_DIR=$CUDSS -DWITH_SUITESPARSE=ON -DEigen3_DIR=$EIGEN -DBUILD_TESTING=OFF \
      -DBUILD_EXAMPLES=OFF -DBUILD_BENCHMARKS=OFF -DBUILD_SHARED_LIBS=ON
    nice -n 19 cmake --build ceres/build-full -j "$JOBS" && touch ceres/build-full/done
  fi
  cmake --install ceres/build-full > /dev/null
  if [ ! -e colmap/build-full/done ]; then
    cmake -S colmap -B colmap/build-full -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" -DCUDA_ENABLED=ON \
      -DCMAKE_CUDA_ARCHITECTURES=86 -Dcudss_DIR=$CUDSS -DEigen3_DIR=$EIGEN -DGUI_ENABLED=OFF -DTESTS_ENABLED=OFF \
      -DOPENGL_ENABLED=OFF -DCASPAR_ENABLED=ON -DONNX_ENABLED=ON -DOPENMP_ENABLED=ON
    nice -n 19 cmake --build colmap/build-full -j "$JOBS" && touch colmap/build-full/done
  fi
  cmake --install colmap/build-full > /dev/null
  ldconfig
fi
if [ ! -e /opt/pycolmap-$V.done ]; then
  nice -n 19 apt-get update -qq
  nice -n 19 apt-get install -y -qq --no-install-recommends python3-dev python3-pip python3-numpy python3-pil
  pip install --break-system-packages --no-cache-dir scikit-build-core "pybind11==3.0.4"
  cd /opt/colmap
  nice -n 19 pip install --break-system-packages --no-cache-dir --no-build-isolation --force-reinstall --no-deps -v . \
    -Ccmake.define.GENERATE_STUBS=OFF -Ccmake.define.CCACHE_ENABLED=OFF -Cbuild-dir=/tmp/pycolmap-build-$V \
    -Ccmake.define.cudss_DIR=$CUDSS -Ccmake.define.Eigen3_DIR=$EIGEN "-Ccmake.define.CMAKE_CXX_FLAGS=$F" \
    -Ccmake.define.CMAKE_CUDA_ARCHITECTURES=86 -Ccmake.define.CMAKE_BUILD_TYPE=Release
  touch /opt/pycolmap-$V.done
  cd /opt
fi
if [ "$V" = full ]; then
  if [ ! -e opencv/build-full/done ]; then
    cmake -S opencv -B opencv/build-full -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
      -DBUILD_LIST=core,imgproc,imgcodecs,calib3d,features2d,flann,highgui -DWITH_CUDA=OFF -DBUILD_TESTS=OFF \
      -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF -DWITH_GTK=OFF -DWITH_QT=OFF -DWITH_FFMPEG=OFF \
      -DWITH_LAPACK=ON -DOPENCV_GENERATE_PKGCONFIG=ON -DCPU_BASELINE=AVX2 -DWITH_OPENMP=ON
    nice -n 19 cmake --build opencv/build-full -j "$JOBS" && touch opencv/build-full/done
  fi
  cmake --install opencv/build-full > /dev/null
  if [ ! -e openMVS/build-full/done ]; then
    cmake -S openMVS -B openMVS/build-full -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$F" \
      -DVCG_ROOT=/opt/vcglib -DCGAL_DIR=/opt/cgal6/lib/cmake/CGAL -DOpenCV_DIR=/usr/local/lib/cmake/opencv4 -DEigen3_DIR=$EIGEN \
      -DOpenMVS_USE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DOpenMVS_USE_PYTHON=OFF -DOpenMVS_BUILD_VIEWER=OFF \
      -DOpenMVS_USE_BREAKPAD=OFF -DOpenMVS_ENABLE_TESTS=OFF -DOpenMVS_USE_CERES=OFF -DOpenMVS_USE_OPENMP=ON
    nice -n 19 cmake --build openMVS/build-full -j "$JOBS" && touch openMVS/build-full/done
  fi
  cmake --install openMVS/build-full > /dev/null
  ldconfig
fi
set +e
bash /incr/verify-env.sh "$V" 2>&1 | tee /opt/verify-$V.txt
exit "${PIPESTATUS[0]}"
