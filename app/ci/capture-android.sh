#!/bin/bash
# Tries a whole capture in an emulator whose back camera shows the emulator's made-up room, which is the one place ARCore
# tracks without a phone: the room's camera is moved by one of the emulator's own recorded walks, buttons are pressed by
# key, and the screen tapped where the small model and the room should be. Best effort: nothing here fails the run. What
# it leaves is a picture of each step and whatever photos the app took, with their readings checked by the server's own
# reader.
#
#   capture-android.sh <a test server's address, as the emulator sees it> <folder for what it leaves>
set -u
SERVER=$1; OUT=$2; ID=sh.boulderme.app
mkdir -p "$OUT"
pic() { adb exec-out screencap -p > "$OUT/android-room-$1.png"; }
press() { adb shell input keyevent KEYCODE_TAB; sleep 0.5; adb shell input keyevent KEYCODE_ENTER; sleep "${1:-2}"; }    # the first button
macros="$ANDROID_HOME/emulator/resources/macros"
ls "$macros" > "$OUT/android-room-macros.txt" 2>&1
adb logcat -c
adb shell am start -S -W -n $ID/.MainActivity --es server "$SERVER" --es screen capture > /dev/null
sleep 15; pic 1-started
for walk in Track_horizontal_plane Track_vertical_plane; do
    adb emu automation play "$macros/$walk" >> "$OUT/android-room-macros.txt" 2>&1 || true
    sleep 20
done
pic 2-walked
press 3; pic 3-level-skipped
adb shell input tap 430 1340; sleep 1.5; pic 4-model-tapped
adb shell input tap 300 1600; sleep 1.5
adb shell input tap 650 1340; sleep 1.5
adb shell input tap 820 1600; sleep 3; pic 5-two-spots
press 3; pic 6-fit-confirmed
sleep 15; pic 7-capturing
press 4; press 4; pic 8-taken
adb pull "/sdcard/Android/data/$ID/files/captures" "$OUT/android-room-captures" > /dev/null 2>&1 || true
adb logcat -d | grep -E "$ID|ARCore|native  :|AndroidRuntime" | tail -2000 > "$OUT/android-room-capture-logcat.txt"
exit 0
