#!/bin/bash
# Runs the Android app in the emulator that's up, opening each of its screens by an extra on its intent (MainActivity's
# tryOut()), and keeps a picture of each. Fails if the app dies on any.
#
#   screens-android.sh <the debug apk> <a test server's address, as the emulator sees it> <folder for the pictures>
#
# ONLY=capture skips the screens with no camera; PREFIX names the pictures (android by default). SAMPLE: a capture made by
# app/ci/sample_capture.py (its folder), put on the phone for the screens that show what an upload holds.
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
if [ "$ONLY" != capture ] && [ -n "${SAMPLE:-}" ]; then
    files=/sdcard/Android/data/$ID/files/captures
    adb shell mkdir -p "$files" && adb push "$SAMPLE" "$files/" > /dev/null || {
        # where the shell may not write there itself: by way of somewhere it may, as the app
        adb push "$SAMPLE" /data/local/tmp/ > /dev/null && adb shell run-as $ID cp -r "/data/local/tmp/$(basename "$SAMPLE")" "$files/"; }
    shot home-with-upload
    shot upload --es screen upload
    shot photo --es screen photo
    WAIT=20 shot viewer --es screen viewer
fi
WAIT=25 shot capture --es screen capture
WAIT=12 shot capture-new --es screen capture --es mesh none
# the site's "Sign the app in" (bouldermesh://pair): with an account's code from a server with accounts (PAIR_SERVER,
# PAIR_CODE, PAIR_NAME, from app/ci/pair_code.py), the app keeps a token and credits its photos to the account's name
if [ "$ONLY" != capture ] && [ -n "${PAIR_CODE:-}" ]; then
    adb shell am force-stop $ID
    adb shell "am start -W -a android.intent.action.VIEW -d 'bouldermesh://pair?code=$PAIR_CODE&server=$PAIR_SERVER' --es server '$PAIR_SERVER' $ID" > /dev/null
    sleep 8
    adb exec-out screencap -p > "$OUT/$PREFIX-signed-in.png"
    prefs=$(adb shell run-as $ID cat shared_prefs/bouldermesh.xml)
    if echo "$prefs" | grep -q "name=\"credit\">$PAIR_NAME<" && echo "$prefs" | grep -qE 'name="token">[^<]{10,}<'; then
        echo "signed in from a link: credited to $PAIR_NAME"
    else
        echo "::error::the app didn't sign in from bouldermesh://pair, or didn't credit its photos to $PAIR_NAME"; echo "$prefs" | sed 's/name="token">[^<]*</name="token">…</'; fail=1
    fi
    if [ -z "$(adb shell pidof $ID)" ]; then echo "::error::the Android app died signing in from a link"; adb logcat -d -b crash | tail -80; fail=1; fi
    adb shell pm clear $ID > /dev/null     # its token is the other server's: the next run starts clean
fi
adb shell dumpsys media.camera 2>/dev/null | grep -iE "Number of camera devices|Device [0-9]+ is|Facing|camera provider" | head -12 > "$OUT/$PREFIX-cameras.txt"
adb logcat -d | grep -E "$ID|ARCore|arcore|native  :|AndroidRuntime|GLES|EGL" | tail -3000 > "$OUT/$PREFIX-logcat.txt"
exit $fail
