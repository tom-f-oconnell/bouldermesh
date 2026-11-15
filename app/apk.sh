#!/usr/bin/env bash
# Builds the Android app's debug APK from the code as it is here, and keeps a copy named by its commit:
# dist/BoulderMesh-<commit>[-changed]-debug.apk ("-changed" if app/ has changes not committed).
#
#     app/apk.sh              build it, and say where it is
#     app/apk.sh --install    and install it on the phone adb sees (USB or wireless debugging), keeping its data
#
# Needs a JDK and the Android SDK (README, "Building"). BOULDERMESH_TOOLCHAIN_ENV names a file to source for them
# first (JAVA_HOME, ANDROID_HOME, and BOULDERMESH_APP_BUILD if the build goes outside the checkout).
set -euo pipefail
cd "$(dirname "$0")"
if [ -n "${BOULDERMESH_TOOLCHAIN_ENV:-}" ]; then . "$BOULDERMESH_TOOLCHAIN_ENV"; fi
install=0
case "${1:-}" in
    --install) install=1 ;;
    "") ;;
    *) echo "usage: $0 [--install]" >&2; exit 2 ;;
esac
nice -n 10 ./gradlew --no-daemon --console=plain ${GRADLE_PROJECT_CACHE:+--project-cache-dir "$GRADLE_PROJECT_CACHE"} :android:assembleDebug
built=android/build
if [ -n "${BOULDERMESH_APP_BUILD:-}" ]; then built="$BOULDERMESH_APP_BUILD/android"; fi
apk="$built/outputs/apk/debug/android-debug.apk"
commit=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain -- . 2>/dev/null)" ] || commit="$commit-changed"
mkdir -p dist
out="dist/BoulderMesh-$commit-debug.apk"
cp "$apk" "$out"
echo "built: $(pwd)/$out ($(( $(stat -c %s "$out") / 1000000 )) MB)"
if [ "$install" = 1 ]; then
    adb=${ANDROID_HOME:+$ANDROID_HOME/platform-tools/}adb
    "$adb" install -r "$out"      # -r: over the one there, keeping its captures; fails if that one was signed by another machine
fi
