#!/bin/sh
# Builds the images the public setup and remote machines run, from the commit checked out here as committed (nothing
# uncommitted goes in), and records in them which commit that is:
#     deploy/build.sh [image] [GPU image]
# - The server's, with torch's CPU build (2.5 GB): it meshes nothing itself, so it has no use for a GPU. Tagged
#   bouldermesh:public, or [image], and <registry>:<commit>, the name deploy/ship.sh pushes it under.
# - The machines', with torch's CUDA build, for VGGT placing photos where COLMAP can't (10.4 GB). Tagged bouldermesh:gpu,
#   or [GPU image], and <registry>:<commit>-gpu: remote machines pull it by that name, and hal runs it.
# GPU=0 builds only the server's. The registry is ghcr.io/<owner>/bouldermesh, the owner from the GitHub remote, unless
# REGISTRY says otherwise.
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$dir"
image=${1:-bouldermesh:public}
gpu_image=${2:-bouldermesh:gpu}
commit=$(git rev-parse HEAD | cut -c1-7)
if [ -z "${REGISTRY:-}" ]; then
    owner=$(git remote get-url origin | sed -nE 's#^(git@github\.com:|https://github\.com/)([^/]+)/.*#\2#p' | tr 'A-Z' 'a-z')
    REGISTRY=${owner:+ghcr.io/$owner/bouldermesh}
fi
[ -n "$REGISTRY" ] || { echo "set REGISTRY, such as ghcr.io/you/bouldermesh: the origin remote isn't on GitHub" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || echo "note: uncommitted changes here aren't in it: it's $commit as committed"
[ -n "$(git branch -r --contains HEAD 2>/dev/null)" ] || echo "note: $commit isn't pushed yet, so its code can't be looked up from the image's name"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git archive HEAD | tar -x -C "$tmp"
machines=$REGISTRY:$commit-gpu
[ "${GPU:-1}" != 0 ] || machines=$REGISTRY:$commit       # without a GPU build, machines run the server's
docker build --build-arg TORCH=cpu --build-arg BOULDERMESH_COMMIT="$commit" --build-arg BOULDERMESH_IMAGE="$REGISTRY:$commit" \
    --build-arg BOULDERMESH_WORKER_IMAGE="$machines" -t "$image" -t "$REGISTRY:$commit" "$tmp"
echo "built $image, which is $REGISTRY:$commit"
if [ "${GPU:-1}" != 0 ]; then
    docker build --build-arg TORCH=cu126 --build-arg BOULDERMESH_COMMIT="$commit" --build-arg BOULDERMESH_IMAGE="$machines" \
        --build-arg BOULDERMESH_WORKER_IMAGE="$machines" -t "$gpu_image" -t "$machines" "$tmp"
    echo "built $gpu_image, which is $machines"
fi
echo "Next: deploy/ship.sh you@host $image"
