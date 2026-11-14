#!/bin/bash
# Runs the Android app in the emulator that's up, opening each of its screens by an extra on its intent (MainActivity's
# tryOut()), and keeps a picture of each. Fails if the app dies on any.
#
#   screens-android.sh <the debug apk> <a test server's address, as the emulator sees it> <folder for the pictures>
#
# ONLY=capture skips the screens with no camera; PREFIX names the pictures (android by default).
set -u
APK=$1; SERVER=$2; OUT=$3; ID=sh.boulderme.app; PREFIX=${PREFIX:-android}; ONLY=${ONLY:-}
mkdir -p "$OUT"
adb install -r "$APK"
for p in CAMERA ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION; do adb shell pm grant $ID android.permission.$p; done
adb emu geo fix -116.0002 34.0001 || true
adb logcat -c
fail=0
shot() {    # shot <name> [extras for the intent]
    local name=$1; shift
    adb shell am start -S -W -n $ID/.MainActivity --es server "$SERVER" "$@" > /dev/null
    sleep "${WAIT:-6}"
    adb exec-out screencap -p > "$OUT/$PREFIX-$name.png"
    if [ -z "$(adb shell pidof $ID)" ]; then echo "::error::the Android app died showing $name"; adb logcat -d -b crash | tail -80; fail=1; fi
}
if [ "$ONLY" != capture ]; then
    shot terms --es terms show
    shot home
    shot settings --es screen settings
    shot calibrate --es screen calibrate
fi
WAIT=15 shot browse --es screen browse --ez get true
[ "$ONLY" = capture ] || shot home-with-mesh
WAIT=25 shot capture --es screen capture
WAIT=12 shot capture-new --es screen capture --es mesh none
adb shell dumpsys media.camera 2>/dev/null | grep -iE "Number of camera devices|Device [0-9]+ is|Facing|camera provider" | head -12 > "$OUT/$PREFIX-cameras.txt"
adb logcat -d | grep -E "$ID|ARCore|arcore|native  :|AndroidRuntime|GLES|EGL" | tail -3000 > "$OUT/$PREFIX-logcat.txt"
exit $fail
