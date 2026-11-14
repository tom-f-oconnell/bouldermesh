#!/usr/bin/env bash
# Prepares an Ubuntu machine with an NVIDIA card to run the benchmark's GPU steps in Docker: NVIDIA's driver, its
# container toolkit, and the few host tools bench.py, chain.py and build.sh call. That is all the host needs: the CUDA
# toolkit, cuDSS, cuDNN, ONNX Runtime's CUDA provider, SuiteSparse, METIS and OpenBLAS are installed by build.sh into
# each tree, inside the nvidia/cuda image, and verify_build.py fails a tree that lacks one.
# Run it as yourself (it calls sudo), once before a reboot and once after:
#   setup-gpu-host.sh [BENCH dir]       DRIVER=<apt package> (nvidia-driver-595-open, hal's driver version)
# The first run installs and asks for a reboot (the driver can't replace nouveau under a running desktop). The run
# after the reboot installs nothing: it checks the driver, then a GPU container, and makes the BENCH folder.
set -euo pipefail
BENCH=${1:-/mnt/d0/mapperbench} DRIVER=${DRIVER:-nvidia-driver-595-open}
IMG=nvidia/cuda:12.9.1-devel-ubuntu24.04
KEY=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
LIST=/etc/apt/sources.list.d/nvidia-container-toolkit.list

if ! nvidia-smi > /dev/null 2>&1; then
  if dpkg -s "$DRIVER" > /dev/null 2>&1 && command -v nvidia-ctk > /dev/null; then
    echo "$DRIVER is installed but not loaded. Reboot, then run this again."
    exit 1
  fi
  # the toolkit isn't in Ubuntu's archive: it comes from NVIDIA's own apt repository
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o $KEY
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed "s#deb https://#deb [signed-by=$KEY] https://#g" | sudo tee $LIST > /dev/null
  sudo apt update
  sudo apt install "$DRIVER" nvidia-container-toolkit python3-venv python3-numpy rsync git curl gnupg
  sudo nvidia-ctk runtime configure --runtime=docker
  echo
  echo "Installed. Docker has not been restarted, so its containers are still running: the reboot restarts it."
  echo "Reboot, then run this again to check the result."
  exit 0
fi

echo "== driver"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
if ! docker info 2> /dev/null | grep -q 'Runtimes:.*nvidia'; then
  echo "Docker doesn't list the nvidia runtime yet: sudo systemctl restart docker (stops its running containers)"
  exit 1
fi
echo "== a GPU container (the first run downloads the 10 GB image the benchmark's tools run in)"
docker run --rm --gpus all --memory 4g --memory-swap 4g --pids-limit 512 $IMG \
  nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
mkdir -p "$BENCH"
echo "== room"
df -h --output=target,avail "$BENCH" "$(docker info -f '{{.DockerRootDir}}')"
echo "All good. BENCH=$BENCH"
