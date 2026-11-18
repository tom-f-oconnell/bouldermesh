#!/bin/bash
# Runs the iPhone app in a simulator, opening each of its screens by a launch argument (ios/.../Main.kt), and keeps a
# picture of each. Fails if the app dies on any. A simulator has no camera to track with, so the capture screen is
# checked only as far as saying so.
#
#   screens-ios.sh <BoulderMesh.app> <a test server's address> <folder for the pictures>
#
# SAMPLE: a capture made by app/ci/sample_capture.py (its folder), put in the app's own for the screens that show what an
# upload holds.
set -u
APP=$1; SERVER=$2; OUT=$3; ID=sh.boulderme.app
mkdir -p "$OUT"
UDID=$(xcrun simctl list devices available -j | python3 -c '
import json, re, sys
found = [(runtime, d) for runtime, ds in json.load(sys.stdin)["devices"].items() if "iOS" in runtime for d in ds if d["name"].startswith("iPhone")]
# the version is the numbers right after "iOS-" (Xcode 27 adds more after them, such as "iOS-27-0-iPhone19")
# and an iPhone with one screen ("iPhone 17", "iPhone 17 Pro"), where there is one: a folding one's two can't be pictured as one
found.sort(key=lambda f: ([int(n) for n in re.search(r"iOS-(\d+(?:-\d+)*)", f[0]).group(1).split("-")],
                          bool(re.fullmatch(r"iPhone \d+( Pro)?", f[1]["name"]))))
print(found[-1][1]["udid"])')
[ -n "$UDID" ] || { echo "::error::no iPhone simulator found"; xcrun simctl list runtimes; exit 1; }
echo "simulator $UDID"
xcrun simctl boot "$UDID" || true
xcrun simctl bootstatus "$UDID" -b
xcrun simctl install "$UDID" "$APP"
xcrun simctl privacy "$UDID" grant location "$ID"
xcrun simctl location "$UDID" set 34.0001,-116.0002 || true
fail=0
shot() {    # shot <name> [launch arguments]
    local name=$1; shift
    xcrun simctl terminate "$UDID" "$ID" 2>/dev/null
    local pid
    pid=$(xcrun simctl launch "$UDID" "$ID" -server "$SERVER" -terms "${TERMS:-1}" -sign_in_asked "${SIGNIN_ASKED:-YES}" "$@" | awk '{print $NF}')
    sleep "${WAIT:-8}"
    xcrun simctl io "$UDID" screenshot "$OUT/ios-$name.png"
    if ! kill -0 "$pid" 2>/dev/null; then echo "::error::the iPhone app died showing $name"; fail=1; fi
}
TERMS=0 shot terms
shot home
SIGNIN_ASKED=NO shot signin
shot settings -screen settings
shot calibrate -screen calibrate
WAIT=15 shot browse -screen browse -get YES
shot home-with-mesh
if [ -n "${SAMPLE:-}" ]; then
    docs="$(xcrun simctl get_app_container "$UDID" "$ID" data)/Documents/captures"
    mkdir -p "$docs" && cp -R "$SAMPLE" "$docs/"
    shot home-with-upload
    shot upload -screen upload
    shot photo -screen photo
    WAIT=20 shot viewer -screen viewer
fi
shot capture -screen capture
shot capture-new -screen capture -mesh none
xcrun simctl terminate "$UDID" "$ID" 2>/dev/null
cp ~/Library/Logs/DiagnosticReports/BoulderMesh* "$OUT"/ 2>/dev/null
exit $fail
