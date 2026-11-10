#!/bin/sh
# Builds the image the public setup and remote machines run, from the commit checked out here as committed (nothing
# uncommitted goes in), and records in it which commit that is:
#     deploy/build.sh [image]
# It's tagged bouldermesh:public, or [image], and <registry>:<commit>, the name deploy/ship.sh pushes it under and
# remote machines pull it by. The registry is ghcr.io/<owner>/bouldermesh, the owner from the GitHub remote, unless
# REGISTRY says otherwise.
set -eu
dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$dir"
image=${1:-bouldermesh:public}
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
docker build --build-arg BOULDERMESH_COMMIT="$commit" --build-arg BOULDERMESH_IMAGE="$REGISTRY:$commit" \
    -t "$image" -t "$REGISTRY:$commit" "$tmp"
echo "built $image, which is $REGISTRY:$commit. Next: deploy/ship.sh you@host $image"
